import os
import uuid
import aiofiles
from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, WebSocket, WebSocketDisconnect, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config
from database import sqlite_client, lance_client
from services import workflow, ai_agent

app = FastAPI(title="AI Native Local Knowledge Database")

current_network_online = True

async def network_monitor_loop():
    """
    定期的にネットワークの疎通確認（30秒周期）を行い、
    ステータス変化のブロードキャストおよび保留中ジョブの実行を行う。
    """
    global current_network_online
    try:
        current_network_online = ai_agent.check_network_status()
    except Exception as e:
        print(f"[Network Monitor] Initial check failed: {e}")
        current_network_online = False
    print(f"[Network Monitor] Initial status: {'online' if current_network_online else 'offline'}")
    
    if current_network_online:
        asyncio.create_task(workflow.process_queued_jobs())

    while True:
        await asyncio.sleep(30.0)
        try:
            status = ai_agent.check_network_status()
            if status != current_network_online:
                current_network_online = status
                print(f"[Network Monitor] Status changed to: {'online' if current_network_online else 'offline'}")
                await workflow.broadcast_ws_message({
                    "type": "network_status",
                    "status": "online" if current_network_online else "offline"
                })
            
            if current_network_online:
                asyncio.create_task(workflow.process_queued_jobs())
        except Exception as e:
            print(f"[Network Monitor] Error in loop: {e}")

from fastapi import Request
from fastapi.responses import JSONResponse
import sqlite3

@app.middleware("http")
async def offline_mode_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/system") or request.url.path.startswith("/static") or request.url.path.startswith("/api/settings"):
        pass # allow
    elif config.APP_STATE == "offline":
        return JSONResponse(status_code=503, content={"detail": "データベースがオフラインです。再接続するかローカル仮置きモードに切り替えてください。"})
        
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        err_str = str(e).lower()
        if (isinstance(e, sqlite3.OperationalError) and ("disk i/o error" in err_str or "database is locked" in err_str)) or \
           (isinstance(e, OSError) and ("network" in err_str or "no such file" in err_str or "unreachable" in err_str)):
            config.APP_STATE = "offline"
            config.OFFLINE_ERROR_MSG = str(e)
            return JSONResponse(status_code=503, content={"detail": f"データベースとの接続が失われました: {e}"})
        raise e

# 画像の静的ファイルサーブ用
os.makedirs(config.IMAGE_DIR, exist_ok=True)
@app.get("/local_images/{file_path:path}")
async def get_local_image(file_path: str):
    from fastapi.responses import FileResponse
    full_path = os.path.abspath(os.path.join(config.IMAGE_DIR, file_path))
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="画像が見つかりません。")
    return FileResponse(full_path)

import asyncio

@app.on_event("startup")
async def startup_event():
    if config.APP_STATE == "offline":
        print(f"Application starting in OFFLINE mode. Skipping DB init. Error: {config.OFFLINE_ERROR_MSG}")
        return

    try:
        # SQLite データベースの作成と初期フォルダのインサート
        sqlite_client.init_db()
        # LanceDB テーブルの作成
        lance_client.get_table()
        # バックグラウンドマイグレーションの実行
        asyncio.create_task(workflow.migrate_existing_data_to_v2())
        # ネットワークモニターの開始
        asyncio.create_task(network_monitor_loop())
    except Exception as e:
        print(f"Error during DB initialization: {e}")
        config.APP_STATE = "offline"
        config.OFFLINE_ERROR_MSG = str(e)

# トップページ (index.html) を直接サーブ
@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = os.path.join(config.BASE_DIR, "templates", "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="templates/index.html not found")
    async with aiofiles.open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=await f.read())

# WebSocket エンドポイント (リアルタイム状態同期)
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        await websocket.send_json({
            "type": "network_status",
            "status": "online" if current_network_online else "offline"
        })
    except Exception:
        pass
    async with workflow._ws_lock:
        workflow.active_connections.append(websocket)
    try:
        while True:
            # 切断検知のため受信ループを維持
            await websocket.receive_text()
    except WebSocketDisconnect:
        async with workflow._ws_lock:
            if websocket in workflow.active_connections:
                workflow.active_connections.remove(websocket)
    except Exception:
        async with workflow._ws_lock:
            if websocket in workflow.active_connections:
                workflow.active_connections.remove(websocket)

# 1. インボックスへの雑多収集 (ノンブロッキング受付)
@app.post("/api/save", status_code=status.HTTP_202_ACCEPTED)
async def save_note(
    background_tasks: BackgroundTasks,
    text: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None)
):
    if not text and not image:
        raise HTTPException(status_code=400, detail="テキストか画像のどちらか一方は必須です。")

    note_id = str(uuid.uuid4())
    image_relative_path = None

    # 画像の物理保存
    if image:
        filename = f"{note_id}.png"
        image_physical_path = os.path.join(config.IMAGE_DIR, filename)
        async with aiofiles.open(image_physical_path, "wb") as buffer:
            while content := await image.read(1024 * 1024):
                await buffer.write(content)
        image_relative_path = f"local_images/{filename}"

    # SQLite へ暫定レコードを登録
    raw_text = text if text else ""
    page_id = sqlite_client.create_note(
        note_id=note_id,
        parent_folder_id="inbox",
        raw_text=raw_text,
        image_path=image_relative_path
    )

    # 非同期バックグラウンド処理をキック
    background_tasks.add_task(workflow.async_pipeline_workflow, note_id, page_id)

    # 1ミリ秒でクライアントを解放
    return {"status": "processing", "note_id": note_id, "page_id": page_id}

@app.post("/api/notes/import", status_code=status.HTTP_202_ACCEPTED)
async def import_note(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    folder_id: str = Form("inbox")
):
    # 1. 拡張子の確認
    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()
    supported_exts = ['.pdf', '.pptx', '.ppt', '.docx', '.xlsx', '.xlsm', '.xls', '.txt', '.md']
    if ext not in supported_exts:
        raise HTTPException(
            status_code=400,
            detail=f"サポートされていないファイル形式です。対応形式: {', '.join(supported_exts)}"
        )

    # 2. フォルダの存在確認
    folders = sqlite_client.get_folders()
    if folder_id != "inbox" and not any(f["id"] == folder_id for f in folders):
        raise HTTPException(status_code=404, detail="フォルダが見つかりません。")

    # 3. ノートIDの生成とSQLiteへの初期登録
    note_id = str(uuid.uuid4())
    
    # 一時ファイルの作成 (delete=False)
    temp_dir = os.path.join(config.BASE_DIR, "temp_import")
    os.makedirs(temp_dir, exist_ok=True)
    temp_file_path = os.path.abspath(os.path.join(temp_dir, f"{note_id}{ext}"))

    # ファイルの書き込み
    async with aiofiles.open(temp_file_path, "wb") as buffer:
        while chunk := await file.read(1024 * 1024):
            await buffer.write(chunk)

    # 初期ノートを "processing" ステータスで作成
    sqlite_client.create_note(
        note_id=note_id,
        parent_folder_id=folder_id,
        raw_text=""
    )
    # 作成直後はタイトルが空になるので、ファイル名をタイトルに設定
    sqlite_client.update_note_metadata(
        note_id=note_id,
        title=filename,
        ai_ocr_text="",
        ai_summary="ドキュメントのインポート処理中です...",
        ai_tags="",
        parent_folder_id=folder_id,
        status="processing",
        reference_urls=""
    )

    # バックグラウンド処理のキック
    background_tasks.add_task(
        workflow.process_document_import_workflow,
        note_id=note_id,
        file_path=temp_file_path,
        filename=filename
    )

    return {"status": "processing", "note_id": note_id}

# 2. フォルダ一覧取得
@app.get("/api/folders")
def get_folders():
    return sqlite_client.get_folders()

# フォルダ作成スキーマ
class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[str] = None

# 3. フォルダ作成
@app.post("/api/folders")
def create_folder(folder_data: FolderCreate):
    if not folder_data.name.strip():
        raise HTTPException(status_code=400, detail="フォルダ名は空にできません。")
    folder_id = sqlite_client.create_folder(folder_data.name, folder_data.parent_id)
    return {"folder_id": folder_id}

# ノート手動作成スキーマ
class NoteCreate(BaseModel):
    title: Optional[str] = "無題のノート"

# 3.1 フォルダ内へのノート手動新規作成
@app.post("/api/folders/{folder_id}/notes")
def create_note_in_folder(folder_id: str, note_data: NoteCreate):
    folders = sqlite_client.get_folders()
    if folder_id != "inbox" and not any(f["id"] == folder_id for f in folders):
        raise HTTPException(status_code=404, detail="フォルダが見つかりません。")
        
    note_id = str(uuid.uuid4())
    page_id = sqlite_client.create_manual_note(note_id, folder_id, note_data.title.strip() or "無題のノート")
    return {"note_id": note_id, "page_id": page_id}

# フォルダ更新スキーマ
class FolderUpdate(BaseModel):
    name: str

# 3.5. フォルダ名前変更
@app.put("/api/folders/{folder_id}")
def update_folder(folder_id: str, folder_data: FolderUpdate):
    if folder_id == "inbox":
        raise HTTPException(status_code=400, detail="インボックスの名前は変更できません。")
    if not folder_data.name.strip():
        raise HTTPException(status_code=400, detail="フォルダ名は空にできません。")
    sqlite_client.update_folder_name(folder_id, folder_data.name.strip())
    return {"status": "updated", "folder_id": folder_id}

# 3.6. フォルダ削除
@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: str, delete_notes: bool = False):
    if folder_id == "inbox":
        raise HTTPException(status_code=400, detail="インボックスは削除できません。")
        
    folders = sqlite_client.get_folders()
    if not any(f["id"] == folder_id for f in folders):
        raise HTTPException(status_code=404, detail="フォルダが見つかりません。")
        
    if delete_notes:
        notes = sqlite_client.get_notes_by_folder(folder_id)
        for note in notes:
            note_id = note["id"]
            # DBから削除し、関連画像パスを取得
            image_paths = sqlite_client.delete_note(note_id)
            for img_path in image_paths:
                if img_path:
                    image_physical_path = os.path.join(config.STORAGE_BASE, img_path)
                    if os.path.exists(image_physical_path):
                        try:
                            os.remove(image_physical_path)
                        except Exception as e:
                            print(f"Failed to delete physical image file {image_physical_path}: {e}")
                                
            lance_client.delete_all_vector_data_for_note(note_id)
                        
    sqlite_client.delete_folder(folder_id, delete_notes=delete_notes)
    return {"status": "deleted", "folder_id": folder_id}


# 4. 指定フォルダのノート一覧取得
@app.get("/api/folders/{folder_id}/notes")
def get_notes_by_folder(folder_id: str):
    return sqlite_client.get_notes_by_folder(folder_id)

# 5. ノート詳細取得
@app.get("/api/notes/{note_id}")
def get_note(note_id: str):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
    return note

# 6. ノート物理削除 (SQLite, LanceDB, 複数画像ファイルのクリーンアップ)
@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
        
    # SQLite レコード削除 (紐づく全画像パスを取得)
    image_paths = sqlite_client.delete_note(note_id)
    
    # 複数画像の物理削除
    for img_path in image_paths:
        if img_path:
            image_physical_path = os.path.join(config.STORAGE_BASE, img_path)
            if os.path.exists(image_physical_path):
                try:
                    os.remove(image_physical_path)
                except Exception as e:
                    print(f"Failed to delete physical image file {image_physical_path}: {e}")

    # LanceDB ベクトルデータ削除 (ノートに紐づく全ページ分)
    lance_client.delete_all_vector_data_for_note(note_id)
                
    return {"status": "deleted", "note_id": note_id}

# ノート手動編集スキーマ
class NoteUpdate(BaseModel):
    title: str
    raw_text: str
    ai_summary: str
    ai_tags: str
    parent_folder_id: str
    reference_urls: Optional[str] = None

# 7. ノート手動編集 (ベクトル再計算または自動仕分けをキック)
@app.put("/api/notes/{note_id}")
def update_note(note_id: str, note_data: NoteUpdate, background_tasks: BackgroundTasks):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
        
    is_inbox = note_data.parent_folder_id == "inbox"
    status_str = "processing" if is_inbox else "completed"
        
    # AI要約とタグが空の場合は、DBの既存値を維持するようにマージ
    ai_summary = note_data.ai_summary
    if (not ai_summary or ai_summary.strip() == "") and note.get("ai_summary"):
        ai_summary = note["ai_summary"]
        
    ai_tags = note_data.ai_tags
    if (not ai_tags or ai_tags.strip() == "") and note.get("ai_tags"):
        ai_tags = note["ai_tags"]

    sqlite_client.update_note_metadata(
        note_id=note_id,
        title=note_data.title,
        ai_ocr_text=note.get("ai_ocr_text") or "", # OCRは編集非対称
        ai_summary=ai_summary,
        ai_tags=ai_tags,
        parent_folder_id=note_data.parent_folder_id,
        status=status_str,
        reference_urls=note_data.reference_urls
    )
    
    # 下位互換性のため、1番目のページの内容も更新する
    pages = note.get("pages", [])
    if len(pages) <= 1:
        if pages:
            first_page = pages[0]
            sqlite_client.update_page_metadata(
                page_id=first_page["id"],
                page_name=first_page["page_name"],
                raw_text=note_data.raw_text,
                reference_urls=note_data.reference_urls or "",
                ai_summary=ai_summary or "",
                ai_tags=ai_tags or ""
            )
            first_page_id = first_page["id"]
        else:
            first_page_id = sqlite_client.create_page(note_id, "ページ1")
            sqlite_client.update_page_metadata(
                page_id=first_page_id,
                page_name="ページ1",
                raw_text=note_data.raw_text,
                reference_urls=note_data.reference_urls or "",
                ai_summary=ai_summary or "",
                ai_tags=ai_tags or ""
            )
        
        if is_inbox:
            # 仮置き（自動整理）フォルダ所属の場合は自動仕分けをバックグラウンド実行
            background_tasks.add_task(workflow.async_pipeline_workflow, note_id, first_page_id)
            return {"status": "processing", "note_id": note_id, "page_id": first_page_id}
        else:
            # 通常フォルダ所属の場合は単にベクトル再計算
            background_tasks.add_task(workflow.recalculate_vector_on_edit, note_id, first_page_id)
            return {"status": "updated", "note_id": note_id, "page_id": first_page_id}
    else:
        # 複数ページが存在する場合、親ノートのメタデータのみ更新し、各ページの生テキストは書き換えない。
        # ただし全ページのベクトルをバックグラウンドで再計算して検索インデックスを最新化する。
        pages = note.get("pages", [])
        for page in pages:
            background_tasks.add_task(workflow.recalculate_vector_on_edit, note_id, page["id"])
        return {"status": "updated", "note_id": note_id}

# 7.6. 特定ページへの一般ファイル添付・追加 (ノンブロッキング)
@app.post("/api/notes/{note_id}/pages/{page_id}/attachments", status_code=status.HTTP_202_ACCEPTED)
async def upload_page_attachment(
    note_id: str,
    page_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")

    # ファイルの物理保存 (local_attachments 内に保存)
    att_uuid = str(uuid.uuid4())
    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()
    physical_filename = f"{att_uuid}{ext}"
    physical_path = os.path.join(config.ATTACHMENT_DIR, physical_filename)
    
    async with aiofiles.open(physical_path, "wb") as buffer:
        while content := await file.read(1024 * 1024):
            await buffer.write(content)
            
    # 物理ファイルのサイズを取得
    file_size = os.path.getsize(physical_path)
    # DB登録用相対パス
    relative_path = f"local_attachments/{physical_filename}"
    
    # DB登録
    att_id = sqlite_client.add_page_attachment(
        note_id=note_id,
        page_id=page_id,
        file_path=relative_path,
        file_name=filename,
        file_size=file_size,
        mime_type=file.content_type
    )
    
    sqlite_client.update_note_status(note_id, "processing")
    
    # バックグラウンドAI解析タスクをキック
    background_tasks.add_task(workflow.process_attachment_ai_workflow, att_id)
    
    return {"status": "processing", "note_id": note_id, "page_id": page_id, "attachment_id": att_id}

# 添付ファイルのダウンロード API
@app.get("/api/attachments/{attachment_id}")
def download_attachment(attachment_id: str):
    from fastapi.responses import FileResponse
    attachment = sqlite_client.get_attachment(attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="添付ファイルが見つかりません。")
        
    physical_path = os.path.abspath(os.path.join(config.STORAGE_BASE, attachment["file_path"]))
    if not os.path.exists(physical_path):
        raise HTTPException(status_code=404, detail="物理ファイルが存在しません。")
        
    return FileResponse(
        path=physical_path,
        filename=attachment["file_name"],
        media_type=attachment["mime_type"] or "application/octet-stream"
    )

# 添付ファイルの削除 API
@app.delete("/api/attachments/{attachment_id}")
async def delete_attachment(attachment_id: str, background_tasks: BackgroundTasks):
    attachment = sqlite_client.get_attachment(attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="添付ファイルが見つかりません。")
        
    note_id = attachment["note_id"]
    page_id = attachment["page_id"]
    
    # DB削除
    file_path = sqlite_client.delete_attachment(attachment_id)
    
    # 物理ファイル削除
    if file_path:
        physical_path = os.path.join(config.STORAGE_BASE, file_path)
        if os.path.exists(physical_path):
            try:
                os.remove(physical_path)
            except Exception as e:
                print(f"Failed to delete physical file {physical_path}: {e}")
                
    # LanceDB からのベクトル削除
    lance_client.delete_vector_data(attachment_id)
    
    # ページの Embedding 再計算
    background_tasks.add_task(workflow.recalculate_vector_on_edit, note_id, page_id)
    
    return {"status": "deleted", "attachment_id": attachment_id}

# 7.5. 特定ページへの画像添付・追加 (ノンブロッキング)
@app.post("/api/notes/{note_id}/pages/{page_id}/image", status_code=status.HTTP_202_ACCEPTED)
async def upload_page_image(
    note_id: str,
    page_id: str,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...)
):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")

    # 画像の物理保存 (ユニークなファイル名にする)
    img_uuid = str(uuid.uuid4())
    filename = f"{note_id}_{img_uuid}.png"
    image_physical_path = os.path.join(config.IMAGE_DIR, filename)
    async with aiofiles.open(image_physical_path, "wb") as buffer:
        while content := await image.read(1024 * 1024):
            await buffer.write(content)
    image_relative_path = f"local_images/{filename}"

    # DBにレコードを追加
    img_id = sqlite_client.add_page_image(note_id, page_id, image_relative_path)
    sqlite_client.update_note_status(note_id, "processing")

    # バックグラウンドで非同期に画像単体のOCR ＆ ページの要約を更新
    background_tasks.add_task(workflow.process_new_image_workflow, note_id, page_id, img_id)

    return {"status": "processing", "note_id": note_id, "page_id": page_id, "image_id": img_id}

# (下位互換用) ノートへの画像添付・追加
@app.post("/api/notes/{note_id}/image", status_code=status.HTTP_202_ACCEPTED)
async def upload_note_image(
    note_id: str,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...)
):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
        
    pages = note.get("pages", [])
    if not pages:
        page_id = sqlite_client.create_page(note_id, "ページ1")
    else:
        page_id = pages[0]["id"]
        
    return await upload_page_image(note_id, page_id, background_tasks, image)

# 7.6. ページ内の特定画像削除API (ノンブロッキング)
@app.delete("/api/notes/{note_id}/pages/{page_id}/images/{image_id}")
def delete_page_image(note_id: str, page_id: str, image_id: str, background_tasks: BackgroundTasks):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")

    # DBから画像情報を削除し、画像パスを取得
    image_path = sqlite_client.delete_note_image(image_id)
    if image_path:
        image_physical_path = os.path.join(config.STORAGE_BASE, image_path)
        if os.path.exists(image_physical_path):
            try:
                os.remove(image_physical_path)
            except Exception as e:
                print(f"Failed to delete physical image file {image_physical_path}: {e}")

    # ステータスを更新し、バックグラウンドで再構造化
    sqlite_client.update_note_status(note_id, "processing")
    background_tasks.add_task(workflow.recalculate_on_image_delete, note_id, page_id)

    return {"status": "processing", "note_id": note_id, "page_id": page_id}

# (下位互換用) 画像削除
@app.delete("/api/notes/{note_id}/images/{image_id}")
def delete_note_image(note_id: str, image_id: str, background_tasks: BackgroundTasks):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")

    pages = note.get("pages", [])
    target_page_id = None
    for p in pages:
        if any(img["id"] == image_id for img in p.get("images", [])):
            target_page_id = p["id"]
            break
            
    if not target_page_id:
        raise HTTPException(status_code=404, detail="対象ノート内に指定された画像が存在しません。")
        
    return delete_page_image(note_id, target_page_id, image_id, background_tasks)

# 新規ページ追加 API
class PageCreate(BaseModel):
    page_name: str

@app.post("/api/notes/{note_id}/pages")
def create_page(note_id: str, page_data: PageCreate):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
    page_id = sqlite_client.create_page(note_id, page_data.page_name.strip() or "無題のページ")
    return {"page_id": page_id}

# ページ更新 API
class PageUpdate(BaseModel):
    page_name: str
    raw_text: str
    reference_urls: Optional[str] = ""
    ai_summary: Optional[str] = ""
    ai_tags: Optional[str] = ""

@app.put("/api/notes/{note_id}/pages/{page_id}")
def update_page(note_id: str, page_id: str, page_data: PageUpdate, background_tasks: BackgroundTasks):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
        
    p_obj = next((p for p in note.get("pages", []) if p["id"] == page_id), None)
    if not p_obj:
        raise HTTPException(status_code=404, detail="ページが見つかりません。")
        
    ai_summary = page_data.ai_summary
    if (not ai_summary or ai_summary.strip() == "") and p_obj.get("ai_summary"):
        ai_summary = p_obj["ai_summary"]
        
    ai_tags = page_data.ai_tags
    if (not ai_tags or ai_tags.strip() == "") and p_obj.get("ai_tags"):
        ai_tags = p_obj["ai_tags"]

    sqlite_client.update_page_metadata(
        page_id=page_id,
        page_name=page_data.page_name,
        raw_text=page_data.raw_text,
        reference_urls=page_data.reference_urls or "",
        ai_summary=ai_summary or "",
        ai_tags=ai_tags or ""
    )
    
    is_inbox = note.get("parent_folder_id") == "inbox"
    if is_inbox:
        background_tasks.add_task(workflow.async_pipeline_workflow, note_id, page_id)
        return {"status": "processing", "note_id": note_id, "page_id": page_id}
    else:
        background_tasks.add_task(workflow.recalculate_vector_on_edit, note_id, page_id)
        return {"status": "updated", "note_id": note_id, "page_id": page_id}

# ページ削除 API
@app.delete("/api/notes/{note_id}/pages/{page_id}")
def delete_page(note_id: str, page_id: str):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
        
    pages = note.get("pages", [])
    if len(pages) <= 1:
        raise HTTPException(status_code=400, detail="唯一のページは削除できません。")
        
    _, image_paths, attachments = sqlite_client.delete_page(page_id)
    for img_path in image_paths:
        if img_path:
            image_physical_path = os.path.join(config.STORAGE_BASE, img_path)
            if os.path.exists(image_physical_path):
                try:
                    os.remove(image_physical_path)
                except Exception as e:
                    print(f"Failed to delete physical image file {image_physical_path}: {e}")
                    
    for att in attachments:
        if att["file_path"]:
            att_physical_path = os.path.join(config.STORAGE_BASE, att["file_path"])
            if os.path.exists(att_physical_path):
                try:
                    os.remove(att_physical_path)
                except Exception as e:
                    print(f"Failed to delete physical attachment file {att_physical_path}: {e}")
        lance_client.delete_vector_data(att["id"])

    lance_client.delete_vector_data(page_id)
    return {"status": "deleted", "note_id": note_id, "page_id": page_id}

# ページ順序変更 API
class PagesReorder(BaseModel):
    page_ids: list[str]

@app.put("/api/notes/{note_id}/pages/reorder")
def reorder_pages(note_id: str, reorder_data: PagesReorder):
    sqlite_client.reorder_pages(note_id, reorder_data.page_ids)
    return {"status": "reordered", "note_id": note_id}

# フォルダ順序変更 API
class FoldersReorder(BaseModel):
    folder_ids: list[str]

@app.put("/api/folders/reorder")
def reorder_folders(reorder_data: FoldersReorder):
    sqlite_client.reorder_folders(reorder_data.folder_ids)
    return {"status": "reordered"}

# ノート順序変更 API
class NotesReorder(BaseModel):
    note_ids: list[str]

@app.put("/api/folders/{folder_id}/notes/reorder")
def reorder_notes(folder_id: str, reorder_data: NotesReorder):
    sqlite_client.reorder_notes(folder_id, reorder_data.note_ids)
    return {"status": "reordered", "folder_id": folder_id}


# フォルダ修正スキーマ
class FolderFix(BaseModel):
    note_id: str
    correct_folder_id: str
    reason: Optional[str] = ""

# 8. フォルダ手動指定・修正 (フィードバック学習付き)
@app.post("/api/fix-folder")
def fix_folder(fix_data: FolderFix, background_tasks: BackgroundTasks):
    note = sqlite_client.get_note(fix_data.note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
        
    # バックグラウンドで仕分けルール学習とノートフォルダの更新を起動
    background_tasks.add_task(
        workflow.update_rules_with_feedback,
        fix_data.note_id,
        fix_data.correct_folder_id,
        fix_data.reason
    )
    
    return {"status": "feedback_processing", "note_id": fix_data.note_id}

# 9. ハイブリッド検索 ＆ ローカルRAG
@app.get("/api/search")
async def search_notes(q: str, limit: Optional[int] = None):
    try:
        if not current_network_online:
            raise HTTPException(status_code=503, detail="ネットワーク不通のため、AI質問は一時的に利用できません。")
        if not q.strip():
            raise HTTPException(status_code=400, detail="検索クエリは空にできません。")
            
        # RAG参照数の設定値をDBから取得
        if limit is None:
            try:
                limit = int(sqlite_client.get_setting("rag_limit", "5"))
            except ValueError:
                limit = 5
                
        # RAG足切り閾値の設定値（コサイン距離：小さいほど厳しく、大きいほど緩い）をDBから取得
        try:
            distance_threshold = float(sqlite_client.get_setting("rag_threshold", "0.8"))
        except ValueError:
            distance_threshold = 0.8

        # クエリを高速に最適化
        optimized_q = ai_agent.optimize_search_query(q)
        print(f"[RAG Search] Original: '{q}' -> Optimized: '{optimized_q}', Distance threshold: {distance_threshold}")

        # 最適化されたクエリをベクトル化
        query_vector = await ai_agent.generate_embedding_via_azure(optimized_q, dimensions=512)

        # LanceDB でハイブリッド検索（コサイン距離の閾値をそのまま使用）
        search_results = lance_client.hybrid_search(query_vector, optimized_q, limit=limit, distance_threshold=distance_threshold)
        
        # SQLite 上のメタデータと結合
        matched_notes = []
        for r in search_results:
            note_id = r.get("note_id")
            if not note_id:
                # 移行前の古いインデックスに対する互換性フォールバック
                note = sqlite_client.get_note(r["id"])
                if note:
                    matched_notes.append({
                        "id": note["id"],
                        "page_id": note["pages"][0]["id"] if note.get("pages") else None,
                        "page_name": "ページ1",
                        "title": note["title"],
                        "parent_folder_id": note["parent_folder_id"],
                        "ai_summary": note["ai_summary"],
                        "ai_tags": note["ai_tags"],
                        "raw_text": note["raw_text"],
                        "ai_ocr_text": note["ai_ocr_text"],
                        "image_path": note["image_path"],
                        "updated_at": note["updated_at"]
                    })
            else:
                note = sqlite_client.get_note(note_id)
                if note:
                    # 該当するページまたは添付ファイルを取得
                    page = next((p for p in note.get("pages", []) if p["id"] == r["id"]), None)
                    attachment = next((a for p in note.get("pages", []) for a in p.get("attachments", []) if a["id"] == r["id"]), None)
                    
                    if attachment:
                        matched_notes.append({
                            "id": note["id"],
                            "page_id": r["id"],
                            "page_name": f"添付ファイル: {attachment['file_name']}",
                            "title": f"{note['title']} > 添付: {attachment['file_name']}",
                            "parent_folder_id": note["parent_folder_id"],
                            "ai_summary": attachment.get("ai_summary", ""),
                            "ai_tags": "添付ファイル",
                            "raw_text": attachment.get("ai_ocr_text", ""),
                            "ai_ocr_text": attachment.get("ai_ocr_text", ""),
                            "image_path": note["image_path"],
                            "updated_at": note["updated_at"]
                        })
                    else:
                        page_name = page["page_name"] if page else "ページ1"
                        matched_notes.append({
                            "id": note["id"],
                            "page_id": r["id"],
                            "page_name": page_name,
                            "title": f"{note['title']} > {page_name}",
                            "parent_folder_id": note["parent_folder_id"],
                            "ai_summary": page["ai_summary"] if page else note["ai_summary"],
                            "ai_tags": page["ai_tags"] if page else note["ai_tags"],
                            "raw_text": page["raw_text"] if page else note["raw_text"],
                            "ai_ocr_text": page["ai_ocr_text"] if page else note["ai_ocr_text"],
                            "image_path": page["images"][0]["image_path"] if page and page.get("images") else note["image_path"],
                            "updated_at": note["updated_at"]
                        })
                
        # ローカルRAGによる回答生成（matched_notesをコンテキストとして渡す）
        if matched_notes:
            answer = ai_agent.generate_rag_response(q, matched_notes)
        else:
            answer = "関連するナレッジが見つかりませんでした。"
            
        return {
            "answer": answer,
            "references": matched_notes,
            "optimized_query": optimized_q
        }
    except HTTPException:
        raise
    except ai_agent.OfflineException as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        print(f"Search API error: {e}")
        raise HTTPException(status_code=500, detail=f"内部エラーが発生しました: {str(e)}")

# 10. 設定用スキーマとAPI
class SettingsUpdate(BaseModel):
    settings: dict[str, str]

@app.get("/api/settings")
def get_settings():
    return sqlite_client.get_all_settings()

@app.post("/api/settings")
def update_settings(data: SettingsUpdate):
    for k, v in data.settings.items():
        sqlite_client.set_setting(k, str(v))
    return {"status": "success", "message": "設定を更新しました。"}

@app.get("/api/system/status")
def system_status():
    return {
        "status": config.APP_STATE,
        "error_msg": config.OFFLINE_ERROR_MSG,
        "storage_base": config.STORAGE_BASE
    }

@app.get("/api/system/select_directory")
def select_directory():
    import tkinter as tk
    from tkinter import filedialog
    try:
        root = tk.Tk()
        root.attributes("-topmost", True)
        root.withdraw()
        folder_path = filedialog.askdirectory(parent=root, title="保存先フォルダを選択してください")
        root.destroy()
        return {"path": folder_path}
    except Exception as e:
        return {"error": str(e)}

class StorageCheckRequest(BaseModel):
    new_path: str

class StorageApplyRequest(BaseModel):
    new_path: str
    action: str

@app.post("/api/settings/storage/check")
def check_storage_path(request: StorageCheckRequest):
    new_path = request.new_path
    if not os.path.isabs(new_path):
        return {"is_valid": False, "error": "絶対パスを指定してください。"}
    
    if not os.path.exists(new_path):
        return {"is_valid": True, "exists": False, "has_data": False, "empty": True}
        
    try:
        items = os.listdir(new_path)
    except Exception as e:
        return {"is_valid": False, "error": f"アクセスできません: {e}"}
        
    if not items:
        return {"is_valid": True, "exists": True, "has_data": False, "empty": True}
        
    has_data = False
    if "local_knowledge.db" in items or "lancedb_data" in items:
        has_data = True
        
    return {"is_valid": True, "exists": True, "has_data": has_data, "empty": False}

@app.post("/api/settings/storage/apply")
def apply_storage_path(request: StorageApplyRequest):
    new_path = request.new_path
    action = request.action
    
    if not os.path.isabs(new_path):
        raise HTTPException(status_code=400, detail="絶対パスを指定してください。")
        
    import shutil
    import json
    old_base = config.STORAGE_BASE
    
    # 接続をリセット
    lance_client.reset_connection()
    
    try:
        if action == "move":
            os.makedirs(new_path, exist_ok=True)
            for item in ["local_images", "local_attachments", "lancedb_data", "local_knowledge.db"]:
                src = os.path.join(old_base, item)
                dst = os.path.join(new_path, item)
                if os.path.exists(src):
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
            
            os.makedirs(os.path.dirname(config.STORAGE_JSON_PATH), exist_ok=True)
            with open(config.STORAGE_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump({"storage_base": new_path}, f)
            config.update_storage_paths(new_path)
            
            # 古いデータを削除
            for item in ["local_images", "local_attachments", "lancedb_data", "local_knowledge.db"]:
                src = os.path.join(old_base, item)
                if os.path.exists(src):
                    if os.path.isdir(src):
                        shutil.rmtree(src, ignore_errors=True)
                    else:
                        try:
                            os.remove(src)
                        except:
                            pass
        
        elif action in ["create_new", "use_existing"]:
            os.makedirs(new_path, exist_ok=True)
            os.makedirs(os.path.dirname(config.STORAGE_JSON_PATH), exist_ok=True)
            with open(config.STORAGE_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump({"storage_base": new_path}, f)
            config.update_storage_paths(new_path)
            
            if action == "create_new" or action == "use_existing":
                sqlite_client.init_db()
                lance_client.get_table()
    except Exception as e:
        config.update_storage_paths(old_base)
        raise HTTPException(status_code=500, detail=f"保存先の変更中にエラーが発生しました: {e}")
        
    return {"status": "success", "new_path": new_path}

@app.post("/api/system/offline/temp_local")
def switch_to_temp_local():
    temp_dir = os.path.join(config.BASE_DIR, "temp_offline_db")
    os.makedirs(temp_dir, exist_ok=True)
    
    config.update_storage_paths(temp_dir)
    config.APP_STATE = "temp_local"
    
    lance_client.reset_connection()
    sqlite_client.init_db()
    lance_client.get_table()
    
    return {"status": "success", "new_path": temp_dir}

@app.post("/api/system/offline/sync")
def sync_temp_local_to_main():
    if config.APP_STATE != "temp_local":
        raise HTTPException(status_code=400, detail="仮置きモードではありません。")
        
    import json
    import shutil
    try:
        with open(config.STORAGE_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            main_base = data.get("storage_base", config.BASE_DIR)
    except:
        main_base = config.BASE_DIR
        
    if not os.path.exists(main_base):
        raise HTTPException(status_code=503, detail="正式なデータベースのパスにまだアクセスできません。接続を確認してください。")
        
    temp_base = config.STORAGE_BASE
    
    # 1. 物理ファイルのコピー
    for folder in ["local_images", "local_attachments"]:
        src = os.path.join(temp_base, folder)
        dst = os.path.join(main_base, folder)
        if os.path.exists(src):
            os.makedirs(dst, exist_ok=True)
            for file in os.listdir(src):
                shutil.copy2(os.path.join(src, file), os.path.join(dst, file))
                
    # 2. メインDBに切り替え
    config.update_storage_paths(main_base)
    lance_client.reset_connection()
    
    # 3. SQLiteデータのマージ
    conn = sqlite_client.get_connection()
    temp_db_path = os.path.join(temp_base, "local_knowledge.db")
    try:
        conn.execute("ATTACH DATABASE ? AS tempdb", (temp_db_path,))
        conn.execute("INSERT OR IGNORE INTO folders SELECT * FROM tempdb.folders")
        conn.execute("INSERT OR REPLACE INTO notes SELECT * FROM tempdb.notes")
        conn.execute("INSERT OR REPLACE INTO note_pages SELECT * FROM tempdb.note_pages")
        conn.execute("INSERT OR REPLACE INTO note_images SELECT * FROM tempdb.note_images")
        conn.execute("INSERT OR REPLACE INTO note_attachments SELECT * FROM tempdb.note_attachments")
        conn.commit()
    except Exception as e:
        conn.execute("DETACH DATABASE tempdb")
        conn.close()
        raise HTTPException(status_code=500, detail=f"SQLiteマージエラー: {e}")
    finally:
        try:
            conn.execute("DETACH DATABASE tempdb")
        except:
            pass
        conn.close()
        
    # 4. LanceDBデータのマージ
    temp_lance_dir = os.path.join(temp_base, "lancedb_data")
    if os.path.exists(temp_lance_dir):
        import lancedb
        temp_db = lancedb.connect(temp_lance_dir)
        if lance_client.TABLE_NAME in temp_db.table_names():
            temp_table = temp_db.open_table(lance_client.TABLE_NAME)
            data_to_insert = temp_table.to_pandas()
            if not data_to_insert.empty:
                main_table = lance_client.get_table()
                # スキーマv5 (512d) で一致している前提
                try:
                    main_table.add(data_to_insert)
                except Exception as e:
                    print(f"LanceDB Merge Error: {e}")
                    
    # 5. クリーンアップ
    try:
        shutil.rmtree(temp_base, ignore_errors=True)
    except:
        pass
        
    config.APP_STATE = "normal"
    return {"status": "success"}

@app.get("/api/settings/storage/current")
def get_current_storage():
    return {"current_path": config.STORAGE_BASE}

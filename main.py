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

# 画像の静的ファイルサーブ用
os.makedirs(config.IMAGE_DIR, exist_ok=True)
app.mount("/local_images", StaticFiles(directory=config.IMAGE_DIR), name="local_images")

@app.on_event("startup")
def startup_event():
    # SQLite データベースの作成と初期フォルダのインサート
    sqlite_client.init_db()
    # LanceDB テーブルの作成
    lance_client.get_table()

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
    workflow.active_connections.append(websocket)
    try:
        while True:
            # 切断検知のため受信ループを維持
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in workflow.active_connections:
            workflow.active_connections.remove(websocket)
    except Exception:
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
                    image_physical_path = os.path.join(config.BASE_DIR, img_path)
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
            image_physical_path = os.path.join(config.BASE_DIR, img_path)
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
        
    physical_path = os.path.abspath(os.path.join(config.BASE_DIR, attachment["file_path"]))
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
        physical_path = os.path.join(config.BASE_DIR, file_path)
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
        image_physical_path = os.path.join(config.BASE_DIR, image_path)
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
    page_id = pages[0]["id"] if pages else "default"
    return delete_page_image(note_id, page_id, image_id, background_tasks)

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
        
    _, image_paths = sqlite_client.delete_page(page_id)
    for img_path in image_paths:
        if img_path:
            image_physical_path = os.path.join(config.BASE_DIR, img_path)
            if os.path.exists(image_physical_path):
                try:
                    os.remove(image_physical_path)
                except Exception as e:
                    print(f"Failed to delete physical image file {image_physical_path}: {e}")
                    
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
def search_notes(q: str, limit: Optional[int] = None):
    if not q.strip():
        raise HTTPException(status_code=400, detail="検索クエリは空にできません。")
        
    # RAG参照数の設定値をDBから取得
    if limit is None:
        try:
            limit = int(sqlite_client.get_setting("rag_limit", "5"))
        except ValueError:
            limit = 5
            
    # RAG足切り閾値の設定値をDBから取得
    try:
        threshold = float(sqlite_client.get_setting("rag_threshold", "0.7"))
    except ValueError:
        threshold = 0.7
        
    # クエリを高速に最適化
    optimized_q = ai_agent.optimize_search_query(q)
    print(f"[RAG Search] Original: '{q}' -> Optimized: '{optimized_q}', Threshold: {threshold}")
    
    # 最適化されたクエリをベクトル化
    query_vector = ai_agent.generate_embedding_via_azure(optimized_q)
    
    # LanceDB でハイブリッド検索（最適化されたクエリと閾値を使用）
    search_results = lance_client.hybrid_search(query_vector, optimized_q, limit=limit, distance_threshold=threshold)
    
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
                    "image_path": note["image_path"],
                    "updated_at": note["updated_at"]
                })
        else:
            note = sqlite_client.get_note(note_id)
            if note:
                # 該当するページを取得
                page = next((p for p in note.get("pages", []) if p["id"] == r["id"]), None)
                page_name = page["page_name"] if page else "ページ1"
                matched_notes.append({
                    "id": note["id"],
                    "page_id": r["id"],
                    "page_name": page_name,
                    "title": f"{note['title']} > {page_name}",
                    "parent_folder_id": note["parent_folder_id"],
                    "ai_summary": page["ai_summary"] if page else note["ai_summary"],
                    "ai_tags": page["ai_tags"] if page else note["ai_tags"],
                    "image_path": page["images"][0]["image_path"] if page and page.get("images") else note["image_path"],
                    "updated_at": note["updated_at"]
                })
            
    # ローカルRAGによる回答生成
    if matched_notes:
        answer = ai_agent.generate_rag_response(q, search_results)
    else:
        answer = "関連するナレッジが見つかりませんでした。"
        
    return {
        "answer": answer,
        "references": matched_notes,
        "optimized_query": optimized_q
    }

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

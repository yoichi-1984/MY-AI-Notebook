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
    sqlite_client.create_note(
        note_id=note_id,
        parent_folder_id="inbox",
        raw_text=raw_text,
        image_path=image_relative_path
    )

    # 非同期バックグラウンド処理をキック
    background_tasks.add_task(workflow.async_pipeline_workflow, note_id)

    # 1ミリ秒でクライアントを解放
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
    sqlite_client.create_manual_note(note_id, folder_id, note_data.title.strip() or "無題のノート")
    return {"note_id": note_id}

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
            # 複数画像の物理削除
            full_note = sqlite_client.get_note(note_id)
            if full_note:
                images = full_note.get("images", [])
                for img in images:
                    img_path = img.get("image_path")
                    if img_path:
                        image_physical_path = os.path.join(config.BASE_DIR, img_path)
                        if os.path.exists(image_physical_path):
                            try:
                                os.remove(image_physical_path)
                            except Exception as e:
                                print(f"Failed to delete physical image file {image_physical_path}: {e}")
                                
            sqlite_client.delete_note(note_id)
            lance_client.delete_vector_data(note_id)
                        
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
        
    # 複数画像の物理削除
    images = note.get("images", [])
    for img in images:
        img_path = img.get("image_path")
        if img_path:
            image_physical_path = os.path.join(config.BASE_DIR, img_path)
            if os.path.exists(image_physical_path):
                try:
                    os.remove(image_physical_path)
                except Exception as e:
                    print(f"Failed to delete physical image file {image_physical_path}: {e}")

    # SQLite レコード削除 (note_images も削除)
    sqlite_client.delete_note(note_id)
    
    # LanceDB ベクトルデータ削除
    lance_client.delete_vector_data(note_id)
                
    return {"status": "deleted", "note_id": note_id}

# ノート手動編集スキーマ
class NoteUpdate(BaseModel):
    title: str
    raw_text: str
    ai_summary: str
    ai_tags: str
    parent_folder_id: str

# 7. ノート手動編集 (ベクトル再計算または自動仕分けをキック)
@app.put("/api/notes/{note_id}")
def update_note(note_id: str, note_data: NoteUpdate, background_tasks: BackgroundTasks):
    note = sqlite_client.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
        
    is_inbox = note_data.parent_folder_id == "inbox"
    status_str = "processing" if is_inbox else "completed"
        
    sqlite_client.update_note_metadata(
        note_id=note_id,
        title=note_data.title,
        ai_ocr_text=note.get("ai_ocr_text") or "", # OCRは編集非対称
        ai_summary=note_data.ai_summary,
        ai_tags=note_data.ai_tags,
        parent_folder_id=note_data.parent_folder_id,
        status=status_str
    )
    
    # 本文（raw_text）の更新も行う
    sqlite_client.update_note_content(note_id, note_data.raw_text)
    
    if is_inbox:
        # 仮置き（自動整理）フォルダ所属の場合は自動仕分けをバックグラウンド実行
        background_tasks.add_task(workflow.async_pipeline_workflow, note_id)
        return {"status": "processing", "note_id": note_id}
    else:
        # 通常フォルダ所属の場合は単にベクトル再計算
        background_tasks.add_task(workflow.recalculate_vector_on_edit, note_id)
        return {"status": "updated", "note_id": note_id}

# 7.5. ノートへの画像添付・追加 (ノンブロッキング)
@app.post("/api/notes/{note_id}/image", status_code=status.HTTP_202_ACCEPTED)
async def upload_note_image(
    note_id: str,
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
    img_id = sqlite_client.add_note_image(note_id, image_relative_path)
    sqlite_client.update_note_status(note_id, "processing")

    # バックグラウンドで非同期に画像単体のOCR ＆ 全体の要約を更新
    background_tasks.add_task(workflow.process_new_image_workflow, note_id, img_id)

    return {"status": "processing", "note_id": note_id, "image_id": img_id}

# 7.6. ノート内の特定画像削除API (ノンブロッキング)
@app.delete("/api/notes/{note_id}/images/{image_id}")
def delete_note_image(note_id: str, image_id: str, background_tasks: BackgroundTasks):
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
    background_tasks.add_task(workflow.recalculate_on_image_delete, note_id)

    return {"status": "processing", "note_id": note_id}

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
        
    # クエリをベクトル化
    query_vector = ai_agent.generate_embedding_via_azure(q)
    
    # LanceDB でハイブリッド検索
    search_results = lance_client.hybrid_search(query_vector, q, limit=limit)
    
    # SQLite 上のメタデータと結合
    matched_notes = []
    for r in search_results:
        note = sqlite_client.get_note(r["id"])
        if note:
            matched_notes.append({
                "id": note["id"],
                "title": note["title"],
                "parent_folder_id": note["parent_folder_id"],
                "ai_summary": note["ai_summary"],
                "ai_tags": note["ai_tags"],
                "image_path": note["image_path"],
                "updated_at": note["updated_at"]
            })
            
    # ローカルRAGによる回答生成
    if matched_notes:
        answer = ai_agent.generate_rag_response(q, search_results)
    else:
        answer = "関連するナレッジが見つかりませんでした。"
        
    return {
        "answer": answer,
        "references": matched_notes
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

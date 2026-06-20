import sqlite3
import datetime
import uuid
from config import SQLITE_DB_PATH

def get_connection():
    # ロック競合を防ぐために十分なタイムアウトを設定
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        # folders テーブルの作成
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id TEXT
            )
        """)
        # notes テーブルの作成
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                parent_folder_id TEXT NOT NULL,
                title TEXT,
                raw_text TEXT NOT NULL,
                image_path TEXT,
                ai_ocr_text TEXT,
                ai_summary TEXT,
                ai_tags TEXT,
                status TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                FOREIGN KEY (parent_folder_id) REFERENCES folders (id)
            )
        """)

        # note_images テーブルの作成
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS note_images (
                id TEXT PRIMARY KEY,
                note_id TEXT NOT NULL,
                image_path TEXT NOT NULL,
                ai_ocr_text TEXT,
                created_at TIMESTAMP NOT NULL,
                FOREIGN KEY (note_id) REFERENCES notes (id)
            )
        """)
        
        # 初回起動時にインボックスフォルダを作成
        cursor.execute("SELECT id FROM folders WHERE id = 'inbox'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO folders (id, name, parent_id) VALUES ('inbox', '📥 インボックス', NULL)")

        # settings テーブルの作成
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        
        # デフォルト設定の初期登録
        default_settings = {
            "thinking_level": "standard",
            "model_name": "gemini-3.5-flash",
            "confidence_threshold": "0.7",
            "rag_limit": "5"
        }
        for k, v in default_settings.items():
            cursor.execute("SELECT key, value FROM settings WHERE key = ?", (k,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))


        # 既存データの移行 (notes.image_path -> note_images)
        cursor.execute("PRAGMA table_info(notes)")
        columns = [row[1] for row in cursor.fetchall()]
        if "image_path" in columns:
            cursor.execute("SELECT id, image_path, ai_ocr_text, updated_at FROM notes WHERE image_path IS NOT NULL AND image_path != ''")
            notes_with_images = cursor.fetchall()
            for row in notes_with_images:
                n_id, img_path, ocr_txt, updated_at = row
                # すでに移行済みかチェック
                cursor.execute("SELECT id FROM note_images WHERE note_id = ? AND image_path = ?", (n_id, img_path))
                if not cursor.fetchone():
                    img_id = str(uuid.uuid4())
                    cursor.execute(
                        "INSERT INTO note_images (id, note_id, image_path, ai_ocr_text, created_at) VALUES (?, ?, ?, ?, ?)",
                        (img_id, n_id, img_path, ocr_txt, updated_at)
                    )

        conn.commit()

def create_folder(name: str, parent_id: str = None) -> str:
    folder_id = str(uuid.uuid4())
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO folders (id, name, parent_id) VALUES (?, ?, ?)",
            (folder_id, name, parent_id)
        )
        conn.commit()
    return folder_id

def get_folders():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, parent_id FROM folders")
        return [dict(row) for row in cursor.fetchall()]

def create_note(note_id: str, parent_folder_id: str, raw_text: str, image_path: str = None) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO notes (id, parent_folder_id, title, raw_text, image_path, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, parent_folder_id, "", raw_text, image_path, "processing", now)
        )
        conn.commit()

def update_note_metadata(note_id: str, title: str, ai_ocr_text: str, ai_summary: str, ai_tags: str, parent_folder_id: str, status: str) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE notes
            SET title = ?, ai_ocr_text = ?, ai_summary = ?, ai_tags = ?, parent_folder_id = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, ai_ocr_text, ai_summary, ai_tags, parent_folder_id, status, now, note_id)
        )
        conn.commit()

def update_note_status(note_id: str, status: str) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notes SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, note_id)
        )
        conn.commit()

def update_note_folder(note_id: str, parent_folder_id: str) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notes SET parent_folder_id = ?, updated_at = ? WHERE id = ?",
            (parent_folder_id, now, note_id)
        )
        conn.commit()

def update_note_content(note_id: str, raw_text: str) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notes SET raw_text = ?, updated_at = ? WHERE id = ?",
            (raw_text, now, note_id)
        )
        conn.commit()

def add_note_image(note_id: str, image_path: str) -> str:
    img_id = str(uuid.uuid4())
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO note_images (id, note_id, image_path, created_at) VALUES (?, ?, ?, ?)",
            (img_id, note_id, image_path, now)
        )
        conn.commit()
    return img_id

def update_note_image_ocr(image_id: str, ai_ocr_text: str) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE note_images SET ai_ocr_text = ? WHERE id = ?",
            (ai_ocr_text, image_id)
        )
        conn.commit()

def delete_note_image(image_id: str) -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT image_path FROM note_images WHERE id = ?", (image_id,))
        row = cursor.fetchone()
        img_path = row["image_path"] if row else None
        
        cursor.execute("DELETE FROM note_images WHERE id = ?", (image_id,))
        conn.commit()
    return img_path

def get_note_images(note_id: str) -> list:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, image_path, ai_ocr_text FROM note_images WHERE note_id = ? ORDER BY created_at ASC", (note_id,))
        return [dict(row) for row in cursor.fetchall()]

def get_note(note_id: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = cursor.fetchone()
        if not row:
            return None
        note_dict = dict(row)
        
        # 紐づく画像一覧も取得して返す
        cursor.execute("SELECT id, image_path, ai_ocr_text FROM note_images WHERE note_id = ? ORDER BY created_at ASC", (note_id,))
        note_dict["images"] = [dict(r) for r in cursor.fetchall()]
        return note_dict

def get_notes_by_folder(folder_id: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM notes WHERE parent_folder_id = ? ORDER BY updated_at DESC", (folder_id,))
        results = []
        for row in cursor.fetchall():
            note_dict = dict(row)
            # 各ノートの最初の画像を代表画像パスとする（UI互換性維持のため）
            cursor.execute("SELECT image_path FROM note_images WHERE note_id = ? ORDER BY created_at ASC LIMIT 1", (note_dict["id"],))
            img_row = cursor.fetchone()
            note_dict["image_path"] = img_row["image_path"] if img_row else None
            results.append(note_dict)
        return results

def delete_note(note_id: str) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM note_images WHERE note_id = ?", (note_id,))
        cursor.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()

def get_recent_titles(limit: int = 100):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT title FROM notes WHERE title IS NOT NULL AND title != '' ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [row['title'] for row in cursor.fetchall()]

def get_setting(key: str, default: str = None) -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        conn.commit()

def get_all_settings() -> dict:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in cursor.fetchall()}

def update_folder_name(folder_id: str, new_name: str) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE folders SET name = ? WHERE id = ?",
            (new_name, folder_id)
        )
        conn.commit()

def delete_folder(folder_id: str, delete_notes: bool = False) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        if not delete_notes:
            # フォルダ内のノートを inbox に退避する
            cursor.execute(
                "UPDATE notes SET parent_folder_id = 'inbox', updated_at = ? WHERE parent_folder_id = ?",
                (now, folder_id)
            )
        else:
            # 物理削除の際、SQLite側の関連ノートも物理削除する
            cursor.execute("DELETE FROM notes WHERE parent_folder_id = ?", (folder_id,))
            
        cursor.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        conn.commit()

def create_manual_note(note_id: str, parent_folder_id: str, title: str) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO notes (id, parent_folder_id, title, raw_text, image_path, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, parent_folder_id, title, "", None, "completed", now)
        )
        conn.commit()



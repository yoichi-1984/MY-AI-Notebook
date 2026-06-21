import sqlite3
import datetime
import uuid
from config import SQLITE_DB_PATH

def get_connection():
    # ロック競合を防ぐために十分なタイムアウトを設定
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

LATEST_VERSION = 6

def get_db_version(conn) -> int:
    cursor = conn.cursor()
    cursor.execute("PRAGMA user_version")
    return cursor.fetchone()[0]

def set_db_version(conn, version: int):
    conn.execute(f"PRAGMA user_version = {version}")

def init_db():
    with get_connection() as conn:
        current_version = get_db_version(conn)
        
        # 既存DB（バージョン管理導入前）の自動バージョン判定
        if current_version == 0:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notes'")
            has_notes = cursor.fetchone() is not None
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='note_images'")
            has_note_images = cursor.fetchone() is not None
            
            if has_notes:
                if has_note_images:
                    current_version = 2
                else:
                    current_version = 1
                set_db_version(conn, current_version)
                conn.commit()
        
        while current_version < LATEST_VERSION:
            next_version = current_version + 1
            
            # 明示的なトランザクションの開始
            conn.execute("BEGIN")
            try:
                cursor = conn.cursor()
                if next_version == 1:
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
                        "thinking_level": "medium",
                        "model_name": "gemini-3.5-flash",
                        "confidence_threshold": "0.7",
                        "rag_limit": "5",
                        "rag_threshold": "0.8"
                    }
                    for k, v in default_settings.items():
                        cursor.execute("SELECT key, value FROM settings WHERE key = ?", (k,))
                        if not cursor.fetchone():
                            cursor.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))
                
                elif next_version == 2:
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
                
                elif next_version == 3:
                    # notes テーブルに新規ビジネス用メタデータカラムを追加
                    cursor.execute("ALTER TABLE notes ADD COLUMN note_type TEXT")
                    cursor.execute("ALTER TABLE notes ADD COLUMN event_date TEXT")
                    cursor.execute("ALTER TABLE notes ADD COLUMN client_name TEXT")
                    cursor.execute("ALTER TABLE notes ADD COLUMN attendees TEXT")
                    
                    # tasks テーブル (TODOタスク管理用) の新規作成
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS tasks (
                            id TEXT PRIMARY KEY,
                            note_id TEXT NOT NULL,
                            description TEXT NOT NULL,
                            due_date TEXT,
                            is_completed INTEGER DEFAULT 0,
                            FOREIGN KEY (note_id) REFERENCES notes (id)
                        )
                    """)
                
                elif next_version == 4:
                    # notes テーブルに reference_urls カラムを追加
                    cursor.execute("ALTER TABLE notes ADD COLUMN reference_urls TEXT")
                    
                    # note_pages テーブル (将来の複数ページ機能用) の新規作成
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS note_pages (
                            id TEXT PRIMARY KEY,
                            note_id TEXT NOT NULL,
                            page_name TEXT NOT NULL,
                            raw_text TEXT,
                            ai_summary TEXT,
                            ai_tags TEXT,
                            sort_order INTEGER DEFAULT 0,
                            created_at TIMESTAMP,
                            FOREIGN KEY (note_id) REFERENCES notes (id)
                        )
                    """)
                
                elif next_version == 5:
                    # 1. カラムの追加
                    cursor.execute("ALTER TABLE note_images ADD COLUMN page_id TEXT")
                    cursor.execute("ALTER TABLE tasks ADD COLUMN page_id TEXT")
                    cursor.execute("ALTER TABLE note_pages ADD COLUMN reference_urls TEXT")
                    cursor.execute("ALTER TABLE note_pages ADD COLUMN ai_ocr_text TEXT")
                    
                    # 2. 既存ノートデータの移行
                    cursor.execute("SELECT id, raw_text, ai_summary, ai_tags, ai_ocr_text, reference_urls, updated_at FROM notes")
                    all_notes = cursor.fetchall()
                    
                    mappings = [] # [{"note_id": "...", "page_id": "..."}]
                    
                    for note_row in all_notes:
                        n_id, raw_txt, ai_sum, ai_tg, ai_ocr, ref_urls, updated_at = note_row
                        page_id = str(uuid.uuid4())
                        
                        # note_pages へ挿入
                        cursor.execute(
                            """
                            INSERT INTO note_pages (id, note_id, page_name, raw_text, ai_summary, ai_tags, ai_ocr_text, reference_urls, sort_order, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (page_id, n_id, "ページ1", raw_txt, ai_sum, ai_tg, ai_ocr, ref_urls, 0, updated_at)
                        )
                        
                        mappings.append({
                            "note_id": n_id,
                            "page_id": page_id
                        })
                    
                    # 3. note_images の紐付け更新
                    for m in mappings:
                        cursor.execute(
                            "UPDATE note_images SET page_id = ? WHERE note_id = ?",
                            (m["page_id"], m["note_id"])
                        )
                        
                    # 4. tasks の紐付け更新
                    for m in mappings:
                        cursor.execute(
                            "UPDATE tasks SET page_id = ? WHERE note_id = ?",
                            (m["page_id"], m["note_id"])
                        )
                    
                    # 5. LanceDB の移行呼び出し
                    from database import lance_client
                    lance_client.migrate_to_v5(mappings)
                
                elif next_version == 6:
                    # note_attachments テーブルの新規作成
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS note_attachments (
                            id TEXT PRIMARY KEY,
                            note_id TEXT NOT NULL,
                            page_id TEXT NOT NULL,
                            file_path TEXT NOT NULL,
                            file_name TEXT NOT NULL,
                            file_size INTEGER NOT NULL,
                            mime_type TEXT,
                            created_at TIMESTAMP NOT NULL,
                            ai_summary TEXT,
                            ai_ocr_text TEXT,
                            FOREIGN KEY (note_id) REFERENCES notes (id),
                            FOREIGN KEY (page_id) REFERENCES note_pages (id)
                        )
                    """)
                
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"Database migration to version {next_version} failed: {e}")
            
            # トランザクション外で user_version を設定
            set_db_version(conn, next_version)
            current_version = next_version
            
        # 'inbox' フォルダの名前を「仮置き（自動整理）」へ確実に統一更新
        # ※ 外側の with get_connection() の conn をそのまま使う（重複接続を防ぐ）
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM folders WHERE id = 'inbox'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO folders (id, name, parent_id) VALUES ('inbox', '📥 仮置き（自動整理）', NULL)")
        else:
            cursor.execute("UPDATE folders SET name = '📥 仮置き（自動整理）' WHERE id = 'inbox'")
            
        # 既存の thinking_level の値の正規化 (standard/creative -> medium)
        cursor.execute("SELECT value FROM settings WHERE key = 'thinking_level'")
        row = cursor.fetchone()
        if row:
            val = row["value"]
            if val not in ["minimal", "low", "medium", "high"]:
                new_val = "medium"
                if val == "high":
                    new_val = "high"
                elif val == "low":
                    new_val = "low"
                cursor.execute("UPDATE settings SET value = ? WHERE key = 'thinking_level'", (new_val,))
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

def create_note(note_id: str, parent_folder_id: str, raw_text: str, image_path: str = None) -> str:
    now = datetime.datetime.now().isoformat()
    page_id = str(uuid.uuid4())
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO notes (id, parent_folder_id, title, raw_text, image_path, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, parent_folder_id, "", raw_text, image_path, "processing", now)
        )
        
        # note_pages テーブルへ初期ページを挿入
        cursor.execute(
            """
            INSERT INTO note_pages (id, note_id, page_name, raw_text, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (page_id, note_id, "ページ1", raw_text, 0, now)
        )
        
        # 画像がある場合は、初期ページの画像として note_images にも登録
        if image_path:
            img_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO note_images (id, note_id, page_id, image_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (img_id, note_id, page_id, image_path, now)
            )
            
        conn.commit()
    return page_id

def update_note_metadata(note_id: str, title: str, ai_ocr_text: str, ai_summary: str, ai_tags: str, parent_folder_id: str, status: str, reference_urls: str = None) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE notes
            SET title = ?, ai_ocr_text = ?, ai_summary = ?, ai_tags = ?, parent_folder_id = ?, status = ?, reference_urls = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, ai_ocr_text, ai_summary, ai_tags, parent_folder_id, status, reference_urls, now, note_id)
        )
        conn.commit()

def update_note_metadata_optimistic(note_id: str, title: str, ai_ocr_text: str, ai_summary: str, ai_tags: str, parent_folder_id: str, status: str, expected_updated_at: str) -> bool:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE notes
            SET title = ?, ai_ocr_text = ?, ai_summary = ?, ai_tags = ?, parent_folder_id = ?, status = ?, updated_at = ?
            WHERE id = ? AND updated_at = ?
            """,
            (title, ai_ocr_text, ai_summary, ai_tags, parent_folder_id, status, now, note_id, expected_updated_at)
        )
        conn.commit()
        return cursor.rowcount > 0

def update_note_metadata_merge(note_id: str, title: str, ai_ocr_text: str, ai_summary: str, ai_tags: str, parent_folder_id: str, status: str) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT title FROM notes WHERE id = ?", (note_id,))
        row = cursor.fetchone()
        current_title = row["title"] if row else None
        
        final_title = current_title
        if not current_title or current_title == "無題 of ノート" or current_title == "無題のノート" or current_title.strip() == "":
            final_title = title if title else "無題のノート"

        cursor.execute(
            """
            UPDATE notes
            SET title = ?, ai_ocr_text = ?, ai_summary = ?, ai_tags = ?, parent_folder_id = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (final_title, ai_ocr_text, ai_summary, ai_tags, parent_folder_id, status, now, note_id)
        )
        
        # 最初のページも同期更新（互換性維持のため）
        cursor.execute("SELECT id FROM note_pages WHERE note_id = ? ORDER BY sort_order ASC LIMIT 1", (note_id,))
        p_row = cursor.fetchone()
        if p_row:
            cursor.execute(
                """
                UPDATE note_pages
                SET ai_ocr_text = ?, ai_summary = ?, ai_tags = ?
                WHERE id = ?
                """,
                (ai_ocr_text, ai_summary, ai_tags, p_row["id"])
            )
        conn.commit()

def update_page_metadata_merge(page_id: str, title: str, ai_ocr_text: str, ai_summary: str, ai_tags: str, parent_folder_id: str = None, status: str = "completed") -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT note_id FROM note_pages WHERE id = ?", (page_id,))
        row = cursor.fetchone()
        note_id = row["note_id"] if row else None
        
        # ページの更新
        cursor.execute(
            """
            UPDATE note_pages
            SET ai_ocr_text = ?, ai_summary = ?, ai_tags = ?
            WHERE id = ?
            """,
            (ai_ocr_text, ai_summary, ai_tags, page_id)
        )
        
        # 親ノートの更新
        if note_id:
            cursor.execute("SELECT title, parent_folder_id, status FROM notes WHERE id = ?", (note_id,))
            n_row = cursor.fetchone()
            current_title = n_row["title"] if n_row else None
            
            final_title = current_title
            if not current_title or current_title == "無題 of ノート" or current_title == "無題のノート" or current_title.strip() == "":
                final_title = title if title else "無題のノート"
                
            final_folder_id = parent_folder_id if parent_folder_id else (n_row["parent_folder_id"] if n_row else "inbox")
            final_status = status if status else (n_row["status"] if n_row else "completed")
            
            cursor.execute(
                """
                UPDATE notes
                SET title = ?, parent_folder_id = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (final_title, final_folder_id, final_status, now, note_id)
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
        # 最初のページの raw_text も同時に更新（互換性維持のため）
        cursor.execute("SELECT id FROM note_pages WHERE note_id = ? ORDER BY sort_order ASC LIMIT 1", (note_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE note_pages SET raw_text = ? WHERE id = ?", (raw_text, row["id"]))
        conn.commit()

def add_page_image(note_id: str, page_id: str, image_path: str) -> str:
    img_id = str(uuid.uuid4())
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO note_images (id, note_id, page_id, image_path, created_at) VALUES (?, ?, ?, ?, ?)",
            (img_id, note_id, page_id, image_path, now)
        )
        conn.commit()
    return img_id

def add_note_image(note_id: str, image_path: str) -> str:
    # 下位互換性のために、最初のページに画像を紐付ける
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM note_pages WHERE note_id = ? ORDER BY sort_order ASC LIMIT 1", (note_id,))
        row = cursor.fetchone()
        page_id = row["id"] if row else None
    if not page_id:
        page_id = create_page(note_id, "ページ1")
    return add_page_image(note_id, page_id, image_path)

def create_page(note_id: str, page_name: str) -> str:
    page_id = str(uuid.uuid4())
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        # 最大 sort_order の取得
        cursor.execute("SELECT MAX(sort_order) FROM note_pages WHERE note_id = ?", (note_id,))
        max_row = cursor.fetchone()
        max_order = max_row[0] if max_row and max_row[0] is not None else -1
        
        cursor.execute(
            """
            INSERT INTO note_pages (id, note_id, page_name, raw_text, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (page_id, note_id, page_name, "", max_order + 1, now)
        )
        cursor.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id))
        conn.commit()
    return page_id

def update_page_metadata(page_id: str, page_name: str, raw_text: str, reference_urls: str, ai_summary: str, ai_tags: str, ai_ocr_text: str = None) -> str:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT note_id FROM note_pages WHERE id = ?", (page_id,))
        row = cursor.fetchone()
        note_id = row["note_id"] if row else None
        
        cursor.execute(
            """
            UPDATE note_pages
            SET page_name = ?, raw_text = ?, reference_urls = ?, ai_summary = ?, ai_tags = ?, ai_ocr_text = ?
            WHERE id = ?
            """,
            (page_name, raw_text, reference_urls, ai_summary, ai_tags, ai_ocr_text, page_id)
        )
        if note_id:
            cursor.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id))
        conn.commit()
    return note_id

def delete_page(page_id: str) -> tuple[str, list[str]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT note_id FROM note_pages WHERE id = ?", (page_id,))
        row = cursor.fetchone()
        note_id = row["note_id"] if row else None
        
        # 紐づく画像パスの取得
        cursor.execute("SELECT image_path FROM note_images WHERE page_id = ?", (page_id,))
        image_paths = [r["image_path"] for r in cursor.fetchall() if r["image_path"]]
        
        cursor.execute("DELETE FROM note_images WHERE page_id = ?", (page_id,))
        cursor.execute("DELETE FROM tasks WHERE page_id = ?", (page_id,))
        cursor.execute("DELETE FROM note_pages WHERE id = ?", (page_id,))
        
        if note_id:
            now = datetime.datetime.now().isoformat()
            cursor.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id))
        conn.commit()
    return note_id, image_paths

def reorder_pages(note_id: str, page_ids: list[str]) -> None:
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        for idx, p_id in enumerate(page_ids):
            cursor.execute(
                "UPDATE note_pages SET sort_order = ? WHERE id = ? AND note_id = ?",
                (idx, p_id, note_id)
            )
        cursor.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id))
        conn.commit()

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
        
        # 属する全ページを取得 (sort_order の昇順)
        cursor.execute("SELECT * FROM note_pages WHERE note_id = ? ORDER BY sort_order ASC, created_at ASC", (note_id,))
        pages_rows = cursor.fetchall()
        pages = []
        
        for p_row in pages_rows:
            p_dict = dict(p_row)
            # ページごとの画像を取得
            cursor.execute("SELECT id, image_path, ai_ocr_text FROM note_images WHERE page_id = ? ORDER BY created_at ASC", (p_dict["id"],))
            p_dict["images"] = [dict(img_row) for img_row in cursor.fetchall()]
            # ページごとのタスクを取得
            cursor.execute("SELECT id, description, due_date, is_completed FROM tasks WHERE page_id = ?", (p_dict["id"],))
            p_dict["tasks"] = [dict(t_row) for t_row in cursor.fetchall()]
            # ページごとの添付ファイルを取得
            cursor.execute("SELECT id, file_path, file_name, file_size, mime_type, created_at, ai_summary FROM note_attachments WHERE page_id = ? ORDER BY created_at ASC", (p_dict["id"],))
            p_dict["attachments"] = [dict(a_row) for a_row in cursor.fetchall()]
            pages.append(p_dict)
            
        note_dict["pages"] = pages
        
        # 下位互換性のために、1番目のページの内容を notes レコードとしてマージして返す
        if pages:
            first_page = pages[0]
            note_dict["raw_text"] = first_page.get("raw_text") or ""
            note_dict["ai_summary"] = first_page.get("ai_summary") or ""
            note_dict["ai_tags"] = first_page.get("ai_tags") or ""
            note_dict["ai_ocr_text"] = first_page.get("ai_ocr_text") or ""
            note_dict["reference_urls"] = first_page.get("reference_urls") or ""
            note_dict["images"] = first_page.get("images") or []
            note_dict["attachments"] = first_page.get("attachments") or []
        else:
            note_dict["pages"] = []
            note_dict["images"] = []
            note_dict["attachments"] = []
            
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

def delete_note(note_id: str) -> list[str]:
    # 紐づく全ての画像パスおよび添付ファイルパスを取得して、物理削除できるようにする
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT image_path FROM note_images WHERE note_id = ?", (note_id,))
        image_paths = [r["image_path"] for r in cursor.fetchall() if r["image_path"]]
        
        cursor.execute("SELECT file_path FROM note_attachments WHERE note_id = ?", (note_id,))
        attachment_paths = [r["file_path"] for r in cursor.fetchall() if r["file_path"]]
        
        # トランザクションで削除
        cursor.execute("DELETE FROM note_images WHERE note_id = ?", (note_id,))
        cursor.execute("DELETE FROM note_attachments WHERE note_id = ?", (note_id,))
        cursor.execute("DELETE FROM tasks WHERE note_id = ?", (note_id,))
        cursor.execute("DELETE FROM note_pages WHERE note_id = ?", (note_id,))
        cursor.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
    return image_paths + attachment_paths

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
            # 物理削除: FK 依存順に関連レコードをすべて削除してから notes を削除する
            # （PRAGMA foreign_keys は明示ONしていないため CASCADE が効かない）
            cursor.execute(
                """
                DELETE FROM note_images WHERE note_id IN (
                    SELECT id FROM notes WHERE parent_folder_id = ?
                )
                """,
                (folder_id,)
            )
            cursor.execute(
                """
                DELETE FROM note_attachments WHERE note_id IN (
                    SELECT id FROM notes WHERE parent_folder_id = ?
                )
                """,
                (folder_id,)
            )
            cursor.execute(
                """
                DELETE FROM tasks WHERE note_id IN (
                    SELECT id FROM notes WHERE parent_folder_id = ?
                )
                """,
                (folder_id,)
            )
            cursor.execute(
                """
                DELETE FROM note_pages WHERE note_id IN (
                    SELECT id FROM notes WHERE parent_folder_id = ?
                )
                """,
                (folder_id,)
            )
            cursor.execute("DELETE FROM notes WHERE parent_folder_id = ?", (folder_id,))

        cursor.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        conn.commit()

def create_manual_note(note_id: str, parent_folder_id: str, title: str) -> str:
    now = datetime.datetime.now().isoformat()
    page_id = str(uuid.uuid4())
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO notes (id, parent_folder_id, title, raw_text, image_path, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, parent_folder_id, title, "", None, "completed", now)
        )
        
        # note_pages テーブルへ初期ページを挿入
        cursor.execute(
            """
            INSERT INTO note_pages (id, note_id, page_name, raw_text, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (page_id, note_id, "ページ1", "", 0, now)
        )
        conn.commit()
    return page_id

def add_page_attachment(note_id: str, page_id: str, file_path: str, file_name: str, file_size: int, mime_type: str) -> str:
    att_id = str(uuid.uuid4())
    now = datetime.datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO note_attachments (id, note_id, page_id, file_path, file_name, file_size, mime_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (att_id, note_id, page_id, file_path, file_name, file_size, mime_type, now)
        )
        conn.commit()
    return att_id

def get_page_attachments(page_id: str) -> list:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM note_attachments WHERE page_id = ? ORDER BY created_at ASC",
            (page_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

def get_attachment(attachment_id: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM note_attachments WHERE id = ?",
            (attachment_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

def delete_attachment(attachment_id: str) -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_path FROM note_attachments WHERE id = ?", (attachment_id,))
        row = cursor.fetchone()
        file_path = row["file_path"] if row else None

        # file_path が NULL でもレコード自体は必ず削除する（ゾンビレコード防止）
        if row:
            cursor.execute("DELETE FROM note_attachments WHERE id = ?", (attachment_id,))
            conn.commit()
        return file_path

def update_attachment_ai_metadata(attachment_id: str, ai_summary: str, ai_ocr_text: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE note_attachments
            SET ai_summary = ?, ai_ocr_text = ?
            WHERE id = ?
            """,
            (ai_summary, ai_ocr_text, attachment_id)
        )
        conn.commit()



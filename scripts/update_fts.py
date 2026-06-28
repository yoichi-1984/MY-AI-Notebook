import os
import sys

# プロジェクトルートにパスを通す
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import lancedb
from database import sqlite_client, lance_client
import config

def run_update():
    db = lancedb.connect(config.LANCEDB_DIR)
    if lance_client.TABLE_NAME not in db.table_names():
        print(f"Table {lance_client.TABLE_NAME} not found. Skip.")
        return

    table = db.open_table(lance_client.TABLE_NAME)
    all_data = table.to_arrow().to_pylist()

    pages, attachments = sqlite_client.get_all_pages_for_migration()
    
    pages_dict = {p["page_id"]: p for p in pages}
    attachments_dict = {a["attachment_id"]: a for a in attachments}
    
    updated_data = []
    
    for row in all_data:
        record_id = row.get("id")
        
        title = ""
        page_name = ""
        raw_text = ""
        ocr_text = ""
        
        if record_id in pages_dict:
            page = pages_dict[record_id]
            title = page.get("title") or ""
            page_name = page.get("page_name") or ""
            raw_text = page.get("raw_text") or ""
            ocr_text = page.get("merged_ocr_text") or ""
        elif record_id in attachments_dict:
            att = attachments_dict[record_id]
            title = att.get("title") or ""
            page_name = att.get("page_name") or ""
            raw_text = att.get("file_name") or ""
            ocr_text = att.get("extracted_text") or ""
        else:
            print(f"ID {record_id} not found in DB. Leaving as is.")
            updated_data.append(row)
            continue
            
        new_fts = f"{title}\n\n{page_name}\n\n{raw_text}\n\n{ocr_text}"
        row["fts_text"] = new_fts
        updated_data.append(row)
        
    if updated_data:
        schema = table.schema
        db.drop_table(lance_client.TABLE_NAME)
        new_table = db.create_table(lance_client.TABLE_NAME, schema=schema, data=updated_data)
        new_table.create_fts_index("fts_text", replace=True)
        print("Updated FTS text successfully.")

if __name__ == "__main__":
    run_update()

import os
import lancedb
import pyarrow as pa
from config import LANCEDB_DIR

_db = None
_table = None
TABLE_NAME = "knowledge_vector_table"

def get_table():
    global _db, _table
    if _table is None:
        _db = lancedb.connect(LANCEDB_DIR)
        if TABLE_NAME in _db.table_names():
            _table = _db.open_table(TABLE_NAME)
        else:
            # 1536次元のベクトルスキーマ定義 (Azure OpenAI text-embedding-3-small) + note_id カラム追加
            schema = pa.schema([
                pa.field("id", pa.string()),        # page_id として使用
                pa.field("note_id", pa.string()),   # 親ノートのID
                pa.field("vector", pa.list_(pa.float32(), 1536)),
                pa.field("search_text", pa.string()),
                pa.field("ocr_text", pa.string())
            ])
            _table = _db.create_table(TABLE_NAME, schema=schema)
    return _table

def upsert_vector_data(page_id: str, note_id: str, vector: list[float], search_text: str, ocr_text: str):
    table = get_table()
    data = [{
        "id": page_id,
        "note_id": note_id,
        "vector": vector,
        "search_text": search_text,
        "ocr_text": ocr_text
    }]
    
    # 既存データを削除して再登録 (重複排除)
    try:
        table.delete(f"id = '{page_id}'")
    except Exception as e:
        print(f"No existing record to delete or delete failed: {e}")
        
    table.add(data)
    
    # 全文検索 (FTS) インデックスの再構築
    try:
        table.create_fts_index("ocr_text", replace=True)
    except Exception as e:
        print(f"Warning: Failed to create FTS index: {e}. Keyword search will fallback to memory matching.")

def delete_vector_data(page_id: str):
    table = get_table()
    try:
        table.delete(f"id = '{page_id}'")
        # インデックス再構築
        try:
            table.create_fts_index("ocr_text", replace=True)
        except Exception:
            pass
    except Exception as e:
        print(f"Error deleting vector data for page_id {page_id}: {e}")

def delete_all_vector_data_for_note(note_id: str):
    table = get_table()
    try:
        table.delete(f"note_id = '{note_id}'")
        try:
            table.create_fts_index("ocr_text", replace=True)
        except Exception:
            pass
    except Exception as e:
        print(f"Error deleting all vector data for note_id {note_id}: {e}")

def migrate_to_v5(mappings: list[dict]):
    """
    スキーマv5への移行ロジック。
    1. 旧テーブルから全データを読み出す。
    2. 古いテーブルを削除し、新しいスキーマで再作成する。
    3. SQLiteから渡された note_id と page_id のマッピングに基づいて、旧 id (note_id) のデータを id = page_id, note_id = note_id に変換。
    4. 新しいテーブルにインサートする。
    """
    global _db, _table
    if _db is None:
        _db = lancedb.connect(LANCEDB_DIR)
        
    # テーブルが存在しない場合は何も移行しない
    if TABLE_NAME not in _db.table_names():
        return
        
    old_table = _db.open_table(TABLE_NAME)
    
    # 全データの読み込み
    try:
        old_data = old_table.to_arrow().to_pylist()
    except Exception as e:
        print(f"Failed to read old LanceDB data: {e}")
        old_data = []
        
    # マッピングの辞書化
    # mappings: [{"note_id": "...", "page_id": "..."}]
    note_to_page = {m["note_id"]: m["page_id"] for m in mappings}
    
    # 移行データの作成
    new_data = []
    for row in old_data:
        old_id = row.get("id") # 旧スキーマでは note_id が格納されている
        vector = row.get("vector")
        search_text = row.get("search_text") or ""
        ocr_text = row.get("ocr_text") or ""
        
        if old_id in note_to_page:
            new_page_id = note_to_page[old_id]
            new_data.append({
                "id": new_page_id,
                "note_id": old_id,
                "vector": vector,
                "search_text": search_text,
                "ocr_text": ocr_text
            })
            
    # 古いテーブルをドロップ
    _db.drop_table(TABLE_NAME)
    _table = None
    
    # 新しいテーブルを作成
    table = get_table()
    
    # データを投入
    if new_data:
        table.add(new_data)
        try:
            table.create_fts_index("ocr_text", replace=True)
        except Exception as e:
            print(f"Warning: Failed to create FTS index after migration: {e}")
    print(f"LanceDB migration completed. Migrated {len(new_data)} records.")


def hybrid_search(query_vector: list[float], query_text: str, limit: int = 5, distance_threshold: float = 0.7) -> list[dict]:
    table = get_table()
    
    # 1. ベクトル検索 (cosine 類似度)
    vector_results = []
    try:
        # cosine類似度を使うため metric="cosine"
        raw_vector_results = table.search(query_vector).metric("cosine").limit(limit * 2).to_list()
        # コサイン距離が閾値以下のものだけを採用
        vector_results = [
            item for item in raw_vector_results
            if item.get("_distance", 1.0) <= distance_threshold
        ]
    except Exception as e:
        print(f"Vector search failed: {e}")
        
    # 2. 全文・キーワード検索 (FTS)
    fts_results = []
    if query_text:
        try:
            fts_results = table.search(query_text).limit(limit * 2).to_list()
        except Exception as e:
            print(f"FTS search failed: {e}. Falling back to keyword memory matching.")
            # メモリ上での部分一致フォールバック (pandas 依存を回避)
            try:
                # pyarrow Table から直接 python list[dict] へ変換
                all_data = table.to_arrow().to_pylist()
                # ocr_text が None の場合も考慮して空文字にフォールバック
                fts_results = [
                    d for d in all_data
                    if query_text.lower() in (d.get("ocr_text") or "").lower()
                ][:limit * 2]
            except Exception as fe:
                print(f"Keyword memory matching fallback failed: {fe}")
                
    # 3. RRF (Reciprocal Rank Fusion) によるランキング融合
    # RRFスコア計算 (パラメータ k=60)
    rrf_scores = {}
    k = 60
    
    # ベクトル検索の順位を反映
    for rank, item in enumerate(vector_results, start=1):
        note_id = item["id"]
        if note_id not in rrf_scores:
            rrf_scores[note_id] = {"item": item, "score": 0.0}
        rrf_scores[note_id]["score"] += 1.0 / (k + rank)
        
    # FTS検索の順位を反映
    for rank, item in enumerate(fts_results, start=1):
        note_id = item["id"]
        if note_id not in rrf_scores:
            rrf_scores[note_id] = {"item": item, "score": 0.0}
        rrf_scores[note_id]["score"] += 1.0 / (k + rank)
        
    # スコアの高い順にソート
    sorted_results = sorted(rrf_scores.values(), key=lambda x: x["score"], reverse=True)
    
    # 結果整形して返却
    final_results = []
    for entry in sorted_results[:limit]:
        item = entry["item"]
        final_results.append({
            "id": item["id"],  # page_id
            "note_id": item.get("note_id", ""),  # 親ノートID
            "search_text": item.get("search_text", ""),
            "ocr_text": item.get("ocr_text", ""),
            "score": entry["score"]
        })
        
    return final_results

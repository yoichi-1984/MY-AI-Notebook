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
            # 1536次元のベクトルスキーマ定義 (Azure OpenAI text-embedding-3-small)
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 1536)),
                pa.field("search_text", pa.string()),
                pa.field("ocr_text", pa.string())
            ])
            _table = _db.create_table(TABLE_NAME, schema=schema)
    return _table

def upsert_vector_data(note_id: str, vector: list[float], search_text: str, ocr_text: str):
    table = get_table()
    data = [{
        "id": note_id,
        "vector": vector,
        "search_text": search_text,
        "ocr_text": ocr_text
    }]
    
    # 既存データを削除して再登録 (重複排除)
    try:
        table.delete(f"id = '{note_id}'")
    except Exception as e:
        print(f"No existing record to delete or delete failed: {e}")
        
    table.add(data)
    
    # 全文検索 (FTS) インデックスの再構築
    try:
        table.create_fts_index("ocr_text", replace=True)
    except Exception as e:
        # 環境によっては tantivy-py 等の依存関係不足でFTSインデックスが作成できない可能性があるため警告に留める
        print(f"Warning: Failed to create FTS index: {e}. Keyword search will fallback to memory matching.")

def delete_vector_data(note_id: str):
    table = get_table()
    try:
        table.delete(f"id = '{note_id}'")
        # インデックス再構築
        try:
            table.create_fts_index("ocr_text", replace=True)
        except Exception:
            pass
    except Exception as e:
        print(f"Error deleting vector data for note_id {note_id}: {e}")

def hybrid_search(query_vector: list[float], query_text: str, limit: int = 5) -> list[dict]:
    table = get_table()
    
    # 1. ベクトル検索 (cosine 類似度)
    vector_results = []
    try:
        # cosine類似度を使うため metric="cosine"
        vector_results = table.search(query_vector).metric("cosine").limit(limit * 2).to_list()
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
            "id": item["id"],
            "search_text": item.get("search_text", ""),
            "ocr_text": item.get("ocr_text", ""),
            "score": entry["score"]
        })
        
    return final_results

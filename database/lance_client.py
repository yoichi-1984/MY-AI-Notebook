import os
import lancedb
import pyarrow as pa
import config

_db = None
_table = None
TABLE_NAME = "knowledge_vector_table_v2"

def reset_connection():
    global _db, _table
    _db = None
    _table = None

def get_table():
    global _db, _table
    if _table is None:
        _db = lancedb.connect(config.LANCEDB_DIR)
        if TABLE_NAME in _db.table_names():
            _table = _db.open_table(TABLE_NAME)
        else:
            # 512次元のマルチベクトルスキーマ定義 (Azure OpenAI text-embedding-3-small 512)
            schema = pa.schema([
                pa.field("id", pa.string()),        # page_id として使用
                pa.field("note_id", pa.string()),   # 親ノートのID
                pa.field("summary_vector", pa.list_(pa.float32(), 512)),
                pa.field("tags_vector", pa.list_(pa.float32(), 512)),
                pa.field("body_vector", pa.list_(pa.float32(), 512)),
                pa.field("fts_text", pa.string())
            ])
            _table = _db.create_table(TABLE_NAME, schema=schema)
    return _table

def upsert_vector_data(page_id: str, note_id: str, summary_vector: list[float], tags_vector: list[float], body_vector: list[float], fts_text: str):
    table = get_table()
    data = [{
        "id": page_id,
        "note_id": note_id,
        "summary_vector": summary_vector,
        "tags_vector": tags_vector,
        "body_vector": body_vector,
        "fts_text": fts_text
    }]
    
    # 既存データを削除して再登録 (重複排除)
    try:
        table.delete(f"id = '{page_id}'")
    except Exception as e:
        print(f"No existing record to delete or delete failed: {e}")
        
    table.add(data)
    
    # 全文検索 (FTS) インデックスの再構築
    try:
        table.create_fts_index("fts_text", replace=True)
    except Exception as e:
        print(f"Warning: Failed to create FTS index: {e}. Keyword search will fallback to memory matching.")

def delete_vector_data(page_id: str):
    table = get_table()
    try:
        table.delete(f"id = '{page_id}'")
        # インデックス再構築
        try:
            table.create_fts_index("fts_text", replace=True)
        except Exception:
            pass
    except Exception as e:
        print(f"Error deleting vector data for page_id {page_id}: {e}")

def delete_all_vector_data_for_note(note_id: str):
    table = get_table()
    try:
        table.delete(f"note_id = '{note_id}'")
        try:
            table.create_fts_index("fts_text", replace=True)
        except Exception:
            pass
    except Exception as e:
        print(f"Error deleting all vector data for note_id {note_id}: {e}")

def migrate_to_v5(mappings: list[dict]):
    """
    スキーマv5への移行ロジック。
    旧テーブル（スキーマ: vector/search_text/ocr_text）と現行テーブル（スキーマ: summary_vector/tags_vector/body_vector/fts_text）は
    カラム構成が完全に異なるため、旧データをそのまま挿入するとデータ破壊が発生する。
    そのため、テーブルのドロップ＆空テーブルの再作成のみを行い、データ再投入は起動時に実行される
    migrate_existing_data_to_v2()（workflow.py）に委任する。
    migrate_existing_data_to_v2() はテーブルが空のとき SQLite から全データを再インデックスするため、
    データは安全に復元される。
    """
    global _db, _table
    if _db is None:
        _db = lancedb.connect(config.LANCEDB_DIR)

    # テーブルが存在しない場合は何もしない
    if TABLE_NAME not in _db.table_names():
        print(f"[migrate_to_v5] Table '{TABLE_NAME}' does not exist. Skipping drop.")
        return

    # 旧テーブルをドロップ（スキーマ不一致のデータ挿入を防ぐ）
    _db.drop_table(TABLE_NAME)
    _table = None
    print(f"[migrate_to_v5] Dropped old table '{TABLE_NAME}'.")

    # 新スキーマでテーブルを再作成（空テーブル）
    get_table()
    print(f"[migrate_to_v5] Recreated empty table '{TABLE_NAME}' with new schema (multi-vector 512d).")
    print("[migrate_to_v5] Re-indexing will be handled by migrate_existing_data_to_v2() on startup.")


def hybrid_search(query_vector: list[float], query_text: str, limit: int = 5, distance_threshold: float = 0.7) -> list[dict]:
    table = get_table()
    
    # 1. ベクトル検索 (cosine 類似度) - 3つのベクトルカラムそれぞれで検索
    # summary_vector に対して検索
    summary_results = []
    try:
        raw_summary_results = table.search(query_vector, vector_column_name="summary_vector").metric("cosine").limit(limit * 2).to_list()
        summary_results = [
            item for item in raw_summary_results
            if item.get("_distance", 1.0) <= distance_threshold
        ]
    except Exception as e:
        print(f"Summary vector search failed: {e}")

    # tags_vector に対して検索
    tags_results = []
    try:
        raw_tags_results = table.search(query_vector, vector_column_name="tags_vector").metric("cosine").limit(limit * 2).to_list()
        tags_results = [
            item for item in raw_tags_results
            if item.get("_distance", 1.0) <= distance_threshold
        ]
    except Exception as e:
        print(f"Tags vector search failed: {e}")

    # body_vector に対して検索
    body_results = []
    try:
        raw_body_results = table.search(query_vector, vector_column_name="body_vector").metric("cosine").limit(limit * 2).to_list()
        body_results = [
            item for item in raw_body_results
            if item.get("_distance", 1.0) <= distance_threshold
        ]
    except Exception as e:
        print(f"Body vector search failed: {e}")
        
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
                # fts_text が None の場合も考慮して空文字にフォールバック
                fts_results = [
                    d for d in all_data
                    if query_text.lower() in (d.get("fts_text") or "").lower()
                ][:limit * 2]
            except Exception as fe:
                print(f"Keyword memory matching fallback failed: {fe}")
                
    # 3. RRF (Reciprocal Rank Fusion) によるランキング融合
    # RRFスコア計算 (パラメータ k=60)
    rrf_scores = {}
    k = 60
    
    # 各検索結果の順位を反映
    def add_rankings(results):
        for rank, item in enumerate(results, start=1):
            note_id = item["id"]
            if note_id not in rrf_scores:
                rrf_scores[note_id] = {"item": item, "score": 0.0}
            rrf_scores[note_id]["score"] += 1.0 / (k + rank)
            
    add_rankings(summary_results)
    add_rankings(tags_results)
    add_rankings(body_results)
    add_rankings(fts_results)
        
    # スコアの高い順にソート
    sorted_results = sorted(rrf_scores.values(), key=lambda x: x["score"], reverse=True)
    
    # 結果整形して返却
    final_results = []
    for entry in sorted_results[:limit]:
        item = entry["item"]
        final_results.append({
            "id": item["id"],  # page_id
            "note_id": item.get("note_id", ""),  # 親ノートID
            "fts_text": item.get("fts_text", ""),
            "search_text": item.get("fts_text", ""), # 互換性のためのエイリアス
            "ocr_text": item.get("fts_text", ""),    # 互換性のためのエイリアス
            "score": entry["score"]
        })
        
    return final_results

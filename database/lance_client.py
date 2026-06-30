import os
import lancedb
import pyarrow as pa
import threading
import config

_db = None
_table = None
_db_lock = threading.Lock()
TABLE_NAME = "knowledge_vector_table_v3"

def reset_connection():
    global _db, _table
    _db = None
    _table = None

def get_table():
    global _db, _table
    if _table is None:
        with _db_lock:
            if _table is None:
                _db = lancedb.connect(config.LANCEDB_DIR)
                if TABLE_NAME in _db.table_names():
                    _table = _db.open_table(TABLE_NAME)
                else:
                    # 512次元のマルチベクトルスキーマ定義 (Azure OpenAI text-embedding-3-small 512)
                    schema = pa.schema([
                        pa.field("id", pa.string()),          # chunk_id として使用
                        pa.field("source_id", pa.string()),   # 親の page_id または attachment_id
                        pa.field("note_id", pa.string()),     # 親ノートのID
                        pa.field("chunk_index", pa.int32()),  # チャンクのインデックス
                        pa.field("summary_vector", pa.list_(pa.float32(), 512)),
                        pa.field("tags_vector", pa.list_(pa.float32(), 512)),
                        pa.field("body_vector", pa.list_(pa.float32(), 512)),
                        pa.field("fts_text", pa.string())
                    ])
                    _table = _db.create_table(TABLE_NAME, schema=schema)
    return _table

def upsert_vector_data_chunks(source_id: str, note_id: str, chunks_data: list[dict]):
    table = get_table()
    data = []
    for chunk in chunks_data:
        data.append({
            "id": f"{source_id}_{chunk['chunk_index']}",
            "source_id": source_id,
            "note_id": note_id,
            "chunk_index": chunk["chunk_index"],
            "summary_vector": chunk["summary_vector"],
            "tags_vector": chunk["tags_vector"],
            "body_vector": chunk["body_vector"],
            "fts_text": chunk["fts_text"]
        })
    
    with _db_lock:
        # 既存データを削除して再登録 (重複排除)
        try:
            table.delete(f"source_id = '{source_id}'")
        except Exception as e:
            print(f"No existing record to delete or delete failed: {e}")
            
        table.add(data)
        
        # 全文検索 (FTS) インデックスの再構築
        try:
            table.create_fts_index("fts_text", replace=True)
        except Exception as e:
            print(f"Warning: Failed to create FTS index: {e}. Keyword search will fallback to memory matching.")

def delete_vector_data(source_id: str):
    table = get_table()
    with _db_lock:
        try:
            table.delete(f"source_id = '{source_id}'")
            # インデックス再構築
            try:
                table.create_fts_index("fts_text", replace=True)
            except Exception:
                pass
        except Exception as e:
            print(f"Error deleting vector data for source_id {source_id}: {e}")

def delete_all_vector_data_for_note(note_id: str):
    table = get_table()
    with _db_lock:
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
    [廃止済み / DEPRECATED]
    このSQLite DBマイグレーション(v5)は、LanceDBテーブルの再作成を担うものでしたが、
    現在のテーブル名は knowledge_vector_table_v3 であり、v5マイグレーションは
    DB version=10 の現環境では絶対に呼ばれません。
    万が一誤呼出しが起きた場合に v3 テーブルを破壊しないよう、処理を完全に無効化しています。
    """
    print("[migrate_to_v5] WARNING: This deprecated migration function was called unexpectedly. Skipping to prevent data loss.")
    return


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
            source_id = item.get("source_id") or item["id"]  # v3 schema uses source_id
            if source_id not in rrf_scores:
                rrf_scores[source_id] = {"item": item, "score": 0.0}
            rrf_scores[source_id]["score"] += 1.0 / (k + rank)
            
    add_rankings(summary_results)
    add_rankings(tags_results)
    add_rankings(body_results)
    add_rankings(fts_results)
        
    # note_id でグループ化して重複排除:
    # 検索結果はUI上「ノート」単位で表示されるため、同一ノートの複数ページ・複数チャンクが
    # ヒットした場合は、最もスコアの高い1件（最関連ページ）のみを代表として採用する。
    grouped_by_note = {}
    for entry in rrf_scores.values():
        note_id = entry["item"].get("note_id", "")
        if not note_id:
            continue
        # 最もスコアの高いページ（チャンク）を採用
        if note_id not in grouped_by_note or entry["score"] > grouped_by_note[note_id]["score"]:
            grouped_by_note[note_id] = entry

    # スコアの高い順にソート
    sorted_results = sorted(grouped_by_note.values(), key=lambda x: x["score"], reverse=True)
    
    # 結果整形して返却
    final_results = []
    for entry in sorted_results[:limit]:
        item = entry["item"]
        final_results.append({
            "id": item.get("source_id") or item["id"],  # page_id として返す
            "note_id": item.get("note_id", ""),  # 親ノートID
            "fts_text": item.get("fts_text", ""),
            "search_text": item.get("fts_text", ""), # 互換性のためのエイリアス
            "ocr_text": item.get("fts_text", ""),    # 互換性のためのエイリアス
            "score": entry["score"]
        })
        
    return final_results

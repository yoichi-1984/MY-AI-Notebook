import os
import datetime
import aiofiles
import config
from database import sqlite_client, lance_client
from services import ai_agent

# WebSocketアクティブ接続リスト
active_connections = []

async def broadcast_ws_message(message: dict):
    """接続しているすべてのクライアントへ非同期でWebSocketメッセージを送信"""
    for connection in list(active_connections):
        try:
            await connection.send_json(message)
        except Exception:
            if connection in active_connections:
                active_connections.remove(connection)

async def async_pipeline_workflow(note_id: str, skip_classification: bool = False):
    """
    ノート作成・画像追加時のバックグラウンド非同期構造化・自動仕分けパイプライン
    """
    try:
        # 1. SQLiteから生データ取得
        note = sqlite_client.get_note(note_id)
        if not note:
            print(f"Error: Note {note_id} not found in SQLite.")
            return

        # 2. コンテキスト収集
        # 既存フォルダ一覧
        folders = sqlite_client.get_folders()
        folder_list_str = "\n".join([f"- ID: '{f['id']}', 名前: '{f['name']}' (親ID: '{f['parent_id']}')" for f in folders])
        
        # 直近ノートタイトル（トーン同調用）
        recent_titles = sqlite_client.get_recent_titles(limit=100)
        existing_titles_str = ", ".join(recent_titles) if recent_titles else "（まだ既存タイトルはありません）"
        
        # 過去の仕分けナレッジ (rules.md)
        rules_str = ""
        if os.path.exists(config.RULES_PATH):
            async with aiofiles.open(config.RULES_PATH, "r", encoding="utf-8") as f:
                rules_str = await f.read()

        # 画像バイナリの読み込み
        image_bytes = None
        if note.get("image_path"):
            full_image_path = os.path.join(config.IMAGE_DIR, os.path.basename(note["image_path"]))
            if os.path.exists(full_image_path):
                async with aiofiles.open(full_image_path, "rb") as f:
                    image_bytes = await f.read()

        # 3. Geminiによる解析・構造化
        structured_data = ai_agent.analyze_and_structure_with_gemini(
            image_bytes=image_bytes,
            raw_text=note["raw_text"],
            folder_list_string=folder_list_str,
            existing_titles_string=existing_titles_str,
            rules_string=rules_str
        )

        # 4. 「迷い・ゆらぎ」の判定 ＆ 自動仕分け制御
        suggested_folder_id = note["parent_folder_id"]
        status = "completed"

        if not skip_classification:
            valid_folder_ids = [f["id"] for f in folders]
            is_valid_folder = structured_data.suggested_folder_id in valid_folder_ids
            
            # 自信度閾値をDBから動的に取得 (デフォルト 0.7)
            try:
                confidence_threshold = float(sqlite_client.get_setting("confidence_threshold", "0.7"))
            except Exception:
                confidence_threshold = 0.7
            
            # 自信度が低いか、仕分け先がない、または提案されたフォルダIDが実在しない場合
            if structured_data.confidence_score < confidence_threshold or structured_data.suggested_folder_id == "unclassified" or not is_valid_folder:
                # 仕分け保留 (pending_review)
                title = structured_data.refined_title if structured_data.refined_title else "無題のノート"
                sqlite_client.update_note_metadata(
                    note_id=note_id,
                    title=title,
                    ai_ocr_text=structured_data.ocr_raw_text,
                    ai_summary=structured_data.clean_summary,
                    ai_tags=",".join(structured_data.tags),
                    parent_folder_id=note["parent_folder_id"], # 元のフォルダ（通常は inbox）
                    status="pending_review"
                )
                # WebSocketプッシュ送信
                await broadcast_ws_message({
                    "type": "pending_review",
                    "note_id": note_id,
                    "title": title,
                    "folder_id": note["parent_folder_id"],
                    "confidence_score": structured_data.confidence_score
                })
                print(f"Note {note_id} is pending review (Confidence: {structured_data.confidence_score}).")
                return
            else:
                suggested_folder_id = structured_data.suggested_folder_id

        # 5. 通常ルート：自動仕分け成功（またはスキップ）
        current_title = note.get("title")
        if not current_title or current_title == "無題のノート" or current_title.strip() == "":
            title = structured_data.refined_title if structured_data.refined_title else "無題のノート"
        else:
            title = current_title
        tags_str = ",".join(structured_data.tags)

        # SQLite 更新
        sqlite_client.update_note_metadata(
            note_id=note_id,
            title=title,
            ai_ocr_text=structured_data.ocr_raw_text,
            ai_summary=structured_data.clean_summary,
            ai_tags=tags_str,
            parent_folder_id=suggested_folder_id,
            status=status
        )

        # ベクトルデータの構築と登録
        search_text = f"要約: {structured_data.clean_summary}\nタグ: {tags_str}"
        vector = ai_agent.generate_embedding_via_azure(search_text)
        
        lance_client.upsert_vector_data(
            note_id=note_id,
            vector=vector,
            search_text=search_text,
            ocr_text=structured_data.ocr_raw_text
        )

        # WebSocketプッシュ送信
        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "title": title,
            "folder_id": structured_data.suggested_folder_id,
            "message": "自動仕分けが完了しました。"
        })
        print(f"Note {note_id} successfully classified to {structured_data.suggested_folder_id}")

    except Exception as e:
        print(f"Error in async_pipeline_workflow for note {note_id}: {e}")
        # 例外発生時は保留扱いで画面通知
        try:
            sqlite_client.update_note_status(note_id, "pending_review")
            await broadcast_ws_message({
                "type": "pending_review",
                "note_id": note_id,
                "title": "エラーが発生したノート",
                "folder_id": "inbox",
                "confidence_score": 0.0,
                "error": str(e)
            })
        except Exception as inner_e:
            print(f"Failed to handle error callback: {inner_e}")

async def update_rules_with_feedback(note_id: str, correct_folder_id: str, reason: str = ""):
    """
    手動仕分け修正時のインコンテキスト学習フィードバックと rules.md の更新
    """
    try:
        # 1. データの収集
        note = sqlite_client.get_note(note_id)
        if not note:
            print(f"Error: Note {note_id} not found.")
            return

        folders = sqlite_client.get_folders()
        folder_map = {f["id"]: f["name"] for f in folders}
        
        old_folder_id = note["parent_folder_id"]
        old_folder_name = folder_map.get(old_folder_id, "インボックス/不明")
        correct_folder_name = folder_map.get(correct_folder_id, "不明なフォルダ")

        # 2. rules.md の読み込み
        rules_str = ""
        if os.path.exists(config.RULES_PATH):
            async with aiofiles.open(config.RULES_PATH, "r", encoding="utf-8") as f:
                rules_str = await f.read()

        # 3. Gemini によるルールの学習と更新
        gemini_key = os.getenv("GEMINI_API_KEY")
        json_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or config.GEMINI_JSON_PATH
        new_rules = ""
        if not gemini_key and (not json_path or not os.path.exists(json_path)):
            print("Warning: No Gemini credentials set. Updating rules.md in MOCK mode.")
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_rule_entry = f"\n- [{timestamp}] 「{note.get('title', '無題')}」を「{old_folder_name}」から「{correct_folder_name}」へ手動移動。理由: {reason or '指示なし'}\n"
            new_rules = rules_str + new_rule_entry
        else:
            prompt = f"""
            ユーザーは以下のノートをフォルダ「{old_folder_name} (ID: {old_folder_id})」から「{correct_folder_name} (ID: {correct_folder_id})」へ移動させました。
            
            【移動されたノートの情報】
            - タイトル: {note.get('title')}
            - 生テキスト: {note.get('raw_text')}
            - AI要約: {note.get('ai_summary')}
            - タグ: {note.get('ai_tags')}
            - ユーザーが提示した修正理由: {reason or '特になし'}

            【現在の仕分けルール (rules.md)】
            {rules_str}

            この修正アクション of 事実から、今後の自動仕分けの判断基準となるガイドラインを抽出・アップデートしてください。
            既存のルールと矛盾しないように整理し、全体を綺麗に整理したMarkdownの箇条書き（および構成）として出力し直してください。
            余計な説明（「以下が更新されたルールです」など）は省き、純粋なMarkdownテキストのみを返してください。
            """
            try:
                response = ai_agent.generate_content_with_fallback(
                    contents=prompt,
                    temperature=0.3
                )
                new_rules = response.text
            except Exception as e:
                print(f"Failed to generate feedback rules: {e}. Falling back to MOCK mode rule entry.")
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                new_rule_entry = f"\n- [{timestamp}] 「{note.get('title', '無題')}」を「{old_folder_name}」から「{correct_folder_name}」へ手動移動。理由: {reason or '指示なし'}\n"
                new_rules = rules_str + new_rule_entry

        # 4. rules.md の上書き保存
        if new_rules:
            async with aiofiles.open(config.RULES_PATH, "w", encoding="utf-8") as f:
                await f.write(new_rules)

        # 5. SQLite および LanceDB の更新
        # ノートのメタデータとフォルダIDを更新し、ステータスを完了にする
        sqlite_client.update_note_metadata(
            note_id=note_id,
            title=note["title"] or "無題のノート",
            ai_ocr_text=note["ai_ocr_text"] or "",
            ai_summary=note["ai_summary"] or "",
            ai_tags=note["ai_tags"] or "",
            parent_folder_id=correct_folder_id,
            status="completed"
        )

        # ベクトルデータの再登録
        search_text = f"要約: {note['ai_summary'] or ''}\nタグ: {note['ai_tags'] or ''}"
        vector = ai_agent.generate_embedding_via_azure(search_text)
        
        lance_client.upsert_vector_data(
            note_id=note_id,
            vector=vector,
            search_text=search_text,
            ocr_text=note["ai_ocr_text"] or ""
        )

        # WebSocketで画面へ通知
        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "title": note["title"] or "無題のノート",
            "folder_id": correct_folder_id,
            "message": "手動仕分けおよびフィードバック学習が完了しました。"
        })
        print(f"Feedback learning completed for note {note_id}. rules.md updated.")

    except Exception as e:
        print(f"Error in update_rules_with_feedback for note {note_id}: {e}")

async def process_new_image_workflow(note_id: str, image_id: str):
    """
    新規画像追加時の画像単体OCR ＆ ノート全体の再構造化パイプライン
    """
    try:
        # 1. SQLiteからノートおよび画像情報取得
        note = sqlite_client.get_note(note_id)
        if not note:
            print(f"Error: Note {note_id} not found.")
            return
            
        images = note.get("images", [])
        target_image = next((img for img in images if img["id"] == image_id), None)
        if not target_image:
            print(f"Error: Image {image_id} not found in note {note_id}.")
            return

        # 2. 画像のロード
        image_bytes = None
        full_image_path = os.path.join(config.IMAGE_DIR, os.path.basename(target_image["image_path"]))
        if os.path.exists(full_image_path):
            async with aiofiles.open(full_image_path, "rb") as f:
                image_bytes = await f.read()

        if not image_bytes:
            print(f"Error: Physical image file {full_image_path} not found.")
            return

        # 3. 画像単体のOCR
        ocr_text = ai_agent.ocr_image_with_gemini(image_bytes)
        
        # SQLite更新 (画像個別のOCR結果)
        sqlite_client.update_note_image_ocr(image_id, ocr_text)

        # 4. 全画像のOCRテキストを結合
        updated_note = sqlite_client.get_note(note_id)
        updated_images = updated_note.get("images", [])
        all_ocr_texts = [img["ai_ocr_text"] for img in updated_images if img["ai_ocr_text"]]
        merged_ocr_text = "\n\n".join(all_ocr_texts)

        # 5. ノート全体の再構造化 (要約・タグ・タイトル)
        if note.get("parent_folder_id") == "inbox":
            # 仮置き（自動整理）フォルダ所属の場合は、画像OCR完了時点で自動仕分けパイプラインへ移譲
            await async_pipeline_workflow(note_id)
            return

        recent_titles = sqlite_client.get_recent_titles(limit=100)
        existing_titles_str = ", ".join(recent_titles) if recent_titles else "（まだ既存タイトルはありません）"

        summary_data = ai_agent.generate_summary_and_tags_with_gemini(
            raw_text=note["raw_text"],
            merged_ocr_text=merged_ocr_text,
            existing_titles_string=existing_titles_str
        )

        title = note.get("title")
        if not title or title == "無題のノート" or title.strip() == "":
            title = summary_data.refined_title if summary_data.refined_title else "無題のノート"

        tags_str = ",".join(summary_data.tags)

        # SQLite 更新 (ノート全体)
        sqlite_client.update_note_metadata(
            note_id=note_id,
            title=title,
            ai_ocr_text=merged_ocr_text,
            ai_summary=summary_data.clean_summary,
            ai_tags=tags_str,
            parent_folder_id=note["parent_folder_id"],
            status="completed"
        )

        # 6. ベクトル登録
        search_text = f"要約: {summary_data.clean_summary}\nタグ: {tags_str}"
        vector = ai_agent.generate_embedding_via_azure(search_text)
        
        lance_client.upsert_vector_data(
            note_id=note_id,
            vector=vector,
            search_text=search_text,
            ocr_text=merged_ocr_text
        )

        # 7. WebSocketプッシュ送信
        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "title": title,
            "folder_id": note["parent_folder_id"],
            "message": "画像の追加とAI解析が完了しました。"
        })
        print(f"Successfully processed new image {image_id} for note {note_id}")

    except Exception as e:
        print(f"Error in process_new_image_workflow for note {note_id}: {e}")
        try:
            sqlite_client.update_note_status(note_id, "completed")
            await broadcast_ws_message({
                "type": "completed",
                "note_id": note_id,
                "title": "解析エラー",
                "folder_id": note["parent_folder_id"] if note else "inbox",
                "error": str(e)
            })
        except Exception as inner_e:
            print(f"Failed to handle error callback: {inner_e}")

async def recalculate_on_image_delete(note_id: str):
    """
    画像削除後のノート再要約・タグ再生成パイプライン
    """
    try:
        note = sqlite_client.get_note(note_id)
        if not note:
            return

        images = note.get("images", [])
        all_ocr_texts = [img["ai_ocr_text"] for img in images if img["ai_ocr_text"]]
        merged_ocr_text = "\n\n".join(all_ocr_texts)

        recent_titles = sqlite_client.get_recent_titles(limit=100)
        existing_titles_str = ", ".join(recent_titles) if recent_titles else "（まだ既存タイトルはありません）"

        summary_data = ai_agent.generate_summary_and_tags_with_gemini(
            raw_text=note["raw_text"],
            merged_ocr_text=merged_ocr_text,
            existing_titles_string=existing_titles_str
        )

        title = note.get("title")
        if not title or title == "無題のノート" or title.strip() == "":
            title = summary_data.refined_title if summary_data.refined_title else "無題のノート"

        tags_str = ",".join(summary_data.tags)

        sqlite_client.update_note_metadata(
            note_id=note_id,
            title=title,
            ai_ocr_text=merged_ocr_text,
            ai_summary=summary_data.clean_summary,
            ai_tags=tags_str,
            parent_folder_id=note["parent_folder_id"],
            status="completed"
        )

        search_text = f"要約: {summary_data.clean_summary}\nタグ: {tags_str}"
        vector = ai_agent.generate_embedding_via_azure(search_text)
        
        lance_client.upsert_vector_data(
            note_id=note_id,
            vector=vector,
            search_text=search_text,
            ocr_text=merged_ocr_text
        )

        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "title": title,
            "folder_id": note["parent_folder_id"],
            "message": "画像削除に伴う再解析が完了しました。"
        })
        print(f"Recalculated summary for note {note_id} after image deletion.")

    except Exception as e:
        print(f"Error in recalculate_on_image_delete: {e}")
        try:
            sqlite_client.update_note_status(note_id, "completed")
            await broadcast_ws_message({
                "type": "completed",
                "note_id": note_id,
                "title": "解析エラー",
                "folder_id": note["parent_folder_id"] if 'note' in locals() and note else "inbox",
                "error": str(e)
            })
        except Exception as inner_e:
            print(f"Failed to handle error callback: {inner_e}")

async def recalculate_vector_on_edit(note_id: str):
    """
    ノート手動編集後の Embedding 再計算と LanceDB の更新
    """
    try:
        note = sqlite_client.get_note(note_id)
        if not note:
            return

        search_text = f"要約: {note['ai_summary'] or ''}\nタグ: {note['ai_tags'] or ''}"
        if not note['ai_summary'] and not note['ai_tags']:
            search_text = note['raw_text']

        vector = ai_agent.generate_embedding_via_azure(search_text)
        
        # 複数画像のOCRテキストをマージ
        images = note.get("images", [])
        all_ocr_texts = [img["ai_ocr_text"] for img in images if img["ai_ocr_text"]]
        merged_ocr_text = "\n\n".join(all_ocr_texts)

        lance_client.upsert_vector_data(
            note_id=note_id,
            vector=vector,
            search_text=search_text,
            ocr_text=merged_ocr_text
        )
        print(f"Recalculated embedding for note {note_id} after edit.")
    except Exception as e:
        print(f"Error in recalculate_vector_on_edit for note {note_id}: {e}")

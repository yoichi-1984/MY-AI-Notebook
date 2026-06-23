import os
import datetime
import aiofiles
import asyncio
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

async def async_pipeline_workflow(note_id: str, page_id: str = None, skip_classification: bool = False):
    """
    ノート作成・画像追加時のバックグラウンド非同期構造化・自動仕分けパイプライン (ページ単位)
    """
    try:
        # 1. SQLiteからノート取得
        note = sqlite_client.get_note(note_id)
        if not note:
            print(f"Error: Note {note_id} not found in SQLite.")
            return

        pages = note.get("pages", [])
        if not pages:
            print(f"Error: Note {note_id} has no pages.")
            return
            
        # ターゲットとなるページを特定
        if page_id is None:
            target_page = pages[0]
            page_id = target_page["id"]
        else:
            target_page = next((p for p in pages if p["id"] == page_id), None)
            if not target_page:
                print(f"Error: Page {page_id} not found in note {note_id}.")
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

        # 画像バイナリの読み込み (対象ページの画像から最初のものをGeminiへ送る)
        image_bytes = None
        page_images = target_page.get("images", [])
        if page_images:
            first_img = page_images[0]
            full_image_path = os.path.join(config.IMAGE_DIR, os.path.basename(first_img["image_path"]))
            if os.path.exists(full_image_path):
                async with aiofiles.open(full_image_path, "rb") as f:
                    image_bytes = await f.read()

        # 3. Geminiによる解析・構造化
        structured_data = ai_agent.analyze_and_structure_with_gemini(
            image_bytes=image_bytes,
            raw_text=target_page["raw_text"] or "",
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
                analyzed_image_count = len(target_page.get("images", []))
                sqlite_client.update_page_metadata_merge(
                    page_id=page_id,
                    title=title,
                    ai_ocr_text=structured_data.ocr_raw_text,
                    ai_summary=structured_data.clean_summary,
                    ai_tags=",".join(structured_data.tags),
                    parent_folder_id=note["parent_folder_id"], # 元のフォルダ（通常は inbox）
                    status="pending_review",
                    analyzed_image_count=analyzed_image_count
                )
                # WebSocketプッシュ送信
                await broadcast_ws_message({
                    "type": "pending_review",
                    "note_id": note_id,
                    "page_id": page_id,
                    "title": title,
                    "folder_id": note["parent_folder_id"],
                    "confidence_score": structured_data.confidence_score
                })
                print(f"Note {note_id} (Page: {page_id}) is pending review (Confidence: {structured_data.confidence_score}).")
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

        analyzed_image_count = len(target_page.get("images", []))
        sqlite_client.update_page_metadata_merge(
            page_id=page_id,
            title=title,
            ai_ocr_text=structured_data.ocr_raw_text,
            ai_summary=structured_data.clean_summary,
            ai_tags=tags_str,
            parent_folder_id=suggested_folder_id,
            status=status,
            analyzed_image_count=analyzed_image_count
        )

        # ベクトルデータの構築と登録
        summary_text = f"要約: {structured_data.clean_summary}"
        tags_text = f"タグ: {tags_str}"
        body_text = f"本文: {note.get('raw_text') or ''}"
        
        summary_vector, tags_vector, body_vector = await asyncio.gather(
            ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
        )
        
        fts_text = f"{note.get('raw_text') or ''}\n\n{structured_data.ocr_raw_text or ''}"
        
        lance_client.upsert_vector_data(
            page_id=page_id,
            note_id=note_id,
            summary_vector=summary_vector,
            tags_vector=tags_vector,
            body_vector=body_vector,
            fts_text=fts_text
        )

        # WebSocketプッシュ送信
        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "page_id": page_id,
            "title": title,
            "folder_id": suggested_folder_id,
            "message": "自動仕分けが完了しました。"
        })
        print(f"Note {note_id} (Page: {page_id}) successfully classified to {suggested_folder_id}")

    except Exception as e:
        print(f"Error in async_pipeline_workflow for note {note_id} (page {page_id}): {e}")
        # 例外発生時は保留扱いで画面通知
        try:
            sqlite_client.update_note_status(note_id, "pending_review")
            await broadcast_ws_message({
                "type": "pending_review",
                "note_id": note_id,
                "page_id": page_id,
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

        pages = note.get("pages", [])
        if not pages:
            print(f"Error: Note {note_id} has no pages.")
            return
        first_page = pages[0]
        page_id = first_page["id"]

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
            - 生テキスト: {first_page.get('raw_text')}
            - AI要約: {first_page.get('ai_summary')}
            - タグ: {first_page.get('ai_tags')}
            - ユーザーが提示した修正理由: {reason or '特になし'}

            【現在の仕分けルール (rules.md)】
            {rules_str}

            この修正アクション of 事実から、今後の自動仕分けの判断基準となるガイドラインを抽出・アップデートしてください。
            既存のルールと矛盾しないように整理し、全体を綺麗に整理したMarkdownの箇条書き（および構成）として出力し直してください。
            余計な説明（「以下が更新されたルールです」など）は省き、純粋なMarkdownテキストのみを返してください。
            """
            try:
                response = ai_agent.generate_content_with_fallback(
                    contents=prompt
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
        sqlite_client.update_page_metadata(
            page_id=page_id,
            page_name=first_page["page_name"],
            raw_text=first_page["raw_text"] or "",
            reference_urls=first_page["reference_urls"] or "",
            ai_summary=first_page["ai_summary"] or "",
            ai_tags=first_page["ai_tags"] or "",
            ai_ocr_text=first_page["ai_ocr_text"] or ""
        )
        
        sqlite_client.update_note_metadata(
            note_id=note_id,
            title=note["title"] or "無題のノート",
            ai_ocr_text=first_page["ai_ocr_text"] or "",
            ai_summary=first_page["ai_summary"] or "",
            ai_tags=first_page["ai_tags"] or "",
            parent_folder_id=correct_folder_id,
            status="completed",
            reference_urls=first_page["reference_urls"] or ""
        )

        # ベクトルデータの再登録
        summary_text = f"要約: {first_page['ai_summary'] or ''}"
        tags_text = f"タグ: {first_page['ai_tags'] or ''}"
        body_text = f"本文: {first_page['raw_text'] or ''}"
        
        summary_vector, tags_vector, body_vector = await asyncio.gather(
            ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
        )
        
        fts_text = f"{first_page['raw_text'] or ''}\n\n{first_page['ai_ocr_text'] or ''}"
        
        lance_client.upsert_vector_data(
            page_id=page_id,
            note_id=note_id,
            summary_vector=summary_vector,
            tags_vector=tags_vector,
            body_vector=body_vector,
            fts_text=fts_text
        )

        # WebSocketで画面へ通知
        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "page_id": page_id,
            "title": note["title"] or "無題のノート",
            "folder_id": correct_folder_id,
            "message": "手動仕分けおよびフィードバック学習が完了しました。"
        })
        print(f"Feedback learning completed for note {note_id}. rules.md updated.")

    except Exception as e:
        print(f"Error in update_rules_with_feedback for note {note_id}: {e}")

async def process_new_image_workflow(note_id: str, page_id: str, image_id: str):
    """
    新規画像追加時の画像単体OCR ＆ ページ全体の再構造化パイプライン
    """
    # try ブロック先頭での例外でも except 内参照が UnboundLocalError にならないよう初期化
    note = None
    try:
        # 1. SQLiteからノートおよび画像情報取得
        note = sqlite_client.get_note(note_id)
        if not note:
            print(f"Error: Note {note_id} not found.")
            return

        pages = note.get("pages", [])
        target_page = next((p for p in pages if p["id"] == page_id), None)
        if not target_page:
            print(f"Error: Page {page_id} not found in note {note_id}.")
            return
            
        images = target_page.get("images", [])
        target_image = next((img for img in images if img["id"] == image_id), None)
        if not target_image:
            print(f"Error: Image {image_id} not found in page {page_id}.")
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

        # 4. ページ内の全画像のOCRテキストを結合
        updated_note = sqlite_client.get_note(note_id)
        updated_pages = updated_note.get("pages", [])
        updated_page = next((p for p in updated_pages if p["id"] == page_id), None)
        if not updated_page:
            return
            
        updated_images = updated_page.get("images", [])
        all_ocr_texts = [img["ai_ocr_text"] for img in updated_images if img["ai_ocr_text"]]
        merged_ocr_text = "\n\n".join(all_ocr_texts)

        # 5. ページ全体の再構造化 (要約・タグ・タイトル)
        if updated_note.get("parent_folder_id") == "inbox":
            # 仮置き（自動整理）フォルダ所属の場合は、画像OCR完了時点で自動仕分けパイプラインへ移譲
            await async_pipeline_workflow(note_id, page_id)
            return

        recent_titles = sqlite_client.get_recent_titles(limit=100)
        existing_titles_str = ", ".join(recent_titles) if recent_titles else "（まだ既存タイトルはありません）"

        summary_data = ai_agent.generate_summary_and_tags_with_gemini(
            raw_text=updated_page["raw_text"] or "",
            merged_ocr_text=merged_ocr_text,
            existing_titles_string=existing_titles_str
        )

        title = updated_note.get("title")
        if not title or title == "無題のノート" or title.strip() == "":
            title = summary_data.refined_title if summary_data.refined_title else "無題のノート"

        tags_str = ",".join(summary_data.tags)

        # SQLite 更新 (ページ全体)
        sqlite_client.update_page_metadata_merge(
            page_id=page_id,
            title=title,
            ai_ocr_text=merged_ocr_text,
            ai_summary=summary_data.clean_summary,
            ai_tags=tags_str,
            parent_folder_id=updated_note["parent_folder_id"],
            status="completed",
            analyzed_image_count=len(all_ocr_texts)
        )

        # 6. ベクトル登録
        summary_text = f"要約: {summary_data.clean_summary}"
        tags_text = f"タグ: {tags_str}"
        body_text = f"本文: {updated_note.get('raw_text') or ''}"
        
        summary_vector, tags_vector, body_vector = await asyncio.gather(
            ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
        )
        
        fts_text = f"{updated_note.get('raw_text') or ''}\n\n{merged_ocr_text or ''}"
        
        lance_client.upsert_vector_data(
            page_id=page_id,
            note_id=note_id,
            summary_vector=summary_vector,
            tags_vector=tags_vector,
            body_vector=body_vector,
            fts_text=fts_text
        )

        # 7. WebSocketプッシュ送信
        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "page_id": page_id,
            "title": title,
            "folder_id": updated_note["parent_folder_id"],
            "message": "画像の追加とAI解析が完了しました。"
        })
        print(f"Successfully processed new image {image_id} for note {note_id} page {page_id}")

    except Exception as e:
        print(f"Error in process_new_image_workflow for note {note_id}: {e}")
        try:
            sqlite_client.update_note_status(note_id, "completed")
            await broadcast_ws_message({
                "type": "completed",
                "note_id": note_id,
                "page_id": page_id,
                "title": "解析エラー",
                "folder_id": note["parent_folder_id"] if note else "inbox",
                "error": str(e)
            })
        except Exception as inner_e:
            print(f"Failed to handle error callback: {inner_e}")

async def recalculate_on_image_delete(note_id: str, page_id: str):
    """
    画像削除後のページ再要約・タグ再生成パイプライン
    """
    # try ブロック先頭での例外でも except 内参照が UnboundLocalError にならないよう初期化
    note = None
    try:
        note = sqlite_client.get_note(note_id)
        if not note:
            return

        pages = note.get("pages", [])
        target_page = next((p for p in pages if p["id"] == page_id), None)
        if not target_page:
            return

        images = target_page.get("images", [])
        all_ocr_texts = [img["ai_ocr_text"] for img in images if img["ai_ocr_text"]]
        merged_ocr_text = "\n\n".join(all_ocr_texts)

        recent_titles = sqlite_client.get_recent_titles(limit=100)
        existing_titles_str = ", ".join(recent_titles) if recent_titles else "（まだ既存タイトルはありません）"

        summary_data = ai_agent.generate_summary_and_tags_with_gemini(
            raw_text=target_page["raw_text"] or "",
            merged_ocr_text=merged_ocr_text,
            existing_titles_string=existing_titles_str
        )

        title = note.get("title")
        if not title or title == "無題のノート" or title.strip() == "":
            title = summary_data.refined_title if summary_data.refined_title else "無題のノート"

        tags_str = ",".join(summary_data.tags)

        sqlite_client.update_page_metadata_merge(
            page_id=page_id,
            title=title,
            ai_ocr_text=merged_ocr_text,
            ai_summary=summary_data.clean_summary,
            ai_tags=tags_str,
            parent_folder_id=note["parent_folder_id"],
            status="completed",
            analyzed_image_count=len(all_ocr_texts)
        )

        summary_text = f"要約: {summary_data.clean_summary}"
        tags_text = f"タグ: {tags_str}"
        body_text = f"本文: {note.get('raw_text') or ''}"
        
        summary_vector, tags_vector, body_vector = await asyncio.gather(
            ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
        )
        
        fts_text = f"{note.get('raw_text') or ''}\n\n{merged_ocr_text or ''}"
        
        lance_client.upsert_vector_data(
            page_id=page_id,
            note_id=note_id,
            summary_vector=summary_vector,
            tags_vector=tags_vector,
            body_vector=body_vector,
            fts_text=fts_text
        )

        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "page_id": page_id,
            "title": title,
            "folder_id": note["parent_folder_id"],
            "message": "画像削除に伴う再解析が完了しました。"
        })
        print(f"Recalculated summary for note {note_id} page {page_id} after image deletion.")

    except Exception as e:
        print(f"Error in recalculate_on_image_delete: {e}")
        try:
            sqlite_client.update_note_status(note_id, "completed")
            await broadcast_ws_message({
                "type": "completed",
                "note_id": note_id,
                "page_id": page_id,
                "title": "解析エラー",
                "folder_id": note["parent_folder_id"] if note else "inbox",
                "error": str(e)
            })
        except Exception as inner_e:
            print(f"Failed to handle error callback: {inner_e}")

async def recalculate_vector_on_edit(note_id: str, page_id: str):
    """
    ページ手動編集後の Embedding 再計算と LanceDB の更新
    """
    try:
        note = sqlite_client.get_note(note_id)
        if not note:
            return

        pages = note.get("pages", [])
        target_page = next((p for p in pages if p["id"] == page_id), None)
        if not target_page:
            return

        summary_text = f"要約: {target_page['ai_summary'] or ''}"
        tags_text = f"タグ: {target_page['ai_tags'] or ''}"
        body_text = f"本文: {target_page['raw_text'] or ''}"
        
        summary_vector, tags_vector, body_vector = await asyncio.gather(
            ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
            ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
        )
        
        # 画像のOCRテキストをマージ
        images = target_page.get("images", [])
        all_ocr_texts = [img["ai_ocr_text"] for img in images if img["ai_ocr_text"]]
        merged_ocr_text = "\n\n".join(all_ocr_texts)
        
        fts_text = f"{target_page['raw_text'] or ''}\n\n{merged_ocr_text}"
        
        lance_client.upsert_vector_data(
            page_id=page_id,
            note_id=note_id,
            summary_vector=summary_vector,
            tags_vector=tags_vector,
            body_vector=body_vector,
            fts_text=fts_text
        )
        print(f"Recalculated embedding for note {note_id} page {page_id} after edit.")
    except Exception as e:
        print(f"Error in recalculate_vector_on_edit for note {note_id} page {page_id}: {e}")

async def process_document_import_workflow(note_id: str, file_path: str, filename: str):
    """
    ドキュメント（PDF, PPTX, DOCX, XLSX, TXT等）をインポートし、
    各ページ/スライドを個別のページタブとしてSQLite/LanceDBに展開する非同期バックグラウンドタスク
    """
    try:
        from services.document_parser import DocumentParser
        import asyncio

        # 1. ノートの存在確認
        note = sqlite_client.get_note(note_id)
        if not note:
            print(f"Error: Note {note_id} not found.")
            return

        # 2. ファイルを読み込み、パースを実行
        async with aiofiles.open(file_path, "rb") as f:
            file_bytes = await f.read()

        # パースはCPUバウンドなので to_thread で実行
        parsed_pages = await asyncio.to_thread(DocumentParser.parse, file_bytes, filename)

        if not parsed_pages:
            raise ValueError("ファイルからコンテンツを抽出できませんでした。")

        # 3. 既存のデータをクリア (初期の空ページ等を削除)
        with sqlite_client.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM note_images WHERE note_id = ?", (note_id,))
            cursor.execute("DELETE FROM note_pages WHERE note_id = ?", (note_id,))
            conn.commit()

        # LanceDBの既存データも削除
        lance_client.delete_all_vector_data_for_note(note_id)
        
        # 4. 各ページをループ処理
        import uuid
        import config
        from services import ai_agent

        recent_titles = sqlite_client.get_recent_titles(limit=100)
        existing_titles_str = ", ".join(recent_titles) if recent_titles else "（まだ既存タイトルはありません）"

        page_summaries = []
        page_tags_all = set()
        merged_ocr_all = []

        # 進捗ブロードキャスト
        total_pages = len(parsed_pages)
        await broadcast_ws_message({
            "type": "import_progress",
            "note_id": note_id,
            "current": 0,
            "total": total_pages,
            "message": "ドキュメントの解析を開始しました..."
        })

        for idx, page_data in enumerate(parsed_pages):
            page_name = page_data["page_name"]
            raw_text = page_data["text"] or ""
            image_bytes = page_data["image_bytes"]
            
            # ページ作成
            page_id = sqlite_client.create_page(note_id, page_name)
            
            # 画像の保存と紐付け
            image_path = None
            ocr_text = ""
            
            if image_bytes:
                # local_images/ フォルダに保存
                img_filename = f"{uuid.uuid4()}{page_data['image_ext'] or '.png'}"
                full_img_path = os.path.join(config.IMAGE_DIR, img_filename)
                
                # 画像の物理保存
                async with aiofiles.open(full_img_path, "wb") as img_f:
                    await img_f.write(image_bytes)
                
                # DBに画像レコードを追加
                img_path_web = f"/local_images/{img_filename}"
                img_id = sqlite_client.add_page_image(note_id, page_id, img_path_web)
                
                # スキャンPDFや画像スライドなどの判定（抽出テキストが非常に少ない場合）
                if len(raw_text.strip()) <= 50:
                    # Gemini APIでの画像OCR
                    ocr_text = await asyncio.to_thread(ai_agent.ocr_image_with_gemini, image_bytes)
                    if ocr_text:
                        sqlite_client.update_note_image_ocr(img_id, ocr_text)
                        merged_ocr_all.append(ocr_text)

            # テキストまたはOCRテキストがある場合にAI解析
            ai_summary = ""
            ai_tags = ""
            
            combined_text = raw_text.strip()
            if ocr_text:
                combined_text += f"\n[OCR Text]\n{ocr_text}"

            if combined_text:
                # ページごとの要約とタグ生成
                summary_data = await asyncio.to_thread(
                    ai_agent.generate_summary_and_tags_with_gemini,
                    raw_text=raw_text,
                    merged_ocr_text=ocr_text,
                    existing_titles_string=existing_titles_str
                )
                ai_summary = summary_data.clean_summary
                ai_tags = ",".join(summary_data.tags)
                page_summaries.append(ai_summary)
                for t in summary_data.tags:
                    page_tags_all.add(t)

                # ページメタデータを更新 (タイトルはそのまま)
                sqlite_client.update_page_metadata(
                    page_id=page_id,
                    page_name=page_name,
                    raw_text=raw_text,
                    reference_urls="",
                    ai_summary=ai_summary,
                    ai_tags=ai_tags,
                    ai_ocr_text=ocr_text if ocr_text else None
                )

                # LanceDBへの登録
                summary_text = f"要約: {ai_summary}"
                tags_text = f"タグ: {ai_tags}"
                body_text = f"本文: {raw_text or ''}"
                
                summary_vector, tags_vector, body_vector = await asyncio.gather(
                    ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
                    ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
                    ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
                )
                
                fts_text = f"{raw_text or ''}\n\n{ocr_text or ''}"
                
                lance_client.upsert_vector_data(
                    page_id=page_id,
                    note_id=note_id,
                    summary_vector=summary_vector,
                    tags_vector=tags_vector,
                    body_vector=body_vector,
                    fts_text=fts_text
                )

            # 進捗の通知
            await broadcast_ws_message({
                "type": "import_progress",
                "note_id": note_id,
                "current": idx + 1,
                "total": total_pages,
                "message": f"ページ {idx + 1}/{total_pages} を処理中..."
            })

        # 5. ノート全体のサマリーとタイトルの作成
        base_title = os.path.splitext(filename)[0]
        
        all_summaries_text = "\n".join(page_summaries)
        all_ocr_text_merged = "\n\n".join(merged_ocr_all)
        
        if all_summaries_text:
            overall_summary_prompt = f"""
            以下はドキュメント「{filename}」の各ページから抽出された要約のリストです。
            これらを簡潔にまとめ、ドキュメント全体の内容が把握できるような3行の代表要約を日本語で作成してください。
            
            【各ページの要約】
            {all_summaries_text}
            """
            try:
                overall_response = await asyncio.to_thread(
                    ai_agent.generate_content_with_fallback,
                    contents=overall_summary_prompt
                )
                overall_summary = overall_response.text
            except Exception as e:
                print(f"[Warning] 全体要約の生成に失敗しました: {e}")
                overall_summary = "\n".join(page_summaries[:3])
        else:
            overall_summary = "テキストコンテンツがないため要約を生成できませんでした。"

        # 全体テキストを notes.raw_text に集約
        full_doc_raw_text = "\n\n".join([f"--- {p['page_name']} ---\n{p['text']}" for p in parsed_pages if p['text']])

        # ノート全体のメタデータを更新
        sqlite_client.update_note_metadata(
            note_id=note_id,
            title=base_title,
            ai_ocr_text=all_ocr_text_merged,
            ai_summary=overall_summary,
            ai_tags=",".join(list(page_tags_all)[:10]),
            parent_folder_id=note["parent_folder_id"],
            status="completed",
            reference_urls=""
        )

        # 全体のraw_textも上書き更新（下位互換性のため）
        sqlite_client.update_note_content(note_id, full_doc_raw_text)

        # 6. 完了のWebSocket通知
        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "page_id": None,
            "title": base_title,
            "folder_id": note["parent_folder_id"],
            "message": f"「{filename}」のインポートが完了しました！"
        })

    except Exception as e:
        print(f"Error in process_document_import_workflow: {e}")
        try:
            sqlite_client.update_note_status(note_id, "failed")
            await broadcast_ws_message({
                "type": "failed",
                "note_id": note_id,
                "title": filename,
                "error": str(e)
            })
        except Exception as inner_e:
            print(f"Failed to handle error callback: {inner_e}")
    finally:
        # 一時ファイルの削除
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Failed to remove temp file {file_path}: {e}")

async def process_attachment_ai_workflow(attachment_id: str):
    """
    添付ファイル（PDF, Word, Excel, TXT等）の中身を解析し、
    AIによる要約とRAG用ベクトルの作成・登録を行う非同期バックグラウンドタスク
    """
    # try ブロック先頭での例外でも except 内参照が UnboundLocalError にならないよう初期化
    note = None
    note_id = None
    page_id = None
    try:
        from services.document_parser import DocumentParser
        import asyncio
        from services import ai_agent

        # 1. 添付ファイル情報の取得
        attachment = sqlite_client.get_attachment(attachment_id)
        if not attachment:
            print(f"Error: Attachment {attachment_id} not found.")
            return

        note_id = attachment["note_id"]
        page_id = attachment["page_id"]
        
        note = sqlite_client.get_note(note_id)
        if not note:
            print(f"Error: Parent note {note_id} not found.")
            return

        physical_path = os.path.abspath(os.path.join(config.BASE_DIR, attachment["file_path"]))
        if not os.path.exists(physical_path):
            raise FileNotFoundError(f"添付ファイルの物理ファイルが見つかりません: {physical_path}")

        # 2. ファイルをバイナリとして読み込む
        async with aiofiles.open(physical_path, "rb") as f:
            file_bytes = await f.read()

        # 3. パーサで解析を実行
        try:
            parsed_pages = await asyncio.to_thread(DocumentParser.parse, file_bytes, attachment["file_name"])
        except Exception as pe:
            print(f"[Warning] 添付ファイルのパースに失敗しました: {pe}. テキスト抽出をスキップします。")
            parsed_pages = []

        # 4. 解析テキストと画像の整理
        extracted_texts = []
        for p in parsed_pages:
            if p.get("text"):
                extracted_texts.append(p["text"])
        merged_text = "\n\n".join(extracted_texts).strip()

        # スキャンPDFや画像のみのファイルの場合、OCRを実行
        if len(merged_text) <= 50 and parsed_pages:
            ocr_texts = []
            for p in parsed_pages[:3]: # 最初の3ページを上限とする
                if p.get("image_bytes"):
                    ocr_txt = await asyncio.to_thread(ai_agent.ocr_image_with_gemini, p["image_bytes"])
                    if ocr_txt:
                        ocr_texts.append(ocr_txt)
            if ocr_texts:
                merged_text = "\n\n".join(ocr_texts).strip()

        # 5. Geminiによる要約生成とDB更新
        ai_summary = ""
        if merged_text:
            summary_prompt = f"""
            以下は添付ファイル「{attachment['file_name']}」から抽出されたテキスト情報です。
            このファイルの内容が把握できるように、3行以内の簡潔な要約文を日本語で作成してください。
            余計な前置きや「以下が要約です」などの説明は省いてください。

            【抽出テキスト】
            {merged_text[:10000]}
            """
            try:
                summary_response = await asyncio.to_thread(
                    ai_agent.generate_content_with_fallback,
                    contents=summary_prompt
                )
                ai_summary = summary_response.text.strip()
            except Exception as se:
                print(f"[Warning] 添付ファイルのAI要約生成に失敗しました: {se}")
                ai_summary = "ファイルのテキスト抽出は完了しましたが、AI要約の生成に失敗しました。"
        else:
            ai_summary = "ファイルから解析可能なテキスト情報が見つかりませんでした。"

        # SQLite 添付ファイルレコードの更新
        sqlite_client.update_attachment_ai_metadata(attachment_id, ai_summary, merged_text)

        # 6. LanceDB へのベクトル登録（検索対応）
        if merged_text or ai_summary:
            summary_text = f"要約: {ai_summary}"
            tags_text = f"タグ: 添付ファイル, {attachment['file_name']}"
            body_text = f"本文: {merged_text or ''}"
            
            try:
                summary_vector, tags_vector, body_vector = await asyncio.gather(
                    ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
                    ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
                    ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
                )
                
                fts_text = f"{attachment['file_name']}\n\n{merged_text or ''}"
                
                lance_client.upsert_vector_data(
                    page_id=attachment_id, # LanceDBには id = attachment_id で登録
                    note_id=note_id,
                    summary_vector=summary_vector,
                    tags_vector=tags_vector,
                    body_vector=body_vector,
                    fts_text=fts_text
                )
            except Exception as ve:
                print(f"[Warning] 添付ファイルのベクトル登録に失敗しました: {ve}")

        # 7. ノートステータスを完了に戻し、WebSocketで通知
        sqlite_client.update_note_status(note_id, "completed")

        await broadcast_ws_message({
            "type": "completed",
            "note_id": note_id,
            "page_id": page_id,
            "title": note["title"] or "無題のノート",
            "folder_id": note["parent_folder_id"],
            "message": f"添付ファイル「{attachment['file_name']}」の解析が完了しました！"
        })

    except Exception as e:
        print(f"Error in process_attachment_ai_workflow for attachment {attachment_id}: {e}")
        try:
            sqlite_client.update_note_status(note_id, "completed")
            await broadcast_ws_message({
                "type": "completed",
                "note_id": note_id,
                "page_id": page_id,
                "title": "解析失敗",
                "folder_id": note["parent_folder_id"] if note else "inbox",
                "error": f"添付ファイルの解析中にエラーが発生しました: {e}"
            })
        except Exception as inner_e:
            print(f"Failed to handle error callback: {inner_e}")

async def migrate_existing_data_to_v2():
    """
    スキーマ v2 (512次元マルチベクトル) へのデータ移行処理。
    すでに v2 テーブルが存在し、データが入っている場合は何もしない。
    テーブルが空の場合、SQLite から全データを取得して並行で再インデックス化する。
    """
    try:
        table = lance_client.get_table()
        # レコード件数を確認
        count = len(table.to_arrow())
        if count > 0:
            print(f"[Migration] New table {lance_client.TABLE_NAME} already has {count} records. Skipping migration.")
            return
            
        print("[Migration] Starting migration to 512-dimension multi-vector schema v2...")
        
        pages, attachments = sqlite_client.get_all_pages_for_migration()
        print(f"[Migration] Found {len(pages)} pages and {len(attachments)} attachments to migrate.")
        
        # 1. ページの移行
        for idx, page in enumerate(pages):
            page_id = page["page_id"]
            note_id = page["note_id"]
            
            summary_text = f"要約: {page['ai_summary'] or ''}"
            tags_text = f"タグ: {page['ai_tags'] or ''}"
            body_text = f"本文: {page['raw_text'] or ''}"
            
            try:
                # 3つのベクトルを並行で取得
                summary_vector, tags_vector, body_vector = await asyncio.gather(
                    ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
                    ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
                    ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
                )
                
                fts_text = f"{page['raw_text'] or ''}\n\n{page['merged_ocr_text'] or ''}"
                
                lance_client.upsert_vector_data(
                    page_id=page_id,
                    note_id=note_id,
                    summary_vector=summary_vector,
                    tags_vector=tags_vector,
                    body_vector=body_vector,
                    fts_text=fts_text
                )
                print(f"[Migration] Page {idx+1}/{len(pages)} migrated.")
            except Exception as e:
                print(f"[Migration Warning] Failed to migrate page {page_id}: {e}")
                
        # 2. 添付ファイルの移行
        for idx, att in enumerate(attachments):
            att_id = att["attachment_id"]
            note_id = att["note_id"]
            
            summary_text = f"要約: {att['ai_summary'] or ''}"
            tags_text = f"タグ: 添付ファイル, {att['file_name'] or ''}"
            body_text = f"本文: {att['extracted_text'] or ''}"
            
            try:
                summary_vector, tags_vector, body_vector = await asyncio.gather(
                    ai_agent.generate_embedding_via_azure(summary_text, dimensions=512),
                    ai_agent.generate_embedding_via_azure(tags_text, dimensions=512),
                    ai_agent.generate_embedding_via_azure(body_text, dimensions=512)
                )
                
                fts_text = f"{att['file_name'] or ''}\n\n{att['extracted_text'] or ''}"
                
                lance_client.upsert_vector_data(
                    page_id=att_id,
                    note_id=note_id,
                    summary_vector=summary_vector,
                    tags_vector=tags_vector,
                    body_vector=body_vector,
                    fts_text=fts_text
                )
                print(f"[Migration] Attachment {idx+1}/{len(attachments)} migrated.")
            except Exception as e:
                print(f"[Migration Warning] Failed to migrate attachment {att_id}: {e}")
                
        print("[Migration] Migration completed successfully.")
    except Exception as e:
        print(f"[Migration Error] Migration failed: {e}")


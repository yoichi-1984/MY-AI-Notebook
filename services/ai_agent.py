import os
import random
import datetime
import numpy as np
import httpx
import requests
import base64
from pydantic import BaseModel, Field
from config import AZURE_OPENAI_API_VERSION, VERTEX_PROJECT_ID, VERTEX_LOCATION, GEMINI_JSON_PATH
from database import sqlite_client

# プロキシの環境変数がある場合に openai (内部で利用される httpx) が proxies 引数エラーを起こすバグを回避
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

# AIから100%この形式のJSONで返却させる構造化スキーマ
class NoteStructuringSchema(BaseModel):
    suggested_folder_id: str = Field(description="既存フォルダ一覧の中から、内容に最も合致するフォルダID。どれにも合致しない場合は 'unclassified' とする。")
    confidence_score: float = Field(description="仕分けの自信度。0.0から1.0の間。")
    refined_title: str = Field(description="画像やテキストから自動生成した10〜20文字の洗練されたタイトル。既存のタイトル群のトーンに合わせること。")
    ocr_raw_text: str = Field(description="画像内に含まれる全ての文字を泥臭く正確に書き起こしたOCRテキスト。画像がない場合は空文字にすること。")
    clean_summary: str = Field(description="ナレッジDBとして後から調査しやすいように、内容を綺麗に整理した3行の要約文。")
    tags: list[str] = Field(description="意味検索を補強するための、関連する技術キーワードや文脈を表すタグの配列。")

def write_debug_log(message: str):
    try:
        from config import BASE_DIR
        log_path = os.path.join(BASE_DIR, "debug_ai.log")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception as log_err:
        print(f"Failed to write debug log: {log_err}")


def has_any_credentials():
    """
    なんらかの認証情報が設定されているか確認
    """
    json_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or GEMINI_JSON_PATH
    has_json = json_path and os.path.exists(json_path)
    return (
        has_json or
        os.getenv("GEMINI_API_KEY") is not None
    )

def convert_pydantic_to_gemini_schema(pydantic_model):
    """
    Pydantic v2 のスキーマを Gemini REST API 用の簡素なスキーマ形式に変換
    """
    schema = pydantic_model.model_json_schema()
    
    def clean_schema(s):
        result = {}
        t = s.get("type")
        if t:
            result["type"] = t.upper() # Gemini API は大文字の型定義 (STRING, NUMBER, OBJECT, ARRAY, BOOLEAN) を使用
        if "description" in s:
            result["description"] = s["description"]
            
        if t == "object":
            properties = {}
            for k, v in s.get("properties", {}).items():
                properties[k] = clean_schema(v)
            result["properties"] = properties
            if "required" in s:
                result["required"] = s["required"]
        elif t == "array":
            if "items" in s:
                result["items"] = clean_schema(s["items"])
        return result
        
    return clean_schema(schema)

def generate_content_via_rest(contents_list, model_name="gemini-3.5-flash", response_schema=None, system_instruction=None, thinking_level=None) -> str:
    """
    GOOGLE_APPLICATION_CREDENTIALS (env/gemini.json) を使用して、
    Google AI Studio REST API を OAuth2 認証で直接呼び出します。
    """
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as AuthRequest

    json_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or GEMINI_JSON_PATH
    if not json_path or not os.path.exists(json_path):
        raise ValueError("Credentials file not found.")

    creds = service_account.Credentials.from_service_account_file(
        json_path,
        scopes=["https://www.googleapis.com/auth/generative-language"]
    )
    creds.refresh(AuthRequest())
    token = creds.token

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    parts = []
    for content in contents_list:
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, dict) and "inlineData" in content:
            parts.append(content)

    body = {
        "contents": [{"parts": parts}]
    }

    generation_config = {}
    if response_schema:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = convert_pydantic_to_gemini_schema(response_schema)
        
    if thinking_level:
        generation_config["thinkingConfig"] = {
            "thinkingLevel": thinking_level.upper()
        }

    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    body["generationConfig"] = generation_config

    try:
        res = requests.post(url, headers=headers, json=body, timeout=30)
        res.raise_for_status()
        res_json = res.json()
        return res_json["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"REST API call failed: {e}")
        raise e

def generate_content_with_fallback(contents, response_schema=None, system_instruction=None):
    """
    認証情報の種類に応じて、Vertex AI, OAuth2 REST API (Gemini API), または SDK APIキー の順で呼び出しを行います。
    """
    from google import genai
    from google.genai import types
    from google.oauth2 import service_account
    from database import sqlite_client

    db_model_name = sqlite_client.get_setting("model_name", "gemini-3.5-flash")
    thinking_level = sqlite_client.get_setting("thinking_level", "medium")
    if thinking_level not in ["minimal", "low", "medium", "high"]:
        thinking_level = "medium"

    print(f"[AI Agent] Generating content using model {db_model_name} with thinking_level={thinking_level}")

    thinking_config = types.ThinkingConfig(thinking_level=thinking_level)

    gemini_api_key = os.getenv("GEMINI_API_KEY")
    json_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or GEMINI_JSON_PATH
    errors = []

    config_kwargs = {
        "thinking_config": thinking_config
    }
    if response_schema:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = response_schema
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    config_obj = types.GenerateContentConfig(**config_kwargs)

    # 1. APIキーが明示的に設定されている場合は、まず標準 API キーによる呼び出しを最優先する
    if gemini_api_key:
        try:
            print("Attempting generation using standard Gemini API with API key (Prioritized)...")
            client = genai.Client(api_key=gemini_api_key)
            response = client.models.generate_content(
                model=db_model_name,
                contents=contents,
                config=config_obj,
            )
            return response
        except Exception as e:
            err_msg = f"Standard API Key優先モード失敗: {e}"
            print(err_msg)
            write_debug_log(err_msg)
            errors.append(err_msg)

    # 2. Google AI Studio REST API 直接呼び出し (サービスアカウントキーが存在する場合の最安定ルート)
    if json_path and os.path.exists(json_path):
        try:
            rest_contents = []
            for item in contents if isinstance(contents, list) else [contents]:
                if isinstance(item, str):
                    rest_contents.append(item)
                elif hasattr(item, "inline_data") or (isinstance(item, types.Part) and item.inline_data):
                    part_data = item.inline_data
                    rest_contents.append({
                        "inlineData": {
                            "mimeType": part_data.mime_type,
                            "data": base64.b64encode(part_data.data).decode('utf-8')
                        }
                    })
                elif isinstance(item, dict) and "inlineData" in item:
                    rest_contents.append(item)
            text_response = generate_content_via_rest(
                contents_list=rest_contents,
                model_name=db_model_name,
                response_schema=response_schema,
                system_instruction=system_instruction,
                thinking_level=thinking_level
            )
            class MockResponse:
                def __init__(self, text, schema=None):
                    self.text = text
                    if schema:
                        import json
                        try:
                            cleaned_text = text.strip()
                            if cleaned_text.startswith("```"):
                                lines = cleaned_text.split("\n")
                                if lines[0].startswith("```json"):
                                    cleaned_text = "\n".join(lines[1:-1])
                                else:
                                    cleaned_text = "\n".join(lines[1:-1])
                            self.parsed = schema.model_validate_json(cleaned_text)
                        except Exception as parse_err:
                            print(f"Failed to parse JSON response to schema: {parse_err}")
                            self.parsed = None
                    else:
                        self.parsed = None
            return MockResponse(text_response, response_schema)
        except Exception as e:
            err_msg = f"REST API(サービスアカウント)失敗: {e}"
            print(err_msg)
            write_debug_log(err_msg)
            errors.append(err_msg)

    # 3. Vertex AI (GCP) モード試行
    if json_path and VERTEX_PROJECT_ID:
        try:
            print("Attempting generation using Vertex AI with service account...")
            creds = service_account.Credentials.from_service_account_file(
                json_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            client = genai.Client(
                vertexai=True,
                project=VERTEX_PROJECT_ID,
                location=VERTEX_LOCATION,
                credentials=creds
            )
            response = client.models.generate_content(
                model=db_model_name,
                contents=contents,
                config=config_obj,
            )
            return response
        except Exception as e:
            err_msg = f"Vertex AI(SDK)失敗: {e}"
            print(err_msg)
            write_debug_log(err_msg)
            errors.append(err_msg)

    # 4. 通常の Gemini API モード試行 (APIキーによる標準呼び出し)
    try:
        print("Attempting generation using standard Gemini API with API key...")
        client = genai.Client()
        response = client.models.generate_content(
            model=db_model_name,
            contents=contents,
            config=config_obj,
        )
        return response
    except Exception as e:
        err_msg = f"Standard API Keyフォールバック失敗: {e}"
        print(err_msg)
        write_debug_log(err_msg)
        errors.append(err_msg)
        
    error_summary = " / ".join(errors)
    write_debug_log(f"All fallbacks failed. Model: {db_model_name}, Errors: {error_summary}")
    raise RuntimeError(f"すべてのGemini接続フォールバックが失敗しました。詳細 -> " + error_summary)


class ImageOcrSchema(BaseModel):
    ocr_raw_text: str = Field(description="画像内に含まれる全ての文字を泥臭く正確に書き起こしたOCRテキスト。")

class NoteSummarySchema(BaseModel):
    refined_title: str = Field(description="画像やテキストから自動生成した10〜20文字の洗練されたタイトル。既存のタイトル群のトーンに合わせること。")
    clean_summary: str = Field(description="ナレッジDBとして後から調査しやすいように、内容を綺麗に整理した3行の要約文。")
    tags: list[str] = Field(description="意味検索を補強するための、関連する技術キーワードや文脈を表すタグ of 配列。")

def ocr_image_with_gemini(image_bytes: bytes) -> str:
    if not has_any_credentials():
        print("Warning: No Gemini/Vertex credentials set. Running in MOCK OCR mode.")
        return "Mock OCR Text: [OCR extraction simulated from image_bytes]."
        
    from google.genai import types
    
    prompt = """
    提供された画像の内容を解析し、画像内の文字を一言一句漏らさずOCR（書き起こし）してください。
    余計な推測や要約、説明は省き、純粋に画像内に写っているテキストのみを出力してください。
    """
    
    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
        prompt
    ]
    
    try:
        response = generate_content_with_fallback(
            contents=contents,
            response_schema=ImageOcrSchema
        )
        if response and response.parsed:
            return response.parsed.ocr_raw_text
        raise ValueError("AI response parsing failed")
    except Exception as e:
        error_msg = f"[GCP/Gemini OCRエラー] {e}"
        print(error_msg)
        return f"Mock OCR Text: [GCP/Gemini OCRエラーのためモック抽出] (エラー詳細: {e})"

def generate_summary_and_tags_with_gemini(raw_text: str, merged_ocr_text: str, existing_titles_string: str) -> NoteSummarySchema:
    if not has_any_credentials():
        print("Warning: No Gemini/Vertex credentials set. Running in MOCK Summary mode.")
        mock_summary = f"1. これはデモ用のモック要約です。\n2. 本文: {raw_text[:50]}\n3. OCRテキスト: {merged_ocr_text[:50]}"
        return NoteSummarySchema(
            refined_title="モック生成タイトル " + datetime.datetime.now().strftime("%H:%M:%S"),
            clean_summary=mock_summary,
            tags=["demo", "mock"]
        )

    prompt = f"""
    あなたはパーソナルナレッジDBの管理AIエージェントです。
    提供されたノート本文および、ノートに添付された全ての画像から抽出されたOCRテキストを元に、要約、メタデータタグ、タイトルを生成してください。

    【提供データ】
    - ノート本文: {raw_text}
    - 添付画像のOCRテキスト: {merged_ocr_text}

    【制約・判断材料】
    1. 既存のノートタイトル一覧（命名トーンを同調させてください）: {existing_titles_string}

    もしユーザーがタイトルを打ち込んでおらず、空白だった場合は、既存タイトルのトーンを厳格に参考にして、新タイトルを同調させて生成してください。
    """
    
    try:
        response = generate_content_with_fallback(
            contents=prompt,
            response_schema=NoteSummarySchema
        )
        if response and response.parsed:
            return response.parsed
        raise ValueError("AI response parsing failed")
    except Exception as e:
        error_msg = f"[GCP/Gemini 要約エラー] {e}"
        print(error_msg)
        write_debug_log(error_msg)
        mock_summary = f"1. [GCP/Gemini 要約エラーのためモック要約を表示]\n2. エラー詳細: {e}\n3. ノート本文: {raw_text[:50]}"
        return NoteSummarySchema(
            refined_title="[エラー]モック生成タイトル " + datetime.datetime.now().strftime("%H:%M:%S"),
            clean_summary=mock_summary,
            tags=["error-mock", "gemini-error"]
        )

def analyze_and_structure_with_gemini(image_bytes: bytes, raw_text: str, folder_list_string: str, existing_titles_string: str, rules_string: str) -> NoteStructuringSchema:
    if not has_any_credentials():
        print("Warning: No Gemini/Vertex credentials set. Running in MOCK mode.")
        import re
        folder_ids = re.findall(r"'id': '([^']+)'", folder_list_string)
        if not folder_ids:
            folder_ids = ["inbox"]
            
        confidence = round(random.uniform(0.5, 0.95), 2)
        suggested_folder = "unclassified" if confidence < 0.7 or random.random() < 0.2 else random.choice(folder_ids)
        
        mock_ocr = "Mock OCR Text: [OCR extraction simulated]. Text sample: " + raw_text[:30] if image_bytes else ""
        mock_summary = f"1. これはデモ用のモック要約です。\n2. 生入力: {raw_text[:50]}\n3. 画像データを受信しました（画像サイズ: {len(image_bytes)} bytes）" if image_bytes else f"1. テキスト入力のモック要約。\n2. 内容: {raw_text[:80]}"
        mock_tags = ["demo", "mock", "auto-tag"]
        mock_title = "モック生成タイトル " + datetime.datetime.now().strftime("%H:%M:%S")
        
        return NoteStructuringSchema(
            suggested_folder_id=suggested_folder,
            confidence_score=confidence,
            refined_title=mock_title,
            ocr_raw_text=mock_ocr,
            clean_summary=mock_summary,
            tags=mock_tags
        )

    from google.genai import types
    
    prompt = f"""
    あなたはパーソナルナレッジDBの超優秀な管理AIエージェントです。
    提供された画像およびテキストの内容を解析し、構造化データを生成してください。

    【制約・判断材料】
    1. 現在のフォルダ階層一覧: {folder_list_string}
    2. 過去の仕分けルール（優先指示）: {rules_string}
    3. 既存のノートタイトル一覧（命名トーンを同調させてください）: {existing_titles_string}

    画像内の文字は一言句漏らさずOCRしてください。
    もし画像がなくテキストのみの場合は、ocr_raw_textは空文字にしてください。
    もしユーザーがタイトルを打ち込んでおらず、空白だった場合は、既存タイトルのトーンを厳格に参考にして、新タイトルを制止・同調させて生成してください。
    自信度が 0.7 未満と判断される場合、あるいは既存フォルダのどれにも適さない場合は、suggested_folder_id を 'unclassified' にしてください。
    """
    
    contents = []
    if image_bytes:
        contents.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/png")
        )
    if raw_text:
        contents.append(raw_text)
    contents.append(prompt)
    
    try:
        response = generate_content_with_fallback(
            contents=contents,
            response_schema=NoteStructuringSchema
        )
        return response.parsed
    except Exception as e:
        error_msg = f"[GCP/Gemini 構造化仕分けエラー] {e}"
        print(error_msg)
        write_debug_log(error_msg)
        raise e

def generate_embedding_via_azure(text: str) -> list[float]:
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    
    if not api_key or not endpoint:
        print("Warning: AZURE_OPENAI_API_KEY or AZURE_OPENAI_ENDPOINT is not set (MOCK mode).")
        vec = np.random.randn(1536)
        vec /= np.linalg.norm(vec)
        return vec.tolist()
        
    try:
        from openai import AzureOpenAI
        
        # プロキシによる TypeError: Client.__init__() got an unexpected keyword argument 'proxies' バグを回避
        http_client = httpx.Client(proxy=None)
        
        client = AzureOpenAI(
            api_key=api_key,
            api_version=AZURE_OPENAI_API_VERSION,
            azure_endpoint=endpoint,
            http_client=http_client
        )
        
        deployment_name = os.getenv("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-small")
        response = client.embeddings.create(
            input=[text],
            model=deployment_name
        )
        return response.data[0].embedding
    except Exception as e:
        error_msg = f"[Azure Embeddingエラー] {e}"
        print(error_msg)
        raise RuntimeError(error_msg) from e

def generate_rag_response(query: str, search_results: list[dict]) -> str:
    if not has_any_credentials():
        print("Warning: No Gemini/Vertex credentials set (MOCK RAG mode).")
        notes_summary = "\n".join([f"- ID: {r['id']}, 要約: {r['search_text'][:60]}..." for r in search_results])
        return (
            f"【モックRAG回答】\nユーザーの質問: 「{query}」に対する回答シミュレーションです。\n\n"
            f"検索にヒットしたコンテキスト:\n{notes_summary}\n\n"
            f"※認証情報が設定されると、実際のローカル知識に基づくGeminiの生成回答が表示されます。"
        )
        
    context_parts = []
    for r in search_results:
        context_parts.append(
            f"【ノートID: {r['id']}】\n"
            f"【ナレッジ要約・メタデータ】\n{r['search_text']}\n"
            f"【生OCRテキスト】\n{r['ocr_text']}"
        )
    context = "\n\n---\n\n".join(context_parts)
    
    system_instruction = f"""
    あなたはユーザーが持つローカルナレッジベースに基づいて回答する超優秀なAIアシスタントです。
    提供される【コンテキスト】の内容のみを「唯一の絶対的なソース（事実）」として、ユーザーの質問に日本語で回答してください。
    提供された情報から直接的・間接的に判断できない場合は、絶対に知ったかぶりや推測をせず、「提供されたナレッジからは分かりませんでした」と明確に回答してください。
    
    【コンテキスト】
    {context}
    """
    
    try:
        response = generate_content_with_fallback(
            contents=query,
            system_instruction=system_instruction
        )
        return response.text
    except Exception as e:
        print(f"Failed to generate RAG response: {e}. Running RAG in MOCK mode.")
        notes_summary = "\n".join([f"- ID: {r['id']}, 要約: {r['search_text'][:60]}..." for r in search_results])
        return (
            f"【RAGエラーフォールバック回答】\n"
            f"APIエラーにより実際の回答生成に失敗しました。\n"
            f"検索にヒットしたコンテキスト概要:\n{notes_summary}"
        )
stream = False

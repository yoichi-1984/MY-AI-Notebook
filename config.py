import os
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. env/azure.env の手動パースと環境変数適用
AZURE_ENV_PATH = os.path.join(BASE_DIR, "env", "azure.env")
if os.path.exists(AZURE_ENV_PATH):
    try:
        with open(AZURE_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    os.environ[key] = val
        print("Successfully loaded env/azure.env variables.")
    except Exception as e:
        print(f"Error loading env/azure.env: {e}")

# 2. env/gemini.json のパースと Google Cloud 認証用の環境変数適用
GEMINI_JSON_PATH = os.path.join(BASE_DIR, "env", "gemini.json")
VERTEX_PROJECT = None
if os.path.exists(GEMINI_JSON_PATH):
    # Vertex AI SDK がサービスアカウントキーを自動的に読み込めるよう絶対パスを指定
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath(GEMINI_JSON_PATH)
    try:
        with open(GEMINI_JSON_PATH, "r", encoding="utf-8") as f:
            gemini_data = json.load(f)
            VERTEX_PROJECT = gemini_data.get("project_id")
            # Vertex AI SDK 用に GCP プロジェクト環境変数もセット
            os.environ["GCP_PROJECT"] = VERTEX_PROJECT
        print(f"Successfully configured Vertex AI (Project: {VERTEX_PROJECT}) from env/gemini.json.")
    except Exception as e:
        print(f"Error loading env/gemini.json: {e}")

# 各種設定のエクスポート
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") # Vertex AIではなくAPIキーを使う場合のフォールバック
VERTEX_PROJECT_ID = VERTEX_PROJECT or os.getenv("VERTEX_PROJECT")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "asia-northeast1") # デフォルトリージョン (東京)

AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2023-05-15")

# 物理パス設定
IMAGE_DIR = os.path.join(BASE_DIR, "local_images")
SQLITE_DB_PATH = os.path.join(BASE_DIR, "local_knowledge.db")
LANCEDB_DIR = os.path.join(BASE_DIR, "lancedb_data")
RULES_PATH = os.path.join(BASE_DIR, "rules.md")

# ディレクトリ作成
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(LANCEDB_DIR, exist_ok=True)

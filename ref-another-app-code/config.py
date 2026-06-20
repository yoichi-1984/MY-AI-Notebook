# --- Constants ---
MAX_CANVASES = 40

# --- Environment Variable Keys ---
GCP_PROJECT_ID_NAME = "GCP_PROJECT_ID"
GCP_LOCATION_NAME = "GCP_LOCATION"
GEMINI_MODEL_ID_NAME = "GEMINI_MODEL_ID"

# --- Code Execution Settings (New) ---
# コード実行エンジンの設定
EXECUTION_TIMEOUT = 30 # 秒
TEMP_WORKSPACE_DIR = "temp_workspace"

# --- LLM Routing Settings ---
LLM_ROUTE_STANDARD = "standard"
LLM_ROUTE_PRIORITY = "priority"

PRIORITY_HEADER_REQUEST_TYPE = "X-Vertex-AI-LLM-Request-Type"
PRIORITY_HEADER_SHARED_REQUEST_TYPE = "X-Vertex-AI-LLM-Shared-Request-Type"
PRIORITY_HEADER_REQUEST_TYPE_VALUE = "shared"
PRIORITY_HEADER_SHARED_REQUEST_TYPE_VALUE = "priority"

LLM_RETRYABLE_STATUS_CODES = (408, 429, 500, 502, 503, 504)
PRIORITY_APP_RETRY_COUNT = 3
PRIORITY_APP_RETRY_WAIT_SECONDS = (2.0, 4.0, 8.0)

# --- Editor Settings ---
ACE_EDITOR_SETTINGS = {
    "language": "python",
    "theme": "monokai",
    "font_size": 14,
    "show_gutter": True,
    "wrap": False,
}
ACE_EDITOR_DEFAULT_CODE = "# コードはここに \n"

# --- System Prompts ---
# コーディング特化ではなく、汎用的な役割定義に変更
DEFAULT_SYSTEM_ROLE = """You are Gemini, a helpful and versatile AI assistant.
Your capabilities include:
1. **General Knowledge**: Answering questions on a wide range of topics.
2. **Coding**: Writing, debugging, and explaining code in various languages.
3. **Document Analysis**: Understanding and summarizing contents of PDFs, Word documents, PowerPoint presentations, and text files.
4. **Image Understanding**: Analyzing images and diagrams.
5. **Data Analysis**: Executing Python code to analyze data and visualize results.

Always respond in a helpful, polite, and accurate manner.
When dealing with code, provide clean, efficient, and well-commented solutions.
"""

# --- Default Session State ---
SESSION_STATE_DEFAULTS = {
    "messages": [],
    "system_role_defined": False,
    "total_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    "is_generating": False,
    "last_usage_info": None,
    "python_canvases": [ACE_EDITOR_DEFAULT_CODE],
    "multi_code_enabled": True,
    "stop_generation": False,
    "canvas_key_counter": 0,
    "reasoning_effort": "high",
    "debug_logs": [],
    "current_model_id": "gemini-3.5-flash", # UIで切り替え可能にする
    "enable_google_search": True, # Grounding機能用フラグ
    "enable_more_research": False, # 深掘り調査モード用フラグ
    "uploaded_file_queue": [], # 送信待ちのファイルリスト
    
    # --- 新機能用ステート ---
    "auto_plot_enabled": False, # グラフ描画・データ分析モード
    "auto_save_enabled": True,  # 自動履歴保存
    "clipboard_queue": [],      # クリップボード画像キュー
}

# 選択可能なモデルリスト
AVAILABLE_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.0-flash-exp",
    "gpt-5.3-codex",
]

# --- UI Texts ---
class UITexts:
    APP_TITLE = "🤖GP-Chat 汎用AIアプリ with Gemini" # タイトルも汎用的に変更
    SIDEBAR_HEADER = "設定"
    RESET_BUTTON_LABEL = "会話履歴をリセット"
    CODEX_MINI_INFO = "`Gemini 3 は最大1Mまでのトークンを使用可能です` ."
    HISTORY_SUBHEADER = "会話履歴 (JSON)"
    DOWNLOAD_HISTORY_BUTTON = "会話履歴をダウンロード"
    UPLOAD_HISTORY_LABEL = "JSONで会話を再開"
    HISTORY_LOADED_SUCCESS = "会話履歴とCanvasを読み込みました"
    OLD_HISTORY_FORMAT_WARNING = "古いフォーマットなので対応していません"
    JSON_FORMAT_ERROR = "対応できないJSON形式です"
    JSON_LOAD_ERROR = "JSON load error: {e}"

    EDITOR_SUBHEADER = "🔧 コードエディタ"
    MULTI_CODE_CHECKBOX = "マルチコードを有効化"
    ADD_CANVAS_BUTTON = "Canvasを追加"
    CLEAR_BUTTON = "クリア"
    REVIEW_BUTTON = "レビュー"
    VALIDATE_BUTTON = "検証"

    FILE_UPLOAD_HEADER = "📂 ファイル添付"
    # PPT/PPTXを追加
    FILE_UPLOAD_LABEL = "画像 / PDF / Word / PPT / Excel"
    FILE_UPLOAD_HELP = "チャット送信時にAIに読み込ませます。×ボタンで手動削除するまで、毎ターン送信され続けます。"
    # ppt, pptx, xlsx, xlsm, xlsを追加
    SUPPORTED_FILE_TYPES = ["png", "jpg", "jpeg", "bmp", "gif", "pdf", "docx", "pptx", "ppt", "txt", "md", "yaml", "json", "xlsx", "xlsm", "xls"]

    SYSTEM_PROMPT_HEADER = "Set AI System Role"
    SYSTEM_PROMPT_TEXT_AREA_LABEL = "System Role"
    START_CHAT_BUTTON = "Start Chat"

    ENV_VARS_ERROR = "Error: Environment variable '{vars}' is not set."
    CLIENT_INIT_ERROR = "SDK initialization failed: {e}"
    API_REQUEST_ERROR = "API request failed: {e}"
    
    NO_CODE_TO_VALIDATE = "No code to validate."
    VALIDATE_SPINNER_MULTI = "Validating Canvas-{i}..."
    VALIDATE_SPINNER_SINGLE = "Validating code..."
    
    PYLINT_SYNTAX_ERROR = "⚠️ Syntax error detected by pylint."

    STOP_GENERATION_BUTTON = "Stop"
    CHAT_INPUT_PLACEHOLDER = "Message Gemini..."
    
    REVIEW_PROMPT_SINGLE = "### Reference Code (Canvas)\nPlease review this code and suggest improvements."
    REVIEW_PROMPT_MULTI = "### Reference Code (Canvas-{i})\nPlease review this canvas and suggest improvements."
    
    WEB_SEARCH_LABEL = "Web検索 (Grounding)"
    WEB_SEARCH_HELP = "Google検索を使用して回答を生成します。"
    
    # --- 新規追加 ---
    MORE_RESEARCH_LABEL = "徹底調査モード (More Research)"
    MORE_RESEARCH_HELP = "AIに複数回のWeb検索と自問自答を強制し、情報の正確性を高めます。回答に時間がかかります。"
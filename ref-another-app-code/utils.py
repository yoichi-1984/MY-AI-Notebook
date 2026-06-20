import os
import sys
import yaml
import tempfile
import subprocess
import io
import glob
import hashlib
import json
import re
import datetime
import copy
from importlib import resources
import streamlit as st
from google import genai
from google.genai import types

# --- Import Logic for Package vs Script execution ---
try:
    from . import config
    from . import llm_router
    from . import state_manager
except ImportError:
    import config
    import llm_router
    import state_manager

# python-docxのインポート（Wordファイル用）
try:
    import docx
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

# pywin32 (PowerPoint操作用) のインポート
try:
    import win32com.client
    import pythoncom
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

def load_prompts():
    """ルート直下の prompts/prompts.yaml を優先して読み込み、無ければデフォルトから自動コピーする"""
    local_prompts_dir = "prompts"
    local_prompts_path = os.path.join(local_prompts_dir, "prompts.yaml")
    
    # 1. ルート直下の prompts/prompts.yaml をチェック
    if os.path.exists(local_prompts_path):
        try:
            with open(local_prompts_path, "r", encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)
                if yaml_data and "prompts" in yaml_data:
                    return yaml_data.get("prompts", {})
        except Exception as e:
            print(f"Warning: Failed to load local prompts/prompts.yaml: {e}")

    # 2. 存在しないか読み込み失敗した場合、デフォルトをロードしてコピーを作成
    default_data = None
    
    # パッケージ内リソースからの読み込みを試行
    try:
        with resources.open_text("gp_chat", "prompts.yaml") as f:
            default_data = yaml.safe_load(f)
    except Exception as e:
        # パッケージ化されていない場合のフォールバック（開発時のカレントディレクトリ）
        try:
            with open("prompts.yaml", "r", encoding="utf-8") as f:
                default_data = yaml.safe_load(f)
        except Exception as e2:
            print(f"Warning: Default prompts.yaml load failed: {e}, {e2}")

    # デフォルトデータのロードに成功した場合、それをローカルに保存して返す
    if default_data and "prompts" in default_data:
        try:
            os.makedirs(local_prompts_dir, exist_ok=True)
            with open(local_prompts_path, "w", encoding="utf-8") as f:
                yaml.dump(default_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            print(f"Created local prompts copy at: {local_prompts_path}")
        except Exception as e:
            print(f"Warning: Failed to copy prompts.yaml to local dir: {e}")
        return default_data.get("prompts", {})
        
    return {}

def save_prompts(prompts_dict):
    """ルート直下の prompts/prompts.yaml にプロンプトデータを書き込む"""
    local_prompts_dir = "prompts"
    local_prompts_path = os.path.join(local_prompts_dir, "prompts.yaml")
    
    try:
        os.makedirs(local_prompts_dir, exist_ok=True)
        # YAMLファイル全体の構造を作る
        yaml_data = {"prompts": prompts_dict}
        with open(local_prompts_path, "w", encoding="utf-8") as f:
            yaml.dump(yaml_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        print(f"Error: Failed to save prompts to {local_prompts_path}: {e}")
        return False


def find_env_files(directory="env"):
    """指定されたディレクトリ内の.envファイルを検索する"""
    if not os.path.isdir(directory):
        return []
    return [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".env")]

def extract_text_from_docx(file_bytes):
    """docxファイルからテキストを抽出する"""
    if not HAS_DOCX:
        return "[Error] python-docx library is not installed. Please install it to read Word documents."
    
    try:
        doc = docx.Document(io.BytesIO(file_bytes))
        full_text = []
        for para in doc.paragraphs:
            full_text.append(para.text)
        return "\n".join(full_text)
    except Exception as e:
        return f"[Error parsing docx] {str(e)}"
    
def extract_text_from_excel(file_bytes, filename):
    """Excelファイル(xlsx/xlsm/xls)から全シートのデータをテキスト(Markdown)として抽出する"""
    try:
        import pandas as pd
        excel_file = io.BytesIO(file_bytes)
        
        # エンジンの選定 (calamineが利用可能なら優先、なければopenpyxlにフォールバック)
        engine = None
        try:
            import python_calamine
            engine = "calamine"
        except ImportError:
            try:
                import openpyxl
                engine = "openpyxl"
            except ImportError:
                pass
                
        # sheet_name=None で全シートを辞書形式で読み込む
        sheets = pd.read_excel(excel_file, sheet_name=None, engine=engine)
        
        full_text = []
        for sheet_name, df in sheets.items():
            full_text.append(f"### Sheet: {sheet_name}")
            if df.empty:
                full_text.append("(空のシートです)\n")
                continue
                
            # NaNを空文字に置換
            df_clean = df.fillna("")
            
            # Markdownテーブルに変換 (tabulateがない場合はCSVにフォールバック)
            try:
                markdown_table = df_clean.to_markdown(index=False)
                full_text.append(markdown_table)
            except ImportError:
                csv_data = df_clean.to_csv(index=False)
                full_text.append("```csv")
                full_text.append(csv_data)
                full_text.append("```")
                
            full_text.append("")  # 改行を追加
            
        return "\n".join(full_text)
    except Exception as e:
        return f"[Error parsing Excel file {filename}] {str(e)}"


def _convert_ppt_to_images_core(file_bytes, filename):
    """PowerPoint変換の実処理を行う内部関数"""
    if not HAS_WIN32:
        print("Server Configuration Error: 'pywin32' library is missing. PowerPoint conversion unavailable.")
        return []
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_ppt_path = os.path.join(temp_dir, filename)
        with open(temp_ppt_path, "wb") as f:
            f.write(file_bytes)
        
        output_dir = os.path.join(temp_dir, "slides")
        os.makedirs(output_dir, exist_ok=True)

        ppt_app = None
        presentation = None
        
        try:
            pythoncom.CoInitialize()
            ppt_app = win32com.client.Dispatch("PowerPoint.Application")
            presentation = ppt_app.Presentations.Open(os.path.abspath(temp_ppt_path), ReadOnly=True, WithWindow=False)
            presentation.SaveAs(os.path.abspath(os.path.join(output_dir, "slide.png")), 18) # 18 = ppSaveAsPNG
        except Exception as e:
            print(f"PowerPoint conversion error: {e}")
            return []
        finally:
            if presentation:
                try:
                    presentation.Close()
                except Exception:
                    pass
            ppt_app = None
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        
        image_data_list = []
        search_path = os.path.join(output_dir, "*.PNG")
        slide_files = glob.glob(search_path)
        if not slide_files:
             search_path = os.path.join(output_dir, "*.png")
             slide_files = glob.glob(search_path)
        
        if not slide_files and os.path.isdir(os.path.join(output_dir, "slide")):
             search_path = os.path.join(output_dir, "slide", "*.PNG")
             slide_files = glob.glob(search_path)
             if not slide_files:
                search_path = os.path.join(output_dir, "slide", "*.png")
                slide_files = glob.glob(search_path)

        slide_files.sort(key=lambda x: len(x))

        for slide_file in slide_files:
            with open(slide_file, "rb") as img_f:
                img_bytes = img_f.read()
                image_data_list.append((img_bytes, "image/png"))
        
        return image_data_list

def convert_ppt_to_images_win32(file_bytes, filename):
    """ラッパー関数。st.session_stateを使用して手動でキャッシュ管理を行う。"""
    if not HAS_WIN32:
        return []
        
    file_hash = hashlib.md5(file_bytes).hexdigest()
    
    if "ppt_conversion_cache" not in st.session_state:
        st.session_state["ppt_conversion_cache"] = {}

    if file_hash in st.session_state["ppt_conversion_cache"]:
        return st.session_state["ppt_conversion_cache"][file_hash]

    st.toast(f"Processing PowerPoint: {filename}...", icon="🔄")
    images = _convert_ppt_to_images_core(file_bytes, filename)
    
    if images:
        st.session_state["ppt_conversion_cache"][file_hash] = images
        st.toast(f"Converted {len(images)} slides.", icon="✅")
    
    return images

def process_uploaded_files_for_gemini(uploaded_files):
    """アップロードファイルをGemini API用のPartsリストに変換する"""
    from google.genai import types
    
    api_parts = []
    display_info = []

    for uploaded_file in uploaded_files:
        # VirtualUploadedFile (クリップボード) と Streamlit UploadedFile の両方に対応
        file_bytes = uploaded_file.getvalue()
        
        # VirtualUploadedFileの場合は属性として持っている、Streamlitの場合は属性
        mime_type = getattr(uploaded_file, "type", "application/octet-stream")
        filename = getattr(uploaded_file, "name", "unknown_file")
        
        file_ext = os.path.splitext(filename)[1].lower()

        if "wordprocessingml" in mime_type or filename.endswith(".docx"):
            text_content = extract_text_from_docx(file_bytes)
            prompt_text = f"\n\n[Attached Document: {filename}]\n{text_content}\n"
            api_parts.append(types.Part.from_text(text=prompt_text))
            display_info.append({"name": filename, "type": "docx", "size": len(file_bytes)})

        elif file_ext in [".xlsx", ".xlsm", ".xls"]:
            text_content = extract_text_from_excel(file_bytes, filename)
            prompt_text = f"\n\n[Attached Excel File: {filename}]\n{text_content}\n"
            api_parts.append(types.Part.from_text(text=prompt_text))
            display_info.append({"name": filename, "type": "excel", "size": len(file_bytes)})


        elif file_ext in [".ppt", ".pptx"]:
            images = convert_ppt_to_images_win32(file_bytes, filename)
            if images:
                for idx, (img_bytes, img_mime) in enumerate(images):
                    api_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=img_mime))
                display_info.append({"name": filename, "type": "pptx(images)", "size": len(file_bytes)})
            else:
                st.error(f"Failed to convert PowerPoint: {filename}")

        elif mime_type == "application/pdf" or mime_type.startswith("image/"):
            api_parts.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
            display_info.append({"name": filename, "type": mime_type, "size": len(file_bytes)})
        
        elif mime_type.startswith("text/") or filename.endswith((".py", ".js", ".md", ".txt", ".json", ".csv", "yaml")):
            try:
                text_content = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text_content = file_bytes.decode("cp932")
                except UnicodeDecodeError:
                    text_content = file_bytes.decode("utf-8", errors="replace")
                    st.toast(f"⚠️ {filename}: 一部の文字化けを許容して読み込みました", icon="⚠️")
            except Exception as e:
                st.warning(f"Could not read text file {filename}: {e}")
                continue

            prompt_text = f"\n\n[Attached File: {filename}]\n```\n{text_content}\n```\n"
            api_parts.append(types.Part.from_text(text=prompt_text))
            display_info.append({"name": filename, "type": "text", "size": len(file_bytes)})

        else:
            st.warning(f"Unsupported file type for direct AI processing: {filename} ({mime_type})")

    return api_parts, display_info

def run_pylint_validation(canvas_code, canvas_index, prompts):
    """コードに対してpylintを実行し、分析プロンプトを生成する"""
    if not canvas_code or canvas_code.strip() == "" or canvas_code.strip() == config.ACE_EDITOR_DEFAULT_CODE.strip():
        st.toast(config.UITexts.NO_CODE_TO_VALIDATE, icon="⚠️")
        return

    spinner_text = config.UITexts.VALIDATE_SPINNER_MULTI.format(i=canvas_index + 1) if st.session_state['multi_code_enabled'] else config.UITexts.VALIDATE_SPINNER_SINGLE
    with st.spinner(spinner_text):
        tmp_file_path = ""
        pylint_report = ""
        try:
            with tempfile.NamedTemporaryFile(mode='w+', suffix='.py', delete=False, encoding='utf-8') as tmp_file:
                tmp_file_path = tmp_file.name
                tmp_file.write(canvas_code.replace('\r\n', '\n'))
                tmp_file.flush()
            
            result = subprocess.run(
                [sys.executable, "-m", "pylint", tmp_file_path],
                capture_output=True, text=True, check=False
            )
            
            error_output = (result.stderr or "") + (result.stdout or "")
            if "syntax-error" in error_output.lower():
                st.toast(config.UITexts.PYLINT_SYNTAX_ERROR, icon="⚠️")
                return 

            issues = []
            if result.stdout:
                issues = [line for line in result.stdout.splitlines() if line.strip() and not line.startswith(('*', '-')) and 'Your code has been rated' not in line]
            
            if issues:
                cleaned_issues = [issue.replace(f'{tmp_file_path}:', 'Line ') for issue in issues]
                pylint_report = "\n".join(cleaned_issues)
        finally:
            if os.path.exists(tmp_file_path):
                os.remove(tmp_file_path)

    if not pylint_report.strip():
        st.sidebar.success(f"✅ Canvas-{canvas_index + 1}: pylint検証完了。問題なし。")
        return

    validation_template = prompts.get("validation", {}).get("text", "以下はpylintのレポートです。解析してください:\n{pylint_report}\n\n対象コード:\n{code_for_prompt}")
    code_for_prompt = f"```python\n{canvas_code}\n```"
    validation_prompt = validation_template.format(code_for_prompt=code_for_prompt, pylint_report=pylint_report)
    
    system_message = st.session_state['messages'][0] if st.session_state['messages'] and st.session_state['messages'][0]["role"] == "system" else {"role": "system", "content": ""}
    st.session_state['special_generation_messages'] = [system_message, {"role": "user", "content": validation_prompt}]
    st.session_state['is_generating'] = True

def load_app_config():
    """パッケージ内のconfig.yamlを読み込む"""
    try:
        with resources.open_text("gp_chat", "config.yaml") as f:
            return yaml.safe_load(f)
    except Exception:
        # フォールバック: カレントディレクトリから
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except:
            return {}

# --- 自動履歴保存機能用の新規関数 ---

def sanitize_filename(filename):
    """OSで禁止されている文字を置換し、長さを制限する"""
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', filename)
    safe_name = safe_name.replace('\n', '').replace('\r', '').strip()
    return safe_name

def get_unique_filename(directory, base_filename):
    """同名ファイルが存在する場合、連番を付与してユニークなファイル名を生成する"""
    name, ext = os.path.splitext(base_filename)
    counter = 1
    unique_filename = base_filename
    
    while os.path.exists(os.path.join(directory, unique_filename)):
        unique_filename = f"{name}_{counter}{ext}"
        counter += 1
    
    return unique_filename

def generate_chat_title(messages, client_or_llm_clients, model_id=None):
    """
    会話履歴からチャット名を生成する。
    """
    try:
        resolved_model_id = (
            model_id
            or st.session_state.get('current_model_id')
            or os.getenv(config.GEMINI_MODEL_ID_NAME, "gemini-3.5-flash")
        )
        llm_clients = llm_router.coerce_llm_clients(client_or_llm_clients)
        conversation_text = ""
        for m in messages:
            if m["role"] != "system":
                content = m.get("content", "")[:500]
                conversation_text += f"{m['role']}: {content}\n"
        
        prompt = (
            "以下の会話の内容を、15文字から20文字程度の** 日本語ベースの **短い要約（タイトル）にしてください。\n"
            "ファイル名として使用するため、記号は含めないでください。\n"
            f"会話内容:\n{conversation_text}"
        )

        gen_config = types.GenerateContentConfig(
            max_output_tokens=1000,
            temperature=0.1
        )
        if "gemini-3" in resolved_model_id:
             gen_config.thinking_config = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.LOW,
                include_thoughts=True
            )

        response = llm_router.generate_content_with_route(
            llm_clients=llm_clients,
            model_id=resolved_model_id,
            contents=prompt,
            config=gen_config,
            mode="title_generation",
            logger=state_manager.add_debug_log,
        )
        
        title = (response.text or "").strip()
        
        if not title:
            title = "無題のチャット"
            
        return sanitize_filename(title.strip())

    except Exception as e:
        print(f"Title generation failed: {e}")
        return "自動保存チャット"

def save_auto_history(messages, canvases, multi_code_enabled, client_or_llm_clients, current_filename=None):
    """
    履歴を自動保存する。
    """
    log_dir = "chat_log"
    os.makedirs(log_dir, exist_ok=True)
    
    valid_msgs = [m for m in messages if m["role"] != "system"]
    
    if len(valid_msgs) < 4:
        return None

    if not current_filename:
        date_prefix = datetime.datetime.now().strftime("%y%m%d")
        chat_title = generate_chat_title(
            messages,
            client_or_llm_clients,
            model_id=st.session_state.get('current_model_id'),
        )
        base_filename = f"{date_prefix}_{chat_title}.json"
        filename = get_unique_filename(log_dir, base_filename)
        current_filename = filename
    
    history_data = {
        "messages": messages,
        "python_canvases": canvases,
        "multi_code_enabled": multi_code_enabled,
        "enable_more_research": st.session_state.get('enable_more_research', False),
        "enable_report_pdf": st.session_state.get('enable_report_pdf', False),
        "enable_google_search": st.session_state.get('enable_google_search', False),
        "reasoning_effort": st.session_state.get('reasoning_effort', 'high'),
        "auto_plot_enabled": st.session_state.get('auto_plot_enabled', False),
        "current_model_id": st.session_state.get('current_model_id'),
        "selected_env_file": st.session_state.get('selected_env_file'),
        "auto_save_enabled": st.session_state.get('auto_save_enabled', True),
        "always_send_all_canvases": st.session_state.get('always_send_all_canvases', False),
        "current_report_folder": st.session_state.get('current_report_folder'),
        "saved_at": datetime.datetime.now().isoformat()
    }

    file_path = os.path.join(log_dir, current_filename)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        print(f"Auto-saved history to: {file_path}")
        return current_filename
    except Exception as e:
        print(f"Auto-save failed: {e}")
        return current_filename

def generate_branch_filename(current_filename, log_dir="chat_log"):
    """
    現在のファイル名から、新しい分岐ファイル名を生成する。
    """
    today_str = datetime.datetime.now().strftime("%y%m%d")
    base_title = "分岐チャット"

    if current_filename:
        name_no_ext = os.path.splitext(current_filename)[0]
        match = re.match(r'^(?:\d{6}_)?(.*?)(?:-\d{2,})?$', name_no_ext)
        if match and match.group(1):
            base_title = match.group(1)
        else:
            base_title = name_no_ext

    pattern = os.path.join(log_dir, f"*_{base_title}-*.json")
    existing_files = glob.glob(pattern)
    
    max_branch = 1
    for f in existing_files:
        basename = os.path.basename(f)
        name_no_ext = os.path.splitext(basename)[0]
        suffix_match = re.search(r'-(\d{2,})$', name_no_ext)
        if suffix_match:
            num = int(suffix_match.group(1))
            if num > max_branch:
                max_branch = num

    next_branch = max_branch + 1
    branch_str = f"{next_branch:02d}"
    
    return f"{today_str}_{base_title}-{branch_str}.json"


def _normalize_api_role(role):
    """Map UI/session roles to Gemini API conversation roles."""
    if role in ("assistant", "model"):
        return "model"
    return "user"


def _clone_content_for_retry(content):
    """Clone a Gemini content object as deeply as the SDK supports."""
    if hasattr(content, "model_copy"):
        return content.model_copy(deep=True)
    return copy.deepcopy(content)


def build_materialized_chat_context(
    target_messages,
    queue_files,
    python_canvases,
    canvas_enabled_flags,
    is_special_mode,
    auto_plot_enabled,
    data_manager_instance,
):
    """
    Build the fully materialized request context used for the first LLM call.

    Returns:
        tuple:
            - chat_contents
            - system_instruction
            - available_files_map
            - file_attachments_meta
            - retry_context_snapshot
    """
    chat_contents = []
    system_instruction = ""

    for message in target_messages:
        role = message.get("role", "user")
        if role == "system":
            system_instruction = message.get("content", "")
            continue

        chat_contents.append(
            types.Content(
                role=_normalize_api_role(role),
                parts=[types.Part.from_text(text=message.get("content", ""))],
            )
        )

    available_files_map = {}
    file_attachments_meta = []

    if auto_plot_enabled and not is_special_mode and data_manager_instance:
        for queued_file in queue_files:
            try:
                file_path, file_name = data_manager_instance.save_file(queued_file)
                if file_path:
                    available_files_map[file_name] = file_path
            except Exception as e:
                file_label = getattr(queued_file, "name", "unknown_file")
                state_manager.add_debug_log(
                    f"[Context Builder] Failed to save temp file {file_label}: {e}",
                    "error",
                )

    target_user_content = None
    for content in reversed(chat_contents):
        if getattr(content, "role", None) == "user":
            target_user_content = content
            break

    if target_user_content is None and not is_special_mode and (
        queue_files or python_canvases
    ):
        state_manager.add_debug_log(
            "[Context Builder] No user message found for attachment/canvas injection.",
            "warning",
        )

    if not is_special_mode and queue_files and target_user_content is not None:
        file_parts, file_meta = process_uploaded_files_for_gemini(queue_files)
        if file_parts:
            target_user_content.parts = list(file_parts) + list(
                target_user_content.parts or []
            )
            file_attachments_meta = file_meta
            state_manager.add_debug_log(
                (
                    "[Context Builder] Injected "
                    f"{len(file_parts)} attachment parts from {len(file_meta)} files."
                )
            )

    injected_canvas_count = 0
    if not is_special_mode and target_user_content is not None:
        context_parts = []
        for index, code in enumerate(python_canvases):
            is_enabled = (
                canvas_enabled_flags[index]
                if index < len(canvas_enabled_flags)
                else True
            )
            if is_enabled and code.strip() and code != config.ACE_EDITOR_DEFAULT_CODE:
                context_parts.append(
                    types.Part.from_text(
                        text=f"\n[Canvas-{index + 1}]\n```python\n{code}\n```"
                    )
                )
                injected_canvas_count += 1

        if context_parts:
            target_user_content.parts = context_parts + list(
                target_user_content.parts or []
            )
            state_manager.add_debug_log(
                f"[Context Builder] Injected {injected_canvas_count} canvas snippets."
            )

    try:
        retry_context_snapshot = [
            _clone_content_for_retry(content) for content in chat_contents
        ]
        state_manager.add_debug_log(
            "[Context Builder] Created retry snapshot via deep copy."
        )
    except Exception as e:
        # Fallback must preserve the already-materialized multimodal context.
        # Do not rebuild from target_messages here, or attachment/canvas context is lost.
        retry_context_snapshot = list(chat_contents)
        state_manager.add_debug_log(
            (
                "[Context Builder] Deep copy failed; using shallow snapshot of "
                f"materialized context: {e}"
            ),
            "warning",
        )

    return (
        chat_contents,
        system_instruction,
        available_files_map,
        file_attachments_meta,
        retry_context_snapshot,
    )
import os
import sys
import base64
import json
import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types

# --- Local Module Imports ---
try:
    from gp_chat import config
    from gp_chat import utils
    from gp_chat import sidebar
    from gp_chat import data_manager
    from gp_chat import state_manager
    from gp_chat import code_agent
    from gp_chat import research_agent
    from gp_chat import reasoning_agent
    from gp_chat import report_agent
    from gp_chat import llm_router
    from gp_chat import azure_runtime
    from gp_chat import azure_fault_injection
    from gp_chat import azure_context_builder
    from gp_chat import azure_normal_chat
    from gp_chat import azure_research_agent
    from gp_chat import azure_reasoning_agent
    from gp_chat import azure_report_agent
    from gp_chat import azure_code_agent
    from gp_chat import azure_history_utils
    from gp_chat import azure_supervisor_helpers
    from gp_chat import cloud_logging_utils
    from gp_chat.azure_common_types import AzureModeResult
except ImportError:
    import config
    import utils
    import sidebar
    import data_manager
    import state_manager
    import code_agent
    import research_agent
    import reasoning_agent
    import report_agent
    import llm_router
    import azure_runtime
    import azure_fault_injection
    import azure_context_builder
    import azure_normal_chat
    import azure_research_agent
    import azure_reasoning_agent
    import azure_report_agent
    import azure_code_agent
    import azure_history_utils
    import azure_supervisor_helpers
    import cloud_logging_utils
    from azure_common_types import AzureModeResult


def _resolve_mode_name(*, is_special_mode, is_more_research, is_deep_reasoning, is_report_mode):
    if is_report_mode:
        return "report"
    if is_more_research:
        return "research"
    if is_deep_reasoning:
        return "reasoning"
    if is_special_mode:
        return "special"
    return "normal"


def _run_azure_mode(
    *,
    mode_name,
    azure_rt,
    prompts,
    target_messages,
    queue_files,
    python_canvases,
    canvas_enabled_flags,
    is_special_mode,
    auto_plot_enabled,
    data_manager_instance,
    enable_search,
    effort,
    max_output_tokens,
    text_placeholder,
    thought_status,
    thought_placeholder,
    model_id=None,
):
    context = azure_context_builder.build_materialized_context(
        target_messages=target_messages,
        queue_files=queue_files,
        python_canvases=python_canvases,
        canvas_enabled_flags=canvas_enabled_flags,
        is_special_mode=is_special_mode,
        auto_plot_enabled=auto_plot_enabled,
        data_manager_instance=data_manager_instance,
    )
    if context.file_attachments_meta:
        state_manager.add_debug_log(
            f"[Azure] Attached {len(context.file_attachments_meta)} files to the request."
        )

    if mode_name == "report":
        assistant_text, usage_metadata, report_meta = azure_report_agent.run_report_generation(
            runtime=azure_rt,
            prompts=prompts,
            context=context,
            messages=target_messages,
            max_output_tokens=max_output_tokens,
            text_placeholder=text_placeholder,
            thought_status=thought_status,
        )
        return AzureModeResult(
            full_response=assistant_text,
            system_instruction=context.system_instruction,
            usage_metadata=usage_metadata,
            mode_meta=report_meta,
            available_files_map=context.available_files_map,
            file_attachments_meta=context.file_attachments_meta,
            retry_context_snapshot=context.clone_retry_context(),
        )

    if mode_name == "research":
        return azure_research_agent.run_deep_research(
            runtime=azure_rt,
            context=context,
            max_output_tokens=max_output_tokens,
            text_placeholder=text_placeholder,
            thought_status=thought_status,
            thought_placeholder=thought_placeholder,
        )

    if mode_name == "reasoning":
        return azure_reasoning_agent.run_deep_reasoning(
            runtime=azure_rt,
            context=context,
            max_output_tokens=max_output_tokens,
            search_enabled=enable_search,
            text_placeholder=text_placeholder,
            thought_status=thought_status,
            thought_placeholder=thought_placeholder,
        )

    if mode_name == "special":
        return azure_normal_chat.run_special_generation(
            runtime=azure_rt,
            context=context,
            max_output_tokens=max_output_tokens,
            effort=effort,
            text_placeholder=text_placeholder,
            thought_status=thought_status,
            thought_placeholder=thought_placeholder,
            model_id=model_id,
        )

    return azure_normal_chat.run_normal_generation(
        runtime=azure_rt,
        context=context,
        max_output_tokens=max_output_tokens,
        search_enabled=enable_search,
        effort=effort,
        is_special_mode=False,
        text_placeholder=text_placeholder,
        thought_status=thought_status,
        thought_placeholder=thought_placeholder,
        model_id=model_id,
    )


def _save_history_for_provider(
    *,
    used_azure_fallback,
    azure_rt,
    messages,
    canvases,
    multi_code_enabled,
    client,
    current_filename,
):
    if used_azure_fallback and azure_rt is not None:
        return azure_history_utils.save_auto_history(
            messages,
            canvases,
            multi_code_enabled,
            azure_rt,
            current_filename=current_filename,
        )
    return utils.save_auto_history(
        messages,
        canvases,
        multi_code_enabled,
        client,
        current_filename=current_filename,
    )


def _is_valid_user_email(candidate):
    if not isinstance(candidate, str):
        return False
    email = candidate.strip()
    if not email:
        return False
    if any(char.isspace() for char in email):
        return False
    if email.count("@") != 1:
        return False

    local_part, domain_part = email.split("@", 1)
    return bool(local_part and domain_part)


def _ensure_user_email_from_mail_txt():
    mail_path = Path.cwd() / "mail.txt"

    session_email = st.session_state.get("user_email")
    if _is_valid_user_email(session_email):
        st.session_state["user_email"] = session_email.strip()
        return

    read_error = None
    if mail_path.exists():
        try:
            mail_text = mail_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            read_error = exc
        else:
            if _is_valid_user_email(mail_text):
                st.session_state["user_email"] = mail_text
                return
            if mail_text:
                st.warning("mail.txt のメールアドレス形式が不正です。正しいメールアドレスを入力してください。")
            else:
                st.warning("mail.txt が空です。メールアドレスを入力してください。")

    if read_error is not None:
        st.warning(f"mail.txt を読み取れませんでした: {read_error}")

    st.subheader("メールアドレスの設定")
    st.caption(f"Cloud Logging の user_email として使用します。保存先: {mail_path}")
    with st.form("user_email_form"):
        email_input = st.text_input("メールアドレス", placeholder="user@example")
        submitted = st.form_submit_button("保存して続行", type="primary")

    if submitted:
        email = email_input.strip()
        if not _is_valid_user_email(email):
            st.error("メールアドレスの形式が正しくありません。空白を含めず、@ の前後を入力してください。")
            st.stop()
        try:
            mail_path.write_text(email + "\n", encoding="utf-8")
        except OSError as exc:
            st.error(f"mail.txt の保存に失敗しました: {exc}")
            st.stop()

        st.session_state["user_email"] = email
        st.rerun()

    st.stop()


def _send_ai_usage_log(current_usage, model_id, project_id, location):
    if not current_usage:
        return
    # gpt-5.3-codex または Azureルート（フォールバック含む）を使用した場合は GCP Logging への送信をスキップ
    if model_id == "gpt-5.3-codex" or current_usage.get("llm_route") in ("azure_fallback", "azure_direct"):
        return
    cloud_logging_utils.write_ai_usage_log(
        current_usage=current_usage,
        user_email=st.session_state.get("user_email", ""),
        model_name=model_id,
        project_id=project_id,
        location=location,
        logger=state_manager.add_debug_log,
    )


@st.dialog("プロンプトの上書き確認")
def show_overwrite_dialog(name, text, presets, prompts_dict):
    st.warning(f"同名のプロンプト「{name}」が既に存在します。上書き保存してよろしいですか？")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("はい、上書きします", use_container_width=True, type="primary"):
            new_preset = {
                "name": name,
                "text": text
            }
            updated_presets = []
            for p in presets:
                if p["name"] == name:
                    updated_presets.append(new_preset)
                else:
                    updated_presets.append(p)
            
            prompts_dict["system_presets"] = updated_presets
            prompts_dict["system"] = {
                "text": text
            }
            
            if utils.save_prompts(prompts_dict):
                # 選択インデックスの更新
                new_names = [p["name"] for p in updated_presets]
                if name in new_names:
                    st.session_state["selected_preset_index"] = new_names.index(name)
                
                st.session_state['messages'] = [{"role": "system", "content": text}]
                st.session_state['system_role_defined'] = True
                st.rerun()
            else:
                st.error("プロンプトの保存に失敗しました。")
    with c2:
        if st.button("キャンセル", use_container_width=True):
            st.rerun()


def run_chatbot_app():
    st.set_page_config(page_title=config.UITexts.APP_TITLE, layout="wide")
    st.title(config.UITexts.APP_TITLE)
    
    if "debug_logs" not in st.session_state:
        st.session_state["debug_logs"] = []

    # Initialize Data Manager
    dm = data_manager.SessionDataManager()

    # サイドバー描画
    PROMPTS = utils.load_prompts()
    APP_CONFIG = utils.load_app_config()
    supported_extensions = APP_CONFIG.get("file_uploader", {}).get("supported_extensions", [])
    env_files = utils.find_env_files()
    
    if not env_files:
        st.error("env ディレクトリに .env ファイルが必要です。")
        st.stop()

    for key, value in config.SESSION_STATE_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value.copy() if isinstance(value, (dict, list)) else value
    if "enable_report_pdf" not in st.session_state:
        st.session_state["enable_report_pdf"] = False

    _ensure_user_email_from_mail_txt()

    # Canvas読み込み時の文字コード対応関数
    def handle_canvas_upload(index, key):
        uploaded_file = st.session_state.get(key)
        if uploaded_file:
            bytes_data = uploaded_file.getvalue()
            text = ""
            try:
                # まずUTF-8で試す
                text = bytes_data.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    # ダメならCP932 (Windows Shift-JIS) で試す
                    text = bytes_data.decode("cp932")
                except UnicodeDecodeError:
                    st.toast("⚠️ 対応していない文字コードです (UTF-8, CP932以外)", icon="❌")
                    return
            
            st.session_state['python_canvases'][index] = text
            # ファイルアップロード時も自動的に送信をONにする
            if 'canvas_enabled' in st.session_state and index < len(st.session_state['canvas_enabled']):
                st.session_state['canvas_enabled'][index] = True
            
            # Canvasの内容が更新されたことをエディタに通知するためカウンターをインクリメント
            st.session_state['canvas_key_counter'] += 1

    sidebar.render_sidebar(
        supported_extensions, env_files, 
        state_manager.load_history,
        state_manager.load_history_from_local,
        lambda i: st.session_state['python_canvases'].__setitem__(i, config.ACE_EDITOR_DEFAULT_CODE),
        lambda i, m: (st.session_state['messages'].append({"role": "user", "content": config.UITexts.REVIEW_PROMPT_MULTI.format(i=i+1) if m else config.UITexts.REVIEW_PROMPT_SINGLE}), st.session_state.__setitem__('is_generating', True)),
        lambda i: utils.run_pylint_validation(st.session_state['python_canvases'][i], i, PROMPTS),
        handle_canvas_upload 
    )

    # 中断リカバリーチェック
    if st.session_state.get('messages') and st.session_state['messages'][-1]['role'] == 'user' and not st.session_state.get('is_generating'):
        if state_manager.recover_interrupted_session():
            st.rerun()
    
    # --- .env ロードと Client 初期化 ---
    selected_env_file = st.session_state.get('selected_env_file', env_files[0])
    load_dotenv(dotenv_path=selected_env_file, override=True)
    
    project_id = os.getenv(config.GCP_PROJECT_ID_NAME)
    location = os.getenv(config.GCP_LOCATION_NAME, "global") 
    model_id = st.session_state.get('current_model_id', os.getenv(config.GEMINI_MODEL_ID_NAME, "gemini-3.5-flash"))
    azure_rt = azure_runtime.load_azure_runtime_from_env(
        bootstrap_env_path=selected_env_file,
        logger=state_manager.add_debug_log,
    )
    if azure_rt is not None and model_id == "gpt-5.3-codex":
        import dataclasses
        azure_rt = dataclasses.replace(azure_rt, deployment=azure_rt.codex_deployment)
    fault_injection_cfg = azure_fault_injection.load_fault_injection_config()
    
    INPUT_LIMIT = 1000000
    OUTPUT_LIMIT = 65536
    max_tokens_val = min(int(os.getenv("MAX_TOKEN", "65536")), OUTPUT_LIMIT)

    try:
        llm_clients = llm_router.build_llm_clients(
            project_id=project_id,
            location=location,
        )
        client = llm_clients.standard_client
    except Exception as e:
        st.error(f"Client init error: {e}")
        st.stop()

    st.caption(f"Backend: {model_id} | Location: {location}")

    with st.expander("🛠 システムログ", expanded=False):
        st.caption(f"Current Model: {model_id} | Location: {location}")
        last_usage_info = st.session_state.get("last_usage_info")
        if last_usage_info:
            debug_summary_parts = [
                f"prompt={last_usage_info.get('input_tokens', 0):,}",
                f"output={last_usage_info.get('output_tokens', 0):,}",
                f"total={last_usage_info.get('total_tokens', 0):,}",
            ]
            if last_usage_info.get("llm_route"):
                debug_summary_parts.append(f"route={last_usage_info['llm_route']}")
                debug_summary_parts.append(
                    f"retry={last_usage_info.get('llm_retry_count', 0)}"
                )
            if last_usage_info.get("traffic_type") is not None:
                debug_summary_parts.append(
                    f"trafficType={last_usage_info['traffic_type']}"
                )
            if last_usage_info.get("thoughts_tokens"):
                debug_summary_parts.append(
                    f"thoughts={last_usage_info['thoughts_tokens']:,}"
                )
            if last_usage_info.get("cached_tokens"):
                debug_summary_parts.append(
                    f"cached={last_usage_info['cached_tokens']:,}"
                )
            st.caption("Last Usage: " + " | ".join(debug_summary_parts))
        for log in reversed(st.session_state["debug_logs"]):
            st.text(log)

    if not st.session_state['system_role_defined']:
        st.subheader("AIの役割を設定（プリセット選択、または新規作成）")
        
        presets = PROMPTS.get("system_presets", [])
        if not presets and "system" in PROMPTS:
            presets = [{
                "name": "デフォルト (エンジニア向け)",
                "text": PROMPTS["system"].get("text", "")
            }]
            
        preset_names = [p["name"] for p in presets] + ["新規作成..."]
        
        # セレクトボックスでプリセットを選択
        selected_index = 0
        if "selected_preset_index" in st.session_state:
            # 範囲外チェック
            if st.session_state["selected_preset_index"] < len(preset_names):
                selected_index = st.session_state["selected_preset_index"]
                
        selected_name = st.selectbox(
            "プリセットプロンプトを選択", 
            preset_names, 
            index=selected_index,
            key="preset_selector"
        )
        
        # 選択状態を記憶
        current_idx = preset_names.index(selected_name)
        st.session_state["selected_preset_index"] = current_idx
        
        # 編集用フィールドの初期値設定
        if selected_name == "新規作成...":
            default_text = ""
            default_save_name = ""
        else:
            preset_data = presets[current_idx]
            default_text = preset_data.get("text", "")
            default_save_name = ""  # デフォルトでは常に空欄にする
            
        promo_text = st.text_area("プロンプト内容 (System Role)", value=default_text, height=250)
        
        # ボタンと保存用プロンプト名入力欄の配置
        col_left, col_right = st.columns([1, 1])
        
        # ボタンのkeyを利用して、CSSで個別にスタイルを適用
        st.markdown("""
            <style>
            /* 左ボタン (このまま実行): 赤背景白字 */
            .st-key-run_without_save_btn button {
                border: 2px solid #ff4b4b !important;
                background-color: #ff4b4b !important;
                color: #ffffff !important;
                width: 100% !important;
            }
            .st-key-run_without_save_btn button:hover {
                background-color: #ff3333 !important;
                border-color: #ff3333 !important;
                color: #ffffff !important;
            }
            
            /* 右ボタン (保存して実行): 白枠黒字 */
            .st-key-save_and_run_btn button {
                border: 2px solid #000000 !important;
                background-color: #ffffff !important;
                color: #000000 !important;
                width: 100% !important;
            }
            .st-key-save_and_run_btn button:hover {
                background-color: #000000 !important;
                color: #ffffff !important;
            }
            </style>
            """, unsafe_allow_html=True)
            
        with col_left:
            run_without_save = st.button(
                "このまま実行(追加保存無し)", 
                key="run_without_save_btn", 
                use_container_width=True
            )
            
        with col_right:
            save_and_run = st.button(
                "保存して実行", 
                key="save_and_run_btn", 
                use_container_width=True
            )
            
            # 保存するプロンプト名入力テキストボックス
            promo_name_input = st.text_input(
                "保存するプロンプト名", 
                value=default_save_name, 
                placeholder="例: 翻訳アシスタント",
                label_visibility="visible"
            )
            
        if run_without_save:
            if not promo_text.strip():
                st.error("プロンプト内容を入力してください。")
            else:
                st.session_state['messages'] = [{"role": "system", "content": promo_text}]
                st.session_state['system_role_defined'] = True
                st.rerun()
                
        if save_and_run:
            if not promo_name_input.strip():
                st.error("⚠️ プロンプト名を入力してください。")
            elif not promo_text.strip():
                st.error("⚠️ プロンプト内容を入力してください。")
            else:
                name = promo_name_input.strip()
                # 既に同名のプリセットがあるかチェック
                exists = any(p["name"] == name for p in presets)
                if exists:
                    # ダイアログを表示して上書き確認を行う
                    show_overwrite_dialog(name, promo_text, presets, PROMPTS)
                else:
                    # 新規保存処理
                    new_preset = {
                        "name": name,
                        "text": promo_text
                    }
                    updated_presets = presets + [new_preset]
                    
                    # PROMPTS 辞書全体を更新
                    PROMPTS["system_presets"] = updated_presets
                    PROMPTS["system"] = {
                        "text": promo_text
                    }
                    
                    # YAMLファイルへ保存
                    if utils.save_prompts(PROMPTS):
                        st.toast("✅ プロンプトを保存しました", icon="💾")
                    else:
                        st.error("プロンプトの保存に失敗しました。")
                    
                    # 新しく保存したプリセットが次回選択されるようにインデックスを設定
                    new_names = [p["name"] for p in updated_presets]
                    if name in new_names:
                        st.session_state["selected_preset_index"] = new_names.index(name)
                    
                    # セッションステートに設定して実行
                    st.session_state['messages'] = [{"role": "system", "content": promo_text}]
                    st.session_state['system_role_defined'] = True
                    st.rerun()
                
        st.stop()

    # --- 新規追加: チャット分岐処理用のコールバック関数 ---
    def handle_branching(target_index):
        # target_index までのメッセージを抽出 (切り取り)
        new_messages = st.session_state['messages'][:target_index + 1]
        
        # 新しいファイル名の生成
        current_file = st.session_state.get('current_chat_filename')
        new_filename = utils.generate_branch_filename(current_file, "chat_log")
        
        # JSONデータの構築と保存
        history_data = {
            "messages": new_messages,
            "python_canvases": st.session_state.get('python_canvases', []),
            "multi_code_enabled": st.session_state.get('multi_code_enabled', False),
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

        log_dir = "chat_log"
        os.makedirs(log_dir, exist_ok=True)
        new_filepath = os.path.join(log_dir, new_filename)
        try:
            with open(new_filepath, "w", encoding="utf-8") as f:
                json.dump(history_data, f, ensure_ascii=False, indent=2)
                
            # セッションステートの更新
            st.session_state['messages'] = new_messages
            st.session_state['current_chat_filename'] = new_filename
            st.session_state['current_report_folder'] = os.path.splitext(new_filename)[0]
            
            # 累積トークン数の再計算
            total_tokens = sum(
                m.get('usage', {}).get('total_tokens', 0) for m in new_messages if 'usage' in m
            )
            st.session_state['total_usage']['total_tokens'] = total_tokens
            
            state_manager.add_debug_log(f"Branched chat to: {new_filename}")
            st.toast(f"✂️ 会話を分岐し、{new_filename} として保存しました", icon="✅")
        except Exception as e:
            st.error(f"分岐の保存に失敗しました: {e}")
            state_manager.add_debug_log(f"Branch save error: {e}", "error")

    # --- チャット履歴の描画ループ ---
    for i, msg in enumerate(st.session_state['messages']):
        if msg["role"] != "system":
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                
                # --- 画像 (グラフ) の表示ロジック ---
                if "images" in msg and msg["images"]:
                    for img_b64 in msg["images"]:
                        try:
                            st.image(base64.b64decode(img_b64), width="stretch")
                        except Exception as e:
                            st.error(f"画像表示エラー: {e}")

                if "grounding_metadata" in msg and msg["grounding_metadata"]:
                    with st.expander("🔎 検索ソース (Grounding)"):
                        st.json(msg["grounding_metadata"])

                if msg["role"] == "assistant" and "usage" in msg:
                    u = msg["usage"]
                    in_p = (u['input_tokens'] / INPUT_LIMIT) * 100
                    out_p = (u['output_tokens'] / OUTPUT_LIMIT) * 100
                    
                    st.caption(
                        f"📊 **トークン使用量詳細**\n\n"
                        f"📥 **Input (Context):** {u['input_tokens']:,} / {INPUT_LIMIT:,} ({in_p:.2f}%)\n"
                        f"📤 **Output (Response):** {u['output_tokens']:,} / {OUTPUT_LIMIT:,} ({out_p:.2f}%)"
                    )
                    
                    # 生成中でない場合のみボタンを表示
                    extra_usage_lines = []
                    if u.get("llm_route"):
                        extra_usage_lines.append(
                            f"Route: {u['llm_route']} (retry={u.get('llm_retry_count', 0)})"
                        )
                    if u.get("traffic_type") is not None:
                        extra_usage_lines.append(
                            f"Traffic Type: {u['traffic_type']}"
                        )
                    if u.get("thoughts_tokens"):
                        extra_usage_lines.append(
                            f"Thoughts Tokens: {u['thoughts_tokens']:,}"
                        )
                    if u.get("cached_tokens"):
                        extra_usage_lines.append(
                            f"Cached Tokens: {u['cached_tokens']:,}"
                        )
                    if extra_usage_lines:
                        st.caption("\n".join(extra_usage_lines))

                    if not st.session_state.get('is_generating', False):
                        if st.button("✂️ この会話から分岐", key=f"branch_btn_{i}", help="この回答までの履歴で新しいチャットを生成・保存します"):
                            handle_branching(i)
                            st.rerun()

    if st.session_state['total_usage']['total_tokens'] > 0:
        st.divider()
        st.caption(f"🏁 セッション累計使用トークン: {st.session_state['total_usage']['total_tokens']:,}")

    if 'draft_input' in st.session_state:
        st.warning("⚠️ 前回の送信が中断されました。テキストを復元しました。")
        
        with st.form("draft_form"):
            draft_text = st.text_area("編集して再送信", value=st.session_state['draft_input'], height=150)
            c1, c2 = st.columns([1, 4])
            with c1:
                resend = st.form_submit_button("再送信", type="primary", width="stretch")
            with c2:
                cancel_draft = st.form_submit_button("破棄 (入力をクリア)", width="stretch")
            
            if resend:
                st.session_state['messages'].append({"role": "user", "content": draft_text})
                del st.session_state['draft_input']
                st.session_state['is_generating'] = True
                st.rerun()
            elif cancel_draft:
                del st.session_state['draft_input']
                st.rerun()
                
        # 強制的に最下段へスクロールするJSハック
        st.components.v1.html(
            """
            <script>
            setTimeout(function() {
                try {
                    const doc = window.parent.document;
                    let scrolled = false;
                    const iframes = doc.querySelectorAll('iframe');
                    for (let i = 0; i < iframes.length; i++) {
                        if (iframes[i].contentWindow === window) {
                            iframes[i].scrollIntoView({ behavior: 'smooth', block: 'end' });
                            scrolled = true;
                            break;
                        }
                    }
                    if (!scrolled) {
                        const mainContainer = doc.querySelector('.stApp [data-testid="stMainBlockContainer"]') || doc.querySelector('.main .block-container');
                        if (mainContainer) {
                            mainContainer.scrollTop = mainContainer.scrollHeight;
                        }
                    }
                } catch (e) {}
            }, 300);
            </script>
            """,
            height=0
        )
    
    else:
        if prompt := st.chat_input("指示を入力...", disabled=st.session_state['is_generating']):
            st.session_state['messages'].append({"role": "user", "content": prompt})
            st.session_state['is_generating'] = True
            st.rerun()

    if st.session_state['is_generating']:
        st.markdown("---")
        c_stop, c_info = st.columns([1, 5])
        with c_stop:
            if st.button("■ 送信取り消し", key="stop_generating_btn", type="primary"):
                st.session_state['is_generating'] = False
                state_manager.recover_interrupted_session()
                st.rerun()
        with c_info:
            st.info("生成中... 「送信取り消し」を押すと中断し、テキストを復元します。")

        with st.chat_message("assistant"):
            thought_area_container = st.empty()
            with thought_area_container.container():
                thought_status = st.status("思考プロセス (Thinking Process)...", expanded=False)
                thought_placeholder = thought_status.empty()
            
            text_placeholder = st.empty()
            full_response = ""
            full_thought_log = ""
            usage_metadata = None 
            last_llm_route = None
            last_llm_retry_count = 0
            
            is_special_mode = 'special_generation_messages' in st.session_state and st.session_state['special_generation_messages']
            
            target_messages = []
            if is_special_mode:
                target_messages = st.session_state['special_generation_messages']
                state_manager.add_debug_log("Generating response for SPECIAL validation request.")
            else:
                target_messages = st.session_state['messages']

            is_more_research = st.session_state.get('enable_more_research', False) and not is_special_mode
            effort = st.session_state.get('reasoning_effort', 'high')
            is_report_mode = st.session_state.get('enable_report_pdf', False) and not is_special_mode
            is_deep_reasoning = (effort == 'deep') and not is_more_research and not is_report_mode and not is_special_mode
            mode_name = _resolve_mode_name(
                is_special_mode=is_special_mode,
                is_more_research=is_more_research,
                is_deep_reasoning=is_deep_reasoning,
                is_report_mode=is_report_mode,
            )
            gcp_debug_start = len(st.session_state.get("debug_logs", []))
            used_azure_fallback = False
            azure_retry_system_instruction = ""
            forced_mode_exception = None
            is_gpt_5_3_codex = (model_id == "gpt-5.3-codex")
            if is_gpt_5_3_codex:
                state_manager.add_debug_log(
                    f"[Azure Route] Forcing direct Azure branch for model={model_id}.",
                    "info",
                )
                forced_mode_exception = azure_fault_injection.build_synthetic_terminal_429(mode_name)
            elif azure_supervisor_helpers.should_skip_gcp_for_mode(mode_name, fault_injection_cfg):
                state_manager.add_debug_log(
                    f"[Fault Injection] Forcing direct Azure branch for mode={mode_name}.",
                    "warning",
                )
                forced_mode_exception = azure_fault_injection.build_synthetic_terminal_429(mode_name)
            elif azure_fault_injection.should_inject_terminal_429(mode_name, fault_injection_cfg):
                state_manager.add_debug_log(
                    f"[Fault Injection] Injecting synthetic terminal 429 for mode={mode_name}.",
                    "warning",
                )
                forced_mode_exception = azure_fault_injection.build_synthetic_terminal_429(mode_name)

            queue_files = st.session_state.get('uploaded_file_queue', []) + st.session_state.get('clipboard_queue', [])
            canvas_enabled_flags = st.session_state.get('canvas_enabled', [])
            (
                chat_contents,
                system_instruction,
                available_files_map,
                file_attachments_meta,
                retry_context_snapshot,
            ) = utils.build_materialized_chat_context(
                target_messages=target_messages,
                queue_files=queue_files,
                python_canvases=st.session_state.get('python_canvases', []),
                canvas_enabled_flags=canvas_enabled_flags,
                is_special_mode=is_special_mode,
                auto_plot_enabled=st.session_state.get('auto_plot_enabled', False),
                data_manager_instance=dm,
            )
            if file_attachments_meta:
                state_manager.add_debug_log(
                    f"Attached {len(file_attachments_meta)} files to the request."
                )
            
            if is_more_research or is_deep_reasoning:
                t_level = types.ThinkingLevel.HIGH
            else:
                t_level = types.ThinkingLevel.HIGH if effort == 'high' else types.ThinkingLevel.LOW

            tools_config = []
            enable_search = st.session_state.get('enable_google_search', False)
            
            if (enable_search or is_more_research) and not is_special_mode:
                msg = "Google Search Tool Enabled"
                if is_more_research and not enable_search:
                    msg += " (Forced by More Research Mode)."
                elif is_deep_reasoning and enable_search:
                    msg += " (Enabled in Deep Reasoning Mode)."
                state_manager.add_debug_log(msg)
                tools_config = [types.Tool(google_search=types.GoogleSearch())]

            try:
                if forced_mode_exception is not None:
                    raise forced_mode_exception
                state_manager.add_debug_log(f"Requesting stream: {model_id} via {location} (max_output={max_tokens_val})")
                
                gen_config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=max_tokens_val,
                    tools=tools_config
                )
                if "gemini-3" in model_id:
                    gen_config.thinking_config = types.ThinkingConfig(
                        thinking_level=t_level,
                        include_thoughts=True
                    )

                final_grounding_metadata = None
                mode_llm_meta = {}

                if is_report_mode:
                    full_response, usage_metadata, _report_metadata = report_agent.run_report_generation(
                        client=client,
                        model_id=model_id,
                        prompts=PROMPTS,
                        chat_contents=chat_contents,
                        messages=target_messages,
                        system_instruction=system_instruction,
                        max_output_tokens=max_tokens_val,
                        text_placeholder=text_placeholder,
                        thought_status=thought_status
                    )
                    mode_llm_meta = {
                        "llm_route": _report_metadata.get("llm_route"),
                        "llm_retry_count": _report_metadata.get("llm_retry_count", 0),
                    }
                    thought_area_container.empty()
                elif is_more_research:
                    full_response, usage_metadata, final_grounding_metadata, mode_llm_meta = research_agent.run_deep_research(
                        client=client,
                        model_id=model_id,
                        gen_config=gen_config,
                        chat_contents=chat_contents,
                        system_instruction=system_instruction,
                        text_placeholder=text_placeholder,
                        thought_status=thought_status,
                        thought_placeholder=thought_placeholder
                    )
                elif is_deep_reasoning:
                    full_response, usage_metadata, final_grounding_metadata, mode_llm_meta = reasoning_agent.run_deep_reasoning(
                        client=client,
                        model_id=model_id,
                        gen_config=gen_config,
                        chat_contents=chat_contents,
                        system_instruction=system_instruction,
                        text_placeholder=text_placeholder,
                        thought_status=thought_status,
                        thought_placeholder=thought_placeholder
                    )
                else:
                    stream = llm_router.generate_content_stream_with_route(
                        llm_clients=llm_clients,
                        model_id=model_id,
                        contents=chat_contents,
                        config=gen_config,
                        mode="normal",
                        logger=state_manager.add_debug_log,
                    )

                    for chunk in stream:
                        if chunk.usage_metadata:
                            usage_metadata = chunk.usage_metadata

                        if chunk.route:
                            last_llm_route = chunk.route
                        last_llm_retry_count = chunk.app_retry_count

                        if chunk.grounding_metadata:
                            final_grounding_metadata = llm_router.merge_grounding_metadata(
                                final_grounding_metadata,
                                chunk.grounding_metadata,
                            )
                            queries = chunk.grounding_metadata.get("queries", [])
                            if queries:
                                state_manager.add_debug_log(f"[Grounding] Queries detected: {queries}")
                                for query in queries:
                                    action_text = f"\n\n🔍 **Action (Google Search):** `{query}`\n\n"
                                    full_thought_log += action_text
                                    thought_placeholder.markdown(full_thought_log)

                        if chunk.thought_delta:
                            full_thought_log += chunk.thought_delta
                            thought_placeholder.markdown(full_thought_log)
                        elif chunk.text_delta:
                            full_response += chunk.text_delta
                            text_placeholder.markdown(full_response + "▌")
                        
                        
                    text_placeholder.markdown(full_response)
                    
                    if not full_thought_log:
                        thought_area_container.empty()
                    else:
                        thought_status.update(label="思考完了 (Finished Thinking)", state="complete", expanded=False)
                    
                fallback_logs = azure_supervisor_helpers.get_debug_logs_since(gcp_debug_start)
                if (
                    not full_response
                    and azure_supervisor_helpers.should_attempt_azure_fallback(
                        exception=None,
                        log_lines=fallback_logs,
                        visible_output_started=False,
                        azure_runtime_available=azure_runtime.is_azure_runtime_available(azure_rt),
                        mode_supported=True,
                    )
                ):
                    state_manager.add_debug_log(
                        f"[Azure Fallback] Activating Azure fallback for mode={mode_name} after GCP terminal 429.",
                        "warning",
                    )
                    azure_result = _run_azure_mode(
                        mode_name=mode_name,
                        azure_rt=azure_rt,
                        prompts=PROMPTS,
                        target_messages=target_messages,
                        queue_files=queue_files,
                        python_canvases=st.session_state.get('python_canvases', []),
                        canvas_enabled_flags=canvas_enabled_flags,
                        is_special_mode=is_special_mode,
                        auto_plot_enabled=st.session_state.get('auto_plot_enabled', False),
                        data_manager_instance=dm,
                        enable_search=enable_search or is_more_research,
                        effort=effort,
                        max_output_tokens=max_tokens_val,
                        text_placeholder=text_placeholder,
                        thought_status=thought_status,
                        thought_placeholder=thought_placeholder,
                        model_id=model_id,
                    )
                    used_azure_fallback = True
                    full_response = azure_result.full_response
                    full_thought_log = azure_result.thought_log
                    azure_retry_system_instruction = azure_result.system_instruction
                    usage_metadata = azure_result.usage_metadata
                    final_grounding_metadata = azure_result.grounding_metadata
                    mode_llm_meta = dict(azure_result.mode_meta)
                    available_files_map = dict(azure_result.available_files_map)
                    file_attachments_meta = list(azure_result.file_attachments_meta)
                    retry_context_snapshot = list(azure_result.retry_context_snapshot)
                    if mode_name == "report" or not full_thought_log:
                        thought_area_container.empty()

                if final_grounding_metadata and (final_grounding_metadata.get("sources") or final_grounding_metadata.get("queries")):
                    with st.expander("🔎 検索ソース (Grounding)"):
                        st.json(final_grounding_metadata)

                state_manager.add_debug_log("Stream successfully finished.")

                current_usage = None
                if usage_metadata:
                    usage_summary = llm_router.summarize_usage_metadata(usage_metadata)
                    current_usage = {
                        "total_tokens": usage_summary["total_token_count"],
                        "input_tokens": usage_summary["prompt_token_count"],
                        "output_tokens": usage_summary["candidates_token_count"],
                    }
                    if usage_summary.get("traffic_type") is not None:
                        current_usage["traffic_type"] = usage_summary["traffic_type"]
                    if usage_summary.get("thoughts_token_count"):
                        current_usage["thoughts_tokens"] = usage_summary["thoughts_token_count"]
                    if usage_summary.get("cached_content_token_count"):
                        current_usage["cached_tokens"] = usage_summary["cached_content_token_count"]
                    if last_llm_route:
                        current_usage["llm_route"] = last_llm_route
                        current_usage["llm_retry_count"] = last_llm_retry_count
                    elif mode_llm_meta.get("llm_route"):
                        current_usage["llm_route"] = mode_llm_meta.get("llm_route")
                        current_usage["llm_retry_count"] = mode_llm_meta.get("llm_retry_count", 0)

                    st.session_state['total_usage']['total_tokens'] += usage_summary["total_token_count"]
                    st.session_state['last_usage_info'] = current_usage

                assistant_msg = {"role": "assistant", "content": full_response}
                if current_usage:
                    assistant_msg["usage"] = current_usage
                    if not used_azure_fallback:
                        _send_ai_usage_log(current_usage, model_id, project_id, location)
                if final_grounding_metadata:
                    assistant_msg["grounding_metadata"] = final_grounding_metadata
                if is_report_mode:
                    assistant_msg["report_mode"] = True
                
                if is_special_mode:
                    for m in target_messages:
                        if m["role"] == "user":
                            st.session_state['messages'].append(m)
                    st.session_state['messages'].append(assistant_msg)
                    del st.session_state['special_generation_messages']
                else:
                    st.session_state['messages'].append(assistant_msg)
                    
                    if 'canvas_enabled' in st.session_state and not st.session_state.get('always_send_all_canvases', False):
                        for i in range(len(st.session_state['canvas_enabled'])):
                            st.session_state['canvas_enabled'][i] = False
                    
                    if st.session_state.get('auto_save_enabled', True):
                        current_file = st.session_state.get('current_chat_filename')
                        new_filename = _save_history_for_provider(
                            used_azure_fallback=used_azure_fallback,
                            azure_rt=azure_rt,
                            messages=st.session_state['messages'],
                            canvases=st.session_state['python_canvases'],
                            multi_code_enabled=st.session_state.get('multi_code_enabled', False),
                            client=client,
                            current_filename=current_file,
                        )
                        if new_filename:
                            st.session_state['current_chat_filename'] = new_filename

                auto_plot = st.session_state.get('auto_plot_enabled', False)
                state_manager.add_debug_log(f"[DEBUG] Auto Plot Enabled: {auto_plot}, Special Mode: {is_special_mode}")
                
                if auto_plot and not is_special_mode and not is_report_mode:
                    auto_plot_mode = "auto_plot_fix"
                    auto_plot_debug_start = len(st.session_state.get("debug_logs", []))
                    auto_plot_messages_before = len(st.session_state.get("messages", []))
                    synthetic_auto_plot_exc = azure_supervisor_helpers.apply_fault_injection(
                        auto_plot_mode,
                        fault_injection_cfg,
                    )
                    auto_plot_exception = None
                    azure_auto_plot_context = None

                    if used_azure_fallback or azure_supervisor_helpers.should_skip_gcp_for_mode(auto_plot_mode, fault_injection_cfg):
                        if not used_azure_fallback:
                            azure_auto_plot_context = azure_context_builder.build_materialized_context(
                                target_messages=target_messages,
                                queue_files=queue_files,
                                python_canvases=st.session_state.get('python_canvases', []),
                                canvas_enabled_flags=canvas_enabled_flags,
                                is_special_mode=is_special_mode,
                                auto_plot_enabled=st.session_state.get('auto_plot_enabled', False),
                                data_manager_instance=dm,
                            )
                        if synthetic_auto_plot_exc is not None:
                            state_manager.add_debug_log(
                                "[Fault Injection] Forcing direct Azure auto-plot fallback.",
                                "warning",
                            )
                        azure_code_agent.run_auto_plot_agent(
                            runtime=azure_rt,
                            initial_response_text=full_response,
                            available_files_map=available_files_map,
                            max_output_tokens=max_tokens_val,
                            retry_context_snapshot=(
                                retry_context_snapshot
                                if used_azure_fallback
                                else azure_auto_plot_context.clone_retry_context()
                            ),
                            system_instruction=(
                                azure_retry_system_instruction
                                if used_azure_fallback
                                else azure_auto_plot_context.system_instruction
                            ),
                        )
                    else:
                        try:
                            if synthetic_auto_plot_exc is not None:
                                raise synthetic_auto_plot_exc
                            code_agent.run_auto_plot_agent(
                                client=client,
                                model_id=model_id,
                                gen_config=gen_config,
                                initial_response_text=full_response,
                                available_files_map=available_files_map,
                                retry_context_snapshot=retry_context_snapshot,
                            )
                        except Exception as exc:
                            auto_plot_exception = exc

                        auto_plot_logs = azure_supervisor_helpers.get_debug_logs_since(auto_plot_debug_start)
                        auto_plot_messages_after = len(st.session_state.get("messages", []))
                        if (
                            azure_runtime.is_azure_runtime_available(azure_rt)
                            and (
                                azure_supervisor_helpers.detect_terminal_429_from_exception(auto_plot_exception)
                                or azure_supervisor_helpers.can_take_over_auto_plot_fix(
                                    messages_before=auto_plot_messages_before,
                                    messages_after=auto_plot_messages_after,
                                    debug_logs_since_start=auto_plot_logs,
                                )
                            )
                        ):
                            state_manager.add_debug_log(
                                "[Azure Fallback] Activating Azure auto-plot fallback.",
                                "warning",
                            )
                            azure_auto_plot_context = azure_context_builder.build_materialized_context(
                                target_messages=target_messages,
                                queue_files=queue_files,
                                python_canvases=st.session_state.get('python_canvases', []),
                                canvas_enabled_flags=canvas_enabled_flags,
                                is_special_mode=is_special_mode,
                                auto_plot_enabled=st.session_state.get('auto_plot_enabled', False),
                                data_manager_instance=dm,
                            )
                            azure_code_agent.run_auto_plot_agent(
                                runtime=azure_rt,
                                initial_response_text=full_response,
                                available_files_map=available_files_map,
                                max_output_tokens=max_tokens_val,
                                retry_context_snapshot=azure_auto_plot_context.clone_retry_context(),
                                system_instruction=azure_auto_plot_context.system_instruction,
                            )
                else:
                    if not auto_plot:
                         state_manager.add_debug_log("[DEBUG] Execution skipped because Auto Plot is OFF.")

            except Exception as e:
                fallback_logs = azure_supervisor_helpers.get_debug_logs_since(gcp_debug_start)
                if azure_supervisor_helpers.should_attempt_azure_fallback(
                    exception=e,
                    log_lines=fallback_logs,
                    visible_output_started=azure_supervisor_helpers.has_visible_output_started(
                        full_response=full_response,
                    ),
                    azure_runtime_available=azure_runtime.is_azure_runtime_available(azure_rt),
                    mode_supported=True,
                ):
                    state_manager.add_debug_log(
                        f"[Azure Fallback] Activating Azure fallback for mode={mode_name} after exception.",
                        "warning",
                    )
                    try:
                        azure_result = _run_azure_mode(
                            mode_name=mode_name,
                            azure_rt=azure_rt,
                            prompts=PROMPTS,
                            target_messages=target_messages,
                            queue_files=queue_files,
                            python_canvases=st.session_state.get('python_canvases', []),
                            canvas_enabled_flags=canvas_enabled_flags,
                            is_special_mode=is_special_mode,
                            auto_plot_enabled=st.session_state.get('auto_plot_enabled', False),
                            data_manager_instance=dm,
                            enable_search=enable_search or is_more_research,
                            effort=effort,
                            max_output_tokens=max_tokens_val,
                            text_placeholder=text_placeholder,
                            thought_status=thought_status,
                            thought_placeholder=thought_placeholder,
                            model_id=model_id,
                        )
                        used_azure_fallback = True
                        full_response = azure_result.full_response
                        full_thought_log = azure_result.thought_log
                        azure_retry_system_instruction = azure_result.system_instruction
                        usage_metadata = azure_result.usage_metadata
                        final_grounding_metadata = azure_result.grounding_metadata
                        mode_llm_meta = dict(azure_result.mode_meta)
                        available_files_map = dict(azure_result.available_files_map)
                        retry_context_snapshot = list(azure_result.retry_context_snapshot)

                        if final_grounding_metadata and (final_grounding_metadata.get("sources") or final_grounding_metadata.get("queries")):
                            with st.expander("🔎 検索ソース (Grounding)"):
                                st.json(final_grounding_metadata)

                        current_usage = None
                        if usage_metadata:
                            usage_summary = llm_router.summarize_usage_metadata(usage_metadata)
                            current_usage = {
                                "total_tokens": usage_summary["total_token_count"],
                                "input_tokens": usage_summary["prompt_token_count"],
                                "output_tokens": usage_summary["candidates_token_count"],
                                "llm_route": mode_llm_meta.get("llm_route", "azure_fallback"),
                                "llm_retry_count": mode_llm_meta.get("llm_retry_count", 0),
                            }
                            if usage_summary.get("traffic_type") is not None:
                                current_usage["traffic_type"] = usage_summary["traffic_type"]
                            if usage_summary.get("thoughts_token_count"):
                                current_usage["thoughts_tokens"] = usage_summary["thoughts_token_count"]
                            if usage_summary.get("cached_content_token_count"):
                                current_usage["cached_tokens"] = usage_summary["cached_content_token_count"]
                            st.session_state['total_usage']['total_tokens'] += usage_summary["total_token_count"]
                            st.session_state['last_usage_info'] = current_usage

                        assistant_msg = {"role": "assistant", "content": full_response}
                        if current_usage:
                            assistant_msg["usage"] = current_usage
                            # Azureルート使用時は GCP Logging への送信をスキップします
                            # _send_ai_usage_log(current_usage, model_id, project_id, location)
                        if final_grounding_metadata:
                            assistant_msg["grounding_metadata"] = final_grounding_metadata
                        if is_report_mode:
                            assistant_msg["report_mode"] = True

                        if is_special_mode:
                            for m in target_messages:
                                if m["role"] == "user":
                                    st.session_state['messages'].append(m)
                            st.session_state['messages'].append(assistant_msg)
                            del st.session_state['special_generation_messages']
                        else:
                            st.session_state['messages'].append(assistant_msg)
                            if 'canvas_enabled' in st.session_state and not st.session_state.get('always_send_all_canvases', False):
                                for i in range(len(st.session_state['canvas_enabled'])):
                                    st.session_state['canvas_enabled'][i] = False
                            if st.session_state.get('auto_save_enabled', True):
                                current_file = st.session_state.get('current_chat_filename')
                                new_filename = _save_history_for_provider(
                                    used_azure_fallback=True,
                                    azure_rt=azure_rt,
                                    messages=st.session_state['messages'],
                                    canvases=st.session_state['python_canvases'],
                                    multi_code_enabled=st.session_state.get('multi_code_enabled', False),
                                    client=client,
                                    current_filename=current_file,
                                )
                                if new_filename:
                                    st.session_state['current_chat_filename'] = new_filename

                        if st.session_state.get('auto_plot_enabled', False) and not is_special_mode and not is_report_mode:
                            azure_code_agent.run_auto_plot_agent(
                                runtime=azure_rt,
                                initial_response_text=full_response,
                                available_files_map=available_files_map,
                                max_output_tokens=max_tokens_val,
                                retry_context_snapshot=retry_context_snapshot,
                                system_instruction=azure_retry_system_instruction,
                            )
                    except Exception as azure_exc:
                        st.error(f"Error during generation: {e}")
                        state_manager.add_debug_log(str(e), "error")
                        st.error(f"Azure fallback failed: {azure_exc}")
                        state_manager.add_debug_log(str(azure_exc), "error")
                else:
                    st.error(f"Error during generation: {e}")
                    state_manager.add_debug_log(str(e), "error")
            finally:
                st.session_state['is_generating'] = False
                st.rerun()

if __name__ == "__main__":
    run_chatbot_app()
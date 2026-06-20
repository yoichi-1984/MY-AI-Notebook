import streamlit as st
import os
import json
import time
import io
import datetime
from PIL import ImageGrab, Image # クリップボード操作用
from streamlit_ace import st_ace

# --- Import Logic for Package vs Script execution ---
try:
    from . import config
except ImportError:
    import config

# --- 擬似的なアップロードファイルクラス ---
class VirtualUploadedFile:
    """クリップボードの画像をStreamlitのUploadedFileのように振る舞わせるクラス"""
    def __init__(self, file_bytes, name, mime_type):
        self._data = file_bytes
        self.name = name
        self.type = mime_type
        self.size = len(file_bytes)
    
    def getvalue(self):
        return self._data

def render_sidebar(supported_types, env_files, load_history, load_local_history, handle_clear, handle_review, handle_validation, handle_file_upload):
    """Renders the sidebar with Gemini 3 specific options and model selector."""
    
    # トグルUIが操作されたときに裏のステータスを更新するコールバック
    def _toggle_cb(idx, k):
        if k in st.session_state:
            st.session_state['canvas_enabled'][idx] = st.session_state[k]

    with st.sidebar:
        # --- CSS Style Injection ---
        st.markdown(
            """
            <style>
                [data-testid="stFileUploader"] small {
                    display: none;
                }
            </style>
            """,
            unsafe_allow_html=True
        )

        # カウンターを取得（この数字が変わることで、エディタのキャッシュが破棄される）
        c_key = st.session_state.get('canvas_key_counter', 0)
        
        # --- UIロック用のフラグを取得 ---
        is_generating = st.session_state.get('is_generating', False)

        # --- 1. AIモデル選択エリア ---
        st.header("AIモデル選択")
        
        env_idx = 0
        curr_env = st.session_state.get('selected_env_file')
        if curr_env in env_files:
            env_idx = env_files.index(curr_env)
            
        sel_env = st.selectbox(
            label="Environment (.env)",
            options=env_files,
            index=env_idx,
            format_func=lambda x: os.path.basename(x),
            disabled=is_generating,
            key=f"env_sel_{c_key}" # カウンター付きキー
        )
        if sel_env != st.session_state.get('selected_env_file'):
            st.session_state['selected_env_file'] = sel_env
            st.rerun()

        model_idx = 0
        curr_model = st.session_state.get('current_model_id')
        if curr_model in config.AVAILABLE_MODELS:
            model_idx = config.AVAILABLE_MODELS.index(curr_model)

        sel_model = st.selectbox(
            label="Target Model",
            options=config.AVAILABLE_MODELS,
            index=model_idx,
            help="Gemini 3 が 404 になる場合は 2.0 Flash 等で接続を確認してください。",
            disabled=is_generating,
            key=f"model_sel_{c_key}" # カウンター付きキー
        )
        if sel_model != st.session_state.get('current_model_id'):
            st.session_state['current_model_id'] = sel_model
            st.rerun()

        # --- More Research Mode と UI連動・ロック機構 ---
        if 'enable_report_pdf' not in st.session_state:
            st.session_state['enable_report_pdf'] = False
        
        is_report_mode = st.session_state.get('enable_report_pdf', False)
        is_more_research = st.session_state.get('enable_more_research', False)

        effort_options = ['high', 'low', 'deep']
        curr_effort = 'high' if (is_more_research or is_report_mode) else st.session_state.get('reasoning_effort', 'high')
        effort_idx = effort_options.index(curr_effort) if curr_effort in effort_options else 0

        sel_effort = st.selectbox(
            label="Thinking Level",
            options=effort_options,
            index=effort_idx,
            disabled=is_more_research or is_report_mode or is_generating, 
            help="high: 標準の推論. low: 高速応答. deep: 推論特化モード (深い自己批判と多角的な仮説検証を実行)" + (" (Locked to 'high' in More Research or Report Mode)" if (is_more_research or is_report_mode) else ""),
            key=f"effort_sel_{c_key}" 
        )
        if not is_more_research and not is_report_mode and sel_effort != st.session_state.get('reasoning_effort', 'high'):
            st.session_state['reasoning_effort'] = sel_effort
            st.rerun()

        is_deep_reasoning = (st.session_state.get('reasoning_effort') == 'deep') and not is_report_mode

        curr_search = st.session_state.get('enable_google_search', False)
        if is_more_research:
            curr_search = True

        sel_search = st.checkbox(
            label=config.UITexts.WEB_SEARCH_LABEL,
            value=curr_search,
            disabled=is_more_research or is_generating, 
            help=config.UITexts.WEB_SEARCH_HELP + (" (Forced ON in More Research Mode)" if is_more_research else ""),
            key=f"search_chk_{c_key}"
        )
        if not is_more_research and sel_search != st.session_state.get('enable_google_search', False):
            st.session_state['enable_google_search'] = sel_search
            st.rerun()

        sel_more_research = st.checkbox(
            label=config.UITexts.MORE_RESEARCH_LABEL,
            value=is_more_research,
            disabled=is_deep_reasoning or is_report_mode or is_generating,
            help=config.UITexts.MORE_RESEARCH_HELP + (" (Disabled while Report PDF mode is ON)" if is_report_mode else ""),
            key=f"more_res_chk_{c_key}" 
        )
        
        if sel_more_research != is_more_research:
            st.session_state['enable_more_research'] = sel_more_research
            if sel_more_research:
                st.session_state['reasoning_effort'] = 'high'
                st.session_state['enable_google_search'] = True
            st.rerun()

        sel_report_pdf = st.checkbox(
            label="レポート機能（pdf）",
            value=is_report_mode,
            disabled=is_more_research or is_deep_reasoning or is_generating,
            help="ON の間は通常回答の代わりに HTML スライドを生成し、./slide_data 配下へ HTML と PDF を保存します。" + (" (Disabled while More Research or Deep Reasoning is active)" if (is_more_research or is_deep_reasoning) else ""),
            key=f"report_pdf_chk_{c_key}"
        )
        if sel_report_pdf != is_report_mode:
            st.session_state['enable_report_pdf'] = sel_report_pdf
            if sel_report_pdf:
                st.session_state['enable_more_research'] = False
                st.session_state['reasoning_effort'] = 'high'
            st.rerun()
        
        st.divider()

        # --- 2. 設定・履歴エリア ---
        def handle_full_reset():
            keys_to_keep = ['selected_env_file', 'canvas_key_counter']
            for key, value in config.SESSION_STATE_DEFAULTS.items():
                if key in keys_to_keep:
                    continue
                st.session_state[key] = value.copy() if isinstance(value, (dict, list)) else value

            prev_canvas_counter = st.session_state.get('canvas_key_counter', 0)

            for key in list(st.session_state.keys()):
                if (
                    key.startswith("plot_chk_")
                    or key.startswith("model_sel_")
                    or key.startswith("effort_sel_")
                ):
                    del st.session_state[key]
            st.session_state['auto_plot_enabled'] = False
            st.session_state['current_model_id'] = config.SESSION_STATE_DEFAULTS['current_model_id']
            st.session_state['reasoning_effort'] = config.SESSION_STATE_DEFAULTS['reasoning_effort']
            
            # full reset 前の editor/component identity を確実に破棄するため、
            # 既存値から単調増加させる。defaults 経由で 0 に戻してはいけない。
            st.session_state['canvas_key_counter'] = prev_canvas_counter + 1
            # reset 直後の 1 rerun だけ、st_ace の返す旧値で session_state が
            # 巻き戻されるのを防ぐ。
            st.session_state['_canvas_reset_pending'] = True

            if "file_uploader_key" in st.session_state:
                st.session_state["file_uploader_key"] += 1
            else:
                st.session_state["file_uploader_key"] = 1
            
            # --- Canvasの内容も初期化 ---
            st.session_state['python_canvases'] = [config.ACE_EDITOR_DEFAULT_CODE]
            # --------------------------
            
            if 'clipboard_queue' in st.session_state:
                st.session_state['clipboard_queue'] = []
            
            # --- 初期化漏れを完全に防ぐための追加処理 ---
            st.session_state['always_send_all_canvases'] = False
            if 'canvas_enabled' in st.session_state:
                del st.session_state['canvas_enabled']
            if 'toggle_keys' in st.session_state:
                del st.session_state['toggle_keys']
            st.session_state['always_send_all_canvases_ui'] = False
            # -------------------------------------------
            
            if 'current_chat_filename' in st.session_state:
                del st.session_state['current_chat_filename']
            if 'current_report_folder' in st.session_state:
                del st.session_state['current_report_folder']
            st.session_state['enable_report_pdf'] = False

            # Canvas 系 widget の旧 state を次 run へ持ち越さない
            for key in list(st.session_state.keys()):
                if key.startswith("ace_") or key.startswith("up_"):
                    del st.session_state[key]

        st.header(config.UITexts.SIDEBAR_HEADER)
        st.button(config.UITexts.RESET_BUTTON_LABEL, width="stretch", disabled=is_generating, on_click=handle_full_reset)

        # --- 追加機能: グラフ描画・データ分析モード ---
        if 'auto_plot_enabled' not in st.session_state:
            st.session_state['auto_plot_enabled'] = False

        sel_plot = st.checkbox(
            label="📈 グラフ描画・データ分析", 
            value=st.session_state.get('auto_plot_enabled', False),
            help="ONにすると、AIが生成したPythonコードを実行し、グラフ描画や計算結果を表示します。\nアップロードしたファイルは `files['name.csv']` でアクセス可能です。",
            disabled=is_generating,
            key=f"plot_chk_{c_key}" 
        )
        if sel_plot != st.session_state.get('auto_plot_enabled'):
            st.session_state['auto_plot_enabled'] = sel_plot
            st.rerun()

        # History Management
        st.subheader(config.UITexts.HISTORY_SUBHEADER)
        
        if 'auto_save_enabled' not in st.session_state:
            st.session_state['auto_save_enabled'] = True
            
        sel_save = st.checkbox(
            "■ 自動履歴保存", 
            value=st.session_state.get('auto_save_enabled', True),
            help="会話が2往復以上続くと、./chat_log フォルダに自動保存します。",
            disabled=is_generating,
            key=f"save_chk_{c_key}" 
        )
        if sel_save != st.session_state.get('auto_save_enabled'):
            st.session_state['auto_save_enabled'] = sel_save
            st.rerun()
        
        st.caption("📂 保存済み履歴から再開")
        log_dir = "chat_log"
        if os.path.exists(log_dir):
            log_files = [f for f in os.listdir(log_dir) if f.endswith(".json")]
            log_files.sort(key=lambda x: os.path.getmtime(os.path.join(log_dir, x)), reverse=True)
            
            if log_files:
                # --- 修正箇所: formを使ってselectboxによる自動rerunをブロック ---
                def _trigger_load_local_history():
                    # 送信ボタンが押されたタイミングで、セッションステートから最新の選択値を取り出して実行する
                    selected_file = st.session_state.get("local_history_selector")
                    if selected_file:
                        load_local_history(selected_file)

                # border=False オプションで、フォーム特有の枠線を消す（Streamlit 1.31+）
                with st.form(key="local_history_form", border=False):
                    st.selectbox(
                        "履歴ファイルを選択", 
                        options=log_files, 
                        disabled=is_generating, 
                        key="local_history_selector", 
                        label_visibility="collapsed"
                    )
                    st.form_submit_button(
                        "読み込む", 
                        width="stretch",
                        disabled=is_generating,
                        on_click=_trigger_load_local_history
                    )
                # -----------------------------------------------------------
            else:
                st.caption("（履歴ファイルはありません）")
        else:
            st.caption("（履歴フォルダはありません）")

        st.caption("📤 JSONファイルから再開")
        
        if st.session_state.get('messages'):
            history_data = {
                "messages": st.session_state['messages'],
                "python_canvases": st.session_state['python_canvases'],
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
                "current_report_folder": st.session_state.get('current_report_folder')
            }
            # セッションに保存されているファイル名があればそれを使い、探れば固定名にする
            dl_filename = st.session_state.get('current_chat_filename', 'gemini_chat_history.json')
            
            st.download_button(
                label=config.UITexts.DOWNLOAD_HISTORY_BUTTON,
                data=json.dumps(history_data, ensure_ascii=False, indent=2),
                file_name=dl_filename,
                mime="application/json",
                disabled=is_generating,
                width="stretch"
            )

        history_uploader_key = f"history_uploader_{c_key}"
        st.file_uploader(label=config.UITexts.UPLOAD_HISTORY_LABEL, type="json", key=history_uploader_key, disabled=is_generating, on_change=load_history, args=(history_uploader_key,), label_visibility="collapsed")

        st.divider()

        # --- 3. ファイル添付エリア ---
        st.header(config.UITexts.FILE_UPLOAD_HEADER)
        
        if 'uploaded_file_queue' not in st.session_state:
            st.session_state['uploaded_file_queue'] = []
        if 'clipboard_queue' not in st.session_state:
            st.session_state['clipboard_queue'] = []

        if "file_uploader_key" not in st.session_state:
            st.session_state["file_uploader_key"] = 0
            
        uploader_key = f"file_uploader_{st.session_state['file_uploader_key']}"

        ALLOWED_EXTENSIONS = ["png", "jpg", "jpeg", "bmp", "gif", "pdf", "docx", "pptx", "ppt", "txt", "md", "py", "js", "json", "csv", "xlsx", "xlsm", "xls"]
        uploaded_files = st.file_uploader(
            label=config.UITexts.FILE_UPLOAD_LABEL,
            type=ALLOWED_EXTENSIONS,
            accept_multiple_files=True,
            help=config.UITexts.FILE_UPLOAD_HELP,
            disabled=is_generating,
            key=uploader_key
        )
        
        if uploaded_files:
            st.session_state['uploaded_file_queue'] = uploaded_files
        else:
            st.session_state['uploaded_file_queue'] = []

        if st.button("📋 クリップボード画像を追加", width="stretch", disabled=is_generating, help="Win+Shift+S等でコピーした画像を読み込みます"):
            try:
                img = ImageGrab.grabclipboard()
                if isinstance(img, Image.Image):
                    buf = io.BytesIO()
                    img.save(buf, format='PNG')
                    byte_data = buf.getvalue()
                    
                    timestamp = datetime.datetime.now().strftime("%H%M%S")
                    filename = f"clipboard_{timestamp}.png"
                    
                    virtual_file = VirtualUploadedFile(byte_data, filename, "image/png")
                    st.session_state['clipboard_queue'].append(virtual_file)
                    st.toast(f"画像を追加しました: {filename}", icon="✅")
                elif img is None:
                    st.toast("クリップボードに画像がありません", icon="⚠️")
                else:
                    st.toast("対応していないクリップボード形式です", icon="⚠️")
            except Exception as e:
                st.error(f"Clipboard Error: {e}")

        total_files = len(st.session_state['uploaded_file_queue']) + len(st.session_state['clipboard_queue'])
        
        if total_files > 0:
            st.markdown(f"**送信待ち: {total_files} 件**")
            
            if st.session_state['clipboard_queue']:
                st.caption("クリップボード取得分:")
                for i, vfile in enumerate(st.session_state['clipboard_queue']):
                    col_del, col_name = st.columns([1, 5])
                    with col_del:
                        if st.button("❌", key=f"del_clip_{i}", disabled=is_generating):
                            st.session_state['clipboard_queue'].pop(i)
                            st.rerun()
                    with col_name:
                        st.text(vfile.name)
        else:
            st.caption("ファイルは選択されていません")

        st.divider()

        # --- 4. コードエディタ (Canvas) エリア ---
        st.subheader(config.UITexts.EDITOR_SUBHEADER)
        
        # --- 新機能: 全てを常に送信するトグル ---
        if 'always_send_all_canvases' not in st.session_state:
            st.session_state['always_send_all_canvases'] = False

        def _toggle_all_cb():
            is_all_on = st.session_state['always_send_all_canvases_ui']
            st.session_state['always_send_all_canvases'] = is_all_on
            if is_all_on:
                # 全てのCanvasをONにする
                for i in range(len(st.session_state['canvas_enabled'])):
                    st.session_state['canvas_enabled'][i] = True
                    st.session_state['toggle_keys'][i] += 1

        st.toggle(
            "⚡ 全てのCanvasを常にAIへ送る", 
            # value=st.session_state.get('always_send_all_canvases', False),
            key="always_send_all_canvases_ui",
            on_change=_toggle_all_cb,
            disabled=is_generating,
            help="ONにすると、すべてのCanvasが送信対象になり、送信後もOFFに戻りません。"
        )

        def _local_handle_clear(idx):
            handle_clear(idx)
            st.session_state['canvas_key_counter'] += 1

        canvases = st.session_state['python_canvases']
        reset_pending = st.session_state.get('_canvas_reset_pending', False)
        
        # --- Canvasステータスとトグルキーの初期化 ---
        if 'canvas_enabled' not in st.session_state:
            # 1個目のCanvasは初期状態で何も入力されていないためOFF(False)にし、2個目以降をTrueで初期化
            st.session_state['canvas_enabled'] = [False] + [True] * (max(len(canvases), 5) - 1)
        while len(st.session_state['canvas_enabled']) < len(canvases):
            st.session_state['canvas_enabled'].append(True)

        if 'toggle_keys' not in st.session_state:
            st.session_state['toggle_keys'] = [0] * max(len(canvases), 5)
        while len(st.session_state['toggle_keys']) < len(canvases):
            st.session_state['toggle_keys'].append(0)

        if st.session_state.get('multi_code_enabled', False):
            # 上部の追加ボタン
            if len(canvases) < config.MAX_CANVASES and st.button(config.UITexts.ADD_CANVAS_BUTTON, width="stretch", disabled=is_generating, key="add_canvas_top"):
                canvases.append(config.ACE_EDITOR_DEFAULT_CODE)
                st.session_state['canvas_enabled'].append(True)
                st.session_state['toggle_keys'].append(0)
                st.rerun()
            
            for i, content in enumerate(canvases):
                col_title, col_toggle = st.columns([1, 1])
                with col_title:
                    st.write(f"**Canvas-{i + 1}**")
                
                # トグルの場所を確保
                toggle_placeholder = col_toggle.empty()

                ace_key = f"ace_{i}_{st.session_state['canvas_key_counter']}"
                # 修正: auto_update を is_generating に応じて動的に制御し、不意の rerun を防ぐ
                updated = st_ace(value=content, key=ace_key, readonly=is_generating, auto_update=not is_generating, **config.ACE_EDITOR_SETTINGS)
                
                # エディタの入力判定
                # full reset 直後の 1 run は、component 側の旧値を信用しない。
                # ここで反映すると、初期化済み python_canvases が旧コードへ戻る。
                if reset_pending:
                    updated = content
                if updated != content:
                    is_meaningful_change = updated.strip() != content.strip()
                    canvases[i] = updated
                    if is_meaningful_change and not st.session_state['canvas_enabled'][i]:
                        st.session_state['canvas_enabled'][i] = True

                # 裏のステータスとUIの乖離を補正
                tk = st.session_state['toggle_keys'][i]
                expected_key = f"cvs_tog_{i}_{tk}"
                
                if expected_key in st.session_state and st.session_state[expected_key] != st.session_state['canvas_enabled'][i]:
                    st.session_state['toggle_keys'][i] += 1
                    expected_key = f"cvs_tog_{i}_{st.session_state['toggle_keys'][i]}"

                with toggle_placeholder:
                    st.toggle(
                        "AIへ送信", 
                        value=st.session_state['canvas_enabled'][i], 
                        key=expected_key, 
                        on_change=_toggle_cb,
                        args=(i, expected_key),
                        disabled=is_generating,
                        help="ONの場合、次回のチャットにコードが添付されます。送信後自動でOFFになります。"
                    )
                
                c1, c2, c3 = st.columns(3)
                c1.button(config.UITexts.CLEAR_BUTTON, key=f"clr_{i}", on_click=_local_handle_clear, args=(i,), disabled=is_generating, width="stretch")
                c2.button(config.UITexts.REVIEW_BUTTON, key=f"rev_{i}", on_click=handle_review, args=(i, True), disabled=is_generating, width="stretch")
                c3.button(config.UITexts.VALIDATE_BUTTON, key=f"val_{i}", on_click=handle_validation, args=(i,), disabled=is_generating, width="stretch")

                up_key = f"up_{i}_{st.session_state['canvas_key_counter']}"
                st.file_uploader(f"Load into Canvas-{i+1}", type=supported_types, key=up_key, on_change=handle_file_upload, args=(i, up_key), disabled=is_generating)
                st.divider()

            # 下部の追加ボタン
            if len(canvases) < config.MAX_CANVASES and st.button(config.UITexts.ADD_CANVAS_BUTTON, width="stretch", disabled=is_generating, key="add_canvas_bottom"):
                canvases.append(config.ACE_EDITOR_DEFAULT_CODE)
                st.session_state['canvas_enabled'].append(True)
                st.session_state['toggle_keys'].append(0)
                st.rerun()
                
        else:
            if len(canvases) > 1:
                st.session_state['python_canvases'] = [canvases[0]]
                st.rerun()
            
            # シングルモード
            col_title, col_toggle = st.columns([1, 1])
            with col_title:
                st.write("**Canvas**")
            
            toggle_placeholder = col_toggle.empty()

            ace_key = f"ace_single_{st.session_state['canvas_key_counter']}"
            # 修正: auto_update を is_generating に応じて動的に制御し、不意の rerun を防ぐ
            updated = st_ace(value=canvases[0], key=ace_key, readonly=is_generating, auto_update=not is_generating, **config.ACE_EDITOR_SETTINGS)
            
            # full reset 直後の 1 run は、component 側の旧値を信用しない。
            # ここで反映すると、初期化済み python_canvases が旧コードへ戻る。
            if reset_pending:
                updated = canvases[0]
            if updated != canvases[0]:
                is_meaningful_change = updated.strip() != canvases[0].strip()
                canvases[0] = updated
                if is_meaningful_change and not st.session_state['canvas_enabled'][0]:
                    st.session_state['canvas_enabled'][0] = True

            tk = st.session_state['toggle_keys'][0]
            expected_key = f"cvs_tog_s_{tk}"
            
            if expected_key in st.session_state and st.session_state[expected_key] != st.session_state['canvas_enabled'][0]:
                st.session_state['toggle_keys'][0] += 1
                expected_key = f"cvs_tog_s_{st.session_state['toggle_keys'][0]}"

            with toggle_placeholder:
                st.toggle(
                    "AIへ送信", 
                    value=st.session_state['canvas_enabled'][0], 
                    key=expected_key, 
                    on_change=_toggle_cb,
                    args=(0, expected_key),
                    disabled=is_generating,
                    help="ONの場合、次回のチャットにコードが添付されます。送信後自動でOFFになります。"
                )

            c1, c2, c3 = st.columns(3)
            c1.button(config.UITexts.CLEAR_BUTTON, key="clr_s", on_click=_local_handle_clear, args=(0,), disabled=is_generating, width="stretch")
            c2.button(config.UITexts.REVIEW_BUTTON, key="rev_s", on_click=handle_review, args=(0, False), disabled=is_generating, width="stretch")
            c3.button(config.UITexts.VALIDATE_BUTTON, key="val_s", on_click=handle_validation, args=(0,), disabled=is_generating, width="stretch")
            
            up_key = f"up_s_{st.session_state['canvas_key_counter']}"
            st.file_uploader("Load into Canvas", type=supported_types, key=up_key, on_change=handle_file_upload, args=(0, up_key), disabled=is_generating)
            
        if reset_pending:
            st.session_state['_canvas_reset_pending'] = False

        st.markdown("---")
        st.markdown(
            """
            <div style="text-align: center; font-size: 12px; color: #666;">
                Powered by <a href="https://github.com/yoichi-1984/GP-chat_With_Streamlit" target="_blank" style="color: #666;">GP-Chat Ver.0.5.4</a><br>
                © yoichi-1984<br>
                Licensed under <a href="https://www.apache.org/licenses/LICENSE-2.0" target="_blank" style="color: #666;">Apache 2.0</a>
            </div>
            """,
            unsafe_allow_html=True
        )
# AI Native Local Knowledge Database

本システムは、個人の機密情報および組織の内部情報を安全に扱うための、**完全ローカル（オンプレミス）運用のAIネイティブなナレッジデータベース**です。

従来のメモ帳やナレッジ管理ツールにおける画像管理の煩雑さやAI機能の不足を解決し、**「ユーザーはテキストや画像を雑にコピペするだけで、裏でAIが勝手に要約・自動仕分け・ベクトル化を行う」**究極のノンブロッキング知的生産環境を提供します。

---

## 🚀 主な機能

1. **インボックス（雑多収集 ✕ ノンブロッキング設計）**
   - 収集用の画面を開いて、テキスト入力や画像のドラッグ＆ドロップ、クリップボードからの画像ペースト（Ctrl + V）を雑に連続で行うことができます。
   - バックグラウンドの非同期ジョブキュー（FastAPI `BackgroundTasks`）に重いAI処理を委譲することで、UIは一瞬もフリーズせず、次の入力を即座に行えます。

2. **バックグラウンド非同期構造化 ＆ OCR**
   - Gemini 3.5 を使用し、Pydantic モデルを用いた **Structured Outputs（構造化出力）** によって、画像やテキストから「生OCRテキスト」「3行要約」「関連タグ」「分類先フォルダID」「自信度（Confidence Score）」を正確なJSON形式で抽出します。

3. **自動仕分け ＆ インコンテキスト学習 (フィードバック学習)**
   - AIが提示した分類自信度が閾値（デフォルト：0.7）を下回る場合は、勝手に移動せず「仕分け保留（`pending_review`）」としてUI上でユーザーに分類を促します。
   - ユーザーが手動でフォルダを修正した際、その判断事実と理由を Gemini に渡し、自動仕分けガイドライン（`rules.md`）へ追記・更新させます。これにより、使えば使うほど自動仕分けの精度が向上します。

4. **ハイブリッド検索 ＆ ローカルRAG基盤**
   - **ベクトル検索（意味検索）**: AI要約＋タグから生成された1536次元のベクトル空間から、意味の近いノートを LanceDB の近傍検索（ANN）で高速抽出します。
   - **キーワード検索（全文検索）**: 誤字脱字が含まれる可能性のある生のOCRテキストに対して、LanceDBのFTS（Full-Text Search）を利用し、型番やエラーコード、固有名詞などの完全一致でヒットさせます。
   - **ローカルRAG**: ヒットした上位数件のノートコンテキストを Gemini に流し込み、ユーザーの過去のナレッジだけを100%の根拠とした「調査レポート」を生成します。

5. **リアルタイムUI同期 (WebSocket)**
   - バックグラウンドでAIの構造化・自動仕分けが完了した瞬間、WebSocket を通じて画面にプッシュ通知を送信します。ブラウザをリロード（F5）することなく、対象ノートが自動的に適切なフォルダへ移動・表示更新されます。

6. **ダークモード ＆ AIパラメータ設定機能**
   - OneNote風の3カラムUIは、ライト/ダークテーマの切り替えに対応しています。
   - 設定画面から「AIモデル」「推論レベル（`thinking_level`）」「自動仕分け閾値」「RAG参照件数」を動的に変更し、SQLiteに保存・反映できます。

---

## 🛠 技術スタック

- **UIフロントエンド**: プレーン HTML / Vanilla JS / Tailwind CSS (CDN)
- **バックエンド**: Python 3.11+ / FastAPI / Uvicorn / aiofiles
- **リレーショナルDB**: SQLite (階層・メタデータ管理用)
- **ベクトルDB**: LanceDB (列指向・ディスクファースト・超低メモリ消費)
- **利用AI**:
  - 画像解析・構造化・仕分け: Gemini 3.5 (Flash / Pro) via `google-genai` SDK
  - ベクトル化 (Embedding): Azure OpenAI `text-embedding-3-small` (1536次元)

---

## 📂 ディレクトリ構成

```text
local-knowledge-db/
│
├── env/                    # Python 仮想環境 ＆ APIキー設定ファイル配置先
│   ├── azure.env           # Azure OpenAI 接続設定 (Git管理外)
│   └── gemini.json         # Vertex AI サービスアカウントキー (Git管理外)
│
├── local_images/           # ユーザーがペーストした画像の物理保存先 (Git管理外)
├── lancedb_data/           # LanceDB ベクトルデータ保存先 (Git管理外)
├── local_knowledge.db      # SQLite データベースファイル (Git管理外)
├── rules.md                # AIが学習・更新する自動仕分けルールファイル
│
├── requirements.txt        # 依存ライブラリ一覧
├── main.py                 # FastAPI エントリポイント・WebSocket管理
├── config.py               # 環境変数・APIキー読み込み設定
│
├── database/               # データベース制御レイア
│   ├── sqlite_client.py    # 階層・メタデータ管理 (SQLite)
│   └── lance_client.py     # ベクトル・FTS検索管理 (LanceDB)
│
├── services/               # AI・ビジネスロジックレイア
│   ├── ai_agent.py         # Gemini / Azure OpenAI 外部API連携
│   └── workflow.py         # 非同期ワークフロー (仕分け・学習・同期)
│
├── templates/
│   └── index.html          # OneNote風3カラムUI
│
├── start.bat               # Windows用 サーバー起動バッチ
└── stop.bat                # Windows用 サーバー停止バッチ
```

---

## ⚙️ セットアップ ＆ 起動手順

### 1. 仮想環境の構築と依存パッケージのインストール
Python 3.11以上がインストールされていることを確認してください。

```bash
# 仮想環境の作成
python -m venv env

# 仮想環境の有効化 (Windows)
call env\Scripts\activate

# 依存パッケージのインストール
pip install -r requirements.txt
```

### 2. 環境変数の設定 (APIキーの配置)
リポジトリのセキュリティのため、APIキーは直接コードに書かず、`env/` ディレクトリ配下に作成・配置します。

#### A. Azure OpenAI の設定 (ベクトル化用)
`env/azure.env` という名前のファイルを新規作成し、以下の形式でキーとエンドポイントを記述します。
```ini
AZURE_OPENAI_API_KEY="あなたのAzure OpenAI APIキー"
AZURE_OPENAI_ENDPOINT="https://あなたのエンドポイント名.openai.azure.com/"
```

#### B. Gemini / Google Vertex AI の設定 (構造化・OCR・仕分け・RAG用)
Google Cloud の Vertex AI または Google AI Studio を利用できます。以下のいずれかの方法で認証を設定します。

- **サービスアカウントキーの利用 (推奨)**:
  Google Cloud Console からダウンロードしたサービスアカウントの秘密鍵 JSON ファイルを、`env/gemini.json` として配置します。
- **通常の API キーの利用**:
  システム環境変数 `GEMINI_API_KEY` に Google AI Studio の API キーを設定します。

> [!NOTE]
> **APIキーが未設定の場合の動作について**
> APIキーのロードに失敗した場合や、オフライン環境の場合、システムは自動的に「モックフォールバックモード」で動作します。モックテキスト応答やダミーベクトルを生成するため、APIキーを設定しなくても基本的なUI動作や動作確認が可能です。

### 3. アプリケーションの起動

#### Windows環境の場合
ルートディレクトリにある `start.bat` をダブルクリックする、またはコマンドプロンプトから実行します。
```cmd
start.bat
```
自動的に既定のブラウザで `http://localhost:8080` が開き、バックエンドサーバーが起動します。

#### 手動で起動する場合
仮想環境を有効化した状態で、以下のコマンドを実行します。
```bash
uvicorn main:app --host 127.0.0.1 --port 8080 --reload
```
起動後、ブラウザで `http://localhost:8080` にアクセスしてください。

### 4. アプリケーションの停止

#### Windows環境の場合
`stop.bat` をダブルクリック、または実行することで、ポート8080を使用しているUvicornプロセスを安全に強制終了させます。
```cmd
stop.bat
```

---

## 🔒 セキュリティと機密性

- **データのポータビリティ**: データベースファイル (`local_knowledge.db`)、画像フォルダ (`local_images/`)、仕分けルール (`rules.md`) をコピーするだけで、他環境へのバックアップや移行が完全に実行可能です。ベンダーロックインはありません。
- **完全ローカル運用**: AI API を呼び出すHTTPS通信を除き、外部へのテレメトリ（利用ログ等の送信）は一切行われません。社外秘データやプライベートなナレッジも安心して蓄積できます。

---

## 📄 ライセンス

本プロジェクトは [Apache2](LICENSE) の下で公開されています。
（※公開の際、必要に応じて LICENSE ファイルを追加してください。）

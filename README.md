# AI Native Local Knowledge Database

本システムは、個人の機密情報および組織の内部情報を安全に扱うための、**完全ローカル（オンプレミス）運用のAIネイティブなナレッジデータベース**です。

従来のメモ帳やナレッジ管理ツールにおける画像管理の煩雑さやAI機能の不足を解決し、**「ユーザーはテキストや画像を雑にコピペするだけで、裏でAIが勝手に要約・自動仕分け・ベクトル化を行う」**究極のノンブロッキング知的生産環境を提供します。

---

## 🚀 主な機能

1. **インボックス（雑多収集 ✕ ノンブロッキング設計）**
   - 収集用の画面を開いて、テキスト入力や画像のドラッグ＆ドロップ、クリップボードからの画像ペースト（Ctrl + V）を雑に連続で行うことができます。
   - バックグラウンドの非同期ジョブキュー（FastAPI `BackgroundTasks`）に重いAI処理を委譲することで、UIは一瞬もフリーズせず、次の入力を即座に行えます。

2. **マルチ画像（複数画像）の動的添付・削除機能 🌟[NEW]**
   - 1つのノートに対して**複数の画像**を紐づけて管理できます。
   - ノート詳細画面の画像プレースホルダーへのドラッグ＆ドロップやファイル選択により、既存のノートに新しい画像をいつでも追加添付できます。
   - 添付された画像は、個別に拡大（ズーム）表示や**物理削除**（ローカルディスクからの削除 ✕ データベースからのクリーンアップ）が可能です。

3. **バックグラウンド非同期構造化 ＆ OCR**
   - Gemini 3.5 を使用し、Pydantic モデルを用いた **Structured Outputs（構造化出力）** によって、入力されたデータから「生OCRテキスト」「3行要約」「関連タグ」「分類先フォルダID」「自信度（Confidence Score）」を正確なJSON形式で抽出します。
   - 新しい画像を追加または削除した際は、画像単体のOCRを処理したのち、ノート内の全画像のOCR結果を自動でマージし、要約・タグ・タイトル・ベクトルを自動的に再生成・更新します。

4. **自動仕分け ＆ インコンテキスト学習 (フィードバック学習)**
   - AIが提示した分類自信度が閾値（デフォルト：0.7）を下回る場合は、勝手に移動せず「仕分け保留（`pending_review`）」としてUI上でユーザーに分類を促します。
   - ユーザーが手動でフォルダを修正した際、その判断事実と理由を Gemini に渡し、自動仕分けガイドライン（`rules.md`）へ追記・更新させます。これにより、使えば使うほど自動仕分けの精度が向上します。

5. **ハイブリッド検索 ＆ ローカルRAG基盤**
   - **ベクトル検索（意味検索）**: AI要約＋タグから生成された1536次元のベクトル空間から、意味の近いノートを LanceDB の近傍検索（ANN）で高速抽出します。
   - **キーワード検索（全文検索）**: 誤字脱字が含まれる可能性のある全画像のOCRテキストに対して、LanceDBのFTS（Full-Text Search）を利用し、型番やエラーコード、固有名詞などの完全一致でヒットさせます。
   - **ローカルRAG**: ヒットした上位数件のノートコンテキストを Gemini に流し込み、ユーザーの過去のナレッジだけを100%の根拠とした「調査レポート」を生成します。

6. **リアルタイムUI同期 (WebSocket)**
   - バックグラウンドでAIの構造化・自動仕分け・画像追加解析が完了した瞬間、WebSocket を通じて画面にプッシュ通知を送信します。ブラウザをリロード（F5）することなく、対象ノートが自動的に適切なフォルダへ移動・表示更新されます。

7. **ダークモード ＆ AIパラメータ設定機能**
   - OneNote風の3カラムUIは、ライト/ダークテーマの切り替えに対応しています。
   - 設定画面から「AIモデル」「推論レベル（`thinking_level`）」「自動仕分け自信度閾値」「RAG参照件数」を動的に変更し、SQLiteに保存・反映できます。

---

## 🛠 技術スタック

- **UIフロントエンド**: プレーン HTML / Vanilla JS / Tailwind CSS (CDN)
- **バックエンド**: Python 3.11+ / FastAPI / Uvicorn / aiofiles
- **リレーショナルDB**: SQLite (階層・メタデータ・複数画像管理用)
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
├── main.py                 # FastAPI エントリポイント・WebSocket管理・API定義
├── config.py               # 環境変数・APIキー読み込み設定
│
├── database/               # データベース制御レイア
│   ├── sqlite_client.py    # 階層・メタデータ・画像テーブル管理 (SQLite)
│   └── lance_client.py     # ベクトル・FTS検索管理 (LanceDB)
│
├── services/               # AI・ビジネスロジックレイア
│   ├── ai_agent.py         # Gemini / Azure OpenAI 外部API連携
│   └── workflow.py         # 非同期ワークフロー (仕分け・学習・同期・再解析)
│
├── templates/
│   └── index.html          # OneNote風3カラムUI
│
├── start.bat               # Windows用 サーバー起動バッチ
└── stop.bat                # Windows用 サーバー停止バッチ
```

---

## 💾 データベース構造とマイグレーション

複数画像のサポートに伴い、SQLite データベースのスキーマが以下のように設計されています。

### 1. `notes` テーブル (ノート本体)
- `id` (TEXT PRIMARY KEY): UUID
- `parent_folder_id` (TEXT): 紐づくフォルダのID
- `title` (TEXT): ノートタイトル
- `raw_text` (TEXT): 貼り付けられた生テキスト
- `status` (TEXT): `'processing'`, `'completed'`, `'pending_review'`
- `updated_at` (TIMESTAMP): 最終更新日時

### 2. `note_images` テーブル (ノート画像管理) 🌟[NEW]
ノートと 1:N のリレーションを持ち、複数の画像を管理します。
- `id` (TEXT PRIMARY KEY): UUID
- `note_id` (TEXT): `notes.id` への外部キー
- `image_path` (TEXT): ローカル保存された画像の物理相対パス
- `ai_ocr_text` (TEXT): その画像から個別に抽出されたOCRテキスト
- `created_at` (TIMESTAMP): 添付日時

### 3. 自動マイグレーション機能
旧バージョン（シングル画像仕様）のデータベースが存在する場合、起動時（`sqlite_client.init_db()`）に自動的に旧 `notes` テーブルの画像データ（`image_path`, `ai_ocr_text`）が検出され、`note_images` テーブルへ安全にデータ移行が行われます。

---

## 📡 主要APIエンドポイント

### 1. ノート・フォルダ操作
- `POST /api/save`: インボックスへのメモ収集 (ノンブロッキング受付)
- `GET /api/folders`: フォルダ一覧取得
- `POST /api/folders`: フォルダ新規作成
- `PUT /api/folders/{folder_id}`: フォルダ名変更
- `DELETE /api/folders/{folder_id}`: フォルダ削除 (退避 / 一括物理削除の選択可)
- `GET /api/folders/{folder_id}/notes`: 指定フォルダのノート一覧取得
- `GET /api/notes/{note_id}`: ノート詳細・画像リスト取得
- `PUT /api/notes/{note_id}`: ノート手動編集 (ベクトル再計算を自動起動)
- `DELETE /api/notes/{note_id}`: ノート物理削除 (SQLite, LanceDB, 全画像の物理削除)

### 2. マルチ画像操作 🌟[NEW]
- **`POST /api/notes/{note_id}/image`**: 既存ノートへの画像追加
  - 画像ファイルをローカルディスクに保存し、`note_images` レコードを追加。
  - バックグラウンドタスクを起動し、追加画像のOCR実行 ＞ 全画像OCRのマージ ＞ 要約・タグ・ベクトルの再解析・更新 ＞ WebSocketによる完了通知までを自動実行。
- **`DELETE /api/notes/{note_id}/images/{image_id}`**: 既存ノートの画像削除
  - 指定された画像をローカルディスクおよび `note_images` テーブルから物理削除。
  - 削除後、残った画像情報に基づいて、自動的に要約・タグ・ベクトルの再解析および LanceDB の更新を実行。

### 3. 検索・RAG ＆ 設定
- `GET /api/search?q={query}`: ハイブリッド検索 (ベクトル ✕ FTS全文検索) ＆ ローカルRAG回答生成
- `GET /api/settings`: 現在のAI・UI設定一覧の取得
- `POST /api/settings`: 設定の更新（SQLiteへの保存・即時反映）

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

本プロジェクトは [MIT License](LICENSE) の下で公開されています。
（※公開の際、必要に応じて LICENSE ファイルを追加してください。）

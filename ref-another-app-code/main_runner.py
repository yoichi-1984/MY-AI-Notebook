import os
import sys
import subprocess

def run():
    """
    Streamlitアプリケーションをサブプロセスとして起動します。
    このスクリプトと同じ場所にインストールされているmain.pyの絶対パスを
    特定して実行するため、どんな環境でも動作します。
    """
    try:
        # このスクリプト(main_runner.py)の絶対パスを取得
        runner_path = os.path.abspath(__file__)
        
        # main.pyは、このスクリプトと同じディレクトリにあるはず
        # その絶対パスを構築する
        package_dir = os.path.dirname(runner_path)
        main_py_path = os.path.join(package_dir, "main.py")

        # 実行するコマンドを構築
        command = [sys.executable, "-m", "streamlit", "run", main_py_path]
        
        print(f"実行ターゲット: {main_py_path}")
        print(f"実行コマンド: {' '.join(command)}")

        # subprocessを実行（cwdの指定は不要）
        subprocess.run(command, check=True)

    except subprocess.CalledProcessError as e:
        print(f"Streamlitの実行中にエラーが発生しました: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nアプリケーションを終了します。")
        sys.exit(0)
    except FileNotFoundError:
        print("エラー: 'streamlit'コマンドが見つかりません。Streamlitがインストールされているか確認してください。", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    run()
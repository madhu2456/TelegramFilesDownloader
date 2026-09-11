"""Launcher entrypoint for python -m src.web."""
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
import webbrowser

# Auto-detect project virtual environment if dependencies are missing in current environment
repo_root = Path(__file__).resolve().parent.parent.parent
venv_python = repo_root / "venv" / "bin" / "python"
if venv_python.exists() and os.path.abspath(sys.executable) != os.path.abspath(str(venv_python)):
    if importlib.util.find_spec("uvicorn") is None or importlib.util.find_spec("fastapi") is None:
        os.execv(str(venv_python), [str(venv_python), "-m", "src.web"] + sys.argv[1:])

import uvicorn  # noqa: E402

from src.web.app import create_app  # noqa: E402
from src.web.security import generate_ephemeral_token  # noqa: E402

def main():
    token = generate_ephemeral_token()
    url = f"http://127.0.0.1:8000/?token={token}"
    print("=" * 60)
    print(" TeleVault Modern Web Dashboard ")
    print(f" Web UI: {url}")
    print(" Ephemeral startup token generated and active.")
    print("=" * 60)

    def _open_browser():
        time.sleep(0.6)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")

if __name__ == "__main__":
    main()

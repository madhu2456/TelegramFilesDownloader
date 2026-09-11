"""Launcher entrypoint for python -m src.web."""
import threading
import time
import webbrowser
import uvicorn

from src.web.app import create_app
from src.web.security import generate_ephemeral_token

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

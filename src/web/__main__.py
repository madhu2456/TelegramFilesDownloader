"""Launcher entrypoint for python -m src.web."""
import importlib.util
import os
from pathlib import Path
import socket
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


def find_available_port(start_port: int = 8000, max_attempts: int = 50, host: str = "127.0.0.1") -> int:
    """Probe for the first available port starting from start_port."""
    for offset in range(max_attempts):
        port = start_port + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return start_port  # fallback


def main():
    # Acquire single-instance lock per Telegram session
    try:
        from src.config import load_config
        from src.instance_lock import acquire_instance_lock
        cfg = load_config()
        acquire_instance_lock(cfg.session_path)
    except Exception as e:
        print(f"[TeleVault] Instance lock note: {e}")

    env_port = int(os.getenv("TELEVAULT_PORT", "0") or 0)
    port = env_port if env_port > 0 else find_available_port(8000, 50)
    token = generate_ephemeral_token()
    url = f"http://127.0.0.1:{port}/?token={token}"
    print("=" * 60)
    print(" TeleVault Modern Web Dashboard ")
    print(f" Web UI: {url}")
    print(" Ephemeral startup token generated and active.")
    print("=" * 60)

    headless = bool(os.getenv("TELEVAULT_HEADLESS") or os.getenv("DISPLAY") is None)
    if not headless:
        def _open_browser():
            time.sleep(0.6)
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()

    bind_host = os.getenv("TELEVAULT_BIND_HOST", "127.0.0.1")
    app = create_app()
    forwarded_ips = os.getenv("TELEVAULT_FORWARDED_ALLOW_IPS", "127.0.0.1,172.16.0.0/12,*")
    uvicorn.run(
        app,
        host=bind_host,
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips=forwarded_ips,
    )

if __name__ == "__main__":
    main()

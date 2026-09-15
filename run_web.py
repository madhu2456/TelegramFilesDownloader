#!/usr/bin/env python3
"""Desktop launcher for TeleVault Modern Web Dashboard."""
import importlib.util
import os
import sys
from pathlib import Path

# Ensure repo root is in pythonpath
repo_root = Path(__file__).resolve().parent
sys.path.insert(0, str(repo_root))

# Auto-detect project virtual environment if dependencies are missing in current environment
venv_python = repo_root / "venv" / "bin" / "python"
if (
    venv_python.exists()
    and os.path.abspath(sys.executable) != os.path.abspath(str(venv_python))
    and (importlib.util.find_spec("uvicorn") is None or importlib.util.find_spec("fastapi") is None)
):
    os.execv(str(venv_python), [str(venv_python), str(repo_root / "run_web.py"), *sys.argv[1:]])

try:
    from src.web.__main__ import main
except ModuleNotFoundError as err:
    if "uvicorn" in str(err) or "fastapi" in str(err):
        print("Error: Missing required dependencies (uvicorn, fastapi).")
        print("Please install dependencies: pip install -r requirements.txt")
        print("Or run via virtualenv: venv/bin/python run_web.py")
        sys.exit(1)
    raise

if __name__ == "__main__":
    main()

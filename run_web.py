#!/usr/bin/env python3
"""Desktop launcher for TeleVault Modern Web Dashboard."""
import sys
from pathlib import Path

# Ensure repo root is in pythonpath
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.web.__main__ import main

if __name__ == "__main__":
    main()

# TelegramFilesDownloader

[![build](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/example/TelegramFilesDownloader/actions) [![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/) [![license](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

> Hardened Telethon-based Telegram media downloader with safe defaults, resume, and audit manifest.

See `docs/USAGE.md` for full flags, safety limits, and troubleshooting.

## Features

- Download from public channels, private channels/groups (member-only), and DMs.
- 7 target forms: `@user`, `t.me/user` (supports 4–32 char handles including Fragment handles like `@news`, `t.me/auto`), phone `+...`, numeric id, `t.me/c/...`, `t.me/+...`, `t.me/joinchat/...`.
- Dialog discovery: `--list-dialogs` with `--dialog-filter` and `--dialog-limit` to inspect account dialogs safely.
- Filters: `--filter`, `--search`, `--from-user`, `--after` / `--before`, `--ids`, `--min-id`.
- Incremental sync: `--sync` persists monotonic max message ID per chat to SQLite manifest for checkpointed resumes.
- Content hash deduplication: SHA-256 deduplication links identical files as aliases in `messages.jsonl` without re-downloading bytes.
- Robust partial downloads: `--dry-run` writes `messages.jsonl` only; `--resume` keeps `.part` files; `.part` is cleaned automatically on aliases and skips.
- SQLite manifest `out/manifest.db` with `UNIQUE(chat_id, msg_id)` for resume-skip and auditability.
- Flood & rate limit safety: serial requests, 1s + jitter, 300s cap, clean exit `3` on `FloodWaitError` (> 300s) or `PeerFloodError`.
- No auto-join: invites need explicit `--join`; takeout needs explicit `--takeout`.

## Quickstart

```bash
python3 -m ensurepip --upgrade
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Configure `.env` (see `.env.example`, exact keys matter):

```ini
TG_API_ID=your_api_id_here
TG_API_HASH=your_api_hash_here
TG_PHONE=+15551234567
TG_SESSION=session/telegram.session
```

Get `api_id` / `api_hash` at <https://my.telegram.org> under API development tools.

First run (public channel preview):

```bash
venv/bin/python -m src.cli --target @someuser123 --out out --limit 20
```

## CLI examples

| Use case | Command |
| :--- | :--- |
| Public channel | `venv/bin/python -m src.cli --target https://t.me/someuser123 --out out --limit 100` |
| 4-char Fragment handle | `venv/bin/python -m src.cli --target @news --out out --limit 50` |
| List account dialogs | `venv/bin/python -m src.cli --list-dialogs --dialog-filter channel --dialog-limit 50` |
| Incremental sync | `venv/bin/python -m src.cli --target @someuser123 --sync --out out` |
| Private invite with opt-in join | `venv/bin/python -m src.cli --target https://t.me/+AAAAbbbb --join --out out --limit 50` |
| DM by phone or handle | `venv/bin/python -m src.cli --target @someuser123 --out out --limit 20` |
| Dry-run, no bytes | `venv/bin/python -m src.cli --target @someuser123 --dry-run --limit 10 --out out` |
| Resume bounded run | `venv/bin/python -m src.cli --target @someuser123 --resume --limit 100 --max-bytes 104857600 --out out` |

Full flag matrix, exit codes (2 auth, 3 flood / peer flood, 4 disk/perm, 5 resolve), and limits: `docs/USAGE.md`.

## Project structure

```text
src/
  __init__.py
  auth.py       # session bootstrap
  cli.py        # argparse entrypoint (tg-dl)
  config.py     # .env loading, out-dir guards
  dialogs.py    # account dialog table formatting
  downloader.py # bounded fetch + write path
  filesafe.py   # chmod 600/700, umask 077
  resolver.py   # 7-form target parser + membership gate
  store.py      # SQLite manifest + jsonl log + sync checkpoints
docs/
  USAGE.md      # full CLI reference
tests/
```

## Version history

- `v0.3.1` (hardening): Support 4-character Fragment handles (`@news`, `t.me/auto`); exit code 3 on `PeerFloodError` and `FloodWaitError` > 300s; monotonic sync checkpointing (only advances on verified downloads/skips); automatic `.part` cleanup on aliases and same-size skips; directory fsync on replace.
- `v0.3.0`: Incremental sync with `--sync` and `--min-id`; SHA-256 content deduplication with alias records; UTC ISO-8601 timestamps (`Z` suffix); takeout wrapper support; rich metadata in `messages.jsonl`.
- `v0.2.0`: Safe account dialog discovery (`--list-dialogs`, `--dialog-filter`, `--dialog-limit`) without leaking phone numbers or secrets.
- `v0.1.0` (hardened): Bounded `--limit 500`, FloodWait cap, `chmod 600`/`0o700` tree, manifest resume, no auto-join.
- See full history: `git log --oneline`.

## Contributing

- Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Docs-only changes must keep code fences valid and links relative.
- Run `pytest`, `ruff check`, and `mypy` before opening a PR.

## Security

- Never commit `.env` or `*.session*`; both are git-ignored.
- Keep secrets private: `chmod 600 .env session/telegram.session`.
- Use placeholders (`your_api_id_here`, `+15551234567`) in issues and examples.

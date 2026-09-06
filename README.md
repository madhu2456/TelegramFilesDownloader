# TelegramFilesDownloader

[![build](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/example/TelegramFilesDownloader/actions) [![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/) [![license](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

> Hardened Telethon-based Telegram media downloader with safe defaults, resume, and audit manifest.

See `docs/USAGE.md` for full flags, safety limits, and troubleshooting.

## Features

- Download from public channels, private channels/groups (member-only), and DMs.
- 7 target forms: `@user`, `t.me/user`, phone `+...`, numeric id, `t.me/c/...`, `t.me/+...`, `t.me/joinchat/...`.
- Filters: `--filter`, `--search`, `--from-user`, `--after` / `--before`, `--ids`.
- `--dry-run` writes `messages.jsonl` only; `--resume` keeps `.part` offsets.
- SQLite manifest `out/manifest.db` with `UNIQUE(chat_id, msg_id)` for resume-skip.
- FloodWait safety: serial requests, 1s + jitter, 300s cap, exits `3` on flood.
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
| Private invite with opt-in join | `venv/bin/python -m src.cli --target https://t.me/+AAAAbbbb --join --out out --limit 50` |
| DM by phone or handle | `venv/bin/python -m src.cli --target @someuser123 --out out --limit 20` |
| Dry-run, no bytes | `venv/bin/python -m src.cli --target @someuser123 --dry-run --limit 10 --out out` |
| Resume bounded run | `venv/bin/python -m src.cli --target @someuser123 --resume --limit 100 --max-bytes 104857600 --out out` |

Full flag matrix, exit codes (2 auth, 3 flood, 4 disk/perm, 5 resolve), and limits: `docs/USAGE.md`.

## Project structure

```text
src/
  __init__.py
  auth.py       # session bootstrap
  cli.py        # argparse entrypoint (tg-dl)
  config.py     # .env loading, out-dir guards
  downloader.py # bounded fetch + write path
  filesafe.py   # chmod 600/700, umask 077
  resolver.py   # 7-form target parser + membership gate
  store.py      # SQLite manifest + jsonl log
docs/
  USAGE.md      # full CLI reference
tests/
```

## Version history

- `v0.1.0` hardened: bounded `--limit 500`, FloodWait cap, `chmod 600` tree, manifest resume, no auto-join.
- See full history: `git log --oneline`.

## Contributing

- Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Docs-only changes must keep code fences valid and links relative.
- Run `pytest`, `ruff check`, and `mypy` before opening a PR.

## Security

- Never commit `.env` or `*.session*`; both are git-ignored.
- Keep secrets private: `chmod 600 .env session/telegram.session`.
- Use placeholders (`your_api_id_here`, `+15551234567`) in issues and examples.

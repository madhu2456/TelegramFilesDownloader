# Usage

## 1. API credentials via my.telegram.org

1. Go to https://my.telegram.org and log in with your phone number.
2. Open **API development tools** and create an app.
3. Copy `api_id` and `api_hash`.
4. Copy them into `.env` (see `.env.example` — exact key names matter):

```ini
TG_API_ID=your_api_id_here
TG_API_HASH=your_api_hash_here
TG_PHONE=+15551234567
TG_SESSION=session/telegram.session
```

5. Never commit `.env` or `*.session*`. Keep chmod 600 on secrets:

```bash
chmod 600 .env session/telegram.session
```

## 2. Setup with venv + ensurepip

```bash
python3 -m ensurepip --upgrade
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 3. Examples

Public channel by username:

```bash
venv/bin/python -m src.cli --target https://t.me/someuser123 --out out --limit 100
```

Private channel link (must already be a member):

```bash
venv/bin/python -m src.cli --target https://t.me/c/123456/7 --out out --limit 50
```

Invite link (explicit opt-in join only):

```bash
venv/bin/python -m src.cli --target https://t.me/+AAAAbbbb --join --out out --limit 50
```

DM by phone or @user:

```bash
venv/bin/python -m src.cli --target +15551234567 --out out --limit 20
venv/bin/python -m src.cli --target @someuser123 --out out --limit 20
```

Filter by media type + search:

```bash
venv/bin/python -m src.cli --target @someuser123 --filter photo --search hello --out out
```

Dry-run (no bytes, writes messages.jsonl only):

```bash
venv/bin/python -m src.cli --target @someuser123 --dry-run --limit 10 --out out
```

Resume + bounded run:

```bash
venv/bin/python -m src.cli --target @someuser123 --resume --limit 100 --max-bytes 104857600 --out out
```

Filter by sender + date window + specific IDs:

```bash
venv/bin/python -m src.cli --target @someuser123 --from-user 123456 --after 2024-01-01 --before 2024-12-31 --out out
venv/bin/python -m src.cli --target @someuser123 --ids 10,11,12 --out out
```

Bounded run with timeout + verbose logging:

```bash
venv/bin/python -m src.cli --target @someuser123 --limit 100 --timeout 300 --verbose --out out
```

Takeout (opt-in only, default off):

```bash
venv/bin/python -m src.cli --target @someuser123 --takeout --out out --limit 50
```

## 4. Limits and safety notes

- 2GB per-file cap on most accounts; 4GB with Premium. Oversize files fail precheck; use `--max-bytes`.
- FloodWait: serial 1s + jitter, cap 300s; over cap or PEER_FLOOD exits 3. Back off, do not retry hot.
- chmod600: session files 0o600, downloads 0o600 files / 0o700 dirs, umask 077. World-writable out refused.
- Ban-avoidance: limit max 500, serial semaphore(1), no auto-join (`--join` default false), no takeout unless `--takeout`.
- Disk: ENOSPC precheck via statvfs; manifest UNIQUE(chat_id, msg_id) enables resume-skip without re-download.
- Manifest: `out/manifest.db` (SQLite `UNIQUE(chat_id, msg_id)`) enables resume-skip; `out/messages.jsonl` is appended per message. `--dry-run` writes `messages.jsonl` only, no bytes. `--resume` keeps `.part` offset, otherwise restarts.
- Private channels/groups: you must already be a member. Invite links (`t.me/+...`, `t.me/joinchat/...`) require explicit `--join`; without it the run exits 5 (`INVITE_NO_JOIN`). `--join` defaults to false — no auto-join.

## 5. CLI flags (verified against `venv/bin/python -m src.cli --help`)

| Flag | Default | Meaning |
| :--- | :--- | :--- |
| `--target` | (required) | `@user`, phone, numeric id, `t.me/<user>`, `t.me/c/<id>/...`, or `t.me/+...` invite |
| `--out` | `out` | Output dir (refused if world-writable, exit 4) |
| `--limit` | `500` | Message bound, clamped to max 500 |
| `--filter` | none | Media-type substring match (e.g. `photo`) |
| `--after` / `--before` | none | Date-window filters |
| `--from-user` | none | Sender-id substring filter |
| `--search` | none | Server-side text search |
| `--ids` | none | Comma-separated message ids (e.g. `10,11,12`) |
| `--max-bytes` | none | Stop after this many downloaded bytes |
| `--timeout` | none | Stop after this many seconds |
| `--dry-run` | off | Write `messages.jsonl` only, download no bytes |
| `--resume` | off | Keep `.part` files and resume from offset |
| `--join` | `false` | Opt-in join for invites/channels; no auto-join when absent |
| `--takeout` | `false` | Opt-in takeout mode; off by default |
| `--verbose` | off | `DEBUG` logging; default `INFO` |

## 6. Troubleshooting (exit codes)

- Exit 2 (auth): re-login, check `TG_PHONE` format (`+[1-9]...`), complete 2FA code prompt.
- Exit 3 (FloodWait / `PEER_FLOOD`): wait exceeds 300 s cap or flood flag raised. Back off, do not retry hot.
- Exit 4 (disk/permissions): `ENOSPC` from `statvfs` precheck means no space — free disk or lower `--limit` / `--max-bytes`. World-writable `--out` is refused — use a `0o700` dir.
- Exit 5 (resolve): `CHANNEL_PRIVATE` / not-a-participant means join the channel first, then retry. `INVITE_NO_JOIN` means re-run the same invite with `--join`. Expired/invalid invite or unknown username also exits 5 — verify the link.

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

4-character Fragment handle:

```bash
venv/bin/python -m src.cli --target @news --out out --limit 50
venv/bin/python -m src.cli --target t.me/auto --out out --limit 50
```

List account dialogs (discover groups, channels, DMs):

```bash
venv/bin/python -m src.cli --list-dialogs --dialog-filter channel --dialog-limit 50
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

Incremental sync (resumes after highest downloaded message ID per chat):

```bash
venv/bin/python -m src.cli --target @someuser123 --sync --out out
```

Skip messages below or equal to a specific message ID:

```bash
venv/bin/python -m src.cli --target @someuser123 --min-id 500 --out out
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
- FloodWait and rate limiting: serial 1s + jitter, cap 300s; `FloodWaitError` exceeding 300s or `PeerFloodError` commits manifest and exits 3. Back off, do not retry hot.
- Target formats & handles: Supports 7 target forms: `@user`, `t.me/<user>` (supporting 4–32 character handles including 4-character Fragment usernames like `@news` or `t.me/auto`), phone numbers (`+...`), numeric IDs, `t.me/c/<id>/...`, `t.me/+...`, and `t.me/joinchat/...`.
- Atomic sync checkpoint: `--sync` tracks the maximum message ID in SQLite `sync_checkpoints`. Checkpoint updates are strictly monotonic and advance only upon verified file downloads or recorded skips (never on failed downloads).
- Deduplication & part file cleanup: SHA-256 hash deduplication records aliases in `messages.jsonl` without re-downloading duplicated payload files. Partial download `.part` files are cleaned up immediately upon alias assignment or same-size skips.
- Permissions & disk guards: Session files `0o600`, download files `0o600`, directories `0o700`, umask `077`. World-writable output directory is refused (exit 4). Disk space is prechecked before writes (`statvfs`).
- Manifest: `out/manifest.db` (SQLite `UNIQUE(chat_id, msg_id)`) enables resume-skip; `out/messages.jsonl` is appended per message. `--dry-run` writes `messages.jsonl` only, no bytes. `--resume` keeps `.part` offset, otherwise restarts.
- Private channels/groups: you must already be a member. Invite links (`t.me/+...`, `t.me/joinchat/...`) require explicit `--join`; without it the run exits 5 (`INVITE_NO_JOIN`). `--join` defaults to false — no auto-join.

## 5. CLI flags (verified against `venv/bin/python -m src.cli --help`)

| Flag | Default | Meaning |
| :--- | :--- | :--- |
| `--target` | none | Target chat: `@user`, phone, numeric ID, `t.me/<user>` (4–32 chars, incl. 4-char Fragment handles), `t.me/c/<id>/...`, or `t.me/+...` invite. Required unless `--list-dialogs` is specified. |
| `--out` | `out` | Output directory (refused if world-writable, exit 4) |
| `--limit` | `500` | Message bound, clamped to max 500 |
| `--filter` | none | Media-type filter / attribute match (e.g. `photo`, `video`, `audio`, `document`) |
| `--after` / `--before` | none | Date-window filters ISO-8601 (UTC normalized) |
| `--from-user` | none | Sender ID or username substring filter |
| `--search` | none | Server-side text search query |
| `--ids` | none | Comma-separated message IDs (e.g. `10,11,12`) |
| `--max-bytes` | none | Stop after this many downloaded bytes |
| `--timeout` | none | Stop after this many seconds |
| `--dry-run` | off | Write `messages.jsonl` only, download no bytes |
| `--resume` | off | Keep `.part` files and resume; full restart resume, no false offset |
| `--min-id` | none | Skip messages with message ID `<= min-id` |
| `--sync` | off | Persist max message ID per chat to manifest DB; subsequent runs resume from this checkpoint |
| `--join` | `false` | Opt-in join for invites/channels; no auto-join when absent |
| `--takeout` | `false` | Opt-in takeout mode; off by default |
| `--list-dialogs` | off | List account dialogs (chats, channels, DMs) in a scrubbed table; `--target` not required |
| `--dialog-filter` | `all` | Dialog type filter for `--list-dialogs` (`all`, `group`, `channel`, `dm`) |
| `--dialog-limit` | `100` | Maximum number of dialogs to query and list |
| `--verbose` | off | Enable `DEBUG` logging; default `INFO` |

## 6. Troubleshooting (exit codes)

- Exit 0 (Success): All target messages processed, or dialog list successfully generated.
- Exit 1 (Configuration / Args): General error or invalid arguments (e.g. `--target` omitted when `--list-dialogs` is not given, missing `.env` variables).
- Exit 2 (Auth): Authentication error. Re-login required, verify `TG_PHONE` format (`+[1-9]...`), check `TG_API_ID` / `TG_API_HASH`, or complete 2FA code prompt.
- Exit 3 (Rate limit / `FloodWaitError` / `PeerFloodError`): Wait exceeds 300s safety cap or Telegram returns `PeerFloodError` (excessive actions or query rate on a peer). Pending database operations in `manifest.db` are safely committed before exiting. Back off and wait before retrying; do not retry hot.
- Exit 4 (Disk / Permissions): `ENOSPC` from `statvfs` precheck means insufficient disk space — free disk or lower `--limit` / `--max-bytes`. Unhandled filesystem I/O errors, or world-writable `--out` directory refused (`0o700` required).
- Exit 5 (Resolve): Target cannot be resolved or accessed. `CHANNEL_PRIVATE` / not-a-participant means join the channel first, then retry. `INVITE_NO_JOIN` means an invite link was supplied without `--join` (re-run with `--join`). Expired/invalid invite links or unknown handles (including unallocated 4-char Fragment handles) also exit 5.

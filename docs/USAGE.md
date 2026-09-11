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

Full chat history archive (no message limit):

```bash
venv/bin/python -m src.cli --target @someuser123 --no-limit --out out
# Or using --limit 0 for unlimited:
venv/bin/python -m src.cli --target @someuser123 --limit 0 --out out
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
- Uncapped downloads: The previous 500-message ceiling has been removed. Use `--no-limit` or `--limit 0` to download entire chat histories without limit. Bounded runs remain supported by specifying any positive integer (e.g. `--limit 100` or `--limit 2500`).
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
| `--limit` | `500` | Message limit (0 for unlimited) |
| `--no-limit` | `false` | Download all messages without limit (full chat history archive) |
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

## 7. Web Dashboard (TeleVault)

TeleVault features a modern, zero-build web interface powered by FastAPI and vanilla HTML5/CSS3/ES6 Dark Obsidian Glassmorphism (no Node.js or npm build step required).

### Launching the Dashboard

Start the web dashboard server:

```bash
python run_web.py
# Or via module execution:
python -m src.web
```

The launcher:
1. Generates a cryptographically secure 32-byte ephemeral startup token (`secrets.token_urlsafe(32)`).
2. Spawns your default web browser to `http://127.0.0.1:8000/?token=<token>`.
3. Starts the Uvicorn ASGI server binding locally to `127.0.0.1:8000`.

### Security Architecture

- **Ephemeral Startup Token**: Every server invocation generates an active token. All `/api/*` endpoints require the token passed via `x-auth-token` header, `Authorization: Bearer <token>`, or `?token=` query parameter.
- **CSRF & Strict Origin Validation**: State-changing requests (`POST`, `PUT`, `DELETE`, `PATCH`) validate the `Origin` header against `127.0.0.1`, `localhost`, and `[::1]` matching the active server port.
- **PII Masking**: Sensitive identity values such as telephone numbers are masked (e.g. `+1***4567`) across status and auth payloads.
- **WebSocket Session Lifecycle & Token Guard**: Real-time telemetry (`/ws/live`) authenticates via ephemeral token on handshake. If the token is missing, invalid, or expired, the socket terminates immediately with code `1008` (Policy Violation). The Web UI halts auto-reconnect loops, displays "Session Expired", and directs the user to the active URL printed in the server terminal.

### Decoupled JobManager & Mutex

- **Decoupled Lifecycle**: Download execution runs inside a detached `asyncio.Task` managed by `JobManager`. Downloads survive browser tab closures, page refreshes, and transient connection drops.
- **Singleton Execution Mutex**: An asynchronous lock permits only one active download job at a time. Attempting to start a concurrent job returns HTTP 409 Conflict (`JobConflictError`), preventing database lock contention or disk corruption.
- **State & Terminal History**: The `JobManager` maintains a circular buffer of the last 1000 log entries and an execution snapshot, instantly streaming state upon client reconnection.

### No Limit (All Messages) & Streaming Shimmer HUD

- **Interactive Switch**: The download configuration panel includes a "No Limit (All)" toggle switch. When enabled, it disables the numeric limit input and sets the placeholder to `∞ All Messages (unlimited)`.
- **Indeterminate Shimmer HUD**: For unconstrained downloads (`limit=None`), TeleVault displays `Progress: Uncapped (Streaming)` with an animated electric cyan to neon purple shimmer gradient, while continuing to stream live speeds (MB/s), byte totals, and completed counts in real-time.

### Media Gallery Quick Actions & Streaming Player

- **One-Click Quick Actions**: Every gallery media card displays floating quick-action symbols on hover:
  - **Open in Browser** (external link icon): Opens the media stream in a new browser tab (`target="_blank"`).
  - **Download File** (download icon): Directly triggers attachment download using `?download=1`.
  - **Missing File Indicator**: Shows an alert badge (`⚠️`) if a manifest entry's underlying file is missing from disk.
- **Theater Lightbox & Audio Player**: Clicking any media card opens a modal theater viewer featuring:
  - **Seekable Video & Audio Player**: Inline HTML5 video player and dedicated HTML5 audio player (`<audio id="theaterAudio" controls autoplay>`) for music and voice notes.
  - **Image & Document Inspection**: High-resolution image inspection and document previews.
  - **Dual Action Controls**: Prominent "Open in Tab" and "Download File" buttons.
  - **Resource Cleanup**: Halts active media playback and detaches element source (`src`) on modal close to prevent unnecessary background HTTP 206 chunk buffering.
- **RFC 5987 Streaming Headers & Attachment Mode**: The media streaming endpoint (`/api/media/stream/{chat_id}/{msg_id}`) emits RFC 5987 UTF-8 encoded filename headers (`filename*=UTF-8''...`) with ASCII fallbacks to support international Unicode filenames, and respects `?download=1` to switch disposition from `inline` to `attachment`.

### REST & WebSocket API Reference

| Endpoint | Method / Protocol | Description |
| :--- | :--- | :--- |
| `/api/status` | `GET` | Health check: session file presence, masked phone, output directory |
| `/api/auth/me` | `GET` | Current Telegram authorization status and authenticated user info |
| `/api/auth/qr` | `GET` | Fetch QR login token, scan URL, and expiry timestamp |
| `/api/auth/phone/send_code` | `POST` | Request SMS verification code for an E.164 phone number |
| `/api/auth/phone/sign_in` | `POST` | Submit SMS verification code and phone hash |
| `/api/auth/2fa` | `POST` | Submit 2FA cloud password if account requires two-step verification |
| `/api/dialogs` | `GET` | Retrieve account dialogs with `limit`, `kind` (`channel`, `group`, `dm`), and `search` filters |
| `/api/resolve` | `POST` | Resolve target entity metadata and check membership before downloading |
| `/api/download/start` | `POST` | Launch background download task with configured options |
| `/api/download/cancel` | `POST` | Gracefully cancel active download task |
| `/api/download/state` | `GET` | Fetch current job execution snapshot and recent log history |
| `/api/media` | `GET` | Paginated query of downloaded files from `manifest.db` with MIME classification |
| `/api/media/stream/{chat_id}/{msg_id}` | `GET` | RFC 7233 HTTP 206 partial content byte-range streaming; supports RFC 5987 Unicode filenames and `?download=1` attachment disposition |
| `/api/system/storage` | `GET` | Disk space telemetry via `statvfs` (total, free, used %, low-space flag) |
| `/ws/live` | `WebSocket` | Real-time event stream: progress %, download speed (MB/s), active files, logs |


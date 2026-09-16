# TelegramFilesDownloader

[![build](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/example/TelegramFilesDownloader/actions) [![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/) [![license](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

> Hardened Telethon-based Telegram media downloader with safe defaults, resume, audit manifest, and the **TeleVault Modern Web Dashboard**.

See [docs/USAGE.md](docs/USAGE.md) for full flags, safety limits, web API reference, and troubleshooting.

## Features

### TeleVault Web Dashboard
- **Zero-Build Architecture**: FastAPI backend + Vanilla HTML5/CSS3/ES6 Dark Obsidian Glassmorphism UI — zero Node.js/npm dependencies required.
- **Pure-Python In-Memory SVG QR Code Engine**: Built-in SVG QR code generation (`qrcode.image.svg.SvgPathImage`) without PIL/Pillow or C-extension image dependencies, guaranteeing pure in-memory rendering and zero external HTTP telemetry or third-party exfiltration of session credentials.
- **WCAG 2.2 AA Obsidian Dark Glassmorphism Design System**: Desktop-first UI featuring an `#0B0E14` Obsidian canvas with frosted glass panels and high-contrast glowing accents:
  - **High-Contrast Typography**: Rigorously validated to meet or exceed WCAG 2.2 AA contrast ratios (`>= 4.5:1`) across all text labels, metrics, and interactive controls.
  - **Visible Focus Rings**: Prominent 2px cyan (`--accent-cyan`) `:focus-visible` outline with 2px offset on buttons, inputs, links, and media cards.
  - **Touch & Click Target Compliance**: Minimum 44x44px target sizes on interactive action buttons, pagination controls, toggle switches, and lightbox navigation triggers.
  - **Modal Keyboard Focus Traps**: Accessible focus trapping (Tab / Shift+Tab looping) preventing focus escape during 2FA cloud password authentication and Theater Lightbox inspection.
  - **Keyboard Navigation**: Complete keyboard support with Enter/Space card triggers, quick dialog search jump (`/`), modal dismissal (`Escape`), and media browsing (`ArrowLeft`/`ArrowRight`).
- **Real-Time WebSocket Telemetry HUD (`/ws/live`)**:
  - **Animated SVG Speedometer Arc Gauge**: Dynamic SVG speed arc with real-time dashoffset calculations calibrated up to 25 MB/s.
  - **Live Transfer Rate & Multi-Hour ETA**: Real-time download throughput in MB/s with intelligent multi-hour ETA calculation (`Xh Ym Zs`).
  - **Active File Indicator**: Live display of currently downloading file name with a full relative path tooltip on hover.
  - **Circular 500-Line Terminal with Reconnect Replay**: High-throughput monospace streaming log window maintaining a sliding 500-line circular buffer, automatic bottom-pinning scroll lock, and instant state & log replay on WebSocket reconnect.
  - **Visual Progress & Streaming Shimmer**: Indeterminate animated gradient shimmer for uncapped chat streams and determinate percentage progress bars for bounded runs.
- **Paginated Media Gallery & Theater Lightbox**:
  - **SQLite SQL-Pushdown Kind Filtering**: Instant filtering across media kinds (`Video`, `Photo`, `Audio`, `Document`) pushed directly down to SQLite queries (`WHERE mime LIKE ... OR relpath LIKE ...`) with `LIMIT`/`OFFSET` pagination to avoid full-table scans.
  - **Responsive Theater Lightbox**: Fullscreen immersive media viewer for inline video playback, audio listening, and high-resolution photo viewing with `ArrowLeft`/`ArrowRight` navigation, one-click SHA-256 clipboard copy, and direct RFC 7233 stream access.
  - **Chat Explorer & Target Arming**: Searchable account dialog picker (channels, groups, DMs) with 1-click target arming into the download configuration panel.
  - **System Storage Health Monitor**: Live disk capacity telemetry via `os.statvfs` with dynamic low-space warning alerts (<10% threshold).
  - **Decoupled JobManager Resilience**: Background task architecture completely detached from browser sessions; downloads survive browser tab closures, page reloads, and network interruptions under singleton mutex protection.
- **Direct Browser File Extractor & Streaming (`POST /api/chat/scan` & `GET /api/direct/download`)**:
  - **Pre-Download Inspection**: Click "Extract & Select Files (Browser)" before starting downloads to scan and inspect media items directly from any chat.
  - **Zero Server Disk Footprint**: Media chunks stream directly from Telegram MTProto through FastAPI into the client browser, completely bypassing server disk storage (`/out`) with bounded 512 KB memory buffers.
  - **Selective Batch Downloads**: Checkbox selection for individual or all files, live instant search, kind filtering pills (`All`, `Video`, `Photo`, `Audio`, `Docs`), total selected size calculation, and a 750ms staggered queue engine to avoid browser popup blockers.
- **On-The-Fly Streaming ZIP Engine (`POST /api/direct/zip/prepare` & `GET /api/direct/zip/stream/{ticket}`)**:
  - **Single Browser Download Confirmation**: Package hundreds of files into a single `.zip` archive with exactly one browser confirmation, eliminating the friction of dozens or hundreds of individual download popups.
  - **Zero Server Disk Footprint**: Streams directly from Telegram MTProto through FastAPI into the browser using PKWARE Bit 3 streaming data descriptors (`0x08074b50`), generating the archive on-the-fly with constant $O(1)$ 512 KB memory buffer and 0 bytes written to server disk (`/out` and `/tmp`).
  - **Exact Determinate Progress**: Two-phase ticketing protocol (`POST /prepare` $	o$ `GET /stream/{ticket}`) pre-calculates the exact byte length for `Content-Length`, ensuring native browser progress bars and ETAs.
  - **Cross-Platform Compatibility**: Full PKWARE compliance with UTF-8 filename encoding (Bit 11 flag `0x0808`), Unix file attributes (`0o644`), automatic collision deduplication (`{name}_{msg_id}.ext`), and 32-bit local headers guaranteed to open cleanly in Windows Explorer, macOS Archive Utility, and Linux.

### Hardened Core & Security
- **Multi-Tenant Architecture & Visitor Session Isolation**: Public visitors to `https://televault.madhudadi.in/` land directly on the dashboard without requiring an administrative master access token. Each visitor is assigned an isolated 256-bit `televault_session` cookie (`HttpOnly; SameSite=Lax; Path=/`), a sandboxed download directory (`out/sessions/<session_id>`), and an isolated Telethon session (`session/<session_id>.session`) strictly path-jailed against directory traversal.
- **Bounded LRU Pool & Idle TTL Sweeper**: `SessionManager` manages a bounded pool of up to 25 concurrent active Telethon clients with automatic 15-minute idle TTL background reaping (preserving active downloads) and HTTP 503 capacity defense.
- **Telegram Account Lifecycle & In-Browser Logout**: Public visitors log in via Phone SMS or QR code directly in the browser. Authenticated sessions show user profile in the header with an accessible `Log Out` button (`POST /api/auth/logout`) that cleanly terminates the Telethon session, unlinks `.session` storage, and resets client state.
- **Concurrency & SQLite WAL Hardening**: Decoupled web server daemon lock (`session/.televault_server.pid`) permits simultaneous CLI executions. SQLite manifest and Telethon session databases operate in WAL mode (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000; PRAGMA synchronous=NORMAL;`).
- **Master-Token Precedence & Admin Access Entry Points**: Administrative master token authentication (`X-Auth-Token` or `?token=`) retains precedence for headless administration, status monitoring (`/api/status`), and automated CI pipelines. Administrators navigating to the bare dashboard can click **"Admin Access"** in the footer or authenticate with `?token=<master_token>`.
- **Single-Instance Process Locking**: Session-scoped lockfile engine (`.{stem}.pid`) with `os.O_CLOEXEC`, non-blocking flock deadline loop (4.0s timeout), and phantom inode validation (`st_dev` and `st_ino` verification on both acquisition and unlinking to protect against stale descriptor unlinking). Features PID recycling checks via `/proc/{pid}/cmdline` and `ps`, zombie detection (`State: Z`), and clean SIGTERM-to-SIGKILL termination of obsolete instances.
- **Persistent Master Token & In-Browser Authentication**: Automatic token persistence to `data/.token` (`chmod 600`) or environment override (`TELEVAULT_TOKEN`), timing-safe constant-time SHA-256 pre-hashed verification, and an in-browser Access Token Modal (`#tokenModal`) accessible via the header status pill (`#tokenPill`) to enter/update master tokens without modifying URL parameters.
- **Sliding-Window Auth Rate Limiting**: Max 10 failed auth attempts per 60s per client IP (returning HTTP 429 with `Retry-After`), reverse proxy `X-Forwarded-For` client IP tracking, and unconditional bypass for Docker health probes (`/api/health`).
- **RFC 7233 Byte-Range Streaming & Media Sandbox**: On-demand HTTP 206 Partial Content streaming with chunked byte-range parsing, `Accept-Ranges: bytes`, and RFC 5987 UTF-8 encoded `Content-Disposition` headers, protected by defense-in-depth isolation:
  - `Content-Security-Policy: sandbox; default-src 'none'; media-src 'self'; img-src 'self'`
  - `X-Content-Type-Options: nosniff`
  - Output directory containment strictly enforcing that media requests cannot traverse outside the configured output directory.
- **Robust Telegram Resolver**:
  - **4-Character Fragment Handle Support**: Extended regex `[A-Za-z0-9_]{4,32}` supporting 4-character auctioned Fragment handles (e.g. `@news`, `t.me/auto`).
  - **Invite Links with `access_hash` Preservation**: Unpacks invite links (`t.me/+...`, `t.me/joinchat/...`) via `CheckChatInviteRequest` and `ImportChatInviteRequest`, maintaining full entity metadata and `access_hash` integrity without losing authorization.
  - **Positive Channel ID Fallback**: Gracefully resolves positive channel IDs (e.g. `123456789`) by testing `-100{id}` channel prefix transforms and `PeerChannel` resolution.
  - **7 Target Forms Supported**: Full support for `@user`, `t.me/user`, phone numbers (`+...`), numeric IDs, `t.me/c/...`, `t.me/+...`, and `t.me/joinchat/...`.
  - **Membership Gate**: Verifies channel/group participation before download; strictly refuses silent auto-joining without explicit `--join`.
- **Uncapped & Bounded Downloads**: Download without arbitrary message caps via `--no-limit` (or `--limit 0`), or bounded batching with safe defaults.
- **Rate Limit & Flood Safety**: Serial requests with 1s + jitter delays, 300s FloodWait cap, and clean exit code `3` on `FloodWaitError` (> 300s) or `PeerFloodError`.
- **SQLite Manifest Audit & Resumption**: Monotonic sync checkpoints (`--sync`, `--min-id`) in `out/manifest.db` with `UNIQUE(chat_id, msg_id)` and SHA-256 deduplication linking identical payloads as aliases.
- **Filesystem Hardening**: Restrictive `chmod 600` on secrets/files, `0o700` directory trees, `umask 077`, atomic replaces with directory `fsync`, and automatic `.part` cleanup on aliases and skips.

### Production Deployment & CI/CD
- **Hardened Multi-Stage Docker Container**: Production-ready Python 3.12 runner (`Dockerfile`) with least-privilege non-root execution (`madhu:madhu` UID `1000:1000`), container health checks, and 30s graceful teardown.
- **Docker Compose Orchestration**: Configured in `docker-compose.yml` with host loopback mapping `127.0.0.1:8200:8000` (container listens on 8000, mapped to host 8200), mounting persistent volumes for `./session`, `./data`, and `./out`.
- **Nginx Reverse Proxy (Port 8200)**: Hardened production configuration (`deploy/nginx/televault.conf`) with automatic HTTP->HTTPS redirect, TLS 1.2/1.3 ciphers, unbuffered proxy streaming (`proxy_buffering off;`) for direct Telegram MTProto and ZIP downloads, and persistent 24-hour WebSocket upgrades (`/ws/`).
- **Automated GitHub Actions CI/CD with Rollback**: Automated testing gate (Ruff, MyPy, Pytest) and SSH deployment pipeline (`.github/workflows/deploy.yml`) to `/opt/televault` with 30s automated health polling (`http://127.0.0.1:8200/api/health`) and instant zero-downtime rollback to the previous commit and container image on failure.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full deployment instructions and runbooks.

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

### Launch TeleVault Web Dashboard

Start the web dashboard (generates or loads the persistent master security token and opens your browser automatically):

```bash
python run_web.py
# Or via module entrypoint:
python -m src.web
```

Upon first boot, a master token is persisted to `data/.token` (`chmod 600`) and output in the startup banner. On subsequent boots, the existing token is preserved and reused. Public visitors logging into Telegram no longer require a server master access token, landing directly on the dashboard with isolated, unblocked visitor sessions. Administrators navigating to the bare dashboard can click the **"Admin Access"** link in the footer or authenticate directly with `?token=<master_token>` (or `X-Auth-Token` header), which displays the active token pill in the header.

### Or Run via CLI

First run (public channel preview):

```bash
venv/bin/python -m src.cli --target @someuser123 --out out --limit 20
```

## CLI examples

| Use case | Command |
| :--- | :--- |
| Public channel | `venv/bin/python -m src.cli --target https://t.me/someuser123 --out out --limit 100` |
| 4-char Fragment handle | `venv/bin/python -m src.cli --target @news --out out --limit 50` |
| Full chat archive (No Limit) | `venv/bin/python -m src.cli --target @someuser123 --no-limit --out out` |
| List account dialogs | `venv/bin/python -m src.cli --list-dialogs --dialog-filter channel --dialog-limit 50` |
| Incremental sync | `venv/bin/python -m src.cli --target @someuser123 --sync --out out` |
| Private invite with opt-in join | `venv/bin/python -m src.cli --target https://t.me/+AAAAbbbb --join --out out --limit 50` |
| DM by phone or handle | `venv/bin/python -m src.cli --target @someuser123 --out out --limit 20` |
| Dry-run, no bytes | `venv/bin/python -m src.cli --target @someuser123 --dry-run --limit 10 --out out` |
| Resume bounded run | `venv/bin/python -m src.cli --target @someuser123 --resume --limit 100 --max-bytes 104857600 --out out` |

Full flag matrix, exit codes (2 auth, 3 flood / peer flood, 4 disk/perm, 5 resolve), and limits: [docs/USAGE.md](docs/USAGE.md).

## Project structure

```text
src/
  __init__.py
  auth.py           # session bootstrap
  cli.py            # argparse entrypoint (tg-dl)
  config.py         # .env loading, out-dir guards
  dialogs.py        # account dialog table formatting
  downloader.py     # bounded/unlimited fetch + write path
  filesafe.py       # chmod 600/700, umask 077, atomic fsync
  instance_lock.py  # single-instance flock, O_CLOEXEC, phantom inode guards
  resolver.py       # 7-form target parser, Fragment handles, invite resolver
  store.py          # SQLite manifest + jsonl log + sync checkpoints
  web/              # TeleVault web dashboard
    app.py          # FastAPI application & lifespan
    client_helpers.py # Master-token precedence & tenant client resolution
    job_manager.py  # Decoupled download job runner & state machine
    qr.py           # Pure-Python in-memory SVG QR code engine
    routes_direct.py # Browser file extraction & streaming ZIP engine
    routes_jobs.py  # Download job control & WebSocket /ws/live endpoint
    routes_media.py # RFC 7233 byte-range streaming, gallery pagination, storage
    routes_tg.py    # Telegram auth wizard, dialogs explorer, logout
    security.py     # Master token validation, sliding rate limiting, Origin checks
    session_manager.py # Bounded LRU pool, client lifecycle, idle TTL sweeper
    session_security.py # Session ID validation, canonical path jailing, cookie helpers
    static/         # Obsidian Glassmorphism UI (HTML5, CSS3, ES6)
docs/
  USAGE.md          # full CLI reference
tests/              # Comprehensive test suite
```

## Version history

- **Multi-Tenant Session Isolation & Public Dashboard Landing**: Public visitors land directly on the dashboard without requiring an administrative master access token. Dynamic 256-bit visitor session cookies (`televault_session`), path-jailed tenant sessions (`session/<session_id>.session`) and download directories (`out/sessions/<session_id>`); bounded LRU client pool (`SessionManager`, max 25 active clients) with 15-minute idle TTL sweeper and HTTP 503 capacity protection; in-browser account lifecycle with interactive logout (`POST /api/auth/logout`) and unlinking; decoupled server daemon lock (`session/.televault_server.pid`) allowing simultaneous CLI runs; SQLite manifest and session WAL hardening (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000; PRAGMA synchronous=NORMAL;`).
- **TeleVault UI/UX Modernization & Security Hardening**: Pure-Python in-memory SVG QR engine (zero HTTP exfiltration); WCAG 2.2 AA Obsidian Dark Glassmorphism design system (>= 4.5:1 contrast, focus rings, 44x44px targets, focus traps, Enter/Space card triggers); real-time WebSocket telemetry HUD with animated SVG speedometer arc gauge, live transfer rate (MB/s), multi-hour ETA calculation, active file indicator with full path tooltip, and circular 500-line auto-scrolling terminal logs with reconnect replay; paginated media gallery with SQLite SQL-pushdown kind filtering and responsive Theater Lightbox; single-instance process locking with `O_CLOEXEC`, flock deadline, and phantom inode validation; RFC 7233 byte-range streaming with Content-Security-Policy sandbox and `X-Content-Type-Options: nosniff`; robust resolver with 4-char Fragment handles, invite link `access_hash` preservation, and positive channel ID fallback.
- **Uncapped Downloads & No Limit Mode**: Removed 500-message ceiling across CLI and Web UI. Added `--no-limit` (and `--limit 0`) CLI flags for full chat history archiving, interactive "No Limit (All)" toggle switch in Web UI, and indeterminate streaming progress shimmer animation.
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

- **Single-Instance Protection**: Enforced session-scoped PID lockfile with `O_CLOEXEC` and phantom inode verification preventing concurrent session conflicts and database corruptions.
- **Zero-Exfiltration QR Auth**: Pure-Python in-memory SVG QR code engine operating strictly locally without external HTTP calls or third-party telemetry.
- **Media Streaming Sandbox**: Strict `Content-Security-Policy: sandbox; default-src 'none'; media-src 'self'; img-src 'self'` and `X-Content-Type-Options: nosniff` on all media streaming responses.
- **Path Traversal Guards**: Strict resolution checks verifying that all served media paths remain contained within the configured output directory.
- **Master Token Authentication & Sliding-Window Rate Limiting**: Timing-safe constant-time SHA-256 pre-hashed token verification (`hmac.compare_digest`), persistent master token storage in `data/.token` (`chmod 600`) or `TELEVAULT_TOKEN` environment override, sliding-window auth rate limiting (max 10 failed attempts per 60s per client IP returning HTTP 429 with `Retry-After`), and strict WebSocket Origin header verification.
- **Local Secret Isolation**: Never commit `.env` or `*.session*`; both are git-ignored. Keep secrets private: `chmod 600 .env session/telegram.session` and `umask 077`.
- **Sanitized Issue Reporting**: Use placeholders (`your_api_id_here`, `+15551234567`) in issues and examples.

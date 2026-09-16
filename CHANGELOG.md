# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Multi-Tenant Architecture & Visitor Session Isolation**: Public visitors to `https://televault.madhudadi.in/` land directly on the dashboard without requiring an administrative master access token. Each visitor is assigned an isolated 256-bit `televault_session` cookie (`HttpOnly; SameSite=Lax; Path=/`), a sandboxed download directory (`out/sessions/<session_id>`), and an isolated Telethon session (`session/<session_id>.session`) strictly path-jailed against directory traversal.
- **Unblocked Visitor Session Workflows**: Public visitors can authenticate directly via Phone SMS or QR code. All dashboard features—account dialog inspection, media gallery browsing, RFC 7233 byte-range streaming, browser file extraction, and streaming ZIP downloads—operate seamlessly without requiring server master tokens.
- **Admin Access Entry Points**: Added a dedicated **"Admin Access"** button in the dashboard footer (`openTokenModal()`) and direct `?token=<master_token>` URL parameter support for administrators navigating to the bare dashboard. Header token pill (`#tokenPill`) is dynamically hidden for visitors and displays `Token: Set` when an administrative token is active.
- **In-Browser Account Lifecycle & Logout**: Authenticated visitor sessions display the user profile in the header with an accessible `Log Out` button (`POST /api/auth/logout`) that cleanly terminates the Telethon session, unlinks `.session` storage, and resets client state.
- **Bounded LRU Client Pool & Idle Sweeper**: `SessionManager` manages a bounded pool of up to 25 concurrent active Telethon clients with automatic 15-minute idle TTL background reaping (preserving active downloads) and HTTP 503 capacity defense.
- **Decoupled Server Daemon Lock**: Server daemon PID lock (`session/.televault_server.pid`) operates independently from CLI executions, permitting concurrent CLI operations while the web dashboard is running.
- **SQLite WAL Mode Hardening**: Manifest audit database and Telethon session databases operate in WAL mode (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000; PRAGMA synchronous=NORMAL;`) for safe concurrent multi-tenant reads and writes.
- **Direct Streaming Primary Call-To-Action**: Elevated **"Browse & Download Files"** (`openFileSelectorModal()`) as the primary visual call-to-action in Module 3, routing visitors directly into in-memory browser MTProto chunk streaming and on-the-fly streaming ZIP packaging with zero server disk footprint (`/out`). Server-side disk sync (`startDownload()`) was demoted to secondary action **"Server Sync"**.
- **Automated Regression Test**: Added `test_direct_streaming_ui_and_storage_pill_removal` in `tests/test_web_api.py` asserting `#storagePill` DOM absence, `updateStorage` short-circuit integrity, direct streaming empty state text, and primary button labels.

### Changed
- Refactored media streaming and ZIP download URL generators to avoid attaching empty `?token=` query parameters when running in visitor session mode.
- Relaxed token requirements on `/api/dialogs`, `/api/direct/scan`, `/api/direct/download`, and `/api/direct/zip` endpoints in favor of visitor session cookie validation with master-token precedence.
- **Host Disk Storage Pill Removal**: Removed host partition disk monitor pill (`Disk: -- / --` / `#storagePill`) from the dashboard header, eliminating redundant `/api/system/storage` polling overhead for public visitors. `updateStorage()` in `app.js` now immediately short-circuits if `#storagePill` is not present in the DOM.
- **Media Gallery Empty State**: Updated media gallery empty state messaging to explain direct browser streaming and guide visitors to use the File Extractor above.

---

## [v0.5.0] - 2026-03-15

### Added
- Direct browser file extractor and streaming (`POST /api/chat/scan` and `GET /api/direct/download`).
- On-the-fly streaming ZIP engine (`POST /api/direct/zip/prepare` and `GET /api/direct/zip/stream/{ticket}`) with PKWARE Bit 3 streaming data descriptors and $O(1)$ 512 KB memory buffer.
- Persistent master token generation and file storage in `data/.token` (`chmod 600`) with sliding-window auth rate limiting.

---

## [v0.4.0] - 2026-03-10

### Added
- TeleVault Modern Web Dashboard with WCAG 2.2 AA Obsidian Dark Glassmorphism design system.
- Pure-Python in-memory SVG QR code engine (`qrcode.image.svg.SvgPathImage`) with zero external HTTP telemetry or exfiltration.
- Real-time WebSocket telemetry HUD (`/ws/live`) with SVG speedometer arc gauge, live transfer rate (MB/s), and circular 500-line terminal logs with reconnect replay.
- Paginated media gallery with SQLite SQL-pushdown kind filtering and responsive Theater Lightbox.

---

## [v0.3.2] - 2026-03-05

### Added
- Hardened single-instance flock engine with `os.O_CLOEXEC`, phantom inode validation (`st_dev` and `st_ino`), PID recycling checks, and zombie process detection.

---

## [v0.3.1] - 2026-03-01

### Added
- 4-character auctioned Fragment handles support (`@news`, `t.me/auto`).
- Private chat invite unpacking with `access_hash` preservation.
- Monotonic sync checkpoints in `out/manifest.db`.
- Clean exit code `3` on `FloodWaitError` (> 300s) and `PeerFloodError`.

---

## [v0.3.0] - 2026-02-20

### Added
- Incremental sync with `--sync` and `--min-id`.
- SHA-256 content deduplication with alias records in SQLite manifest.
- Rich metadata tracking in `messages.jsonl` with UTC ISO-8601 timestamps.

---

## [v0.2.0] - 2026-02-10

### Added
- Account dialog discovery (`--list-dialogs`, `--dialog-filter`, `--dialog-limit`) without leaking phone numbers or credentials.

---

## [v0.1.0] - 2026-02-01

### Added
- Initial hardened Telethon downloader core.
- Safe limits (500-message ceiling, FloodWait handling).
- File safe utilities (`chmod 600`/`0o700`, `umask 077`, atomic replace with fsync).

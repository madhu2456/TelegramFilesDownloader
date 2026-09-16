# TeleVault Production Deployment Runbook

This runbook covers deploying TeleVault on a Linux production server under `televault.madhudadi.in`.
The primary and recommended deployment strategy uses **Docker & Docker Compose** orchestrated with **automated GitHub Actions CI/CD**, backed by an **Nginx reverse proxy** with automated SSL and unbuffered streaming. A bare-metal **systemd** service guide is retained as an alternative.

---

## 1. Architecture & Prerequisites

### Architecture Overview
- **Application Stack**: Containerized FastAPI + Uvicorn server running Python 3.12 (`Dockerfile`, `docker-compose.yml`).
- **Container Host Port Mapping**: `127.0.0.1:8200:8000` (Internal container port `8000` exposed strictly to host loopback `127.0.0.1:8200`).
- **Reverse Proxy**: Nginx at `/etc/nginx/conf.d/televault.conf` terminating TLS (HTTPS) on port 443 and proxying upstream to `http://127.0.0.1:8200`.
- **Multi-Tenant Public Landing**: Public reverse proxies (Nginx/Cloudflare) forward requests directly to the web dashboard without needing hardcoded master tokens for end users. Visitors are assigned an isolated 256-bit session cookie (`televault_session`), path-jailed `.session` storage, and per-tenant download directories (`out/sessions/<session_id>`).
- **Streaming Pipeline**: Unbuffered proxy streaming (`proxy_buffering off;`) on `/api/direct/` and `/api/media/stream/` for zero-disk direct Telegram MTProto browser downloads and streaming ZIP archives.
- **Real-Time Telemetry**: WebSocket proxy upgrade on `/ws/` for live HUD metrics and log replay.
- **CI/CD Automation**: GitHub Actions pipeline (`.github/workflows/deploy.yml`) running code hardening gates (Ruff, MyPy, Pytest) and SSH production deployment with automated health check polling and zero-downtime rollback.
- **Security Model**: Non-root container process running as UID/GID `1000:1000` (`madhu:madhu`), strict file permissions (`0600`/`0700`), umask `077`, and single-instance session flock locking.

### Host Prerequisites
- Linux Server (Ubuntu 22.04+, Debian 12+, or modern enterprise Linux).
- **Docker Engine 24.0+** and **Docker Compose v2** (`docker compose`).
- **Nginx** and **Certbot** (`python3-certbot-nginx`).
- Inbound firewall (UFW / iptables / security group) allowing ports `80` (HTTP) and `443` (HTTPS). Internal port `8200` must remain bound strictly to `127.0.0.1` and never exposed publicly.
- Public DNS A Record:
  ```text
  televault.madhudadi.in.   IN   A   <YOUR_SERVER_PUBLIC_IP>
  ```

---

## 2. Primary Deployment: Docker & Docker Compose

Production deployments are hosted in `/opt/televault`.

### Step 2.1: Directory Setup & Host Permissions
Create `/opt/televault` owned by the deployment user (UID `1000:1000`):

```bash
sudo mkdir -p /opt/televault
sudo chown -R 1000:1000 /opt/televault
git clone https://github.com/madhu2456/TelegramFilesDownloader.git /opt/televault
cd /opt/televault
```

Pre-create the required persistent host directories with correct ownership and restrictive permissions:

```bash
# Pre-create stateful directories matching container mounts
mkdir -p session data out out/sessions
chown -R 1000:1000 session data out
chmod 700 session data out
```

Multi-Tenant Directory Layout:
```text
/opt/televault/
├── session/                      # chmod 700 - Multi-tenant Telegram MTProto sessions
│   ├── .televault_server.pid    # Web daemon PID lock (decoupled from CLI executions)
│   ├── <session_id>.session     # Isolated visitor MTProto SQLite sessions
│   └── <session_id>.session-wal # SQLite WAL journal
├── data/                         # chmod 700 - Application state & persistent tokens
│   └── .token                   # chmod 600 - Master admin token
└── out/                          # chmod 700 - Media storage and manifests
    ├── manifest.db               # SQLite manifest audit log (WAL mode)
    └── sessions/                 # Per-tenant sandboxed download directories
        └── <session_id>/         # Isolated visitor downloaded media & archives
```

Persistent Volume Mounts:
- `./session:/app/session`: Preserves individual visitor `.session` SQLite databases, WAL journals, and daemon locks across container restarts.
- `./data:/app/data`: Local application state, caches, auxiliary databases, and the persisted master access token (`/app/data/.token`, `chmod 600`).
- `./out:/app/out`: Holds root downloads, per-tenant isolated media archives (`out/sessions/<session_id>/`), and the SQLite audit log (`manifest.db`).

### Step 2.2: Configure Environment Variables (`.env`)
Create the production `.env` file in `/opt/televault/.env`:

```bash
cat << 'EOF' > /opt/televault/.env
# ==============================================================================
# Telegram MTProto Credentials (from https://my.telegram.org)
# ==============================================================================
TG_API_ID=your_api_id_here
TG_API_HASH=your_api_hash_here
TG_PHONE=+15551234567
TG_SESSION=/app/session/telegram.session

# ==============================================================================
# TeleVault Server Configuration
# ==============================================================================
# Master token configuration (optional static override):
# - Set TELEVAULT_TOKEN to enforce a static master access token across all requests.
# - Leave blank/unset to let TeleVault automatically generate a 32-byte cryptographic
#   token on first boot, persisting to `/app/data/.token` (chmod 600) across restarts.
TELEVAULT_TOKEN=your_secure_random_token_here

# TeleVault internal port and binding
TELEVAULT_PORT=8000
TELEVAULT_BIND_HOST=0.0.0.0
TELEVAULT_ALLOWED_HOSTS=televault.madhudadi.in,localhost,127.0.0.1
TELEVAULT_FORWARDED_ALLOW_IPS=127.0.0.1,172.16.0.0/12,*
TELEVAULT_HEADLESS=1
TELEVAULT_OUT_DIR=/app/out
EOF

# Restrict secret file permissions
chmod 600 /opt/televault/.env
```

### Step 2.3: Multi-Tenant Public Access, Master Token Persistence & `docker-compose.yml` Configuration

TeleVault provides multi-tenant public access alongside administrative master-token authentication:

1. **Public Visitor Landing & Session Isolation**:
   - Public visitors connecting through reverse proxies (Nginx or Cloudflare at `https://televault.madhudadi.in`) land directly on the dashboard without needing pre-shared master tokens or passwords.
   - TeleVault automatically issues an isolated 256-bit `televault_session` cookie (`HttpOnly; SameSite=Lax; Path=/`).
   - Each visitor authenticates their own Telegram account via Phone SMS or QR code. Telethon sessions are persisted to `/app/session/<session_id>.session` and downloads are quarantined to `/app/out/sessions/<session_id>/`.
   - The in-memory `SessionManager` bounds concurrency to 25 active clients and automatically disconnects idle sessions after 15 minutes, preserving active download tasks.

2. **Administrative Master-Token Precedence (`./data:/app/data` & `TELEVAULT_TOKEN`)**:
   - Master token authentication (`X-Auth-Token` header or `?token=` parameter) retains strict precedence over visitor sessions. When a master token is supplied:
     - API requests operate against the root session (`TG_SESSION`) and root output directory (`TELEVAULT_OUT_DIR`).
     - Status monitoring (`/api/status`), administrative operations, and automated CI pipelines (`.github/workflows/deploy.yml`) continue executing without visitor interference.
   - On first startup (when `TELEVAULT_TOKEN` is unset or empty), TeleVault generates a master token to `/app/data/.token` (`chmod 600`) or respects the `TELEVAULT_TOKEN` environment override. Administrators navigating to the bare dashboard can click **"Admin Access"** in the footer or visit with `?token=<master_token>` to authenticate and reveal the master token status pill.

### Step 2.4: Launch Container Stack
Start the TeleVault stack using Docker Compose:

```bash
cd /opt/televault
docker compose build --pull
docker compose up -d --remove-orphans
```

Verify running status and container healthcheck:

```bash
# Check container status and health (healthy indicates passing /api/health)
docker compose ps

# Follow live container logs
docker compose logs -f televault
```

---

## 3. Nginx Reverse Proxy Setup

Nginx acts as the front-facing reverse proxy handling TLS encryption, HTTP/2 termination, and unbuffered media chunk streaming.

### Step 3.1: Obtain SSL Certificates via Certbot
```bash
sudo apt update && sudo apt install -y certbot python3-certbot-nginx

# Obtain certificates for televault.madhudadi.in
sudo certbot certonly --standalone -d televault.madhudadi.in --preferred-challenges http
```

Certificates are typically stored at:
- `/etc/letsencrypt/live/televault.madhudadi.in/fullchain.pem`
- `/etc/letsencrypt/live/televault.madhudadi.in/privkey.pem`
*(Note: If using dedicated domain certificates at `/etc/ssl/madhudadi.in/`, ensure paths match in Nginx config).*

### Step 3.2: Install Nginx Configuration
Copy the provided Nginx configuration to `/etc/nginx/conf.d/televault.conf`:

```bash
sudo cp /opt/televault/deploy/nginx/televault.conf /etc/nginx/conf.d/televault.conf
```

*(For Debian/Ubuntu systems using `sites-available`/`sites-enabled`, you may alternatively link to `/etc/nginx/sites-available/televault.conf` and `/etc/nginx/sites-enabled/televault.conf`).*

Ensure `/etc/nginx/conf.d/televault.conf` contains the upstream proxy directives targeting `http://127.0.0.1:8200`:

```nginx
# ==============================================================================
# Nginx Configuration for TeleVault (televault.madhudadi.in)
# Reverse Proxy to TeleVault Web Dashboard (127.0.0.1:8200)
# ==============================================================================

# HTTP -> HTTPS 301 Redirect
server {
    listen 80;
    listen [::]:80;
    server_name televault.madhudadi.in;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# HTTPS Main Server Block
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name televault.madhudadi.in;

    # SSL Certificates
    ssl_certificate /etc/letsencrypt/live/televault.madhudadi.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/televault.madhudadi.in/privkey.pem;

    # SSL Hardening & Modern Ciphers
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384';
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    # HSTS & Security Headers
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    client_max_body_size 50M;

    # Real IP Forwarding
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Port $server_port;

    # 1. Unbuffered Streaming for Media and Streaming ZIP Downloads
    # Directly streams Telegram chunks to client browser without server disk I/O
    location ~ ^/api/(direct|media/stream)/ {
        proxy_pass http://127.0.0.1:8200;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_ignore_client_abort on;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # 2. WebSocket Live Telemetry HUD Stream
    location /ws/ {
        proxy_pass http://127.0.0.1:8200;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # 3. Static Assets & App Dashboard
    location / {
        proxy_pass http://127.0.0.1:8200;
        proxy_http_version 1.1;
        proxy_read_timeout 60s;
    }
}
```

Validate and reload Nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 4. GitHub Actions CI/CD Pipeline & Automated Rollback

TeleVault features full CI/CD automation via `.github/workflows/deploy.yml`.

### Pipeline Overview
1. **Code Hardening & Quality Gate** (Runs on Python 3.12):
   - Dependencies installation (`pip install -r requirements.txt`).
   - Linter validation (`ruff check .`).
   - Static type checking (`mypy src`).
   - Full automated test suite (`pytest -v`).
2. **Production Deployment via SSH** (Triggered on push to `main`):
   - Establishes secure SSH connection using ED25519 keys.
   - Navigates to `/opt/televault`.
   - Records currently deployed git commit hash (`PREV_SHA=$(git rev-parse HEAD)`).
   - Syncs latest git repository changes from origin `main`.
   - Executes container stack rebuild:
     ```bash
     docker compose build --pull && docker compose up -d --remove-orphans
     ```
   - **Health Polling Loop**: Polls `http://127.0.0.1:8200/api/health` every second for up to 30 seconds, verifying response: `{"status": "ok", ...}`.
   - **Automated Zero-Downtime Rollback**: If the build fails or the healthcheck fails after 30 seconds:
     ```bash
     git reset --hard "$PREV_SHA"
     docker compose up -d --build
     exit 1
     ```
     The stack immediately rolls back to the previous stable release, preserving container availability without manual intervention.

### Required GitHub Repository Secrets
Under **Settings > Secrets and variables > Actions**, configure:
- `SSH_PRIVATE_KEY`: Private SSH key authorized for the deployment user on the server.
- `SERVER_HOST`: Production server public IP or hostname (e.g. `152.53.254.183`).
- `SERVER_USER`: Deployment user (e.g. `madhu`).

---

## 5. Alternative: Bare-Metal Systemd Deployment

If running directly on the host without Docker containers, TeleVault can run as a background systemd service.

### Step 5.1: Python Virtualenv Setup
```bash
cd /opt/televault
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 5.2: Configure `.env` for Bare Metal
```bash
cat << 'EOF' > /opt/televault/.env
TG_API_ID=your_api_id_here
TG_API_HASH=your_api_hash_here
TG_PHONE=+15551234567
TG_SESSION=session/telegram.session
TELEVAULT_TOKEN=your_secure_random_token_here
TELEVAULT_PORT=8200
TELEVAULT_BIND_HOST=127.0.0.1
TELEVAULT_ALLOWED_HOSTS=televault.madhudadi.in,localhost,127.0.0.1
TELEVAULT_FORWARDED_ALLOW_IPS=127.0.0.1,172.16.0.0/12,*
TELEVAULT_HEADLESS=1
TELEVAULT_OUT_DIR=out
EOF
chmod 600 /opt/televault/.env
```

### Step 5.3: Systemd Unit Installation
Install `deploy/systemd/televault.service`:

```bash
sudo cp /opt/televault/deploy/systemd/televault.service /etc/systemd/system/televault.service
sudo systemctl daemon-reload
sudo systemctl enable televault.service
sudo systemctl start televault.service
```

Inspect service status and journal logs:

```bash
sudo systemctl status televault.service
journalctl -u televault.service -f
```

---

## 6. Verification & Health Check Runbook

1. **Test HTTP to HTTPS 301 Redirect**:
   ```bash
   curl -I http://televault.madhudadi.in/
   # Expected: HTTP/1.1 301 Moved Permanently -> Location: https://televault.madhudadi.in/
   ```

2. **Test HTTPS Main Endpoint**:
   ```bash
   curl -I https://televault.madhudadi.in/
   # Expected: HTTP/2 200 OK
   ```

3. **Test Local Container Health Endpoint (Host Port 8200)**:
   ```bash
   curl -s http://127.0.0.1:8200/api/health
   # Expected: {"status":"ok","version":"...","session_authenticated":...}
   ```

4. **Test Public Health Endpoint**:
   ```bash
   curl -s https://televault.madhudadi.in/api/health
   ```

---

## 7. Security Best Practices

- **Strict Loopback Binding**: Always bind the container/application port to `127.0.0.1:8200` on the host, never `0.0.0.0:8200`. Traffic must enter through Nginx.
- **Least-Privilege Execution**: The container runs under UID `1000:1000` (`madhu`). Never run the container or systemd service as root.
- **File & Secret Permissions**: Restrict `.env` and `session/` permissions to `chmod 600` and directories to `chmod 700`. TeleVault automatically applies `umask 077` and checks for world-writable directories.
- **Reverse Proxy Protection**: Unbuffered streaming is scoped strictly to `/api/(direct|media/stream)/` with long timeouts (3600s), while standard dashboard endpoints timeout at 60s.
- **Host Header & Origin Protection**: `TELEVAULT_ALLOWED_HOSTS` prevents DNS rebinding attacks and verifies WebSocket Origin headers.

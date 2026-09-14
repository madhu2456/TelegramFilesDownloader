# TeleVault Production Deployment Runbook

This guide covers deploying TeleVault on a Linux server configured under `televault.madhudadi.in` with Nginx reverse proxy, automated SSL certificates via Certbot, and systemd service management.

---

## 1. Prerequisites

- A Linux VPS or dedicated server running Ubuntu 22.04+ / Debian 12+ or Arch Linux.
- Python 3.10+ installed.
- Public DNS A Record pointing to the server's public IP address:
  ```text
  televault.madhudadi.in.   IN   A   <YOUR_SERVER_PUBLIC_IP>
  ```
- Inbound ports `80` (HTTP) and `443` (HTTPS) open on your firewall (UFW / iptables / security group).

---

## 2. Directory Setup & Repository Clone

```bash
cd /run/media/madhud/Storage/LinuxProjects/Codes/Projects
git clone https://github.com/madhu2456/TelegramFilesDownloader.git TelegramDownloader
cd TelegramDownloader

# Create python virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create your production environment file `.env`:
```bash
cat << 'EOF' > .env
# Telegram API Credentials
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=+15551234567

# Optional: Pin a static secret token instead of auto-generated ephemeral token
# TELEVAULT_TOKEN=your_secure_random_token_here

# Production Hosting Configuration
TELEVAULT_HEADLESS=1
TELEVAULT_PORT=8000
TELEVAULT_BIND_HOST=127.0.0.1
TELEVAULT_ALLOWED_HOSTS=televault.madhudadi.in
EOF
chmod 600 .env
```

---

## 3. SSL Certificate Setup via Certbot

Install Certbot and obtain Let's Encrypt certificates:

```bash
sudo apt update && sudo apt install -y certbot python3-certbot-nginx

# Obtain certificate for televault.madhudadi.in
sudo certbot certonly --standalone -d televault.madhudadi.in --preferred-challenges http
```

Certificates will be saved to:
- Certificate: `/etc/letsencrypt/live/televault.madhudadi.in/fullchain.pem`
- Private Key: `/etc/letsencrypt/live/televault.madhudadi.in/privkey.pem`

---

## 4. Nginx Reverse Proxy Setup

Copy the prepared Nginx configuration:

```bash
sudo cp deploy/nginx/televault.conf /etc/nginx/sites-available/televault.conf
sudo ln -sf /etc/nginx/sites-available/televault.conf /etc/nginx/sites-enabled/televault.conf

# Test configuration syntax
sudo nginx -t

# Reload Nginx
sudo systemctl reload nginx
```

Key features of this Nginx configuration:
- Automatic HTTP to HTTPS 301 redirection.
- Modern TLSv1.2 & TLSv1.3 protocols with HSTS headers.
- **Unbuffered streaming** (`proxy_buffering off;`) on `/api/direct/` and `/api/media/stream/` to stream Telegram downloads directly to user browsers without writing to server disk.
- **WebSocket upgrade** with 24-hour timeout for real-time telemetry HUD.

---

## 5. Systemd Service Setup

Install and enable the background systemd service:

```bash
sudo cp deploy/systemd/televault.service /etc/systemd/system/televault.service
sudo systemctl daemon-reload
sudo systemctl enable televault.service
sudo systemctl start televault.service

# Check service status
sudo systemctl status televault.service
```

To inspect logs in real time:
```bash
journalctl -u televault.service -f
```

---

## 6. Verification & Health Check

1. Test HTTP to HTTPS redirect:
   ```bash
   curl -I http://televault.madhudadi.in/
   # Expect: HTTP/1.1 301 Moved Permanently -> Location: https://televault.madhudadi.in/
   ```

2. Test HTTPS endpoint:
   ```bash
   curl -I https://televault.madhudadi.in/
   # Expect: HTTP/2 200 OK
   ```

3. Test API status:
   ```bash
   curl -s -H "Host: televault.madhudadi.in" https://televault.madhudadi.in/api/status
   ```

---

## 7. Security Best Practices

- Always ensure `TELEVAULT_BIND_HOST=127.0.0.1` so the Python backend is never exposed directly on public interfaces.
- The web dashboard uses ephemeral tokens by default. Check `journalctl -u televault.service` on service startup for the active token URL or configure `TELEVAULT_TOKEN` in `.env`.

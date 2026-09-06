# TelegramDownloader

Telegram media downloader built on Telethon.

## Setup: Virtual Environment

```bash
python3 -m ensurepip --upgrade
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Setup: API Credentials via my.telegram.org

1. Go to https://my.telegram.org and log in with your Telegram account.
2. Open **API development tools** and create a new application.
3. Copy the `api_id` and `api_hash` values.
4. Create a `.env` file (see `.env.example`) with:

```ini
TG_API_ID=your_api_id_here
TG_API_HASH=your_api_hash_here
TG_PHONE=+15551234567
TG_SESSION=session/telegram.session
```

5. Never commit `.env`, `*.session*`, or screenshots containing secrets.

See `docs/USAGE.md` for full CLI flags, private-channel/invite `--join` flow, and troubleshooting.

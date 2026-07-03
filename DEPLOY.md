# Deploy Guide

This project is designed to run safely on a VPS or a local Windows machine.

## Security model

The application must listen on `127.0.0.1`, not on a public interface.

```env
APP_HOST=127.0.0.1
APP_PORT=8095
```

Do not publish port `8095` to the Internet. Public access should be one of:

- domain + HTTPS reverse proxy;
- no domain/no public IP + SSH tunnel.

If Telegram is blocked or unavailable, the web panel and marketplace processing stay online. Telegram polling runs in a supervisor task and retries with backoff.

## Fast Ubuntu install

After the repository is on GitHub, the one-command install form is:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/scripts/install-linux.sh | sudo env REPO_URL=https://github.com/OWNER/REPO.git bash
```

If you already cloned the repository:

```bash
cd vpn-reseller-autoseller
sudo bash scripts/install-linux.sh
```

The installer asks for:

- web panel login and password;
- Telegram bot token;
- Telegram admin ID;
- XyraNet API key, or leave empty and fill it in the panel later;
- Digiseller/GGsel keys, optional;
- whether a domain is used;
- domain and Let's Encrypt email when HTTPS is enabled.

The installer creates:

- `/opt/xyranet-reseller-autoseller`;
- `.env` with generated `ADMIN_TOKEN` and `MARKETPLACE_WEBHOOK_SECRET`;
- Python virtual environment;
- systemd service `xyranet-reseller-autoseller`;
- optional nginx + Let's Encrypt HTTPS config.

Useful commands:

```bash
sudo systemctl status xyranet-reseller-autoseller
sudo journalctl -u xyranet-reseller-autoseller -f
sudo systemctl restart xyranet-reseller-autoseller
```

## Ubuntu with domain

Point DNS `A`/`AAAA` records to the server, run the installer, choose `yes` for domain.

The panel URL will be:

```text
https://your-domain.example
```

Webhook examples:

```text
https://your-domain.example/webhooks/plati?secret=YOUR_WEBHOOK_SECRET
https://your-domain.example/webhooks/ggsel?secret=YOUR_WEBHOOK_SECRET
https://your-domain.example/webhooks/plati/text?secret=YOUR_WEBHOOK_SECRET
```

The web panel is not served directly over plain HTTP. nginx only redirects HTTP to HTTPS for the domain and the Python app remains bound to `127.0.0.1`.

If you need port 80 completely closed after certificate issue, you can close it in the firewall, but then Let's Encrypt HTTP renewal will need another validation method.

## Ubuntu without domain or dedicated public IP

Do not expose nginx. Keep the app on localhost and connect with SSH tunneling:

```bash
ssh -L 8095:127.0.0.1:8095 user@server
```

Then open on your local computer:

```text
http://127.0.0.1:8095
```

This HTTP connection is local between your browser and your own computer. The traffic to the server goes through SSH encryption. Nobody on the Internet can open the panel unless they can SSH into the server.

If your SSH server is not on port 22:

```bash
ssh -p 2222 -L 8095:127.0.0.1:8095 user@server
```

If the server is behind NAT and has no incoming SSH access, use one of these:

- provider console/VPN to the private network;
- reverse SSH tunnel from the server to your machine;
- Tailscale/ZeroTier private network;
- Cloudflare Tunnel, but protect it with Cloudflare Access or equivalent authentication.

Reverse SSH example, run on the server:

```bash
ssh -N -R 18095:127.0.0.1:8095 your-user@your-public-machine
```

Then on `your-public-machine` open:

```text
http://127.0.0.1:18095
```

## Windows local run

PowerShell:

```powershell
cd C:\path\to\vpn-reseller-autoseller
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python run.py
```

Open:

```text
http://127.0.0.1:8095
```

Recommended Windows `.env` basics:

```env
APP_HOST=127.0.0.1
APP_PORT=8095
XYRANET_API_BASE_URL=https://xyranet.pro/api/wholesale
TELEGRAM_BOT_TOKEN=your_bot_token
ADMIN_IDS=1606617935
ADMIN_USERNAME=admin
ADMIN_PASSWORD=strong-password
ADMIN_TOKEN=random-long-token
MARKETPLACE_WEBHOOK_SECRET=random-long-secret
DATABASE_PATH=data/reseller.sqlite3
ENABLE_TELEGRAM=true
```

## GitHub publishing

The repository must not contain real secrets or the production database.

Before pushing:

```bash
git init
git add .
git status
git commit -m "Initial autoseller release"
git branch -M main
git remote add origin https://github.com/OWNER/REPO.git
git push -u origin main
```

Check that these files are not staged:

```bash
git status --ignored
```

`.env`, logs, virtualenv, and SQLite DB files are ignored by `.gitignore`.

## Telegram blocked or unavailable

The FastAPI web panel starts independently from Telegram.

If Telegram polling fails:

- the web panel remains available;
- marketplace polling/webhooks continue;
- the bot supervisor logs the error and retries;
- `/admin/api/status` includes Telegram running state and last error.

Restart only Telegram from the panel if needed, or restart the whole service:

```bash
sudo systemctl restart xyranet-reseller-autoseller
```

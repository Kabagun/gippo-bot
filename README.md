# GIPPO Telegram Bot

A small Telegram bot that signs in to the GIPPO loyalty cabinet and shows:

- the current discount;
- the projected discount for the next month based on current-month purchases;
- purchases in the current month;
- the amount remaining until the next level, reported as zero when the account
  has already reached the published maximum tier;
- protected GIPPO and Belmarket loyalty-card images with every `/start` response.

The bot uses Telegram long polling, so it does not need a public HTTP endpoint.
It supports either deliberately open access or a restricted Telegram user-ID
allow-list. The production deployment is configured for open access.

## Commands

- `/start` — send the GIPPO card, the Belmarket card, and then current status.

The final status message has one **Обновить** button. It updates only that text
message; card images are not sent again. Every button from an earlier menu asks
the user to call `/start` and cannot access its old action.

The running bot keeps one shared authenticated GIPPO HTTP session for all
Telegram users. It reuses the same session cookies between status requests and
automatically signs in again once if the cabinet session expires.

## Local setup

Python 3.11 or newer is required.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"  # Windows
cp .env.example .env
```

On Linux, activate `.venv/bin/activate` or invoke `.venv/bin/python` directly.
Load the variables from `.env`, then run:

```bash
gippo-check --json
gippo-bot
```

`gippo-check` verifies the cabinet integration without requiring a Telegram bot
token. Do not commit `.env`; it contains both the cabinet password and bot token.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | bot only | Token issued by BotFather |
| `TELEGRAM_OPEN_ACCESS` | no | Set to `true` to allow every Telegram user; defaults to `false` |
| `TELEGRAM_ALLOWED_USER_IDS` | restricted mode | Comma-separated Telegram numeric user IDs |
| `GIPPO_LOGIN` | yes | GIPPO cabinet login/phone |
| `GIPPO_PASSWORD` | yes | GIPPO cabinet password |
| `GIPPO_BASE_URL` | no | Defaults to `https://cabinet.gippo.by` |
| `GIPPO_TIMEOUT_SECONDS` | no | Request timeout, defaults to 20 seconds |
| `GIPPO_CARD_IMAGE_PATH` | no | GIPPO card PNG; defaults to `/srv/bots/gippo-bot/private/cards/gippo.png` |
| `BELMARKET_CARD_IMAGE_PATH` | no | Belmarket card PNG; defaults to `/srv/bots/gippo-bot/private/cards/belmarket.png` |
| `LOG_LEVEL` | no | Python log level, defaults to `INFO` |

The bot refuses to start if open access is disabled and the allow-list is empty.
With `TELEGRAM_OPEN_ACCESS=true`, anyone who finds the bot can read the shared
cabinet values. Failed HTTP responses, page bodies, and Bot API request URLs are
not logged, avoiding accidental disclosure of cabinet data or the bot token.
Card images contain account-linked barcodes and must be kept outside the public
repository in owner-readable files.

## Tests

```bash
python -m pytest
ruff check .
```

## Linux deployment

The included user-level systemd unit follows the deployment layout on the
target host: the checkout is `/srv/bots/gippo-bot/app`, the virtual environment
is `/srv/bots/gippo-bot/venv`, and secrets live in the owner-readable
`/srv/bots/gippo-bot/.env` file.

```bash
mkdir -p /srv/bots/gippo-bot
git clone https://github.com/Kabaye/gippo-bot.git /srv/bots/gippo-bot/app
python3 -m venv /srv/bots/gippo-bot/venv
/srv/bots/gippo-bot/venv/bin/python -m pip install /srv/bots/gippo-bot/app
chmod 600 /srv/bots/gippo-bot/.env
mkdir -p ~/.config/systemd/user
cp /srv/bots/gippo-bot/app/deploy/gippo-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gippo-bot.service
```

Check it with `systemctl --user status gippo-bot.service` and
`journalctl --user -u gippo-bot.service`. The service runs entirely as the
unprivileged `apps` user and restarts automatically after transient failures.

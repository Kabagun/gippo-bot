# GIPPO Telegram Bot

A small Telegram bot that signs in to the GIPPO loyalty cabinet and shows:

- the current discount;
- the projected discount for the next month based on current-month purchases;
- purchases in the current month;
- the amount remaining until the next level while another level is available;
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
Telegram users. It saves its cookies in a protected file and checks that session
before requesting cabinet status. If no valid session exists, the status request
starts SMS login. A single background monitor polls that existing challenge every
5 seconds and completes login when the code arrives. It edits waiting status
messages in place, so users do not need a second refresh and the bot does not send
duplicate cards or status messages. A request does not wait several minutes for
an SMS.
An authenticated loyalty page may also include the profile phone editor.

When a challenge expires, the monitor stops without automatically requesting
another SMS. The existing provider cooldown and challenge TTL remain in effect.

The optional owner can confirm an existing challenge in a private chat with
`/gippo_sms 12345`. The code must contain exactly five ASCII digits. This command
is hidden from the public command menu and rejected for every other user and for
group chats. Its message is deleted when Telegram allows it. Normal users cannot
supply or change the shared account's authentication code.

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
token. A check without a valid session can start or resume an SMS login.
`gippo-check --sms-code` prompts privately for a pending challenge's code.
Do not pass codes as shell arguments. Do not commit `.env`, session files,
tokens or card images.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | bot only | Token issued by BotFather |
| `TELEGRAM_OPEN_ACCESS` | no | Set to `true` to allow every Telegram user; defaults to `false` |
| `TELEGRAM_ALLOWED_USER_IDS` | restricted mode | Comma-separated Telegram numeric user IDs |
| `TELEGRAM_OWNER_USER_ID` | manual fallback | Owner's numeric Telegram ID; only private chats may confirm SMS |
| `GIPPO_AUTH_MODE` | no | `sms` (default), or optional legacy `password` mode |
| `GIPPO_PHONE` | SMS mode | Shared account phone beginning with `+375` |
| `GIPPO_SESSION_PATH` | SMS mode | Private persistent cookie/challenge file |
| `SMS_FORWARDER_URL` | no | Defaults to `http://127.0.0.1:18090`; use HTTPS for a remote service |
| `SMS_FORWARDER_TOKEN` | automatic SMS | Consumer-scoped service token; without it SMS mode uses manual confirmation |
| `SMS_FORWARDER_TOKEN_FILE` | automatic SMS | Alternate owner-only file containing one consumer token; cannot be combined with inline token |
| `GIPPO_SMS_SENDER` | no | Exact expected SMS sender, defaults to `GIPPO` |
| `GIPPO_SMS_TTL_SECONDS` | no | Challenge validity, 60–300 seconds; default 300 |
| `GIPPO_SMS_COOLDOWN_SECONDS` | no | Minimum SMS interval, at least 900 seconds; default 900 |
| `GIPPO_LOGIN` | password mode | Legacy cabinet login |
| `GIPPO_PASSWORD` | password mode | Legacy cabinet password |
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

On Linux the session directory must belong to the service
user and have mode `0700`; its files use mode `0600`. The Windows operator
must restrict the equivalent directory's ACL to the service owner. State is
written atomically. A process lock and a shared thread lock prevent concurrent
requests from sending duplicate SMS. Damaged, mismatched, or insecure state
files fail closed instead of starting a new login.

The forwarding token can live in a separate `0600` file beneath the `0700`
private directory; configure `SMS_FORWARDER_TOKEN_FILE` to avoid copying it
into a process argument or shell history. Only one token source may be set.

The client creates its forwarding challenge before GIPPO sends the SMS.
An unavailable receiver prevents the SMS request. Every attempted login
reserves a cooldown before network I/O; failures increase that delay up to
24 hours. Pending cookies, code-attempt hashes, and the delay survive a
restart. Ambiguous network failures never trigger an immediate resend. The
background monitor never resends automatically when its challenge expires.
Successful authentication clears the accumulated failure backoff and retains
at most the configured base SMS cooldown (15 minutes by default). Successful
status refreshes do not extend that deadline. If the saved session is rejected
after the cooldown, the next status request starts a new SMS login.
At most three distinct codes can be tried for a challenge. Authentication
requires a positive login response and a valid authenticated personal page.
Runtime logs record session rejection, confirmed authentication, and whether a
Telegram status refresh succeeded or was deferred, without codes or cookies.

The forwarding contract uses Bearer authentication:
`POST /internal/v1/challenges` with `sender=GIPPO`, `ttl_seconds`, and a `code_pattern`
that matches exactly five ASCII digits; `GET /internal/v1/challenges/{id}` returns
`waiting`, `received`, or `expired`; `DELETE` clears it. The service controls the
phone/source association for each consumer. No phone ownership is taken from a
public Telegram user.

Register the bot on the common server as the `apps` user. The internal consumer
name stays `gippo-bot`; its public phone service alias is `gippo`:

```bash
sms-forwarder-client register --name gippo-bot --owner kabaye --service gippo --sender GIPPO
```

The shared phone contract uses one owner URL returned by registration:
`POST /v1/sms/<device-token>`. The test URL uses the same path with `/test`
appended. These URLs are shared by the services registered for that phone owner.

This contract requires SMS Forwarder 0.3.0. Update the gateway before switching
the phone to common ingress. After the gateway update, change an existing
consumer's public alias with the administrator command:

```bash
sms-forwarder-client set-service --name gippo-bot --service gippo
```

Re-registering `gippo-bot` with a different `--service` returns a conflict; it
does not rename the alias. The administrator command preserves consumer
credentials and active challenges.

The phone sends JSON to the live URL:

```json
{"service":"gippo","text":"<incoming SMS>"}
```

When `gippo` has only one registered sender, `sender` may be omitted. If included,
it must match the registered sender exactly. Sender-only routing is allowed only
when one service for that owner matches. The test URL validates routing but never
completes a login.

SMS Forwarder stores unambiguous codes from allowed senders encrypted in its
database for up to 24 hours. This history does not extend the bot's challenge TTL
or supply a code to a later authentication attempt.

Keep the consumer token in `SMS_FORWARDER_TOKEN_FILE` and keep the returned phone
links private.

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
git clone https://github.com/Kabagun/gippo-bot.git /srv/bots/gippo-bot/app
python3 -m venv /srv/bots/gippo-bot/venv
/srv/bots/gippo-bot/venv/bin/python -m pip install /srv/bots/gippo-bot/app
chmod 600 /srv/bots/gippo-bot/.env
mkdir -p /srv/bots/gippo-bot/private
chmod 700 /srv/bots/gippo-bot/private
mkdir -p ~/.config/systemd/user
cp /srv/bots/gippo-bot/app/deploy/gippo-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gippo-bot.service
```

Check it with `systemctl --user status gippo-bot.service` and
`journalctl --user -u gippo-bot.service`. The service runs entirely as the
unprivileged `apps` user and restarts automatically after transient failures.

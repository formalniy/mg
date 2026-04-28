# MoneyGlitch

Realtime listener for [@durov](https://t.me/durov) that opens a leveraged
long on **TONUSDT perpetual** at MEXC the moment Pavel posts a message
containing `TON`. Trading parameters (margin in USD, leverage, stop-loss %)
are configured live through a Telegram bot with a Russian interface.

## How latency is minimized

- **Telethon over MTProto, push-based.** No `GetHistory` polling. The
  Telegram server pushes `updateNewChannelMessage` to the open MTProto
  socket; the handler runs as soon as the socket delivers the update. This
  is strictly lower latency than any polling interval.
- **Novelty check is a single integer compare** (`event.message.id` vs.
  the last seen id). Telegram channel ids are monotonic.
- **No exponential backoff.** Telethon auto-reconnects on transport
  errors; we don't add any sleep on top. MEXC errors propagate as a
  Telegram notification — the next post will retry immediately.

## Components

| File | Role |
|---|---|
| `moneyglitch/parser.py` | Telethon client, regex match, dispatch trade |
| `moneyglitch/mexc.py` | MEXC contract client (HMAC-SHA256, market long + SL) |
| `moneyglitch/bot.py` | aiogram-3 control bot, Russian inline UI |
| `moneyglitch/state.py` | Atomic JSON state shared between processes |
| `moneyglitch/notify.py` | Bot-API push from parser to owner |
| `run_parser.py`, `run_bot.py` | Process entry points |
| `deploy/install.sh` | One-shot Ubuntu 24.x installer |
| `deploy/*.service` | systemd units (Restart=always) |

## Rate limits, accounted for

- **Telethon**: a single MTProto session listening on one channel — no
  flood-wait risk. We do not call `GetHistory`.
- **MEXC contract API**: ~20 req / 2s per account on private endpoints.
  Each TON trigger sends 4 calls (detail, ticker, change_leverage,
  submit_order) — orders of magnitude under the cap. A `trade_lock`
  guarantees triggers serialize even if posts arrive back-to-back.

## Deploying on Ubuntu 24.x

```bash
# on a fresh VPS:
git clone https://github.com/formalniy/mg
cd moneyglitch
sudo bash deploy/install.sh
```

The installer:

1. apt-installs `python3 python3-venv python3-pip`.
2. Creates a non-login service user `moneyglitch`.
3. Copies code to `/opt/moneyglitch`, builds a venv, installs requirements.
4. Seeds `/var/lib/moneyglitch/config.json` from the example (chmod 600).
5. Installs `moneyglitch-parser.service` and `moneyglitch-bot.service`.

After the installer finishes:

```bash
# 1. Fill in credentials
sudo nano /var/lib/moneyglitch/config.json

# 2. Authenticate Telethon ONCE (interactive — phone + code from Telegram)
sudo -u moneyglitch \
  MONEYGLITCH_CONFIG=/var/lib/moneyglitch/config.json \
  MONEYGLITCH_STATE=/var/lib/moneyglitch/state.json \
  /opt/moneyglitch/.venv/bin/python /opt/moneyglitch/run_parser.py
# Ctrl+C once you see "parser connected; listening @durov"

# 3. Start services 24/7
sudo systemctl enable --now moneyglitch-bot.service moneyglitch-parser.service

# 4. Logs
journalctl -u moneyglitch-parser -f
journalctl -u moneyglitch-bot -f
```

## Configuration

`/var/lib/moneyglitch/config.json`:

```json
{
  "telegram": {
    "api_id": 123456,
    "api_hash": "...",
    "session": "/var/lib/moneyglitch/parser"
  },
  "mexc": {
    "api_key": "...",
    "secret": "...",
    "symbol": "TON_USDT",
    "open_type": 1
  },
  "bot": {
    "token": "...",
    "user_id": 111111111
  }
}
```

- `telegram.api_id` / `api_hash`: from <https://my.telegram.org>.
- `mexc.open_type`: `1` = isolated margin, `2` = cross.
- `bot.user_id`: numeric Telegram id of the only user allowed to control
  the bot. Get it from `@userinfobot`.

## Bot interface (Russian)

`/start` shows current state and the inline keyboard:

- 💰 **Сумма (USD)** — input dollar margin per trade
- 📊 **Плечо** — input leverage (1–200)
- 🛑 **Стоп-лосс (%)** — input stop-loss percent below entry
- ▶️ **Включить** / ⏸ **Остановить** — global trading flag
- 🔄 **Обновить** — refresh status

Only the `bot.user_id` configured in `config.json` is allowed to use the
bot; every other update is silently rejected.

## Security notes

- `config.json` and `state.json` live in `/var/lib/moneyglitch` (chmod 700,
  owned by the service user). Both systemd units run with
  `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`.
- The Telethon `.session` file is sensitive — treat it like a password.
- The bot rejects every update whose `from_user.id` is not the configured
  owner.

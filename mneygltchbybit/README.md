# MoneyGlitch (Bybit edition)

Realtime listener for [@durov](https://t.me/durov) that opens a leveraged
long on **TONUSDT perpetual** at **Bybit** the moment Pavel posts a message
containing `TON`. Trading parameters (margin in USD, leverage, stop-loss %)
are configured live through a Telegram bot with a Russian interface.

This is a fork of the MEXC version, ported because MEXC restricted public
futures-trading API access. Bybit's v5 API is open, well-documented, and
available in regions where MEXC futures are blocked (including RU).

## How latency is minimized

- **Telethon over MTProto, push-based.** No `GetHistory` polling. The
  Telegram server pushes `updateNewChannelMessage` to the open MTProto
  socket; the handler runs as soon as the socket delivers the update.
- **Novelty check is a single integer compare** (`event.message.id` vs.
  the last seen id). Telegram channel ids are monotonic.
- **No exponential backoff.** Telethon auto-reconnects on transport
  errors; we don't add any sleep on top. Bybit errors propagate as a
  Telegram notification with full diagnostics — the next post retries
  immediately.

## Components

| File | Role |
|---|---|
| `moneyglitch/parser.py` | Telethon client, regex match, dispatch trade |
| `moneyglitch/bybit.py` | Bybit v5 client (HMAC-SHA256, market long + SL, diagnostics) |
| `moneyglitch/bot.py` | aiogram-3 control bot, Russian inline UI |
| `moneyglitch/state.py` | Atomic JSON state shared between processes |
| `moneyglitch/notify.py` | Bot-API push from parser to owner |
| `run_parser.py`, `run_bot.py` | Process entry points |
| `deploy/install.sh` | One-shot Ubuntu 24.x installer |
| `deploy/*.service` | systemd units (Restart=always) |

## Trade flow

For each TON-matching post when trading is enabled:

1. `GET /v5/market/instruments-info` — fetch `qtyStep`, `minOrderQty`, `tickSize`, `maxLeverage`.
2. `GET /v5/market/tickers` — fetch `lastPrice` (fallback `markPrice`).
3. Compute `qty = floor(amount_usd × leverage / lastPrice / qtyStep) × qtyStep` (in TON, not contracts).
4. Compute `stopLoss = round_to_tick(lastPrice × (1 − sl_pct/100))`.
5. `POST /v5/position/set-leverage` (idempotent on `retCode=110043`).
6. `POST /v5/position/switch-isolated` (best-effort; non-fatal on UTA where margin mode is account-level).
7. `POST /v5/order/create` with `side=Buy`, `orderType=Market`, `stopLoss`, `slTriggerBy=MarkPrice`.

On any `BybitError` the parser broadcasts the raw response **and** runs
`gather_diagnostics(symbol)` which returns instrument info, ticker, both
UNIFIED+CONTRACT wallet balances, and the open positions list — formatted
into a readable Telegram message so you can see exactly why the order
didn't go through.

## Rate limits, accounted for

- **Telethon**: a single MTProto session listening on one channel — no
  flood-wait risk. We do not call `GetHistory`.
- **Bybit v5**: per-key limits are generous (e.g. `order/create` at 10
  req/s for retail UTA). Each TON trigger sends ~5 calls — orders of
  magnitude under the cap. A `trade_lock` serializes triggers even if
  posts arrive back-to-back.

## Deploying on Ubuntu 24.x

```bash
# on a fresh VPS:
git clone <your-fork-url> mneygltchbybit
cd mneygltchbybit
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
# 1. Make sure nothing is running yet — otherwise systemd's Restart=always
#    will keep relaunching the parser against an unfinished config and
#    spam KeyError tracebacks while you edit.
sudo systemctl stop moneyglitch-parser.service moneyglitch-bot.service 2>/dev/null || true

# 2. Fill in credentials
sudo nano /var/lib/moneyglitch/config.json

# 3. Authenticate Telethon ONCE (interactive — phone + code from Telegram)
sudo -u moneyglitch \
  MONEYGLITCH_CONFIG=/var/lib/moneyglitch/config.json \
  MONEYGLITCH_STATE=/var/lib/moneyglitch/state.json \
  /opt/moneyglitch/.venv/bin/python /opt/moneyglitch/run_parser.py
# Ctrl+C once you see "parser connected; listening @durov"

# 4. Start services 24/7
sudo systemctl enable --now moneyglitch-bot.service moneyglitch-parser.service

# 5. Logs
journalctl -u moneyglitch-parser -f
journalctl -u moneyglitch-bot -f
```

## Updating / restarting

The interactive command above runs `/opt/moneyglitch/run_parser.py` — the
**deployed** copy, not your working tree. If you `git pull` (or are
upgrading from the older MEXC fork), the deployed code is stale and the
manual run will crash with errors like `KeyError: 'mexc'` even though the
repo source is correct. Always sync first:

```bash
# Stop everything before touching code or config
sudo systemctl stop moneyglitch-parser.service moneyglitch-bot.service
pgrep -af moneyglitch                       # should print nothing

# Re-deploy: copies repo -> /opt/moneyglitch (idempotent, safe to re-run)
cd ~/Documents/project/mg/mneygltchbybit    # or wherever you cloned it
sudo bash deploy/install.sh

# Bring services back up
sudo systemctl start moneyglitch-bot.service moneyglitch-parser.service
journalctl -u moneyglitch-parser -f
```

To fully disable everything (no auto-restart on boot):

```bash
sudo systemctl disable --now moneyglitch-parser.service moneyglitch-bot.service
sudo pkill -u moneyglitch -f run_parser.py 2>/dev/null || true
sudo pkill -u moneyglitch -f run_bot.py    2>/dev/null || true
```

## Configuration

`/var/lib/moneyglitch/config.json`:

```json
{
  "telegram": {
    "api_id": 123456,
    "api_hash": "...",
    "session": "/var/lib/moneyglitch/parser",
    "channel": "durov"
  },
  "bybit": {
    "api_key": "...",
    "secret": "...",
    "symbol": "TONUSDT",
    "isolated": true
  },
  "bot": {
    "token": "...",
    "user_ids": [111111111, 222222222]
  }
}
```

- `telegram.api_id` / `api_hash`: from <https://my.telegram.org>.
- `telegram.channel`: channel to listen to. Username (`durov`, with or
  without `@`) or numeric id (e.g. `-1001234567890` for private channels;
  the Telethon user account must already be a member).
- `bybit.symbol`: Bybit linear symbol, no underscore — `TONUSDT`, not `TON_USDT`.
- `bybit.isolated`: `true` requests isolated margin per symbol; on UTA
  accounts where margin mode is set at account level, this becomes a
  no-op and is logged as non-fatal.
- `bot.user_ids`: numeric Telegram ids of every user allowed to control
  the bot and receive trade notifications. Get them from `@userinfobot`.
  Single-element list is fine. The legacy `bot.user_id` (single int) is
  still accepted for backwards compatibility.

## Bybit API key requirements

Create a key at <https://www.bybit.com/app/user/api-management> with:

- ✅ **Contract — Orders / Positions: Read + Trade** (mandatory)
- ✅ **Wallet: Read** (used by diagnostics)
- ❌ Withdraw permission — **never** enable
- IP restriction: optional but recommended; whitelist your VPS public IP

System (UTA vs Standard) doesn't matter — the diagnostic and order paths
work for both. The `wallet_balance` query in diagnostics tries `UNIFIED`
first then falls back to `CONTRACT`, so you get the right numbers either
way.

## Bot interface (Russian)

`/start` shows current state and the inline keyboard:

- 💰 **Сумма (USD)** — input dollar margin per trade
- 📊 **Плечо** — input leverage (1–200; Bybit caps TONUSDT at 50x)
- 🛑 **Стоп-лосс (%)** — input stop-loss percent below entry
- ▶️ **Включить** / ⏸ **Остановить** — global trading flag
- 🔄 **Обновить** — refresh status

Only ids listed in `bot.user_ids` in `config.json` are allowed to use the
bot; every other update is silently rejected. All listed users also
receive trade and error notifications from the parser.

## Diagnostics on failure

When `BybitFutures.open_long_market` raises, Telegram receives two
messages: the raw `retCode`/`retMsg`, and a structured diagnostic block
with — for the configured symbol — instrument status, max/min leverage,
qtyStep, minOrderQty, tickSize, last + mark price, USDT wallet balance
and equity, and any open positions. This is enough to distinguish:

- Insufficient balance vs. wrong qty rounding vs. instrument not trading
- Leverage ceiling vs. account-level position cap
- API key missing scope (private GETs would also fail)
- Wrong account type (UNIFIED vs. CONTRACT wallet returns different rows)

Each diagnostic field is independently fault-tolerant: a 401 on one
endpoint doesn't suppress the others.

## Security notes

- `config.json` and `state.json` live in `/var/lib/moneyglitch` (chmod
  700, owned by the service user). Both systemd units run with
  `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`.
- The Telethon `.session` file is sensitive — treat it like a password.
- The bot rejects every update whose `from_user.id` is not in
  `bot.user_ids`.
- Bybit API secrets are shown **once** at key creation. If lost,
  regenerate; never store them outside `/var/lib/moneyglitch/config.json`.

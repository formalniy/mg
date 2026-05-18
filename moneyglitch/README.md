# MoneyGlitch (MEXC edition)

Realtime listener for [@durov](https://t.me/durov) that opens a leveraged
long on a MEXC USDT-perpetual (default **TONCOIN_USDT** — MEXC's API
symbol for the TON Open Network futures, displayed in the web UI as
"TON_USDT") the moment Pavel
posts a message containing `TON`. Multi-account: every account in the
config opens its own trade in parallel (one `httpx` client + one set of
settings per account; each Telegram user is routed to exactly one
account). All trading parameters — margin in USD, leverage, stop-loss %,
take-profit %, fee-neutralizing partial TP, four configurable quick-sell
buttons — are edited live from a Telegram bot with a Russian interface.

This is a port of the Bybit edition back to MEXC's futures (Contract v1)
API. Pricing, fee-neutralize math, and the entire control-bot UX are
identical; only the exchange layer (`moneyglitch/mexc.py`) is exchange-
specific. **Note**: MEXC's public futures-trading API is restricted in
several jurisdictions — verify access from your VPS region before
deploying. Read-only endpoints (instrument/ticker/positions) usually work
even where order submission is blocked.

## How latency is minimized

- **Telethon over MTProto, push-based.** No `GetHistory` polling. The
  Telegram server pushes `updateNewChannelMessage` to the open MTProto
  socket; the handler runs as soon as the socket delivers the update.
- **Novelty check is a single integer compare** (`event.message.id` vs.
  the last seen id). Telegram channel ids are monotonic.
- **No exponential backoff.** Telethon auto-reconnects on transport
  errors; we don't add any sleep on top. MEXC errors propagate as a
  Telegram notification with full diagnostics — the next post retries
  immediately.

## Components

| File | Role |
|---|---|
| `moneyglitch/parser.py` | Telethon client, regex match, dispatch trade |
| `moneyglitch/mexc.py` | MEXC v1 Contract client (HMAC-SHA256, market long + SL, diagnostics) |
| `moneyglitch/bot.py` | aiogram-3 control bot, Russian inline UI |
| `moneyglitch/state.py` | Atomic JSON state shared between processes |
| `moneyglitch/notify.py` | Bot-API push from parser to owner |
| `run_parser.py`, `run_bot.py` | Process entry points |
| `deploy/install.sh` | One-shot Ubuntu 24.x installer |
| `deploy/*.service` | systemd units (Restart=always) |

## Trade flow

For each TON-matching post, the parser fans out to every account whose
`enabled` flag is `true` and runs the following in parallel
(`asyncio.gather`; each account has its own `MEXCFutures`/`httpx`
client). A `trade_lock` only serializes *posts* — accounts inside one
post run concurrently:

1. `GET /api/v1/contract/detail` — `contractSize`, `minVol`, `priceUnit` (tick), `maxLeverage`, `takerFeeRate`.
2. `GET /api/v1/contract/ticker` — `lastPrice` (fallback `fairPrice`).
3. Compute `vol = floor(amount_usd × leverage / (lastPrice × contractSize))` — MEXC futures volume is integer contracts; each contract represents `contractSize` units of the base asset.
4. Compute SL/TP **as % of margin** (with leverage already applied), so the price multiplier is divided by leverage:
   - `stopLossPrice = round_to_tick(lastPrice × (1 − (sl_pct/100)/leverage))`
   - `takeProfitPrice = round_to_tick(lastPrice × (1 + (tp_pct/100)/leverage))` if `take_profit_pct > 0`.
5. `POST /api/v1/private/order/cancel_all` — defensive cleanup so a stale close-side Limit from a previous SL-fired close can't fire on the new position.
6. `POST /api/v1/private/order/submit` — `side=1` (open long), `type=5` (market), `openType=1` (isolated) or `2` (cross), `leverage`, `stopLossPrice`, `takeProfitPrice`. MEXC sets leverage and margin mode atomically on the order itself; no separate `change_leverage` / `switch_isolated` call is needed.
7. Re-read position (up to 5 × 100 ms) via `GET /api/v1/private/position/open_positions` to capture the actual fill `holdAvgPrice` and `holdVol` (and the position id used by close-side reduceOnly Limits).

**Fee-neutralize mode** (toggleable per-account in the bot) replaces the attached TP with two close-side Limit orders:

- A partial TP at `entry × (1 + 3·fee)` for a fraction `X = 3·L·f / (1 + 3·L·f)` of the position — the size whose pre-trade equity equals 3× the round-trip taker fee. When it fills, you've banked exactly the open + this TP + a future full-close fee, so the rest of the position rides risk-free w.r.t. fees.
- The user's main TP (if `take_profit_pct > 0`) on the remaining vol, also `side=4` (close long) Limit. Both order IDs are stored in `state.position` and cancelled on manual close.

The taker fee rate is read from the public `contractDetail.takerFeeRate`; fallback is `0.05%` (MEXC default linear taker).

## AI-gated trading mode

The parser can route the *fire/skip* decision through a neural network
instead of trading on every TON match. The architecture mirrors how the
parser itself works:

- **The neural network is a top-level resource**, like the parser: one
  provider, one API key, one model, one system prompt — configured on the
  VPS in `ai_config.json`, **not** per Telegram user. The HTTP call to the
  model is made **once per post**, regardless of how many accounts are
  attached.
- **Each Telegram user has a local opt-in switch** (per-account
  `ai_enabled` in `state.json`) that decides whether the AI's `0/1` reply
  gates *their* trade. With the switch off, the account fires the long on
  every TON match (existing behavior); with it on, the long fires only
  when the global AI returned `1` for that post.

This matches the existing trading-flag model: the parser runs globally,
but each user has to flip their per-account `enabled` switch **on** to
allow trades on their account in the first place. The new `ai_enabled`
switch sits on the same per-user axis — flipping it on adds the AI
filter on top of the user's already-enabled trading.

### Flow

```
@durov post  ──▶  parser sees "TON"
                       │
                       ▼
                _run_ai_once(post_text)         ← top-level, one HTTP call per post
                       │                          (reads ai_config.json on every call)
                       ▼
            ai_result = {ok | disabled | error}
                       │
                       ▼
         per-account fan-out (asyncio.gather)
                       │
   ┌───────────────────┴────────────────────┐
   │                                        │
[user.enabled = false]                [user.enabled = true]
   │                                        │
   └─ notify "trading disabled"             │
                                            │
                          ┌─────────────────┴──────────────────┐
                          │                                    │
                   [ai_enabled = false]                  [ai_enabled = true]
                          │                                    │
                          ▼                                    ▼
                   open long via MEXC              consult ai_result:
                   (existing trade flow,
                   unaffected by AI status)        decision = 1   → open long
                                                   decision = 0   → notify "skip", no trade
                                                   status = error → notify reason, no trade
                                                   status = disabled
                                                   (no ai_config) → notify, no trade
```

### Global AI config

The parser/bot read `ai_config.json` (path overridable via the
`MONEYGLITCH_AI_CONFIG` env var; default is the CWD). File is **re-read on
every TON post**, so edits take effect without restarting the parser.

```json
{
  "provider": "openrouter",
  "api_key": "sk-or-...",
  "model": "openrouter/owl-alpha",
  "system_prompt": "You are a strict TON cryptocurrency trading signal classifier. Only respond 1 (buy) if the Telegram post explicitly endorses, integrates, or otherwise creates direct bullish demand for TON."
}
```

See `ai_config.example.json`. All required fields must be present for the AI
to be considered "ready"; a missing or malformed file means the AI is
*disabled* and any account with the per-user flag on receives a one-line
notification explaining that the global config is missing.

The provider prefix in the model name (`openrouter/meta-llama/llama-3.1-8b-instruct`,
`huggingface/HuggingFaceH4/zephyr-7b-beta`) is optional and stripped
before the HTTP call.

### What gets sent to the model

- **System prompt**: the operator's `system_prompt` from `ai_config.json`
  (falling back to a default English classifier brief if empty), followed
  by a forced English output-format rule — *"Respond with EXACTLY one
  character: '1' if the post is a positive TON signal and the long should
  be opened, or '0' if the post should be skipped."*
- **User message**: the full Telegram post body (unmodified, in whatever
  language Pavel posted in).

### How the reply is parsed

`_parse_decision` walks the model's reply and returns the first `0` or `1`
digit it finds. Models that obey the format (`"1"`) and models that don't
(`"Decision: 1."`, `"Output: 0"`, `"1\n\nbecause…"`) both produce a valid
signal. If no `0` or `1` appears in the reply at all, the call raises
`AIError` and the trade is **skipped** — the failure mode is conservative
on purpose: an AI outage must never accidentally fire a leveraged long.

### Per-user opt-in in the bot

In Telegram, send `/start`, then tap **🧠 Нейронка**. The submenu shows:

- **Фильтр у вас: ВКЛ / ВЫКЛ** — the per-user toggle (`state.ai_enabled`
  under your account).
- **Глобальные настройки** — a read-only view of `ai_config.json`:
  provider, model, masked API key, and the first 200 chars of the system
  prompt. The Telegram bot has no edit affordance for these — change them
  by editing the file on the VPS and the next post picks up the new values.
- **🔄 Обновить** — re-fetch and re-render the global-config view.
- **⬅️ Назад** — back to the main keyboard.

### Supported providers

| Provider | Endpoint | Auth | Model field example |
|---|---|---|---|
| **OpenRouter** | `POST https://openrouter.ai/api/v1/chat/completions` | `Authorization: Bearer <key>` | `meta-llama/llama-3.1-8b-instruct`, `anthropic/claude-haiku-4-5-20251001` |
| **Hugging Face** | `POST https://api-inference.huggingface.co/models/<model>` | `Authorization: Bearer <key>` | `HuggingFaceH4/zephyr-7b-beta`, `meta-llama/Meta-Llama-3-8B-Instruct` |

### Notifications

For each AI-on account, the parser sends one of the following per post:

- `🧠 <name> · Пост #<id> — нейросеть ответила 1, открываю сделку.`
- `🧠 <name> · Пост #<id> — нейросеть ответила 0, пропускаю.`
- `🧠 <name> · Пост #<id> — пропущен (нейросеть: <reason>)`
- `🧠 <name> · Пост #<id> — пропущен (фильтр ИИ включён у вас, но ai_config.json не настроен на VPS).`

Accounts with AI off see none of these — they fire as before.

On any `MEXCError` the parser broadcasts the raw response **and** runs
`gather_diagnostics(symbol)` which returns instrument info, ticker, USDT
wallet asset, and the open positions list — formatted into a readable
Telegram message so you can see exactly why the order didn't go through.

## Rate limits, accounted for

- **Telethon**: a single MTProto session listening on one channel — no
  flood-wait risk. We do not call `GetHistory`.
- **MEXC v1 Contract**: per-key limits are generous (orders are
  rate-limited at ~20 req/2s for retail). Each TON trigger sends ~4–8
  calls per account (entry + position re-read + optional partial-TP +
  optional main-TP) — well under the cap. A `trade_lock` serializes
  triggers even if posts arrive back-to-back; inside one trigger,
  accounts run in parallel and each has its own `httpx` client /
  connection pool.

## Deploying on Ubuntu 24.x

```bash
# on a fresh VPS:
git clone <your-fork-url> mneygltchmexc
cd mneygltchmexc
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

# 2b. (Optional) configure the AI gate. See ai_config.example.json. Skip
#     this step to run with the AI feature off.
sudo install -o moneyglitch -g moneyglitch -m 600 \
  ai_config.example.json /var/lib/moneyglitch/ai_config.json
sudo nano /var/lib/moneyglitch/ai_config.json

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
**deployed** copy, not your working tree. If you `git pull`, the deployed
code is stale and the manual run will crash. Always sync first.

**Full update** (safe but interrupts both services for a few seconds):

```bash
sudo systemctl stop moneyglitch-parser.service moneyglitch-bot.service
pgrep -af moneyglitch                       # should print nothing
cd ~/mneygltchmexc && git pull
sudo bash deploy/install.sh
sudo systemctl start moneyglitch-bot.service moneyglitch-parser.service
journalctl -u moneyglitch-parser -f
```

**Bot-only update** (when changes touch only `moneyglitch/bot.py` and/or
`moneyglitch/state.py` — parser keeps running):

```bash
cd ~/mneygltchmexc && git pull
sudo bash deploy/install.sh                 # install.sh does NOT restart services
sudo systemctl restart moneyglitch-bot.service
```

To fully disable everything (no auto-restart on boot):

```bash
sudo systemctl disable --now moneyglitch-parser.service moneyglitch-bot.service
sudo pkill -u moneyglitch -f run_parser.py 2>/dev/null || true
sudo pkill -u moneyglitch -f run_bot.py    2>/dev/null || true
```

## Configuration

`/var/lib/moneyglitch/config.json` (canonical multi-account schema, see
`config.example.json`):

```json
{
  "telegram": {
    "api_id": 123456,
    "api_hash": "...",
    "session": "/var/lib/moneyglitch/parser",
    "channel": "durov"
  },
  "bot": {
    "token": "..."
  },
  "accounts": [
    {
      "name": "main",
      "user_ids": [111111111],
      "mexc": {
        "api_key": "...",
        "secret": "...",
        "symbol": "TONCOIN_USDT",
        "isolated": true
      }
    },
    {
      "name": "alt",
      "user_ids": [222222222, 333333333],
      "mexc": {
        "api_key": "...",
        "secret": "...",
        "symbol": "TONCOIN_USDT",
        "isolated": true
      }
    }
  ]
}
```

- `telegram.api_id` / `api_hash`: from <https://my.telegram.org>.
- `telegram.session`: filesystem prefix for the Telethon `.session` blob.
- `telegram.channel`: channel to listen to (`durov` or numeric id).
- `accounts[].name`: arbitrary identifier; must be unique within the file.
- `accounts[].user_ids`: numeric Telegram IDs allowed to manage this
  account. **Disjoint across accounts** — one Telegram user controls at
  most one account. Get IDs from `@userinfobot`.
- `accounts[].mexc.symbol`: MEXC contract symbol. For the TON Open
  Network use **`TONCOIN_USDT`** (the API symbol; MEXC's web UI
  displays it as "TON_USDT" but the actual REST symbol is `TONCOIN_USDT`,
  verified live via `/api/v1/contract/detail`). Other USDT-perp symbols
  follow the same `BASE_USDT` pattern with an underscore.
- `accounts[].mexc.isolated`: `true` → `openType=1` (isolated) per order;
  `false` → `openType=2` (cross).

A legacy single-block schema (`mexc: {...}` + `bot.user_ids` at top level)
is auto-promoted into a one-element `accounts` list by `load_accounts`.

## MEXC API key requirements

Create a key at <https://www.mexc.com/user/openapi> (Futures sub-tab) with:

- ✅ **Read** (positions, balance) — mandatory for diagnostics & live card
- ✅ **Trade** (open/close positions, place orders) — mandatory
- ❌ Withdraw permission — **never** enable
- IP restriction: optional but recommended; whitelist your VPS public IP

The signing scheme is HMAC-SHA256 with the payload
`apiKey + request_time_ms + (sorted_query | raw_json_body)` sent in
headers `ApiKey`/`Request-Time`/`Signature`. See `moneyglitch/mexc.py`.

## Bot interface (Russian)

`/start` shows the account's current state — trading flag, amount,
leverage, SL/TP, fee-neutralize flag, **live USDT wallet balance**, and
the **estimated round-trip taker commission**. Balance is fetched on
demand from `/api/v1/private/account/asset/USDT` with a 10s cache; the
taker fee from `contract.detail` with a 1h cache.

Inline keyboard:

- 💰 **Сумма (USD)** — dollar margin per trade (1–1 000 000).
- 📊 **Плечо** — leverage, integer 1–200 (capped by `contractDetail.maxLeverage`; `TONCOIN_USDT` is 200x as of last check).
- 🛑 **Стоп-лосс (% маржи)** — SL as a percent of margin. Example: `20` at 50× = -0.4% to price.
- 🎯 **Тейк-профит (% маржи)** — TP as a percent of margin, 0 disables.
- 🧮 **Нейтрализация комиссии** — toggle the partial-TP fee-banking mode.
- 💸 **Продажа №1..4** — four user-configurable quick-sell percentages (defaults 25/50/75/100). On a live position the same four buttons appear as `💸 N%` and fire a close-side market order on `N%` of the current live size. `100%` is equivalent to *Закрыть позицию*.
- ▶️ **Включить** / ⏸ **Остановить** — global trading flag for the account.
- 🔄 **Обновить** — re-render status and refresh balance/fee caches.

Live position card (sent by the parser, edited every second by the bot):
shows entry, current price, SL, TP, fee-neutralize TP if active, qty (in
TON), leverage, margin, PnL in USD + % of margin, current USDT balance,
and elapsed time. The four `💸 N%` buttons + `🔴 Закрыть позицию` live
at the bottom.

Only IDs listed in `accounts[].user_ids` are allowed to interact with
the bot; every other update is silently rejected.

## Diagnostics on failure

When `MEXCFutures.open_long_market` raises, Telegram receives two
messages: the raw `code`/`message`, and a structured diagnostic block
with — for the configured symbol — instrument state, max/min leverage,
contractSize, minVol, priceUnit, takerFeeRate, last + fair price, USDT
wallet asset, and any open positions. Each diagnostic field is
independently fault-tolerant: a 401 on one endpoint doesn't suppress
the others.

## Security notes

- `config.json` and `state.json` live in `/var/lib/moneyglitch` (chmod
  700, owned by the service user). Both systemd units run with
  `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`.
- The Telethon `.session` file is sensitive — treat it like a password.
- The bot rejects every update whose `from_user.id` is not listed in any
  `accounts[].user_ids` block.
- MEXC API secrets are shown **once** at key creation. If lost,
  regenerate; never store them outside `/var/lib/moneyglitch/config.json`.

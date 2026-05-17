# MoneyGlitch (Bybit edition)

Realtime listener for [@durov](https://t.me/durov) that opens a leveraged
long on a Bybit USDT-perpetual (default **TONUSDT**) the moment Pavel
posts a message containing `TON`. Multi-account: every account in the
config opens its own trade in parallel (one `httpx` client + one set of
settings per account; each Telegram user is routed to exactly one
account). All trading parameters — margin in USD, leverage, stop-loss %,
take-profit %, fee-neutralizing partial TP, four configurable quick-sell
buttons — are edited live from a Telegram bot with a Russian interface.

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

For each TON-matching post, the parser fans out to every account whose
`enabled` flag is `true` and runs the following in parallel
(`asyncio.gather`; each account has its own `BybitFutures`/`httpx`
client). A `trade_lock` only serializes *posts* — accounts inside one
post run concurrently:

1. `GET /v5/market/instruments-info` — `qtyStep`, `minOrderQty`, `tickSize`, `maxLeverage`.
2. `GET /v5/market/tickers` — `lastPrice` (fallback `markPrice`).
3. Compute `qty = floor(amount_usd × leverage / lastPrice / qtyStep) × qtyStep` (in TON, not contracts).
4. Compute SL/TP **as % of margin** (with leverage already applied), so the price multiplier is divided by leverage:
   - `stopLoss = round_to_tick(lastPrice × (1 − (sl_pct/100)/leverage))`
   - `takeProfit = round_to_tick(lastPrice × (1 + (tp_pct/100)/leverage))` if `take_profit_pct > 0`.
5. `POST /v5/order/cancel-all` — defensive cleanup so a stale reduceOnly Limit from a previous SL-fired close can't fire on the new position.
6. `POST /v5/position/set-leverage` (idempotent on `retCode=110043`).
7. `POST /v5/position/switch-isolated` (best-effort; non-fatal on UTA where margin mode is account-level — codes `110024/110026/110028` are swallowed).
8. `POST /v5/order/create` — `side=Buy`, `orderType=Market`, `stopLoss`, `slTriggerBy=MarkPrice`. The take-profit is attached to the entry order **only** when fee-neutralize is off.
9. Re-read position (up to 5 × 100 ms) to capture the actual fill `avgPrice` and `size`.

**Fee-neutralize mode** (toggleable per-account in the bot) replaces the attached TP with two reduceOnly Limit orders:

- A partial TP at `entry × (1 + 3·fee)` for a fraction `X = 3·L·f / (1 + 3·L·f)` of the position — the size whose pre-trade equity equals 3× the round-trip taker fee. When it fills, you've banked exactly the open + this TP + a future full-close fee, so the rest of the position rides risk-free w.r.t. fees.
- The user's main TP (if `take_profit_pct > 0`) on the remaining qty, also reduceOnly Limit, GTC. Both order IDs are stored in `state.position` and cancelled on manual close.

The live taker fee rate comes from `GET /v5/account/fee-rate`; fallback is `0.055%` (Bybit VIP0 linear taker).

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
                   open long via Bybit             consult ai_result:
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

See `ai_config.example.json`. All four fields are required for the AI to be
considered "ready"; a missing or malformed file means the AI is *disabled*
and any account with the per-user flag on receives a one-line notification
explaining that the global config is missing.

The provider prefix in the model name (`openrouter/owl-alpha`,
`huggingface/meta-llama/Llama-3-8B-Instruct`) is optional and stripped
before the HTTP call, so `owl-alpha` and `openrouter/owl-alpha` are
equivalent when `provider` is `openrouter`.

### What gets sent to the model

- **System prompt**: the operator's `system_prompt` from `ai_config.json`
  (falling back to a default English classifier brief if empty), followed
  by a forced English output-format rule — *"Respond with EXACTLY one
  character: '1' if the post is a positive TON signal and the long should
  be opened, or '0' if the post should be skipped."* This wrapper is
  always appended, so reply parsing is consistent across models.
- **User message**: the full Telegram post body (unmodified, in whatever
  language Pavel posted in — modern chat models handle multilingual input).

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
  under your account). Off → existing buy-every-TON behavior for you. On →
  your trade is gated by the global AI's `0/1` reply.
- **Глобальные настройки** — a read-only view of `ai_config.json`:
  provider, model, masked API key (`abcd…wxyz`), and the first 200 chars
  of the system prompt. The Telegram bot has no edit affordance for these
  — change them by editing the file on the VPS and the next post picks up
  the new values.
- **🔄 Обновить** — re-fetch and re-render the global-config view.
- **⬅️ Назад** — back to the main keyboard.

The only AI field stored per-user in `state.json` is `ai_enabled` (bool).

### Supported providers

| Provider | Endpoint | Auth | Model field example |
|---|---|---|---|
| **OpenRouter** | `POST https://openrouter.ai/api/v1/chat/completions` | `Authorization: Bearer <key>` | `openrouter/owl-alpha`, `anthropic/claude-haiku-4-5-20251001` |
| **Hugging Face** | `POST https://api-inference.huggingface.co/models/<model>` | `Authorization: Bearer <key>` | `huggingface/meta-llama/Llama-3-8B-Instruct` |

OpenRouter uses the OpenAI-style `messages` schema (`role: system` +
`role: user`). Hugging Face's Inference API expects a flat `inputs`
string, so the system prompt and the post are concatenated with a blank
line. In both cases the call is wrapped in a 30 s `httpx` timeout —
exceeded timeouts surface as `AIError` and skip the trade for AI-on
accounts.

### Notifications

For each AI-on account, the parser sends one of the following per post:

- `🧠 <name> · Пост #<id> — нейросеть ответила 1, открываю сделку.` —
  followed by the usual position card from `_trade_for_account`.
- `🧠 <name> · Пост #<id> — нейросеть ответила 0, пропускаю.` — terminal.
- `🧠 <name> · Пост #<id> — пропущен (нейросеть: <reason>)` — terminal;
  HTTP error, timeout, or unparseable reply.
- `🧠 <name> · Пост #<id> — пропущен (фильтр ИИ включён у вас, но
  ai_config.json не настроен на VPS).` — terminal; operator hasn't
  populated the global config.

Accounts with AI off see none of these — they fire as before.

### Cost & latency

- **One HTTP call per post**, not per account. The global config is a
  shared resource: 1 user with AI on and 50 users with AI on cost the
  same on the model side.
- AI gating adds one round-trip to the model provider **before** the
  Bybit fan-out, but only for AI-on accounts — accounts with AI off
  start the trade immediately because they ignore `ai_result`
  regardless of its status.
- `_run_ai_once` is awaited *before* `_trade_for_account` fans out;
  trades for AI-off accounts therefore wait for the AI call to return
  too. This is intentional: the parser-level `trade_lock` already
  serializes posts, and the latency floor on `@durov` is set by
  Telegram MTProto delivery, not by a 1-2 s OpenRouter call.

On any `BybitError` the parser broadcasts the raw response **and** runs
`gather_diagnostics(symbol)` which returns instrument info, ticker, both
UNIFIED+CONTRACT wallet balances, and the open positions list — formatted
into a readable Telegram message so you can see exactly why the order
didn't go through.

## Rate limits, accounted for

- **Telethon**: a single MTProto session listening on one channel — no
  flood-wait risk. We do not call `GetHistory`.
- **Bybit v5**: per-key limits are generous (e.g. `order/create` at 10
  req/s for retail UTA). Each TON trigger sends ~6–10 calls per account
  (entry + position re-read + optional partial-TP + optional main-TP) —
  orders of magnitude under the cap. A `trade_lock` serializes triggers
  even if posts arrive back-to-back; inside one trigger, accounts run in
  parallel and each has its own `httpx` client / connection pool.

## Deploying on Ubuntu 24.x

```bash
# on a fresh VPS:
git clone https://github.com/formalniy/mg -b Bedtime_Stories
cd mg
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
#     this step to run with the AI feature off — accounts will still work
#     normally, but the per-user "🧠 Нейронка" toggle will report the
#     global config as not set.
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
**deployed** copy, not your working tree. If you `git pull` (or are
upgrading from the older MEXC fork), the deployed code is stale and the
manual run will crash with errors like `KeyError: 'mexc'` even though the
repo source is correct. Always sync first.

**Full update** (safe but interrupts both services for a few seconds):

```bash
# Stop everything before touching code or config
sudo systemctl stop moneyglitch-parser.service moneyglitch-bot.service
pgrep -af moneyglitch                       # should print nothing

# Re-deploy: copies repo -> /opt/moneyglitch (idempotent, safe to re-run)
cd ~/mg                                     # or wherever you cloned it
git pull
sudo bash deploy/install.sh

# Bring services back up
sudo systemctl start moneyglitch-bot.service moneyglitch-parser.service
journalctl -u moneyglitch-parser -f
```

**Bot-only update** (when changes touch only `moneyglitch/bot.py` and/or
`moneyglitch/state.py` — parser keeps running without a pause, no posts
missed):

```bash
cd ~/mg && git pull
sudo bash deploy/install.sh                 # install.sh does NOT restart services
sudo systemctl restart moneyglitch-bot.service
```

Python loads modules into memory at start, so overwriting `/opt/moneyglitch/*` on disk does not affect the running parser process. `state.py` schema is additive (new keys merge in via `DEFAULT_ACCOUNT`), so an old parser writing state alongside a new bot won't drop the new fields — the `{**DEFAULT_ACCOUNT, **prev, **fields}` pattern in `update_account` preserves anything already on disk.

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
      "bybit": {
        "api_key": "...",
        "secret": "...",
        "symbol": "TONUSDT",
        "isolated": true
      }
    },
    {
      "name": "alt",
      "user_ids": [222222222, 333333333],
      "bybit": {
        "api_key": "...",
        "secret": "...",
        "symbol": "TONUSDT",
        "isolated": true
      }
    }
  ]
}
```

- `telegram.api_id` / `api_hash`: from <https://my.telegram.org>.
- `telegram.session`: filesystem prefix for the Telethon `.session` blob.
  One file per parser process — running two parsers from the same
  `.session` will fight for the MTProto slot and log each other out.
- `telegram.channel`: channel to listen to. Username (`durov`, with or
  without `@`) or numeric id (e.g. `-1001234567890` for private channels;
  the Telethon user account must already be a member).
- `accounts[].name`: arbitrary identifier; appears on every position
  card and in logs. Must be unique within the file.
- `accounts[].user_ids`: numeric Telegram IDs allowed to manage this
  account and receive its notifications. **Disjoint across accounts** —
  one Telegram user controls at most one account. Get IDs from
  `@userinfobot`.
- `accounts[].bybit.symbol`: Bybit linear symbol, no underscore —
  `TONUSDT`, not `TON_USDT`. Different accounts can trade different
  symbols.
- `accounts[].bybit.isolated`: `true` requests isolated margin per
  symbol; on UTA accounts where margin mode is account-level, this is a
  no-op and is logged as non-fatal.

A legacy single-block schema (`bybit: {...}` + `bot.user_ids` at top
level) is auto-promoted into a one-element `accounts` list by
`load_accounts`, so old configs keep working without edits.

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

`/start` shows the account's current state — trading flag, amount,
leverage, SL/TP, fee-neutralize flag, **live USDT wallet balance**, and
the **estimated round-trip taker commission** (`amount × leverage ×
fee_rate` for each side, plus the total). Balance is fetched on demand
from `/v5/account/wallet-balance` with a 10s cache; the taker fee from
`/v5/account/fee-rate` with a 1h cache.

Inline keyboard:

- 💰 **Сумма (USD)** — dollar margin per trade (1–1 000 000).
- 📊 **Плечо** — leverage, integer 1–200 (Bybit caps TONUSDT at 50x).
- 🛑 **Стоп-лосс (% маржи)** — SL as a percent of margin (with leverage already applied). Example: `20` at 50× = -0.4% to price.
- 🎯 **Тейк-профит (% маржи)** — TP as a percent of margin, 0 disables.
- 🧮 **Нейтрализация комиссии** — toggle the partial-TP fee-banking mode described in *Trade flow*.
- 💸 **Продажа №1..4** — four user-configurable quick-sell percentages (defaults 25/50/75/100). Click to edit each one's percent (0.1–100). When a position is open the live card shows the same four buttons in `💸 N%` form: clicking one fires a reduceOnly market order on `N%` of the current live size. `100%` is equivalent to *Закрыть позицию*; lower values update the tracked `qty` so the live PnL reflects the remaining size. A confirmation message reports the realized PnL and the remaining qty.
- ▶️ **Включить** / ⏸ **Остановить** — global trading flag for the account.
- 🔄 **Обновить** — re-render status and refresh balance/fee caches.

Live position card (sent by the parser, edited every second by the bot):
shows entry, current price, SL, TP, fee-neutralize TP if active, qty,
leverage, margin, PnL in USD + % of margin, current USDT balance, and
elapsed time. The four `💸 N%` buttons + `🔴 Закрыть позицию` live at
the bottom.

Only IDs listed in `accounts[].user_ids` are allowed to interact with
the bot; every other update is silently rejected. All listed users also
receive trade and error notifications from the parser for their
account.

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
- The bot rejects every update whose `from_user.id` is not listed in any
  `accounts[].user_ids` block (the legacy `bot.user_ids` is still
  honored via auto-promotion in `load_accounts`).
- Bybit API secrets are shown **once** at key creation. If lost,
  regenerate; never store them outside `/var/lib/moneyglitch/config.json`.

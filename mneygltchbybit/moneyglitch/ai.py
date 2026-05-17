"""AI / neural network forwarder.

The neural network is configured **globally** on the VPS (one provider, one
API key, one model, one system prompt) via a separate JSON config file
pointed to by `MONEYGLITCH_AI_CONFIG` (default `ai_config.json` in CWD).
Per-user opt-in lives in `state.json` as the `ai_enabled` flag.

Supports two providers:
- OpenRouter (OpenAI-compatible chat completions API).
- Hugging Face Inference API (text-generation pipeline).

`ask_ai_decision` returns 0 (skip) or 1 (buy) for a given post text, or
raises `AIError` with a short human-readable diagnostic. Failure mode is
conservative: the parser treats any AI failure as a skip for AI-gated
accounts so an outage cannot accidentally fire a leveraged long.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HUGGINGFACE_URL = "https://api-inference.huggingface.co/models/{model}"

AI_TIMEOUT_SECONDS = 30.0

AI_CONFIG_PATH = Path(os.environ.get("MONEYGLITCH_AI_CONFIG", "ai_config.json"))

# Wrapper instructions enforcing the 0/1 output format. Forced English because
# all major chat models follow English instructions more reliably than RU/other
# locales — the operator's custom system_prompt provides the decision criteria.
DECISION_DEFAULT_CRITERIA = (
    "You are a binary trading-signal classifier for the TON cryptocurrency. "
    "You will receive a Telegram post that mentions TON. Decide whether the "
    "post is a bullish signal that should trigger an immediate leveraged "
    "long position on TON, or whether it should be ignored."
)
DECISION_OUTPUT_RULES = (
    "Respond with EXACTLY one character and nothing else: '1' if the post is "
    "a positive TON signal and the long should be opened, or '0' if the post "
    "should be skipped. No words, no punctuation, no explanation — only the "
    "single digit."
)


class AIError(Exception):
    pass


def load_ai_config() -> Dict[str, Any]:
    """Read the global AI config from disk. Returns an empty dict if the
    file is missing — that's how the operator signals "AI not configured"
    and the parser falls back to unconditional trading for all accounts.

    Re-read on every call so an edit to `ai_config.json` takes effect on
    the next TON post without restarting the parser/bot.
    """
    if not AI_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("ai_config.json unreadable: %s", e)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def ai_config_ready(cfg: Dict[str, Any]) -> bool:
    """True if the config has the minimum fields to make a successful call."""
    return bool(cfg.get("api_key")) and bool(cfg.get("model")) and bool(cfg.get("provider"))


def _strip_provider_prefix(model: str, prefix: str) -> str:
    """`openrouter/foo/bar` -> `foo/bar`. Leaves untouched if no prefix."""
    m = (model or "").strip()
    p = prefix.lower() + "/"
    if m.lower().startswith(p):
        return m[len(p):]
    return m


async def _ask_openrouter(
    api_key: str,
    model: str,
    system_prompt: str,
    user_text: str,
) -> str:
    payload: Dict[str, Any] = {
        "model": _strip_provider_prefix(model, "openrouter"),
        "messages": [],
    }
    if system_prompt:
        payload["messages"].append({"role": "system", "content": system_prompt})
    payload["messages"].append({"role": "user", "content": user_text})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as c:
        r = await c.post(OPENROUTER_URL, json=payload, headers=headers)
    if r.status_code >= 400:
        raise AIError(f"openrouter HTTP {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
    except ValueError as e:
        raise AIError(f"openrouter bad JSON: {e}") from None
    choices = data.get("choices") or []
    if not choices:
        raise AIError(f"openrouter no choices: {str(data)[:300]}")
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if not content:
        raise AIError(f"openrouter empty content: {str(data)[:300]}")
    return str(content).strip()


async def _ask_huggingface(
    api_key: str,
    model: str,
    system_prompt: str,
    user_text: str,
) -> str:
    name = _strip_provider_prefix(model, "huggingface")
    name = _strip_provider_prefix(name, "hf")
    if not name:
        raise AIError("huggingface model is empty")

    if system_prompt:
        prompt = f"{system_prompt}\n\n{user_text}"
    else:
        prompt = user_text

    payload = {"inputs": prompt, "parameters": {"return_full_text": False}}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = HUGGINGFACE_URL.format(model=name)
    async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as c:
        r = await c.post(url, json=payload, headers=headers)
    if r.status_code >= 400:
        raise AIError(f"huggingface HTTP {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
    except ValueError as e:
        raise AIError(f"huggingface bad JSON: {e}") from None

    # HF returns either [{"generated_text": "..."}] or {"generated_text": "..."}.
    out: Optional[str] = None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            out = first.get("generated_text") or first.get("summary_text")
        elif isinstance(first, str):
            out = first
    elif isinstance(data, dict):
        out = data.get("generated_text") or data.get("summary_text")
        if not out and data.get("error"):
            raise AIError(f"huggingface: {data['error']}")
    if not out:
        raise AIError(f"huggingface unexpected response: {str(data)[:300]}")
    return str(out).strip()


async def _ask_raw(
    provider: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_text: str,
) -> str:
    p = (provider or "").strip().lower()
    if p == "openrouter":
        return await _ask_openrouter(api_key, model, system_prompt, user_text)
    if p in ("huggingface", "hf"):
        return await _ask_huggingface(api_key, model, system_prompt, user_text)
    raise AIError(f"Неизвестный провайдер: {provider}")


def _parse_decision(raw: str) -> int:
    """Pull the first '0' or '1' digit out of the model's reply.

    Tolerant of models that ignore the "no explanation" instruction —
    "Decision: 1", "1.", " 0 " etc. all parse correctly. Raises AIError if
    no 0/1 appears in the reply at all.
    """
    s = (raw or "").strip()
    if not s:
        raise AIError("пустой ответ нейросети")
    for ch in s:
        if ch == "0" or ch == "1":
            return int(ch)
    raise AIError(f"в ответе нет 0 или 1: {s[:120]!r}")


async def ask_ai_decision(cfg: Dict[str, Any], post_text: str) -> int:
    """Ask the configured model to classify `post_text` as 1 (buy) or 0 (skip).

    `cfg` is the global AI config (see `load_ai_config`). The operator's
    `system_prompt` (if non-empty) replaces the default English decision
    criteria; the strict 0/1 output rules are always appended.

    Returns 0 or 1. Raises AIError if the call fails or the reply is
    unparseable.
    """
    if not ai_config_ready(cfg):
        raise AIError("AI config not set (provider/api_key/model missing)")
    provider = str(cfg.get("provider") or "")
    api_key = str(cfg.get("api_key") or "")
    model = str(cfg.get("model") or "")
    system_prompt = str(cfg.get("system_prompt") or "").strip()
    base = system_prompt or DECISION_DEFAULT_CRITERIA
    full_prompt = f"{base}\n\n{DECISION_OUTPUT_RULES}"
    raw = await _ask_raw(provider, api_key, model, full_prompt, post_text)
    return _parse_decision(raw)

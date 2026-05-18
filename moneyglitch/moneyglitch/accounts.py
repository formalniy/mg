"""Per-account trading account routing.

An account = one set of MEXC credentials + the Telegram user IDs allowed
to manage trades on it. user_id sets must be disjoint across accounts:
each Telegram user controls at most one account.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional


@dataclass(frozen=True)
class AccountConfig:
    name: str
    user_ids: FrozenSet[int]
    api_key: str
    secret: str
    symbol: str = "TONCOIN_USDT"
    isolated: bool = True


def _normalize_user_ids(raw: Any) -> FrozenSet[int]:
    if not raw:
        return frozenset()
    return frozenset(int(x) for x in raw if int(x) != 0)


def load_accounts(config: Dict[str, Any]) -> List[AccountConfig]:
    """Parse `accounts: [...]`. Falls back to the legacy single-block schema
    (`mexc` + `bot.user_ids`) so old configs still work."""
    raw = config.get("accounts")
    if not raw:
        mexc = config.get("mexc") or {}
        bot_cfg = config.get("bot") or {}
        users = bot_cfg.get("user_ids") or (
            [bot_cfg["user_id"]] if bot_cfg.get("user_id") else []
        )
        if not mexc.get("api_key"):
            raise ValueError("config has neither 'accounts' nor 'mexc'")
        raw = [{"name": "main", "user_ids": users, "mexc": mexc}]

    accounts: List[AccountConfig] = []
    seen_names: set = set()
    seen_users: Dict[int, str] = {}
    for i, a in enumerate(raw):
        name = str(a.get("name") or f"acct{i}")
        if name in seen_names:
            raise ValueError(f"duplicate account name: {name}")
        seen_names.add(name)
        users = _normalize_user_ids(a.get("user_ids"))
        for u in users:
            prev = seen_users.get(u)
            if prev is not None:
                raise ValueError(
                    f"user_id {u} listed in both '{prev}' and '{name}' — must be unique"
                )
            seen_users[u] = name
        # Accept either "mexc" (canonical) or "bybit" (for migration ease).
        mexc = a.get("mexc") or a.get("bybit") or {}
        if not mexc.get("api_key") or not mexc.get("secret"):
            raise ValueError(f"account '{name}' missing mexc api_key/secret")
        accounts.append(AccountConfig(
            name=name,
            user_ids=users,
            api_key=str(mexc["api_key"]),
            secret=str(mexc["secret"]),
            symbol=str(mexc.get("symbol") or "TONCOIN_USDT"),
            isolated=bool(mexc.get("isolated", True)),
        ))
    return accounts


def account_for_user(accounts: List[AccountConfig], user_id: int) -> Optional[AccountConfig]:
    for a in accounts:
        if user_id in a.user_ids:
            return a
    return None


def all_user_ids(accounts: List[AccountConfig]) -> FrozenSet[int]:
    out: set = set()
    for a in accounts:
        out.update(a.user_ids)
    return frozenset(out)

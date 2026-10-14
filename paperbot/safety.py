"""Hard safety guards. Called at startup and again before every simulated order.

1. PAPER_MODE must be exactly "true". There is no live mode in this codebase.
2. The process must not hold anything that looks like a wallet key or
   trading credential. If it does, the bot refuses to run rather than risk
   storing or logging it.
"""

from __future__ import annotations

import os
import re
from typing import Mapping

# Env var names that suggest wallet / exchange trading credentials.
_FORBIDDEN_NAME_PATTERNS = [
    r"PRIVATE[_-]?KEY",
    r"MNEMONIC",
    r"SEED[_-]?PHRASE",
    r"WALLET",
    r"^POLY(MARKET)?_",
    r"CLOB[_-]?(API[_-]?)?(KEY|SECRET|PASSPHRASE)",
    r"^PK$",
    r"FUNDER",
]
# Values that look like an EVM private key (64 hex chars, optional 0x).
_HEX_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class SafetyError(RuntimeError):
    pass


def assert_paper_mode(env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    value = env.get("PAPER_MODE", "")
    if value.strip().lower() != "true":
        raise SafetyError(
            "PAPER_MODE is not 'true'. This bot only supports paper trading; "
            "set PAPER_MODE=true to run it."
        )


def find_forbidden_credentials(env: Mapping[str, str] | None = None) -> list[str]:
    """Return names (never values) of env vars that look like wallet/trading secrets."""
    env = os.environ if env is None else env
    hits = []
    for name, value in env.items():
        upper = name.upper()
        if any(re.search(p, upper) for p in _FORBIDDEN_NAME_PATTERNS):
            hits.append(name)
        elif value and _HEX_KEY_RE.match(value.strip()):
            hits.append(name)
    return sorted(hits)


def assert_no_wallet_credentials(env: Mapping[str, str] | None = None) -> None:
    hits = find_forbidden_credentials(env)
    if hits:
        raise SafetyError(
            "Refusing to run: environment contains variables that look like wallet "
            f"or trading credentials: {hits}. This bot never needs them. Remove them."
        )


def startup_checks(env: Mapping[str, str] | None = None) -> None:
    assert_paper_mode(env)
    assert_no_wallet_credentials(env)

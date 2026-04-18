"""Shared test fixtures and path setup for Casa tests.

Also installs the `telegram.*` package stubs needed by the Telegram
channel tests. Installing here (once, at session start) guarantees
that every test file sees the SAME `NetworkError` / `TimedOut` /
`TelegramError` class objects, so `except NetworkError:` in
`channels.telegram` catches exceptions raised in tests via these
names. If each test file installed its own stubs instead, pytest's
alphabetical discovery order would decide which file's class "wins",
and later files' locally-defined `_FakeNetworkError` would diverge
from the one production code catches.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Ensure the Casa package root is importable.
_casa_root = str(Path(__file__).resolve().parent.parent / "casa-agent" / "rootfs" / "opt" / "casa")
if _casa_root not in sys.path:
    sys.path.insert(0, _casa_root)


# ---------------------------------------------------------------------------
# telegram.* stubs — shared canonical exception classes across all tests.
# ---------------------------------------------------------------------------


class _FakeNetworkError(Exception):
    pass


class _FakeTimedOut(Exception):
    pass


class _FakeTelegramError(Exception):
    pass


def _install_telegram_stubs() -> None:
    if "telegram" in sys.modules and getattr(
        sys.modules["telegram"], "_casa_stub", False,
    ):
        return

    tg = types.ModuleType("telegram")
    tg._casa_stub = True  # type: ignore[attr-defined]
    tg.Update = MagicMock()

    tg_const = types.ModuleType("telegram.constants")
    tg_const.ChatAction = MagicMock()
    tg.constants = tg_const

    tg_err = types.ModuleType("telegram.error")
    tg_err.TelegramError = _FakeTelegramError
    tg_err.NetworkError = _FakeNetworkError
    tg_err.TimedOut = _FakeTimedOut
    tg.error = tg_err

    tg_ext = types.ModuleType("telegram.ext")
    tg_ext.Application = MagicMock()
    tg_ext.ContextTypes = MagicMock()
    tg_ext.MessageHandler = MagicMock()
    tg_ext.filters = MagicMock()
    tg.ext = tg_ext

    sys.modules["telegram"] = tg
    sys.modules["telegram.constants"] = tg_const
    sys.modules["telegram.error"] = tg_err
    sys.modules["telegram.ext"] = tg_ext


_install_telegram_stubs()

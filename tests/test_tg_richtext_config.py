"""The telegram_rich_text kill-switch: env-flag parsing (default-on)."""
from casa_core import _rich_text_enabled_from_env


def test_env_flag_parsing():
    assert _rich_text_enabled_from_env({}) is True
    assert _rich_text_enabled_from_env({"TELEGRAM_RICH_TEXT": "true"}) is True
    assert _rich_text_enabled_from_env({"TELEGRAM_RICH_TEXT": "false"}) is False
    assert _rich_text_enabled_from_env({"TELEGRAM_RICH_TEXT": "off"}) is False
    assert _rich_text_enabled_from_env({"TELEGRAM_RICH_TEXT": "0"}) is False
    assert _rich_text_enabled_from_env({"TELEGRAM_RICH_TEXT": "  TRUE  "}) is True

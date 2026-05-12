"""U3 + U13 emoji lookup tables + topic-title composition.

Spec: docs/superpowers/specs/2026-05-12-e12-claude_code-channels.md §6.3, §6.9.
"""

from __future__ import annotations

import re

from text_util import truncate_for_topic

# §6.3 state emoji.
STATE_EMOJI: dict[str, str] = {
    "active": "🟢",
    "awaiting": "🟡",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "⏹",
}

# §6.3 role emoji.
ROLE_EMOJI: dict[str, str] = {
    "configurator": "⚙️",
    "plugin-developer": "🛠",
    "casa-builder": "🏗",
    "automation-builder": "🔁",
}

_DEFAULT_ROLE_EMOJI = "🤖"

# §6.9 progress-panel glyphs.
PROGRESS_GLYPH: dict[str, str] = {
    "pending": "☐",
    "in_progress": "⏳",
    "completed": "☑",
    "blocked": "🚫",
    "skipped": "⏭",
}

# §6.3 byte budget for U3 task body (tightened from TELEGRAM_TOPIC_NAME_BYTES=128).
U3_TASK_BYTE_BUDGET = 22

_LEADING_FILLER_RE = re.compile(
    r"^(?:please|can you|i need you to|help me|could you|"
    r"would you|let's|let us)\s+",
    re.IGNORECASE,
)
# §6.3 rule 2: drop articles as whole word tokens (anywhere, not just leading).
_ARTICLE_RE = re.compile(r"\b(?:the|a|an)\s+", re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r"[\.!\?:,;]+$")


def role_emoji(executor_type: str) -> str:
    """§6.3: fall back to 🤖 for unknown executor types (never raises)."""
    return ROLE_EMOJI.get(executor_type, _DEFAULT_ROLE_EMOJI)


def concise_task(task: str) -> str:
    """Apply §6.3 concision rules 1-4 to ``task``.

    Returns a string whose UTF-8 byte length is ≤ U3_TASK_BYTE_BUDGET.
    """
    if not task:
        return ""
    s = task.strip()
    while True:
        m = _LEADING_FILLER_RE.match(s)
        if not m:
            break
        s = s[m.end():].lstrip()
    s = _ARTICLE_RE.sub("", s)
    s = _TRAILING_PUNCT_RE.sub("", s).rstrip()
    return truncate_for_topic(s, byte_budget=U3_TASK_BYTE_BUDGET)


def compose_topic_title(*, state: str, role: str, short_task: str) -> str:
    """Compose '<state>·<role> <short_task>' per §6.3.

    Unknown ``state`` falls back to 🟢 (active) — never raises.
    """
    se = STATE_EMOJI.get(state, STATE_EMOJI["active"])
    re_ = role_emoji(role)
    return f"{se}·{re_} {short_task}".rstrip()

"""Lookup-table coverage for state_emoji per spec §6.3, §6.9."""

from __future__ import annotations


def test_state_emoji_table_covers_locked_states():
    from channels.state_emoji import STATE_EMOJI
    assert STATE_EMOJI["active"] == "🟢"
    assert STATE_EMOJI["awaiting"] == "🟡"
    assert STATE_EMOJI["completed"] == "✅"
    assert STATE_EMOJI["failed"] == "❌"
    assert STATE_EMOJI["cancelled"] == "⏹"


def test_role_emoji_table_covers_known_executors():
    from channels.state_emoji import ROLE_EMOJI
    assert ROLE_EMOJI["configurator"] == "⚙️"
    assert ROLE_EMOJI["plugin-developer"] == "🛠"


def test_role_emoji_unknown_falls_back_to_robot():
    from channels.state_emoji import role_emoji
    assert role_emoji("automation-builder") == "🔁"
    assert role_emoji("casa-builder") == "🏗"
    assert role_emoji("totally-unknown-type") == "🤖"


def test_progress_glyphs_match_spec():
    from channels.state_emoji import PROGRESS_GLYPH
    assert PROGRESS_GLYPH["pending"] == "☐"
    assert PROGRESS_GLYPH["completed"] == "☑"
    assert PROGRESS_GLYPH["blocked"] == "🚫"
    assert PROGRESS_GLYPH["skipped"] == "⏭"
    assert PROGRESS_GLYPH["in_progress"] == "⏳"


def test_compose_topic_title_format():
    from channels.state_emoji import compose_topic_title
    title = compose_topic_title(
        state="active", role="plugin-developer", short_task="add Skill probe-foo",
    )
    assert title == "🟢·🛠 add Skill probe-foo"


def test_concision_strips_filler_drops_articles():
    from channels.state_emoji import concise_task
    assert concise_task(
        "Please add a Skill for the casa-probe-foo plugin"
    ).startswith("add Skill")
    assert concise_task(
        "Can you wire up the morning briefing trigger?"
    ).startswith("wire morning") or concise_task(
        "Can you wire up the morning briefing trigger?"
    ).startswith("wire up morning")


def test_concise_task_respects_byte_budget():
    """U3_TASK_BYTE_BUDGET = 22 (§6.3) — output UTF-8 length ≤ budget."""
    from channels.state_emoji import concise_task, U3_TASK_BYTE_BUDGET
    long = "make the Telegram bot post a snazzy progress panel"
    out = concise_task(long)
    assert len(out.encode("utf-8")) <= U3_TASK_BYTE_BUDGET


def test_compose_topic_title_unknown_state_falls_back_to_active():
    from channels.state_emoji import compose_topic_title
    title = compose_topic_title(state="banana", role="plugin-developer", short_task="x")
    assert title.startswith("🟢")

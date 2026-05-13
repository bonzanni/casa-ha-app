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
    # casa-builder and automation-builder removed — never shipped as
    # actual executors; entries dropped 2026-05-13 as part of v0.37.1
    # D-1 cleanup. ROLE_EMOJI is retained for any future caller that
    # wants the visual role glyph in non-topic contexts (logs, etc.).
    assert "casa-builder" not in ROLE_EMOJI
    assert "automation-builder" not in ROLE_EMOJI


def test_role_emoji_unknown_falls_back_to_robot():
    from channels.state_emoji import role_emoji
    assert role_emoji("totally-unknown-type") == "🤖"
    # Previously-known aspirational roles also fall through to default
    # after the v0.37.1 ROLE_EMOJI cleanup.
    assert role_emoji("casa-builder") == "🤖"
    assert role_emoji("automation-builder") == "🤖"


def test_progress_glyphs_match_spec():
    from channels.state_emoji import PROGRESS_GLYPH
    assert PROGRESS_GLYPH["pending"] == "☐"
    assert PROGRESS_GLYPH["completed"] == "☑"
    assert PROGRESS_GLYPH["blocked"] == "🚫"
    assert PROGRESS_GLYPH["skipped"] == "⏭"
    assert PROGRESS_GLYPH["in_progress"] == "⏳"


def test_compose_topic_title_format():
    from channels.state_emoji import compose_topic_title
    # v0.37.1 D-1: title no longer carries the role emoji — bubble
    # does (via channels.topic_icons). Format is "<state> <task>".
    title = compose_topic_title(
        state="active", short_task="add Skill probe-foo",
    )
    assert title == "🟢 add Skill probe-foo"


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
    """U3_TASK_BYTE_BUDGET = 26 (v0.37.1: bumped from 22 after
    dropping the role-emoji prefix from the title)."""
    from channels.state_emoji import concise_task, U3_TASK_BYTE_BUDGET
    assert U3_TASK_BYTE_BUDGET == 26
    long = "make the Telegram bot post a snazzy progress panel"
    out = concise_task(long)
    assert len(out.encode("utf-8")) <= U3_TASK_BYTE_BUDGET


def test_compose_topic_title_unknown_state_falls_back_to_active():
    from channels.state_emoji import compose_topic_title
    title = compose_topic_title(state="banana", short_task="x")
    assert title.startswith("🟢")
    assert title == "🟢 x"

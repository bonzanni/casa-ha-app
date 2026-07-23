"""Translate Casa executor hooks.yaml into Claude Code .claude/settings.json.

The CC hook shape is:
    {"hooks": {"PreToolUse": [{"matcher": "...", "hooks": [
        {"type": "command", "command": "<proxy-script> <policy-name>"}
    ]}]}}

Casa's hooks.yaml shape (per configurator + plugin-developer defaults):
    pre_tool_use:
      - policy: casa_config_guard
        matcher: Write|Edit
"""

from __future__ import annotations


def translate_hooks_to_settings(
    hooks_yaml: dict, *, proxy_script_path: str,
) -> dict:
    """Convert Casa hooks.yaml -> CC settings.json shape.

    Reads snake_case keys (``pre_tool_use``, ``post_tool_use``) per
    ``defaults/schema/hooks.v1.json``; emits PascalCase
    (``PreToolUse``, ``PostToolUse``) per CC settings.json shape.

    Round-4 (Terra P0): the emitted ``PreToolUse`` block ALWAYS carries a
    ``managed_component_guard`` entry, whatever the yaml declares. Both
    claude_code workspace settings writers (drivers.workspace legacy +
    template paths) route through this function, and definition.yaml's
    ``hooks_file:`` key is a config-editable POINTER — repointing it at a
    hollow yaml previously emitted ZERO hooks, shedding every policy for
    the next session. Yaml policies are additive-only.
    """
    out: dict = {"hooks": {}}
    for snake, pascal in (
        ("pre_tool_use", "PreToolUse"),
        ("post_tool_use", "PostToolUse"),
    ):
        entries = hooks_yaml.get(snake, []) or []
        if not entries:
            continue
        out_entries = []
        for e in entries:
            policy = e.get("policy")
            matcher = e.get("matcher", ".*")
            if not policy:
                continue
            cc_hook: dict = {
                "type": "command",
                "command": f"{proxy_script_path} {policy}",
            }
            # Pass-through optional per-hook timeout (seconds). CC's default
            # is 60s; engagement_permission_relay needs ~600s for the
            # operator-response window (C-1 spec §4.6).
            if "timeout" in e and e["timeout"] is not None:
                cc_hook["timeout"] = int(e["timeout"])
            out_entries.append({
                "matcher": matcher,
                "hooks": [cc_hook],
            })
        out["hooks"][pascal] = out_entries

    # Round-4 (Terra P0) mandatory guard entry. The canonical matcher comes
    # from HOOK_POLICIES so the two paths can't drift. Dedupe: skip only
    # when the yaml already emitted the policy with a COVERING matcher (the
    # canonical one, or the ".*" default a bare declaration gets) — a
    # yaml-supplied narrower matcher does not count, since the matcher is
    # attacker-editable on this path (unlike the SDK path, where matchers
    # come from HOOK_POLICIES). A duplicate deny would be harmless; the
    # skip just avoids trivial double-emission for the shipped files.
    from hooks import HOOK_POLICIES
    canonical_matcher = HOOK_POLICIES["managed_component_guard"]["matcher"]
    guard_cmd = f"{proxy_script_path} managed_component_guard"
    pre = out["hooks"].setdefault("PreToolUse", [])
    already = any(
        e.get("matcher") in (".*", canonical_matcher)
        and any(h.get("command") == guard_cmd for h in e.get("hooks", []))
        for e in pre
    )
    if not already:
        pre.append({
            "matcher": canonical_matcher,
            "hooks": [{"type": "command", "command": guard_cmd}],
        })
    return out

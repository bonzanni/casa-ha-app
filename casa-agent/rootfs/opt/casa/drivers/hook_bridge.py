"""Translate Casa executor hooks.yaml into Claude Code .claude/settings.json.

The CC hook shape is:
    {"hooks": {"PreToolUse": [{"matcher": "...", "hooks": [
        {"type": "command", "command": "<proxy-script> <policy-name>"}
    ]}]}}

Casa's hooks.yaml shape (per hello-driver & configurator defaults):
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
            out_entries.append({
                "matcher": matcher,
                "hooks": [{
                    "type": "command",
                    "command": f"{proxy_script_path} {policy}",
                }],
            })
        out["hooks"][pascal] = out_entries
    return out

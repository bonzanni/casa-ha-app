"""Translate Casa executor hooks.yaml into Claude Code .claude/settings.json.

The CC hook shape is:
    {"hooks": {"PreToolUse": [{"matcher": "...", "hooks": [
        {"type": "command", "command": "<proxy-script> <policy-name>"}
    ]}]}}

Casa's hooks.yaml shape (per hello-driver & configurator defaults):
    PreToolUse:
      - policy: casa_config_guard
        matcher: Write|Edit
"""

from __future__ import annotations


def translate_hooks_to_settings(
    hooks_yaml: dict, *, proxy_script_path: str,
) -> dict:
    """Convert Casa hooks.yaml -> CC settings.json shape."""
    out: dict = {"hooks": {}}
    for event in ("PreToolUse", "PostToolUse"):
        entries = hooks_yaml.get(event, []) or []
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
        out["hooks"][event] = out_entries
    return out

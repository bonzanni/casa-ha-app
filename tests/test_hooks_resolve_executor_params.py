"""H3 (v0.53.0): executor hooks.yaml params must reach the /hooks/resolve
HTTP path so a claude_code executor's configured path_scope / commit_size_guard
is enforced instead of the deny-all factory defaults.

Pre-fix, _build_cc_hook_policies produced only default-configured callbacks, so
path_scope defaulted to writable=[]/readable=[] and denied EVERY Read/Write/Edit
for a plugin-developer engagement.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ENG_ID = "a" * 32


class _Rec:
    status = "active"
    role_or_type = "plugin-developer"


class _Registry:
    def get(self, eng_id):
        return _Rec() if eng_id == ENG_ID else None


def _handler():
    from casa_core import _build_cc_hook_policies
    from hooks import HOOK_POLICIES, build_policy_callbacks_from_hooks_yaml
    from internal_handlers import _make_internal_hooks_resolve_handler
    hooks_yaml = {"pre_tool_use": [
        {"policy": "path_scope",
         "writable": ["/data/engagements/"],
         "readable": ["/data/engagements/", "/config/plugins/store"]},
        {"policy": "commit_size_guard", "max_files": 50},
    ]}
    return _make_internal_hooks_resolve_handler(
        hook_policies=_build_cc_hook_policies(HOOK_POLICIES),
        executor_hook_policies={
            "plugin-developer": build_policy_callbacks_from_hooks_yaml(
                hooks_yaml
            )},
        engagement_registry=_Registry(),
    )


async def test_write_inside_declared_writable_prefix_is_allowed():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "path_scope",
            "payload": {"tool_name": "Write",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {
                            "file_path":
                                f"/data/engagements/{ENG_ID}/plugin/skill.md"}}})
        assert await resp.json() == {}  # RED pre-fix: deny "writable prefixes []"


async def test_read_inside_declared_readable_prefix_is_allowed():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "path_scope",
            "payload": {"tool_name": "Read",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {"file_path": "/config/plugins/store/x.md"}}})
        assert await resp.json() == {}


async def test_write_outside_declared_prefix_still_denied():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "path_scope",
            "payload": {"tool_name": "Write",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {"file_path": "/etc/passwd"}}})
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_unknown_engagement_falls_back_to_default_policies():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "path_scope",
            "payload": {"tool_name": "Write",
                        "cwd": "/somewhere/else",
                        "tool_input": {
                            "file_path": "/data/engagements/x/f"}}})
        body = await resp.json()
        # default writable=[] -> deny
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_commit_size_guard_uses_declared_max_files(monkeypatch):
    import hooks as hooks_mod
    monkeypatch.setattr(
        hooks_mod, "_git_porcelain_count", lambda repo_dir="/config": 30,
    )
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "commit_size_guard",
            "payload": {"tool_name": "Write",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {
                            "file_path": f"/data/engagements/{ENG_ID}/f"}}})
        # 30 < declared max_files=50 -> allow; default max=20 would deny.
        assert await resp.json() == {}

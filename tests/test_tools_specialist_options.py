"""Task N1b Step 23: the system-prompt seam _build_specialist_options gained
in Step 22 — a specialist with an ACTIVE compiled binding is served the
compiled bundle's text projection; a specialist with no binding (still
bundled-in-image, or pending-configuration) falls back unchanged to the
legacy cfg.system_prompt (_compose_prompt's output)."""


def test_build_specialist_options_prefers_compiled_bundle_when_present() -> None:
    from tools import _build_specialist_options
    from prompt_compiler import CompiledPromptBundle, CompiledProjection

    class _FakeCfg:
        role = "mtg"
        model = "claude-sonnet-4-6"
        system_prompt = "LEGACY — should not be used"
        cwd = ""
        hooks = type("H", (), {"pre_tool_use": []})()
        tools = type("T", (), {"allowed": [], "disallowed": [], "permission_mode": "dontAsk",
                                 "max_turns": 8, "skills": "none"})()
        mcp_server_names = []
        compiled_prompt_bundle = CompiledPromptBundle(
            role_id="specialist:mtg", resolved_model="claude-sonnet-4-6",
            text=CompiledProjection(system_prompt="COMPILED TEXT\n", digest="sha256:" + "0" * 64,
                                     estimated_tokens=10),
            voice=CompiledProjection(system_prompt="COMPILED VOICE\n", digest="sha256:" + "1" * 64,
                                      estimated_tokens=10),
            restricted_webhook=CompiledProjection(system_prompt="COMPILED RW\n",
                                                    digest="sha256:" + "2" * 64, estimated_tokens=5),
            binding_digest="sha256:" + "3" * 64,
        )

    opts = _build_specialist_options(_FakeCfg())
    assert opts.system_prompt == "COMPILED TEXT\n"


def test_build_specialist_options_falls_back_to_legacy_system_prompt_when_no_bundle() -> None:
    from tools import _build_specialist_options

    class _FakeCfg:
        role = "finance"
        model = "claude-sonnet-4-6"
        system_prompt = "LEGACY PROMPT\n"
        cwd = ""
        hooks = type("H", (), {"pre_tool_use": []})()
        tools = type("T", (), {"allowed": [], "disallowed": [], "permission_mode": "dontAsk",
                                 "max_turns": 8, "skills": "none"})()
        mcp_server_names = []
        compiled_prompt_bundle = None

    opts = _build_specialist_options(_FakeCfg())
    assert opts.system_prompt == "LEGACY PROMPT\n"

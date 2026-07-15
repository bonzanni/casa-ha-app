#!/usr/bin/env python3
"""One-shot probes for MTG-voice WS-B design questions (run manually; needs API creds).

Usage: venv_test/bin/python test-local/e2e/probe_web_tools_and_dontask.py
Prints PROBE1/2/3: PASS|FAIL lines; exits 0 always (informational).

Running this script requires live Claude API credentials (it opens a real
ClaudeSDKClient session) and is NOT part of the unit gate or CI — it lives
under test-local/ and is invoked manually. When WS-B design work begins,
run it and record the PROBE1/2/3 results into the private design spec
(docs/, not this repo) §7. If PROBE2 (dontAsk) comes back FAIL, WS-B keeps
using permission_mode="acceptEdits" instead. If PROBE3 (text-before-tool
ordering) comes back FAIL, WS-B cannot rely on a spoken preamble sentence
preceding a tool call in the stream and instead relies on the framework's
own progress block alone.
"""
import anyio


async def main() -> None:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock,
        ToolUseBlock,
    )

    # PROBE 2: dontAsk accepted by the pinned SDK?
    try:
        ClaudeAgentOptions(model="haiku", permission_mode="dontAsk",
                           allowed_tools=["WebSearch"], max_turns=2)
        print("PROBE2 dontAsk-accepted: PASS")
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE2 dontAsk-accepted: FAIL ({exc})")

    # PROBE 1 + 3: WebSearch executes; text block precedes tool_use.
    opts = ClaudeAgentOptions(
        model="haiku", allowed_tools=["WebSearch"],
        permission_mode="acceptEdits", max_turns=3,
        system_prompt=("Before any tool call, first say the exact sentence "
                       "'Checking now.' Then use WebSearch."),
    )
    order: list[str] = []
    used_web = False
    async with ClaudeSDKClient(opts) as client:
        await client.query("What is today's date according to the web?")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in getattr(msg, "content", []):
                    if isinstance(block, TextBlock) and block.text.strip():
                        order.append("text")
                    if isinstance(block, ToolUseBlock):
                        order.append(f"tool:{block.name}")
                        used_web = used_web or block.name == "WebSearch"
    print(f"PROBE1 websearch-executed: {'PASS' if used_web else 'FAIL'}")
    first_tool = next((i for i, o in enumerate(order) if o.startswith("tool:")),
                      len(order))
    preamble = any(o == "text" for o in order[:first_tool])
    print(f"PROBE3 text-before-tool: {'PASS' if preamble else 'FAIL'}  (order={order})")


if __name__ == "__main__":
    anyio.run(main)

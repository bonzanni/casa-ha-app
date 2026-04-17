"""V-2: WebSocket smoke — stt_start -> utterance -> done/error.

Drives the port exported via the HOST_PORT env var (same convention as
the bash tests). Requires `aiohttp` on the host Python (NOT inside the
container).
"""

import asyncio
import json
import os
import sys

import aiohttp


HOST_PORT = int(os.environ["HOST_PORT"])
URL = f"ws://localhost:{HOST_PORT}/api/converse/ws"


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(URL, timeout=15) as ws:
            await ws.send_json({"type": "stt_start", "scope_id": "e2e-ws"})
            await ws.send_json({
                "type": "utterance", "utterance_id": "u-1",
                "text": "hi", "agent_role": "butler", "scope_id": "e2e-ws",
            })
            saw_block = False
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                frame = json.loads(msg.data)
                t = frame.get("type")
                if t == "block":
                    saw_block = True
                if t == "done":
                    if not saw_block:
                        print("FAIL: done without block", file=sys.stderr)
                        return 1
                    print("PASS: WS stream emits block + done")
                    return 0
                if t == "error":
                    # Error is acceptable too — confirms error-pipeline wiring.
                    print(f"PASS: WS stream terminated with error frame: {frame}")
                    return 0
    print("FAIL: WS stream closed without terminator", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

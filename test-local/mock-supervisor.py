"""Mock HA Supervisor API for local Docker testing.

Serves the minimal API surface that bashio needs:
- GET /addons/self/info (addon config, ingress port)
- GET /addons/self/options (addon options from options.json)
- GET /info (supervisor info)

Run this on the host, expose to the container via --add-host or docker network.
Or run inside the container as a sidecar process.
"""

import json
import sys
from pathlib import Path
from aiohttp import web


OPTIONS_PATH = Path("/data/options.json")


def load_options():
    if OPTIONS_PATH.exists():
        return json.loads(OPTIONS_PATH.read_text())
    return {}


async def addon_info(request):
    """GET /addons/self/info"""
    options = load_options()
    return web.json_response({
        "result": "ok",
        "data": {
            "name": "Casa Agent",
            "slug": "casa-agent",
            "state": "started",
            "version": "0.1.0",
            "ingress": True,
            "ingress_port": 8080,
            "ingress_entry": "/",
            "ip_address": "127.0.0.1",
            "options": options,
        },
    })


async def addon_options(request):
    """GET /addons/self/options"""
    return web.json_response({
        "result": "ok",
        "data": {"options": load_options()},
    })


async def supervisor_info(request):
    """GET /info"""
    return web.json_response({
        "result": "ok",
        "data": {
            "supervisor": "mock",
            "version": "0.0.0",
            "channel": "stable",
        },
    })


async def core_api_catchall(request):
    """Catch-all for /core/api/* -- returns empty success."""
    return web.json_response({"result": "ok", "data": {}})


async def services_catchall(request):
    """Catch-all for /services/* -- returns empty success."""
    return web.json_response({"result": "ok", "data": {}})


app = web.Application()
app.router.add_get("/addons/self/info", addon_info)
app.router.add_get("/addons/self/options", addon_options)
app.router.add_get("/info", supervisor_info)
app.router.add_route("*", "/core/api/{path:.*}", core_api_catchall)
app.router.add_route("*", "/services/{path:.*}", services_catchall)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    print(f"Mock Supervisor API on :{port}")
    web.run_app(app, host="0.0.0.0", port=port, print=None)

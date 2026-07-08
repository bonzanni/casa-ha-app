"""H1 (v0.50.0): the generated nginx ingress server block must carry the
HA-mandated source restriction (allow 172.30.32.2; deny all;) so only the
Supervisor ingress proxy — not any peer container on the hassio bridge —
can reach the operator dashboard / proxied API / web terminal.

Static-source test: parse the emitted heredoc in setup-nginx.sh rather than
booting bashio/nginx, so it runs in the pure-unit tier.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "casa-agent/rootfs/etc/s6-overlay/scripts/setup-nginx.sh"
)


def _ingress_block(text: str) -> str:
    """Isolate the ingress server block (marker → external API marker)."""
    start = text.index("# --- Ingress server")
    end = text.index("# --- External API server")
    return text[start:end]


def test_setup_nginx_ingress_restricts_to_supervisor():
    text = _SCRIPT.read_text()
    ingress = _ingress_block(text)
    assert "allow 172.30.32.2;" in ingress, "ingress server missing supervisor allow"
    assert "deny all;" in ingress, "ingress server missing deny all"
    # The filter must precede the proxy location so it applies to every
    # route, including /terminal/.
    assert ingress.index("deny all;") < ingress.index("location / {")


def test_external_api_server_unaffected():
    """The public 18065 server keeps its path-based gating and must NOT
    inherit the ingress source filter (it is reached from outside the
    bridge, not from 172.30.32.2)."""
    text = _SCRIPT.read_text()
    ext_start = text.index("# --- External API server")
    external = text[ext_start:]
    assert "allow 172.30.32.2;" not in external

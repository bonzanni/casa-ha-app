import os
import time
from pathlib import Path

import pytest

from hindsight_provenance_contract import run_contract

pytestmark = [pytest.mark.docker, pytest.mark.slow]


def test_supported_hindsight_preserves_provenance_contract(tmp_path: Path) -> None:
    base_url = os.environ["HINDSIGHT_URL"]
    bank = f"casa-provenance-contract-{os.getpid()}-{int(time.time())}"
    try:
        report = run_contract(
            base_url,
            bank=bank,
            expected_version=os.environ["HINDSIGHT_EXPECTED_VERSION"],
            record_path=tmp_path / "complete-envelopes.json",
        )
        assert report["items"]
    finally:
        from hindsight_provenance_contract import _request
        _request(base_url, "DELETE", f"/v1/default/banks/{bank}", timeout=30)

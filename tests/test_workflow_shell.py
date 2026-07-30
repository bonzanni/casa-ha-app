"""Every `run:` block in a workflow must be valid shell.

A broken heredoc or an unclosed `if` is invisible in YAML — the file parses fine and the
step fails only once CI runs it. This caught an unclosed `if` in the docs workflow that a
YAML check had happily accepted.
"""
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))

# GitHub expressions are substituted by the runner, not the shell. Stub the ones we use so
# bash can parse the structure; anything else left in place would be a syntax error here and
# is worth failing on.
STUBS = {
    "${{ github.base_ref }}": "main",
    "${{ github.event_name }}": "pull_request",
    "${{ secrets.GITHUB_TOKEN }}": "token",
}


def _run_blocks(path: Path):
    workflow = yaml.safe_load(path.read_text())
    for job_name, job in (workflow.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            script = step.get("run")
            if script:
                yield f"{path.name}:{job_name}:{step.get('name', index)}", script


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_run_block_is_valid_shell(path):
    failures = []
    for label, script in _run_blocks(path):
        cleaned = script
        for token, value in STUBS.items():
            cleaned = cleaned.replace(token, value)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(cleaned)
            tmp = handle.name
        try:
            result = subprocess.run(["bash", "-n", tmp], capture_output=True, text=True)
        finally:
            os.unlink(tmp)
        if result.returncode != 0:
            failures.append(f"{label}: {result.stderr.strip()}")
    assert not failures, "invalid shell in workflow run blocks:\n" + "\n".join(failures)


def test_the_docs_workflow_runs_on_pull_requests():
    """It is the publication guard; a workflow that only runs on main guards nothing."""
    docs = next(p for p in WORKFLOWS if p.name == "docs.yml")
    # `on` is parsed by PyYAML 1.1 rules as the boolean True, not the string "on".
    workflow = yaml.safe_load(docs.read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers is not None, "no trigger block"
    assert "pull_request" in triggers

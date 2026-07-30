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

_WF_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# Both suffixes: GitHub accepts .yaml too, and a glob for one silently skips the other.
WORKFLOWS = sorted([*_WF_DIR.glob("*.yml"), *_WF_DIR.glob("*.yaml")])

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
    default_shell = ((workflow.get("defaults") or {}).get("run") or {}).get("shell", "bash")
    for job_name, job in (workflow.get("jobs") or {}).items():
        job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell", default_shell)
        for index, step in enumerate(job.get("steps") or []):
            script = step.get("run")
            if not script:
                continue
            shell = step.get("shell", job_shell)
            if shell not in ("bash", "sh"):
                continue          # a different grammar; checking it as bash proves nothing
            yield f"{path.name}:{job_name}:{step.get('name', index)}", script


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_run_block_parses_as_bash(path):
    """Parses — it does not execute, and does not prove the step does what it claims.

    A `run:` block with an explicit non-bash `shell:` would be checked under the wrong
    grammar, so those are skipped rather than silently mis-verified.
    """
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


def test_the_docs_workflow_is_triggered_by_every_pull_request():
    """It is the publication guard, so it must not be narrowed to some PRs.

    The earlier version asserted only that the key was present, which would still pass with
    a `paths-ignore` filter that skipped the guard for exactly the changes worth guarding.
    """
    docs = next(p for p in WORKFLOWS if p.name == "docs.yml")
    # `on` is parsed by PyYAML 1.1 rules as the boolean True, not the string "on".
    workflow = yaml.safe_load(docs.read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers is not None, "no trigger block"
    assert "pull_request" in triggers
    pr = triggers["pull_request"]
    assert pr is None or not (set(pr) & {"paths", "paths-ignore", "branches-ignore"}), (
        f"the guard must run on every PR, not a filtered subset: {pr}"
    )
    for job in workflow["jobs"].values():
        assert "if" not in job, "a job-level condition can disable the guard for some PRs"

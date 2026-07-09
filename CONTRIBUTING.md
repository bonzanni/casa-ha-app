# Contributing

Thanks for your interest in Casa!

## Development setup

Develop on Linux or WSL2 (on the native ext4 filesystem, not `/mnt/c`).
Container-bound files must be LF — `.gitattributes` enforces this.

```bash
make setup        # builds a venv at venv_test/ + installs the git hooks
make test-unit    # fast unit gate — must be green before a PR
make test-docker  # docker-backed tiers (optional locally; CI runs them)
```

CI runs the tiered QA suite, the Home Assistant app linter, and container
validation builds for both architectures on every pull request.

## Pull requests

- Branch from `master`; PRs are squash-merged.
- Releases: bump `version:` in `casa-agent/config.yaml` and prepend a
  user-facing `casa-agent/CHANGELOG.md` entry. Merging to master automatically
  publishes the container images and creates the `vX.Y.Z` tag + GitHub
  Release — no manual steps.
- New or changed app options need a `translations/en.yaml` entry and DOCS.md
  coverage in the same PR.

## AI-assisted contributions

Casa is largely built with Claude Code, and AI-assisted contributions are
welcome. Disclose assistance with an `Assisted-by:` commit trailer (e.g.
`Assisted-by: Claude Code`), review everything you submit, and stand behind
it — you are the author and remain responsible for the change.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind.

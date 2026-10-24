# AGENTS.md

## Project Overview

`maas-code-reviewer` is a Python CLI tool that reviews [Launchpad](https://launchpad.net/) merge proposals and [GitHub](https://github.com/) pull requests using omp over ACP inside a disposable Workshop. omp is the only currently supported harness; the neutral ACP boundary exists to ease a future switch, not to require alternative-harness support or conformance now. Python acquires inputs, validates results, and publishes reviews. See `DESIGN.md` and `PLAN.md` for architecture and verification status.

## Project Structure

- `src/maas_code_reviewer/` — main package
  - `cli.py` — argparse CLI entry point; wires dependencies and dispatches subcommands
  - `review_commands.py` — host repository acquisition, merged-tree review lifetime, and publication adaptation
  - `launchpad_client.py` — `LaunchpadClient` protocol + real implementation
  - `git.py` — `GitClient` protocol + real implementation (subprocess-based)
  - `github_client.py` — `GitHubClient` class and `parse_pr_url()` free function
  - `agent_runner.py` — synchronous, vendor-neutral request/result/usage contract and failures
  - `acp_client.py` — Python ACP SDK transport, permissions, message selection, usage, cancellation
  - `workshop_runner.py` — generated job definition, isolated job lifecycle, snapshot staging, teardown
  - `runtime_config.py` — built-in runtime defaults, optional operator TOML overrides, preflight validation
  - `models.py` — `MergeProposal` and `Comment` dataclasses
  - `repo_tools.py` — host-only top-level convention loading with repository path containment
  - `review_schema.py` — JSON review schema, `validate_review_json()`, `parse_diff_files_and_lines()`
  - `reviewer.py` — `review_diff()` and `review_diff_structured()` orchestration logic
- `tests/` — tests and fakes
  - `factory.py` — test data factories
  - `fake_git.py` — `FakeGitClient` (uses real temp git repos)
  - `fake_github.py` — `FakeGitHubClient`
  - `fake_launchpad.py` — `FakeLaunchpadClient`
  - `fake_launchpadlib.py` — low-level launchpadlib fake
  - `fake_agent_runner.py` — controlled terminal outcomes for policy and CLI tests
  - ACP peer/Workshop service fakes — deterministic transport and lifecycle behavior without model credentials
- `pyproject.toml` — project config (hatchling build, uv for deps)
- `Makefile` — lint, typecheck, test, and build targets

## Workflow

After every change, run:

```
make check
```

This runs linting (`ruff`), type checking (`ty`), and tests (`pytest`). All three must pass before a change is considered complete.

## Conventions

- Python ≥ 3.12
- Keep `try`/`except` blocks minimal — only wrap the code that can actually raise the caught exception.
- Catch specific exceptions, never bare `Exception`.
- Avoid regular expressions. Prefer string methods (`.split()`, `.startswith()`, `.endswith()`).
- Avoid mocking. Use fakes (see `tests/fake_launchpad.py`, `tests/fake_github.py`, `tests/fake_agent_runner.py`) — objects with the same API as the real thing but with exposed internal state for test control.
- Follow the synchronous protocols in `agent_runner.py`; keep ACP, Workshop, and provider implementation types outside review policy and publication clients.
- In modules and classes that have both public and private (underscore-prefixed) methods/functions, public ones come first, private ones at the bottom.
- `cli.py` is a thin wiring layer only — all logic lives in dedicated modules.
- Host convention loading goes through `RepoTools`, which prevents traversal outside the caller repository. Agent file tools are container-local, not host callbacks. Stage only disposable snapshots without following outside symlinks; keep original diffs and trusted runtime policy authoritative on the host. Never forward publication credentials, host terminal/filesystem capabilities, or ambient executable configuration to the agent.
- The explicitly selected Workshop test shim is for already isolated development/testing only. It does not enforce a filesystem, mount, or egress sandbox. Real Workshop/security/lifecycle tests remain operator gates; never report shim evidence as container-isolation proof.
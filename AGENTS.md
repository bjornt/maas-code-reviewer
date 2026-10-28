# AGENTS.md

## Project Overview

`maas-code-reviewer` is a Python CLI tool that reviews [Launchpad](https://launchpad.net/) merge proposals and [GitHub](https://github.com/) pull requests using an LLM. See `SPEC.md` for the full specification.

The `review` command runs its reviews through the omp agent harness over ACP (see `DESIGN.md`).

## Project Structure

- `src/maas_code_reviewer/` — main package
  - `cli.py` — argparse CLI entry point; wires dependencies and dispatches subcommands
  - `review_commands.py` — merge-proposal/pull-request acquisition and publication logic
  - `launchpad_client.py` — `LaunchpadClient` protocol + real implementation
  - `git.py` — `GitClient` protocol + real implementation (subprocess-based)
  - `github_client.py` — `GitHubClient` class and `parse_pr_url()` free function
  - `models.py` — `MergeProposal` and `Comment` dataclasses
  - `repo_tools.py` — `RepoTools`: host-only, path-traversal-safe AGENTS.md loading scoped to a repo directory
  - `review_schema.py` — JSON review schema, `validate_review_json()`, `parse_diff_files_and_lines()`, `parse_review_document()`
  - `reviewer.py` — `review_agent()` review orchestration plus rendering/truncation helpers
  - `review_mcp.py` — the in-session `validate_review` MCP stdio server (spawned by the agent harness via `review-mcp`; advisory only)
  - `agent_runner.py` — vendor-neutral `AgentRunner` contract, request/result dataclasses, and error hierarchy
  - `acp_client.py` — one unattended ACP conversation over the agent subprocess
  - `acp_runner.py` — `ConfiguredAgentRunner`, the production `AgentRunner` over ACP
  - `runtime_config.py` — trusted review runtime policy and its TOML override
  - `telemetry.py` — review-scoped loopback OTLP collector
  - `metrics.py` — `ReviewMetrics` dataclass and `write_metrics()`; populated by the review path
- `tests/` — tests and fakes
  - `factory.py` — test data factories
  - `fake_git.py` — `FakeGitClient` (uses real temp git repos)
  - `fake_github.py` — `FakeGitHubClient`
  - `fake_launchpad.py` — `FakeLaunchpadClient`
  - `fake_launchpadlib.py` — low-level launchpadlib fake
  - `fake_agent_runner.py` — `FakeAgentRunner` for the `AgentRunner` contract
  - `fake_acp.py` — wire-level adversarial ACP subprocess peer
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
- Avoid mocking. Use fakes (see `tests/fake_launchpad.py`, `tests/fake_github.py`) — objects with the same API as the real thing but with exposed internal state for test control.
- Follow the protocols defined in `SPEC.md` when adding new clients or services.
- In modules and classes that have both public and private (underscore-prefixed) methods/functions, public ones come first, private ones at the bottom.
- `cli.py` is a thin wiring layer only — all logic lives in dedicated modules.
- Host-side review-context file access goes through `RepoTools`, which prevents path traversal outside the repository directory; the review agent reads the repository only with its own native tools inside the untrusted container.
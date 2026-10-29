# Review parts for the omp-harness migration diff

`../changes.diff` is the complete diff of the last 18 commits (`6c0ed73..HEAD`,
the google-genai → omp/ACP migration): 57 files, +12667/−5326.

That diff is partitioned here into five category parts:

| Part | File | +/− | Files | Contents |
|---|---|---|---|---|
| 1 | `01-pure-moves.diff` | +423/−354 | 4 | Code relocated **verbatim** between files |
| 2 | `02-refactoring.diff` | +111/−92 | 4 | Behavior-preserving restructures |
| 3 | `03a`–`03i` | +9638/−103 | 30 | New capabilities, split into 9 subsystem chunks (below) |
| 4 | `04-removals.diff` | +20/−4362 | 14 | Deleted capabilities without replacement |
| 5 | `05-other.diff` | +2475/−414 | 20 | Docs, CI, packaging, prompts, lockfile |

### Feature chunks (part 3), in dependency/review order

| Chunk | +/− | What |
|---|---|---|
| `03a-agent-runner-contract.diff` | +233/−1 | `agent_runner.py` vendor-neutral contract + `FakeAgentRunner` and its tests |
| `03b-acp-client.diff` | +2079/−0 | `acp_client.py` ACP conversation client + adversarial wire-level fake + tests |
| `03c-acp-runner.diff` | +493/−0 | `acp_runner.py` production `AgentRunner` over ACP + tests |
| `03d-runtime-policy.diff` | +719/−0 | `runtime_config.py` trusted runtime policy, `policy.yml`, tests |
| `03e-telemetry.diff` | +660/−0 | `telemetry.py` loopback OTLP collector + tests |
| `03f-schema-hardening.diff` | +833/−69 | `review_schema.py` stricter validation, `parse_review_document`, path safety + tests |
| `03g-fetch-publish-commands.diff` | +1892/−12 | `fetch-mp`/`publish-mp`/`fetch-pr`/`publish-pr` end to end: `review_commands.py` additions, git header auth + `rev_parse`, `get_pr_shas()`, CLI handlers/parsers, tests |
| `03h-review-through-acp.diff` | +2178/−21 | `reviewer.py` `review_agent()` orchestration + repair round, `cli.py` `review` wiring, agent-based `ReviewMetrics`, tests |
| `03i-review-mcp-tool.diff` | +551/−0 | `review_mcp.py` in-session `validate_review` MCP server + `review-mcp` CLI wiring + tests |

Chunks compose exactly to the former `03-features.diff` (verified:
+9638/−103 on both sides). `cli.py` and `test_cli.py` span chunks
03g/03h/03i, split at function/class boundaries.

**Integrity guarantee (machine-verified):** every `+`/`−` line of the full diff
appears in exactly one part; per-part totals sum exactly to the full diff.
Hunk headers were recomputed after splitting and all parts parse with
`git apply --stat`, but the parts are for **human review, not for applying**
(a file split across parts appears once per part with only its relevant hunks).

## Suggested review order

1. `01-pure-moves` — skim; only verify the relocation really is verbatim.
2. `02-refactoring` — small; check behavior preservation.
3. `04-removals` — confirm nothing deleted is still needed.
4. `03a`→`03i` — the bulk; the actual new architecture, in the dependency
   order of the chunk table above (contract → transport → runner → policy →
   telemetry → schema → commands → review path → MCP tool).
5. `05-other` — docs/CI/prompts; lowest risk.

## 1. Pure code moves (`01-pure-moves.diff`)

The extraction of `review_commands.py` out of `cli.py` (commit f293094) and
`test_review_commands.py` out of `test_cli.py`, restricted to the hunks that
survived verbatim to HEAD:

- `cli.py` → `review_commands.py`: `MergeProposalSummary`,
  `list_merge_proposals`, `format_merge_proposals`, `has_existing_review`,
  `_lp_repo_url`, `_find_last_review_date`, `_ref_to_branch`, shared imports,
  `_LP_GIT_BASE`, the `REVIEW_MARKER` import, and the comments comprehension
  inside `post_pull_request_review`.
- `test_cli.py` → `test_review_commands.py`: `TestListMergeProposals`,
  `TestFormatMergeProposals`, `TestLpRepoUrl`, `TestRefToBranch`,
  `TestHasExistingReview`, `_setup_repos`, plus a 4-line boilerplate block.

## 2. Refactoring (`02-refactoring.diff`)

- `review_commands.py`: `post_pull_request_review` adapted from the old
  `handle_review_pr` tail (renamed variables, same behavior).
- `reviewer.py`: docstring rewording in `_truncate_diff` (agent tools).
- `test_cli.py`: import re-points to the new module locations;
  `TestHandleListLpMps` and five diff constants relocated verbatim *within*
  the file.
- `test_reviewer.py`: one-line fixture header fix in the kept
  `TestTruncateDiff`.

## 3. Feature additions (chunks `03a`–`03i`)

New architecture, all new modules with their tests and fakes:

- `agent_runner.py` — vendor-neutral `AgentRunner` contract.
- `acp_client.py`, `acp_runner.py` — ACP conversation client + production runner.
- `runtime_config.py` + `runtime/profile/policy.yml` — trusted runtime policy.
- `telemetry.py` — loopback OTLP collector.
- `review_mcp.py` — in-session `validate_review` MCP server.
- `reviewer.py` (additions) — `review_agent()` orchestration, bounded repair
  round, review staging, agent metrics.
- `review_commands.py` (additions) — `fetch_merge_proposal`,
  `fetch_pull_request`, `post_pull_request_review`, `InvalidReviewError`,
  `load_validated_review`, render helpers, diff-prefix detection.
- `cli.py` (additions) — `fetch-mp`/`publish-mp`/`fetch-pr`/`publish-pr`/
  `review`/`review-mcp` commands and their argparse wiring.
- `review_schema.py` — validation hardening: path-prefix resolution,
  `repository_files`, path-traversal rejection, `parse_review_document`.
  (Commit e8bcd30; stricter rejection = behavior change, hence feature.)
- `git.py` — `http_extra_header` one-shot header auth on clone/merge; new
  `rev_parse`.
- `github_client.py` — new `get_pr_shas()`.
- `metrics.py` — `ReviewMetrics` fields swapped to agent-reported usage
  (single interleaved hunk; old LLM token fields removed in place).
- Tests/fakes: `fake_acp.py`, `fake_agent_runner.py`, `test_acp_client.py`,
  `test_acp_runner.py`, `test_review_mcp.py`, `test_runtime_config.py`,
  `test_telemetry.py`, and the new-test portions of `test_cli.py`,
  `test_reviewer.py`, `test_review_commands.py`, `test_review_schema.py`,
  `test_metrics.py`, `test_github_client.py`, `test_fakes.py`,
  `fake_github.py`, `fake_pygithub.py`.

## 4. Code removal (`04-removals.diff`)

- `llm_client.py`, `tests/fake_llm.py`, `tests/test_llm_client.py` — the
  google-genai engine, its fake, and its tests (commit 75de51b).
- `reviewer.py` (deletions) — `review_diff`, `review_diff_structured`,
  `_validate_review`, prompt builders, JSON extraction, LLM metrics helpers.
- `cli.py` (deletions) — legacy `review-mp`/`review-diff`/`review-pr`
  commands, Gemini API-key handling, `--max-tool-calls`.
- `repo_tools.py` + `test_repo_tools.py` — `read_file`/`list_directory`
  agent tools (the agent now uses its own native tools in the container);
  `read_agents_md` kept, de-instrumented.
- `prompts/instructions.md`, `prompts/system_instruction.md` — genai-era
  prompt files.
- `snap/snapcraft.yaml`, `.github/workflows/snap.yml` — snap packaging.
- Test deletions in `test_cli.py`, `test_reviewer.py`, `test_fakes.py`
  covering the above.

## 5. Anything else (`05-other.diff`)

- Docs: `DESIGN.md`, `PLAN.md` (new), `README.md`, `AGENTS.md`.
- CI/infra: three-phase `review-pr.yml` / `review-pr-manual.yml`,
  `sdk-publish.yml`, `ci/` workshop yamls, `hooks/`, `sdkcraft.yaml`,
  `workshop.yaml`.
- Packaging/deps: `pyproject.toml`, `uv.lock` (google-genai out).
- Prompt content edits: `shared_instruction.md`, `structured_instructions.md`,
  `structured_system_instruction.md` (wording for the agent harness; no code
  semantics).
- Two prompt-text-only lines inside `reviewer.py` /
  `review_commands.py` license header boilerplate.

## Gray areas (judgment calls)

- **metrics.py**: old token-field removal and new agent-usage fields are
  interleaved in one 27-line hunk; assigned whole to features.
- **`_clone_merge_diff`** in `review_commands.py`: restructures old
  clone/merge/diff code but adds header auth → feature.
- **`_setup_repos`** appears in both `test_cli.py` (kept) and
  `test_review_commands.py` (copied) → addition side classified as move.
- **Prompt `.md` edits** are under "other" even though prompt text
  influences agent behavior.
- Boundary context lines may repeat in two parts (up to 3 lines) so each
  hunk stays readable standalone.

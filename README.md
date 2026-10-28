# maas-code-reviewer

An LLM-powered code reviewer agent for [GitHub](https://github.com/) pull
requests and [Launchpad](https://launchpad.net/) merge proposals. It can also be
used to review changes locally before submitting.

Its purpose is to help a human reviewer by taking care of the initial review quickly.
The person proposing the change can then make any changes they feel are necessary
before a human reviewer picks up the PR/MP.

It reviews diffs with an LLM agent (omp over ACP — see
[Review runtime configuration](#review-runtime-configuration)) and posts
findings as comments on the merge proposal or pull request.

It's built and maintained by the [MAAS](https://maas.io/) team at
[Canonical](https://canonical.com/). The tool's review defaults reflect what
the MAAS team considers sane behaviour for their projects — but there is
nothing MAAS-specific in how it works, and anyone is welcome to use it on
their own repositories.

## How to use

The primary use case for the code reviewer is on GitHub, where it can
automatically post reviews on new PRs. But it can also be used with
Launchpad MPs or even to review the changes locally. The latter is mostly used
to test out changes to the code reviewer itself.

### Github

Copy the same workflow this project uses: [.github/workflows/review-pr.yml](.github/workflows/review-pr.yml) 

It works as it is. The review runs in a Workshop container through the
omp agent harness; model selection is the
[runtime configuration](#review-runtime-configuration)'s.

You also need a `GEMINI_KEY` secret in the repo — the upstream provider
credential, handed to the `omp-auth-gateway` snap on the runner host and
never entering the review container (see
[Inference gateway](#inference-gateway)).

For split pipelines (reviewing in a separate container), `fetch-pr` writes the
review inputs and `publish-pr` validates and posts a candidate review — see the
command reference.

### Launchpad

There's currently no automation to run code reviews automatically on new merge
proposals. Set up your own automation around the three commands: `fetch-mp`
writes the review inputs, `review` produces the candidate review document, and
`publish-mp` validates and posts it — see the command reference.

### Locally

To use it locally, generate the diff that you want reviewed and run the
`review` command from the checkout. For example:

```sh
git show HEAD > diff.patch
maas-code-reviewer review --repo-dir . --diff-file diff.patch
```

`git show HEAD | maas-code-reviewer review --diff-file -` reads the diff
from stdin. Add `--json-output review.json` to write the structured
document for `publish-mp`/`publish-pr` to validate and post.

## Installation

Requires uv and Python ≥ 3.12.

```sh
git clone https://github.com/canonical/maas-code-reviewer
cd maas-code-reviewer
uv sync
```

You can now run it using uv:

```sh
uv run maas-code-reviewer
```

## Configuration

### GitHub Token

The `fetch-pr` and `publish-pr` commands require a GitHub
personal access token. Provide it in one of two ways:

| Method | Details |
|---|---|
| `--github-token TOKEN` | Pass the token directly on the command line. |
| `GITHUB_TOKEN` | Set this environment variable. Overridden by `--github-token`. |


### Launchpad Credentials

`maas-code-reviewer` uses
[launchpadlib](https://help.launchpad.net/API/launchpadlib) to authenticate
with Launchpad. You can provide credentials in two ways:

| Method | Details |
|---|---|
| `--launchpad-credentials FILE` | Pass a credentials file directly on the command line. |
| `LP_CREDENTIALS_FILE` | Set this environment variable to the path of your credentials file. Overridden by `--launchpad-credentials`. |

If neither is provided, `launchpadlib` will use its default OAuth flow
(opening a browser for authorization on first use).

Cloning private Launchpad repositories additionally requires a
[Launchpad git access token](https://ubuntu.com/docs/launchpad/user/explanation/working-with-code/git-hosting/)
with pull scope, provided in the `LP_GIT_TOKEN` environment variable.
`fetch-mp` uses it as HTTP header authentication for the clone and never
writes it to disk (no credential helper, no credential-bearing remote URLs).
Without it, only public repositories can be fetched.

### Migrating from the retired commands

The composite `review-mp`, `review-pr`, and `review-diff` commands were
removed together with the google-genai engine that backed them: the CI
workflows have driven `fetch-*` → `review` → `publish-*` since the
container migration, and the split pipeline reached feature parity.
Run the three commands instead — see the command reference for
end-to-end examples.

| Retired | Replacement |
|---|---|
| `-g`, `--gemini-api-key-file` / `GEMINI_API_KEY` | None. Model access is the [runtime configuration](#review-runtime-configuration) plus the per-run gateway token ([Inference gateway](#inference-gateway)); no API key file is read anywhere. |
| `--max-tool-calls` | `--review-timeout` — the tool-call cap became a finite review deadline. |
| `--max-diff-chars` | None — `review` never exposed the flag; truncation keeps its built-in file-boundary budget. |
| `--dry-run` on `review-mp`/`review-pr` | Run `review` without `publish-*` — it only ever prints or writes the candidate, never posts. |

## Inference gateway

The containerized review pipeline reaches the model through the
`omp-auth-gateway` snap on the host — never through provider credentials in
the container. The snap wraps `omp auth-gateway`: it holds the real upstream
provider credential on the host and serves inference from `127.0.0.1:4000`.
The review container reaches it through its Workshop inference plug, which
tunnels to that address.

The snap generates a gateway token at install time. That token is valid for
the gateway itself — never for the upstream credential:

```sh
sudo snap install omp-auth-gateway --channel=latest/candidate
sudo snap alias omp-auth-gateway.broker omp-auth-broker
sudo env GEMINI_API_KEY="$GEMINI_API_KEY" \
  OMP_AUTH_BROKER_URL=http://127.0.0.1:8765 \
  omp-auth-broker migrate --from-local --include-env   # upstream credential, host-only
sudo omp-auth-gateway token                            # the gateway-scoped token
```

The runner passes that token — and nothing else — into the review
container's environment with `workshop exec --env`. No GitHub, Launchpad, or
upstream-provider credential ever reaches the agent. The agent reading the
gateway token is acceptable: it authorizes exactly what the agent may do
anyway.

On ephemeral CI runners the token is generated at install time and dies with
the job, so install-time issuance is per-run issuance. On persistent hosts,
rotate it by regenerating it via the snap.

Loopback binding is not the boundary: the inference plug makes the container
a localhost client of the gateway. What makes the token safe is per-run
issuance, the gateway's scoping, and route-scoped authorization — not the
bind address.

### Verifying the token scope

Before trusting this deployment pattern, verify what the per-run token class
authorizes on the gateway: it must permit the inference and catalog routes
omp needs, and nothing else. This verification gate is part of
real-environment acceptance (PLAN.md, phase E) and must pass before the
containerized `review` deployment (PLAN.md, step 15) is trusted.

| Route | Purpose | Expected |
|---|---|---|
| `GET /v1/models` | Model catalog | Token accepted |
| `POST /v1/pi/stream` | Native inference | Token accepted |
| Any token-minting route | Issuing further tokens | Refused |
| Any credential-export route | Reading the upstream credential | Refused |
| Any management route | Gateway administration | Refused |

```sh
TOKEN=$(sudo omp-auth-gateway token)

# Catalog — the token must be accepted (expect 200):
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:4000/v1/models

# Every other route the gateway advertises must refuse the same token
# (401/403/404/405):
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:4000/ROUTE
```

A real inference turn through the gateway (the phase-E model-backed
`review`) completes the positive side of the matrix. If any minting,
credential-export, or management route accepts the token, the gate fails: do
not proceed — either fix the gateway or reinstate a narrowing proxy in front
of it (a loopback allowlisting proxy that forwards only the catalog and
inference routes and injects the gateway bearer itself), then re-verify.


## Command reference

### `list-lp-mps`

List merge proposals for a Launchpad project, filtered by status.

```sh
maas-code-reviewer list-lp-mps [--launchpad-credentials FILE] [--status STATUS] PROJECT
```

| Argument | Description |
|---|---|
| `PROJECT` | Launchpad project name. |
| `--status STATUS` | Filter by merge proposal status (default: `Needs review`). |
| `--launchpad-credentials FILE` | Path to Launchpad credentials file. |

For each proposal, prints the URL, status, and the timestamp of the last
review posted by this tool (or `never`).

**Example:**

```sh
maas-code-reviewer list-lp-mps --status "Needs review" maas
```

### `fetch-mp`

Fetch a merge proposal's review inputs into a directory.

```sh
maas-code-reviewer fetch-mp [--launchpad-credentials FILE] --output-dir DIR MP_URL
```

| Argument | Description |
|---|---|
| `MP_URL` | URL of the merge proposal to fetch. |
| `--output-dir DIR` | Directory to write the review inputs into (required). |
| `--launchpad-credentials FILE` | Path to Launchpad credentials file. |

Checks first whether this tool has already reviewed the proposal. If so, it
writes `status.json`
(`{"status": "skipped", "reason": "already-reviewed"}`) into the output
directory and exits without cloning, so automation can short-circuit.
Otherwise it writes:

- `diff.patch` — the `ORIG_HEAD..HEAD` diff of the source merged into the
  target branch;
- `description.md` — the proposal description or commit message;
- `metadata.json` — proposal and repository URLs, branch names, base/head
  SHAs, and the diff path prefix when it is not the standard `a/` + `b/`
  (forward it to `review --diff-prefix`);
- `repo/` — the merged working tree. Its `.git` stays on the host; exclude
  `.git` when copying the tree into a review container.

### `publish-mp`

Validate a candidate review and post it to a merge proposal.

```sh
maas-code-reviewer publish-mp [--launchpad-credentials FILE] --review FILE --input-dir DIR [--dry-run] MP_URL
```

| Argument | Description |
|---|---|
| `MP_URL` | URL of the merge proposal to publish the review to. |
| `--review FILE` | Path to the candidate review JSON document to publish (required). |
| `--input-dir DIR` | Directory holding the `fetch-mp` output to validate against (required). |
| `--dry-run` | Print the Launchpad comment to stdout instead of posting it. |
| `--launchpad-credentials FILE` | Path to Launchpad credentials file. |

Validates the candidate review JSON against the authoritative `diff.patch`
and `metadata.json` from `fetch-mp`'s output directory, renders the document
as the Launchpad comment text (the general comment, then every inline finding
as a `### path:line` section), prepends the review marker and preamble
exactly once, and posts it as an "Automated review" comment. `--dry-run`
prints the comment instead. An invalid candidate exits nonzero with nothing
posted.

### `fetch-pr`

Fetch a pull request's review inputs into a directory.

```sh
maas-code-reviewer fetch-pr [--github-token TOKEN] --output-dir DIR PR_URL
```

| Argument | Description |
|---|---|
| `PR_URL` | Full GitHub PR URL, e.g. `https://github.com/owner/repo/pull/42`. |
| `--output-dir DIR` | Directory to write the fetched review inputs into. |
| `--github-token TOKEN` | GitHub personal access token. If not provided, read from the `GITHUB_TOKEN` environment variable. |

Fetches the PR diff and description through the GitHub API — no checkout is
performed; the workflow or operator provides the agent's repository context
separately (for example `actions/checkout` in CI). Writes into the output
directory:

- `diff.patch` — the pull request's unified diff (standard `a/` + `b/`
  prefixes);
- `description.md` — the PR body, or an empty file when the PR has none;
- `metadata.json` — the PR URL, owner, repository, and number, the base and
  head SHAs, and the diff path prefix when it is not the standard `a/` + `b/`
  (forward it to `review --diff-prefix`).

Unlike `fetch-mp` there is no already-reviewed check: whether a PR gets
reviewed is decided by whatever triggers the pipeline.

### `publish-pr`

Validate a candidate review and post it to a pull request.

```sh
maas-code-reviewer publish-pr [--github-token TOKEN] --review FILE --input-dir DIR [--dry-run] PR_URL
```

| Argument | Description |
|---|---|
| `PR_URL` | Full GitHub PR URL to publish the review to. |
| `--review FILE` | Path to the candidate review JSON document to publish. |
| `--input-dir DIR` | Directory holding the fetch-pr output (diff.patch and metadata.json) to validate against. |
| `--dry-run` | Print the review JSON to stdout instead of posting it. |
| `--github-token TOKEN` | GitHub personal access token. If not provided, read from the `GITHUB_TOKEN` environment variable. |

Validates the candidate review against the authoritative `diff.patch` and
`metadata.json` from `fetch-pr`'s output directory, prepends the review marker
and preamble to the general comment exactly once, and posts it as a GitHub
pull request review: the general comment as the review body and every inline
finding as a structured review comment. `--dry-run` prints the decorated
review JSON instead and needs no token. An invalid candidate exits nonzero
with nothing posted.

### `review`

Review a unified diff and produce the structured review document. This is the
pipeline-facing command: `fetch-mp`/`fetch-pr` acquire the diff and
description, `review` produces the candidate document, and
`publish-mp`/`publish-pr` validate, decorate, and post it. `review` itself
never posts anything and talks to no Git forge. The diff can be read from a
file or from stdin (pass `-`). The review runs through the omp agent harness
over ACP using the
[runtime configuration](#review-runtime-configuration); there is no Gemini
fallback.

```sh
maas-code-reviewer review --diff-file DIFF_FILE [--description-file FILE] [--diff-prefix PREFIX] [--repo-dir DIR] [--model MODEL] [--runtime-config FILE] [--review-timeout SECONDS] [--repair-attempts N] [--json-output FILE] [--metrics FILE] [--otel-output DIR]
```

| Argument | Description |
|---|---|
| `--diff-file FILE` | Required. Path to a unified diff file, or `-` to read from stdin. |
| `--description-file FILE` | Path to a UTF-8 file with the change description (e.g. the merge proposal or pull request description). |
| `--diff-prefix PREFIX` | Explicit new-side path prefix in the diff (e.g. `w/`). Without it, the prefix is inferred from the diff and the `--repo-dir` listing; pass an empty string for prefix-free paths. |
| `--repo-dir DIR` | Path to the local git repository (default: current working directory). The agent inspects this tree with its native read/grep/glob tools, AGENTS.md is included as untrusted conventions, and validation resolves diff paths against this checkout. |
| `--model MODEL` | Exact model identity to review with (default: the runtime profile). Must be allowed by the runtime profile's allowlist. |
| `--runtime-config FILE` | Explicit trusted TOML runtime configuration overriding individual built-in review runtime defaults. |
| `--review-timeout SECONDS` | Finite positive deadline in seconds for startup and the agent turn of each attempt, including repair rounds (default: the runtime profile). |
| `--repair-attempts N` | Bounded repair rounds for an invalid review document (default: 1; 0 disables repair). When the terminal JSON fails host validation, the agent is re-run with the rejected output and the full failure list, both marked as data, not instructions; usage is summed across attempts. |
| `--json-output FILE` | Write the raw, undecorated JSON review document to `FILE` instead of printing human-readable text to stdout (see [JSON Review Format](#json-review-format)). `publish-mp`/`publish-pr` validate this candidate and add the review marker/preamble when posting. |
| `--metrics FILE` | Write review metrics as JSON to `FILE` (see [Review Metrics](#review-metrics)). |
| `--otel-output DIR` | Collect the agent's OpenTelemetry diagnostics as OTLP JSONL files (`traces.jsonl`, `metrics.jsonl`) in DIR. |

When the agent's terminal document fails host validation, `review`
re-runs the agent up to `--repair-attempts` times with the rejected
output and the full validation failure list, both marked as data, not
instructions. Token usage and cost are summed across attempts, and a
field stays `null` when any attempt did not report it. Exhausting the
rounds fails the review with the validation failure — nothing
unvalidated is written or published. Runner failures (transport
errors, timeouts, cancellations, refusals) are never repaired, and
every attempt receives the full `--review-timeout` deadline.

`review` always produces the structured document — there is no free-text
mode. With `--json-output` the document is written raw: the review marker and
preamble are added by the publish commands, never here. Without
`--json-output`, a human-readable rendering (the general comment, then one
`<path>:<line>: <comment>` line per inline finding) is printed to stdout.

**Examples:**

```sh
# Pipeline: write the candidate document for publish-pr to validate and post
maas-code-reviewer review \
  --repo-dir /path/to/checkout --diff-file diff.patch \
  --description-file description.md \
  --json-output review.json --metrics metrics.json

# Review from stdin, human-readable text to stdout
git diff HEAD~1 | maas-code-reviewer review --diff-file -
```

### `review-mcp`

```
maas-code-reviewer review-mcp --diff-file FILE [--diff-prefix PREFIX] [--repo-dir DIR]
```

Internal plumbing for the in-session validation tool: `review` stages the
reviewed diff at `.maas-code-reviewer/review-input.diff` inside the
checkout and has the agent harness spawn this command as an MCP stdio
server in the review session, exposing one `validate_review` tool whose
verdict reuses the same schema validation as the host. It is not meant
for direct use; the tool is advisory and `publish-mp`/`publish-pr` host
validation against the authoritative diff remains the authority.

## Review runtime configuration

`review` is configured by a trusted runtime profile owned by the package —
never by the reviewed checkout, the environment, or the working directory.
Policy loading is lazy (an ordinary review starts nothing until it runs) and
never reads the reviewed checkout. The built-in defaults:

- Default model `openai-codex/gpt-6.1-sol` with a one-entry allowlist.
- The omp `acp` argv template from the packaged
  `runtime/profile/policy.yml` overlay, which enforces the native tool
  allowlist `read,grep,glob,web_search`.
- Inference endpoint `http://127.0.0.1:4000` (see
  [Inference gateway](#inference-gateway)).
- A 300-second review timeout covering agent startup and the turn.

One explicitly selected TOML file (`--runtime-config FILE`) overrides
individual values, fail-closed: unknown keys are rejected. Supported keys:

```toml
review_timeout_seconds = 600.0

[model]
default = "provider/model"
allowed = ["provider/model"]

[harness]
argv = ["/path/to/omp", "acp", "--model={model}", "..."]

[inference]
endpoint = "http://127.0.0.1:4000"

[profile]
directory = "./profile"
```

`--review-timeout` and `--model` take precedence over the profile: the CLI
timeout must be a finite positive number of seconds, and an explicit model
identity is validated against the profile's allowlist.

The review run's environment is built from scratch — the only secret it
receives is the per-run gateway token, read from the invoking environment's
`GATEWAY_TOKEN` variable and referenced by name in the per-run provider
configuration. A missing or empty `GATEWAY_TOKEN` fails closed before any
agent starts (see [Inference gateway](#inference-gateway)).

With `--otel-output DIR`, the agent's OpenTelemetry diagnostics are
collected by a loopback OTLP receiver into `traces.jsonl` and
`metrics.jsonl` in DIR; the files are flushed before the command writes
metrics, and a collection failure never masks the review result.

## JSON Review Format

When `review --json-output` is used (and when `publish-mp`/`publish-pr` post
the validated candidate), the review document has this schema:

```json
{
  "general_comment": "Overall review summary...",
  "inline_comments": {
    "src/foo.py": {
      "42": "This variable is unused.",
      "108": "Consider using a context manager here."
    },
    "src/bar.py": {
      "17": "This condition is always true."
    }
  }
}
```

- `general_comment` — An overall review summary string.
- `inline_comments` — A map from file path to a map from line number string
  to comment string. Only file paths and line numbers that appear in the diff
  are valid. Use `{}` for no inline comments.

## Review Metrics

The `review` command accepts an optional `--metrics FILE` flag. When
provided, a JSON file is written after the review completes containing usage
and context metrics:

```json
{
  "model_name": "openai-codex/gpt-6.1-sol",
  "tokens_thinking": 1234,
  "tokens_input": 5678,
  "tokens_output": 910,
  "agents_md_read": true,
  "diff_lines": 42,
  "diff_size_bytes": 2048,
  "agent_name": "omp",
  "agent_version": "18.12.2",
  "tokens_cache_read": null,
  "tokens_cache_write": null,
  "tokens_total": null,
  "cost": 0.0125,
  "cost_currency": "USD"
}
```

| Field | Description |
|---|---|
| `model_name` | The model identity the review ran with, or `null` when none was reported. |
| `tokens_thinking` | Number of internal thinking tokens used by the model. |
| `tokens_input` | Number of input (prompt) tokens. |
| `tokens_output` | Number of output (completion) tokens. |
| `agents_md_read` | Whether the repository's `AGENTS.md` was included in the review context as untrusted conventions. |
| `diff_lines` | Number of lines in the original diff (before any truncation). |
| `diff_size_bytes` | Size of the original diff in bytes (UTF-8 encoded). |
| `agent_name` | Name of the agent harness that produced the review (`review` only). |
| `agent_version` | Version of the agent harness (`review` only). |
| `tokens_cache_read` | Number of cache-read tokens reported by the agent (`review` only). |
| `tokens_cache_write` | Number of cache-write tokens reported by the agent (`review` only). |
| `tokens_total` | Total token count reported by the agent (`review` only). |
| `cost` | Inference cost reported by the agent (`review` only). |
| `cost_currency` | Currency of the reported cost (`review` only). |

The agent-path fields (`agent_name` through `cost_currency`) are populated by
`review`. On the agent path, `null` means *unavailable* — the agent did not
report a value — never a fabricated zero.

The metrics file is written after the review completes. If the review is
skipped (e.g. `fetch-mp` reported an already-reviewed proposal), no metrics
file is written.

**Example:**

```sh
maas-code-reviewer review --repo-dir . --diff-file diff.patch --metrics metrics.json
cat metrics.json
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GITHUB_TOKEN` | No | GitHub personal access token. Overridden by `--github-token`. Used by `fetch-pr` and `publish-pr`. |

## Development

Install development dependencies:

```sh
uv sync
```

Run all checks (lint, typecheck, tests):

```sh
make check
```

Individual targets:

```sh
make lint        # ruff check
make typecheck   # ty check
make format      # ruff format
make test        # pytest
make coverage    # pytest with 100% coverage enforcement
```

### Workshop SDK

The deployment artifact is a Workshop SDK (`sdkcraft.yaml` + `hooks/`, built
and published with `sdkcraft`/LXD): `latest/edge` tracks main, and version
tags promote the validated edge revision to `latest/stable` — a channel-pointer
move, never a rebuild. One-time operator prerequisites: register the SDK name
under the team's publisher (`sdkcraft register maas-code-reviewer`) and store
exported Store credentials as the `SDKCRAFT_STORE_CREDENTIALS` secret
(see DESIGN.md "Deployment"). Development still runs from a checkout
(`uv sync`); `make check` is the shared gate for both.

## License

Copyright MAAS Developers.

# maas-code-reviewer

An LLM-powered code reviewer agent for [GitHub](https://github.com/) pull
requests and [Launchpad](https://launchpad.net/) merge proposals. It can also be
used to review changes locally before submitting.

Its purpose is to help a human reviewer by taking care of the initial review quickly.
The person proposing the change can then make any changes they feel are necessary
before a human reviewer picks up the PR/MP.

It uses a Gemini LLM to review diffs and post findings as comments on the merge proposal or pull request. 

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

It works as it is. The only thing you might change is `env.MODEL`, which controls
which Gemini model is being used to do the code review.

You also need to create a secret in the repo called `GEMINI_API_KEY` which
contains the API key that you can get from https://aistudio.google.com.

For split pipelines (reviewing in a separate container), `fetch-pr` writes the
review inputs and `publish-pr` validates and posts a candidate review — see the
command reference.

### Launchpad

There's currently no automation to run code reviews automatically on new merge proposals.
At the moment you have to set up your own automation and then use the `review-mp` command.
For split pipelines, `fetch-mp` writes the review inputs and `publish-mp` validates and
posts a candidate review — see the command reference.

### Locally

To use it locally, generate the diff that you want reviewed and use the `review-diff`
command. For example

  `git show HEAD | maas-code-reviewer review-diff -`

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

### Gemini API Key

All review commands require a Gemini API key. Provide it in one of two ways:

| Method | Details |
|---|---|
| `-g`, `--gemini-api-key-file FILE` | Path to a file containing the key. |
| `GEMINI_API_KEY` | Set this environment variable to the key. Overridden by `--gemini-api-key-file`. |

```sh
maas-code-reviewer review-mp -g /path/to/gemini-api-key MP_URL
```

### GitHub Token

The `review-pr`, `fetch-pr`, and `publish-pr` commands require a GitHub
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

### `review-mp`

Review a single Launchpad merge proposal using Gemini.

```sh
maas-code-reviewer review-mp [--launchpad-credentials FILE] -g KEY_FILE [--model MODEL] [--dry-run] [--metrics FILE] MP_URL
```

| Argument | Description |
|---|---|
| `MP_URL` | URL of the merge proposal to review. |
| `-g`, `--gemini-api-key-file` | Path to file containing the Gemini API key. Falls back to `GEMINI_API_KEY` env var. |
| `--model MODEL` | Gemini model to use (default: `gemini-3-flash-preview`). |
| `--dry-run` | Print the review to stdout instead of posting it as a comment. |
| `--metrics FILE` | Write review metrics as JSON to `FILE` (see [Review Metrics](#review-metrics)). |
| `--launchpad-credentials FILE` | Path to Launchpad credentials file. |

The tool will:

1. Fetch the merge proposal metadata and comments from Launchpad.
2. Skip if a review has already been posted by this tool.
3. Clone the target repository and merge the source branch.
4. Generate a diff.
5. Send the diff to the LLM for review.
6. Post the review as a comment (unless `--dry-run` is set).

**Example:**

```sh
maas-code-reviewer review-mp -g gemini-api-key --dry-run \
  https://code.launchpad.net/~user/project/+git/repo/+merge/123
```

### `review-diff`

Review a unified diff file and print the result to stdout. The diff can be
read from a file or from stdin (pass `-` as the filename).

```sh
maas-code-reviewer review-diff -g KEY_FILE [--model MODEL] [--repo-dir DIR] [--json-output FILE] [--metrics FILE] DIFF_FILE
```

| Argument | Description |
|---|---|
| `DIFF_FILE` | Path to a unified diff file, or `-` to read from stdin. |
| `-g`, `--gemini-api-key-file` | Path to file containing the Gemini API key. Falls back to `GEMINI_API_KEY` env var. |
| `--model MODEL` | Gemini model to use (default: `gemini-3-flash-preview`). |
| `--repo-dir DIR` | Path to the local git repository (default: current working directory). Used for `read_file` and `list_directory` tool calls, and to proactively include the repository's `AGENTS.md` (if present) in the review context. |
| `--json-output FILE` | Write structured JSON review output to `FILE` instead of plain text to stdout. |
| `--metrics FILE` | Write review metrics as JSON to `FILE` (see [Review Metrics](#review-metrics)). |

When `--json-output` is provided, the LLM produces structured output with a
general comment and inline comments keyed by file path and line number (see
[JSON Review Format](#json-review-format) below).

**Examples:**

```sh
# Review a diff file, print plain text to stdout
maas-code-reviewer review-diff -g gemini-api-key changes.diff

# Review from stdin
git diff HEAD~1 | maas-code-reviewer review-diff -g gemini-api-key -

# Produce structured JSON output
maas-code-reviewer review-diff -g gemini-api-key --json-output review.json changes.diff
```

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

### `review-pr`

Review a GitHub pull request using Gemini and post the review via the GitHub
API.

```sh
maas-code-reviewer review-pr -g KEY_FILE [--github-token TOKEN] [--model MODEL] [--repo-dir DIR] [--dry-run] [--metrics FILE] PR_URL
```

| Argument | Description |
|---|---|
| `PR_URL` | Full GitHub PR URL, e.g. `https://github.com/owner/repo/pull/42`. |
| `-g`, `--gemini-api-key-file` | Path to file containing the Gemini API key. Falls back to `GEMINI_API_KEY` env var. |
| `--github-token TOKEN` | GitHub personal access token. Falls back to `GITHUB_TOKEN` env var. |
| `--model MODEL` | Gemini model to use (default: `gemini-3-flash-preview`). |
| `--repo-dir DIR` | Path to a local checkout of the repository (default: current working directory). Used for `read_file` and `list_directory` tool calls, and to proactively include the repository's `AGENTS.md` (if present) in the review context. The caller is responsible for having the repo checked out already. |
| `--dry-run` | Print the review JSON to stdout instead of posting it. |
| `--metrics FILE` | Write review metrics as JSON to `FILE` (see [Review Metrics](#review-metrics)). |

The tool will:

1. Parse the PR URL to extract the owner, repository, and PR number.
2. Fetch the diff and description from GitHub.
3. Send the diff to the LLM for a structured review.
4. Post the review as a GitHub pull request review (unless `--dry-run`).

**Examples:**

```sh
# Review a PR and post the result
maas-code-reviewer review-pr -g gemini-api-key \
  https://github.com/canonical/maas/pull/42

# Dry run — print the JSON review to stdout
maas-code-reviewer review-pr -g gemini-api-key --dry-run \
  https://github.com/canonical/maas/pull/42

# Use a local checkout for extra context
maas-code-reviewer review-pr -g gemini-api-key --repo-dir /path/to/maas \
  https://github.com/canonical/maas/pull/42
```

### `review`

Review a unified diff and produce the structured review document. This is the
pipeline-facing command: `fetch-mp`/`fetch-pr` acquire the diff and
description, `review` produces the candidate document, and
`publish-mp`/`publish-pr` validate, decorate, and post it. `review` itself
never posts anything and talks to no Git forge. The diff can be read from a
file or from stdin (pass `-`).

```sh
maas-code-reviewer review -g KEY_FILE --diff-file DIFF_FILE [--description-file FILE] [--diff-prefix PREFIX] [--repo-dir DIR] [--model MODEL] [--json-output FILE] [--metrics FILE]
```

| Argument | Description |
|---|---|
| `--diff-file FILE` | Required. Path to a unified diff file, or `-` to read from stdin. |
| `--description-file FILE` | Path to a UTF-8 file with the change description (e.g. the merge proposal or pull request description). |
| `--diff-prefix PREFIX` | Explicit new-side path prefix in the diff (e.g. `w/`). Without it, the prefix is inferred from the diff and the `--repo-dir` listing; pass an empty string for prefix-free paths. |
| `-g`, `--gemini-api-key-file` | Path to file containing the Gemini API key. Falls back to `GEMINI_API_KEY` env var. |
| `--model MODEL` | Gemini model to use (default: `gemini-3-flash-preview`). |
| `--repo-dir DIR` | Path to the local git repository (default: current working directory). Used for `read_file` and `list_directory` tool calls, to proactively include the repository's `AGENTS.md` (if present) in the review context, and to resolve diff paths against the checkout listing during validation. |
| `--json-output FILE` | Write the raw, undecorated JSON review document to `FILE` instead of printing human-readable text to stdout (see [JSON Review Format](#json-review-format)). `publish-mp`/`publish-pr` validate this candidate and add the review marker/preamble when posting. |
| `--metrics FILE` | Write review metrics as JSON to `FILE` (see [Review Metrics](#review-metrics)). |

`review` always produces the structured document — there is no free-text
mode. With `--json-output` the document is written raw: the review marker and
preamble are added by the publish commands, never here. Without
`--json-output`, a human-readable rendering (the general comment, then one
`<path>:<line>: <comment>` line per inline finding) is printed to stdout.

**Examples:**

```sh
# Pipeline: write the candidate document for publish-pr to validate and post
maas-code-reviewer review -g gemini-api-key \
  --repo-dir /path/to/checkout --diff-file diff.patch \
  --description-file description.md \
  --json-output review.json --metrics metrics.json

# Review from stdin, human-readable text to stdout
git diff HEAD~1 | maas-code-reviewer review -g gemini-api-key --diff-file -
```

## JSON Review Format

When `review --json-output` or `review-diff --json-output` is used, or when
`review-pr` posts a review, the LLM produces structured output with this
schema:

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

All four review commands (`review-mp`, `review-diff`, `review-pr`, `review`)
accept an optional `--metrics FILE` flag. When provided, a JSON file is written after
the review completes containing usage and context metrics:

```json
{
  "model_name": "gemini-3-flash-preview",
  "tokens_thinking": 1234,
  "tokens_input": 5678,
  "tokens_output": 910,
  "files_read": 3,
  "agents_md_read": true,
  "diff_lines": 42,
  "diff_size_bytes": 2048
}
```

| Field | Description |
|---|---|
| `model_name` | The Gemini model used for the review. |
| `tokens_thinking` | Number of internal thinking tokens used by the model. |
| `tokens_input` | Number of input (prompt) tokens. |
| `tokens_output` | Number of output (completion) tokens. |
| `files_read` | Number of files read via the `read_file` tool during the review. |
| `agents_md_read` | Whether the repository's `AGENTS.md` file was included in the review (fetched proactively, or read via the `read_file` tool). |
| `diff_lines` | Number of lines in the original diff (before any truncation). |
| `diff_size_bytes` | Size of the original diff in bytes (UTF-8 encoded). |

The metrics file is written after the review completes. If the review is
skipped (e.g. an MP was already reviewed), no metrics file is written.

**Example:**

```sh
maas-code-reviewer review-diff -g gemini-api-key --metrics metrics.json changes.diff
cat metrics.json
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GITHUB_TOKEN` | No | GitHub personal access token. Overridden by `--github-token`. Used by `review-pr`, `fetch-pr`, and `publish-pr`. |

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

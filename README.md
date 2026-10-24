# maas-code-reviewer

A Python CLI that reviews GitHub pull requests, Launchpad merge proposals, and
local diffs with an ACP agent. Python acquires inputs, validates the final review
against the full original diff, formats output, and publishes. The agent and its
native tools run in a disposable Workshop selected by an explicit trusted profile.

**omp is the only currently supported harness.** The tested runtime is
**omp 18.12.2** with Python **agent-client-protocol 0.12.1**. ACP and the neutral
runner boundary make a future harness switch easier; alternative-harness
support or conformance is not part of the current deliverable. There is no
Google Python SDK dependency, legacy Gemini execution path, transcript-repair
retry, or automatic local fallback.

**Verification boundary:** the explicitly selected Workshop CLI shim runs the
real installed omp inside an already isolated development environment. It is not
a filesystem, mount, credential, or network sandbox. Real Workshop execution,
mount isolation, SDK/interface pins, and deployment-host lifecycle verification
remain operator gates; see [PLAN.md](PLAN.md). Do not deploy the shim for
untrusted reviews on an ordinary host.

## Installation

Requires Python 3.12 or newer, uv, and git. Production review execution additionally
requires a supported host installation of Workshop, its daemon, LXD, and an
operator-owned runtime profile. `workshopctl` inside a development Workshop is
not a host launcher.
The trusted Workshop base/SDKs must provide `/usr/bin/python3` and standard
`cp`, `chown`, `mount`, `umount`, and `mountpoint` utilities for bootstrap/isolation
checks; SDK/interface availability is verified before agent execution.

From a trusted checkout/revision:

```sh
uv sync --frozen
uv run maas-code-reviewer --help
make check
```

To install a built wheel as a normal host tool:

```sh
uv build
uv tool install --force dist/maas_code_reviewer-0.2.0-py3-none-any.whl
```

Prompts, bootstrap/cleanup resources, and the trusted example harness policy are
packaged with the application. A reviewed checkout is never an installation or
runtime-policy source. Strict Snap execution is unsupported: its former release
workflow and packaging definition have been retired, not changed to classic
confinement. The wheel workflow produces Python distribution artifacts.

## Commands

### Local diffs

```sh
maas-code-reviewer review-diff --runtime-config /etc/maas-code-reviewer/runtime.toml \
  --repo-dir /path/to/checkout changes.diff

# stdin; structured output is written to a file, not review stdout
maas-code-reviewer review-diff --runtime-config /etc/maas-code-reviewer/runtime.toml \
  --repo-dir /path/to/checkout --json-output review.json --metrics metrics.json -
```

Without `--json-output`, stdout contains the validated general comment and every
inline finding, including its repository-relative file and line. The original
diff is retained independently of initial-context truncation. The agent can read
the complete diff and inspect omitted files.

### GitHub pull requests

```sh
maas-code-reviewer review-pr --runtime-config /etc/maas-code-reviewer/runtime.toml \
  --repo-dir /path/to/pr-checkout --dry-run \
  https://github.com/canonical/maas/pull/42
```

`GITHUB_TOKEN` supplies publication/acquisition authentication;
`--github-token TOKEN` overrides it. Prefer the environment to avoid a token in
process arguments. These credentials stay in Python and are not agent model
credentials. The caller must check out the reviewed PR revision before using
`--repo-dir`; the command does not silently fetch or substitute another tree.
Omit `--dry-run` only when publication to that proposal is authorized. Dry run
prints the same host-validated JSON without posting.

### Launchpad merge proposals

```sh
maas-code-reviewer list-lp-mps --status 'Needs review' maas
maas-code-reviewer review-mp --runtime-config /etc/maas-code-reviewer/runtime.toml \
  --launchpad-credentials /path/to/launchpad.credentials --dry-run \
  https://code.launchpad.net/~user/project/+git/repo/+merge/123
```

Launchpad credentials come from `--launchpad-credentials FILE`,
`LP_CREDENTIALS_FILE`, or launchpadlib's normal OAuth flow. The coordinator checks
for an existing bot review first. A duplicate is skipped without runtime
configuration, model authentication, a job, or a metrics artifact. Otherwise it
clones the target branch, merges the source, and keeps the merged tree alive
until the synchronous review and cleanup finish. Dry run prints text; normal
mode posts a host-validated comment.

### Shared review options

| Option | Behavior |
| --- | --- |
| `--runtime-config FILE` | Explicit trusted operator TOML; required only when a nonempty, non-skipped review starts an agent. No repository/home/environment discovery. |
| `--model ID` | Exact identity authorized by the profile and verified against advertised harness support/current selection. An unsupported explicit override fails instead of fuzzy matching or falling back. |
| `--review-timeout SECONDS` | Positive finite total startup/agent deadline; defaults to the profile. Cleanup has a separate bounded grace period. |
| `--max-diff-chars N` | Bounds initial diff context; default 200000. Full-diff validation remains authoritative. |
| `--repo-dir DIR` | Local input checkout for `review-diff`/`review-pr`; defaults to cwd. Never a runtime-definition source. |
| `--metrics FILE` | Write reported usage and host context/diff metadata after a successful validated review. |

Whitespace-only diffs skip the runtime and model authentication. Structured empty
reviews retain their unmarked response; plain text retains the host review
marker/preamble. Empty-review usage is known zero. Runtime/result failures are
nonzero, use stderr, and do not publish or emit review/metrics artifacts.

## Trusted runtime configuration

Callers choose one explicit TOML path. Relative host resources resolve against
the resolved configuration file's directory, not the reviewed checkout or cwd.
Unknown keys, malformed commands/paths, missing executables/templates, unauthorized
models, and invalid deadlines fail before launching a job. Commands are argv
arrays, never shell-interpolated strings.

The profile describes:

- `[workshop]`: executable and trusted project-template directory containing
  `workshop.yaml`; optional absolute host `socket` selects the management Unix
  socket (upstream default `/var/lib/workshop/workshop.socket`). Snap or custom
  hosts must supply their actual socket. The CLI and host change-control API use
  the same endpoint; the agent never receives the socket or its environment key.
  The canonical Workshop Snap declares the socket at
  `/var/snap/workshop/common/workshop/workshop.socket`. Its launcher fixes that
  environment value; for an explicitly different endpoint use the underlying
  `/snap/workshop/current/bin/workshop` binary, not the Snap dispatcher alias.
  Workshop's own Snap is classic-confined; this does not restore the retired
  strict-confined `maas-code-reviewer` package.
- `[harness]`: container-local argv. Trusted placeholders supply the selected
  model, system instruction, protected paths/profile directory, and remaining
  deadline; replacement is per argument, not shell expansion.
- `[paths]`: absolute normalized repository, full-diff, system-instruction,
  session cwd, and agent-state paths. They must be distinct and nonoverlapping;
  the session cwd is outside repository data.
- `[model]`: `default` and exact `allowed` identities. Registry advertisement and
  resolved model identity must agree with the requested identity before a turn.
- `[authentication]`: required inference `SDK:plug`, not provider/publication
  credentials or broker authority.
- `[interfaces]`: complete approved connected plugs and host-backed mounts. Every
  host-backed SDK state mount must be inventoried and remapped to fresh job state.
- `[artifacts]`: exact Workshop version, observed SDK revisions, and trusted
  harness-profile directory. A moving SDK Store channel alone is not a pin;
  drift must fail before agent execution.

There is no bundled verified production Workshop artifact combination. On the
real host, an operator must provision a trusted Store SDK definition, capture its
actual CLI/SDK versions and interface/mount inventory, configure inference access,
and complete the deployment gates below. The packaged empty-SDK Workshop template
is explicitly for the local shim, not a production sandbox definition.

### Local Workshop shim with this container's omp

Use a trusted checkout of this application inside an already isolated test
container. The shim is selected explicitly as the Workshop executable; the
production runner still exercises provisioning, exec, and teardown through that
same boundary. It launches the real omp and does not translate ACP frames.

Create an operator-owned copy of the packaged test resources:

```sh
uv run python - <<'PY'
from importlib.resources import files
from pathlib import Path
import json
import shutil
import tempfile

runtime = Path(tempfile.mkdtemp(prefix="maas-review-profile-"))
shutil.copytree(str(files("maas_code_reviewer").joinpath("runtime")), runtime,
                dirs_exist_ok=True)
shutil.copy2(Path.cwd() / "scripts/workshop_shim.py", runtime / "workshop-shim.py")
config = (runtime / "shim.example.toml").read_text().replace(
    'executable = "/project/scripts/workshop_shim.py"',
    "executable = " + json.dumps(str(runtime / "workshop-shim.py")),
)
(runtime / "runtime.toml").write_text(config)
print(runtime / "runtime.toml")
PY
```

Run this setup from a trusted coordinator checkout, not PR input. It copies the
standalone test shim outside the reviewed tree alongside the protected profile.
Runtime preflight rejects control files, definitions, profiles, or executables
inside the reviewed checkout, even when reached through symlinks. The example
targets this development container's installed omp; change both omp executable
references if its installed location differs. The complete schema/example is
[`runtime/shim.example.toml`](src/maas_code_reviewer/runtime/shim.example.toml).
Use the printed configuration path with `review-diff --runtime-config FILE`.

The example's explicit test settings are:

```toml
[test_shim]
omp_executable = "/var/lib/workshop/sdk/omp/bin/omp"
gateway_url = "http://127.0.0.1:4000"
token_env = "PI_GATEWAY_TOKEN"
```

Supply the existing operator-approved inference gateway bearer through the named
environment variable. Do not print it, put it in argv/TOML values, copy a persistent
omp auth profile, query credential-export endpoints, or use a publication token.
A fresh generated model configuration references the private environment value
by variable name. `[test_shim]` is an explicit development opt-in, not automatic
fallback when Workshop is unavailable. Remove this section and use a real trusted
Workshop executable/template/artifact policy for deployment.

The trusted omp policy requests only `read,grep,glob,web_search`, disables ambient
extensions/skills/rules/LSP/PTY and skill-learning companions, sets unattended
permission behavior explicitly, and disables device-write companions
(`tools.xdev: false`, `autolearn.enabled: false`). Native web search is
pinned by the harness profile to `modelRoles.web: web/duckduckgo`; it was exercised
with real sources without copying search credentials. Changing the model/search
provider requires a tested trusted omp profile, not support for another harness.
Python has no provider API-key switches.

## Input, credential, and lifecycle boundary

The coordinator snapshots input without following symlinks. Links, including
outside or broken links, become inert `[Repository symlink: target]` annotation
files rather than exposing unrelated host files. These are snapshot metadata,
not literal source contents to flag as code errors. Git metadata (`.git`
directories or worktree-pointer files) is not staged: native inspection does
not need hooks or credential-bearing Git configuration. Keep other secrets out
of the selected working tree; it is data provided to the agent.

The authoritative original diff stays host-owned. Repository conventions and
descriptions are untrusted task context, not instructions that can select
Workshop definitions, SDKs, hooks, MCP commands, dotenv, plugins, or model policy.
Host convention loading remains path-contained.

The real Workshop job uses fresh identity/state and verifies its expected version,
SDK revisions, interfaces, and remapped mounts before the agent starts. Trusted
bootstrap copies staged data into container-private locations and removes the
host-backed `/project` view before any agent execution. It then read-only bind
mounts the copied repository, full diff, and system-instruction file; it does
not substitute chmod for the host boundary. This source-derived sequence still
requires real-host verification: mount flags/aliases, persistence of unmounts,
SDK state, shell startup, and host-service reachability cannot be proved by the
shim, which deliberately performs no real mounts.

ACP advertises no host filesystem, terminal, or interactive elicitation services;
unexpected permissions/services are rejected. Protocol stdout and diagnostics are
separate. Visible messages, thoughts, tools, and usage are tracked separately; only
the terminal assistant message after prompt completion may become review content.
Provider errors can arrive with `end_turn`; successful stop reasons/process exits
alone are never acceptance criteria. No review-repair/re-prompt retries,
transcript-wide JSON salvage, or partial-output publication are performed.

Cancellation sends `session/cancel`, settles bounded protocol work, and escalates
to job teardown. SDK shutdown is bounded even if a peer stops reading a large
prompt. EOF can drain an active turn rather than cancel immediately. Launches
use `--no-wait`; ownership of pending daemon changes is retained independently
so abort and rollback settlement precede stop/remove after timeout or owner death.
Host change control uses Workshop's supported Unix-socket HTTP APIs, not invented
CLI abort/wait flags. The explicit shim has a separate internal change helper.

Cleanup deletes host state only after remote removal is confirmed. If disposal
cannot be established, protected recovery metadata and the job identity/control
path are retained and reported; deleting the only recovery data is not cleanup.
A cleanup failure blocks a successful review. When it accompanies a primary
error, both are reported without losing the primary exception/partial usage.
An independent ownership-pipe guard and bounded lifecycle supervisors handle
abrupt coordinator death rather than relying solely on Python `finally` or a
local subprocess exit.

Workshop itself does not filter egress. The operator must restrict host/internal
services and gateway authority and accept/document residual outbound disclosure
risk. The local shim has the current user's filesystem/network authority and
cannot prove these boundaries. Do not expose SSH agents, homes, management sockets,
broker credential export/administration, or GitHub/Launchpad credentials through
mounts/interfaces or ACP capabilities.

## Review output and metrics

Every nonempty review internally returns one JSON object, optionally exactly
fenced, with the existing shape:

```json
{
  "general_comment": "Overall review summary",
  "inline_comments": {
    "src/example.py": {"42": "A concrete finding on a new-side diff line."}
  }
}
```

The host schema/diff validator rejects missing/wrong types, unknown/noncanonical
or absolute paths, and invalid lines before decoration, files, or publication.
The host adds its marker/preamble once. Text output preserves every inline finding;
GitHub and `--json-output` preserve structured coordinates without repairing them.

Metrics retain `model_name`, `agent_name`, `agent_version`, `diff_lines`,
`diff_size_bytes`, and `agents_md_read` (actual host convention inclusion), plus:

| Field | Measurement |
| --- | --- |
| `tokens_input`, `tokens_output`, `tokens_thinking` | Reported input/output/thinking usage. |
| `tokens_cache_read`, `tokens_cache_write`, `tokens_total` | Reported cache and total usage, without assuming provider arithmetic. |
| `cost`, `cost_currency` | Reported monetary amount and currency. |

Unavailable values are JSON `null`, not fabricated zeros. Cumulative notifications
are not summed with prompt-response usage. Exact file-read counts and Gemini
resume/tool-limit fields have been removed. Failure exceptions retain available
partial usage, but failed reviews do not produce a metrics file. Duplicate MP
skips also produce no metrics file.

## GitHub Actions and deployment

Both automatic `pull_request_target` and manual `workflow_dispatch` review paths
use `[self-hosted, linux, x64, maas-review-workshop]`. Provision a dedicated ephemeral
runner with working host Workshop/LXD, uv, and a protected runtime profile before
enabling these jobs. They do not install nested LXD or assume GitHub-hosted runners
can reach a Workshop daemon. Actual deployment/workflow runs are not claimed by
local shim verification.

The workflows check out coordinator code at the trusted workflow SHA separately
from PR input, disable persisted checkout credentials, install through its frozen
lockfile, and pass input values through quoted environment variables/argv. The
runtime definition is operator-owned, never read from PR HEAD. Set repository
variable `MAAS_REVIEW_RUNTIME_CONFIG` (default
`/etc/maas-code-reviewer/runtime.toml`) and optionally `MAAS_REVIEW_MODEL` for
automatic review. Publication uses the step-scoped `GITHUB_TOKEN`; model access
belongs to the runtime/gateway. Manual inputs preserve PR number, dry run, initial
diff size, model, and total review deadline. Metrics are uploaded when produced.

Before production cutover, verify real Workshop login-shell stdout cleanliness,
exact SDK/interface/mount state, protected input/control boundaries, gateway and
search reachability, cancellation/EOF/timeout/coordinator-death termination, host
state deletion, and authorized GitHub/Launchpad/workflow dry runs. Publication
testing must use explicitly authorized disposable proposals.

## Migration and development

`-g`/`--gemini-api-key-file`, `--max-tool-calls`, `GEMINI_API_KEY`, and Snap-based
review installation are not compatibility paths. Use explicit trusted runtime
configuration and a total review deadline. A selected harness/gateway may still
use provider credentials, including Google credentials; the Python coordinator no
longer imports or executes Google's SDK.

```sh
make check       # ruff, ty, pytest; unchanged 100% package coverage requirement
make format
uv build
```

Policy/CLI tests use a small fake runner. ACP tests use a controllable subprocess
peer; lifecycle tests use the Workshop process/service boundary. They do not need
provider credentials or real containers. A fake peer is never a production fallback.
The implementation/evidence checklist is [PLAN.md](PLAN.md); the ownership and
security design is [DESIGN.md](DESIGN.md).

## License

Copyright MAAS Developers. AGPL-3.0-only; see [LICENSE](LICENSE).

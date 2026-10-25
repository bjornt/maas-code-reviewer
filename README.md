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
mount isolation, interface inventory, and deployment-host lifecycle verification
remain operator gates; see [PLAN.md](PLAN.md). Do not deploy the shim for
untrusted reviews on an ordinary host.

## Installation

Requires Python 3.12 or newer, uv, and git. Production review execution
additionally requires a supported host installation of Workshop, its daemon, and
LXD. `workshopctl` inside a development Workshop is not a host launcher.
The trusted Workshop base/SDKs must provide `/usr/bin/python3` and standard
`cp`, `chown`, `rm`, `mount`, `umount`, and `mountpoint` utilities for bootstrap/isolation
checks; SDK/interface availability is verified before agent execution.

From a trusted checkout/revision:

```sh
uv sync --frozen
uv run maas-code-reviewer --help
make check
```

### Local wheel

Build the distribution artifacts from a trusted checkout and run them without
installing anything:

```sh
uv build --wheel
uvx --from dist/maas_code_reviewer-0.2.0-py3-none-any.whl maas-code-reviewer --help
```

`uvx` resolves dependencies from PyPI around the local file and caches that file
by path metadata, so rebuilding the same version after a source change takes
effect without `--refresh`. The wheel is `py3-none-any`: the same file runs on
any host that meets the prerequisites above, including one copied to another
machine.

To install it as a persistent host command instead:

```sh
uv tool install --force --python 3.12 dist/maas_code_reviewer-0.2.0-py3-none-any.whl
```

That creates a private per-user environment under `uv tool dir`
(`~/.local/share/uv/tools/maas-code-reviewer/`) and a `maas-code-reviewer`
symlink in `uv tool dir --bin` (`~/.local/bin`, which must be on `PATH`; see
`uv tool update-shell`). No system interpreter, project environment, or root
location is touched. `--force` is required to reinstall an identical version,
and `--python` pins the tested interpreter instead of the newest one uv finds.
`uv tool uninstall maas-code-reviewer` removes the environment and the symlink.

Prompts, bootstrap/cleanup resources, the generated Workshop definition, and the
trusted harness policy are packaged with the application. A reviewed checkout is
never an installation or runtime-policy source. Strict Snap execution is
unsupported: its former release
workflow and packaging definition have been retired, not changed to classic
confinement. The wheel workflow produces Python distribution artifacts.

### Nightly builds

Every night the CI builds the current `main` (after `make check` passes) into a
dated development version such as `0.3.0.dev202610240900`, publishes it as a
GitHub prerelease, and regenerates a static [PEP 503][pep503] index. Nothing is
published when `main` has not moved; the fourteen most recent nightlies are
retained.

Run the latest nightly without installing it:

```sh
uvx --index https://canonical.github.io/maas-code-reviewer/simple/ \
  --prerelease allow --from maas-code-reviewer maas-code-reviewer --help
```

Install it as a host tool, or pin one exact nightly:

```sh
uv tool install --index https://canonical.github.io/maas-code-reviewer/simple/ \
  --prerelease allow maas-code-reviewer

uvx --index https://canonical.github.io/maas-code-reviewer/simple/ \
  --prerelease allow --from 'maas-code-reviewer==0.3.0.dev202610240900' \
  maas-code-reviewer --help
```

The index lists only this project; dependencies still resolve from PyPI. Each
link carries the release asset's `sha256` digest when GitHub reports one.
Nightlies are untested-in-production artifacts for evaluation, carry no support
promise, and change the application only: the Workshop, LXD, inference, and
trusted-profile requirements above are unchanged, as is the verification
boundary. Publishing requires the repository's Pages source to be set to GitHub
Actions once; the workflow deploys the index, not a branch.

[pep503]: https://peps.python.org/pep-0503/

## Commands

### Local diffs

```sh
maas-code-reviewer review-diff --repo-dir /path/to/checkout changes.diff

# stdin; structured output is written to a file, not review stdout
maas-code-reviewer review-diff --repo-dir /path/to/checkout \
  --json-output review.json --metrics metrics.json -

# with an operator profile overriding the built-in runtime defaults
maas-code-reviewer review-diff --runtime-config /etc/maas-code-reviewer/runtime.toml \
  --repo-dir /path/to/checkout changes.diff
```

Without `--json-output`, stdout contains the validated general comment and every
inline finding, including its repository-relative file and line. The original
diff is retained independently of initial-context truncation. The agent can read
the complete diff and inspect omitted files.

Git's standard `a/` and `b/` prefixes, mnemonic prefixes such as `i/` and `w/`,
custom prefixes (including multi-directory or non-directory strings), and
prefix-free patches are supported. New-side paths are resolved against filenames
in the original checkout and Git rename/copy metadata, without following checkout
symlinks. The original diff is retained unchanged; inline findings still require
repository-relative paths and lines present in its new-side hunks.

Git patches do not record prefix lengths. If more than one checkout filename
matches a custom-prefixed path, the command fails before starting an agent rather
than guessing. For `review-diff`, use `--diff-prefix PREFIX` to specify the exact
new-side prefix, for example `--diff-prefix 'updated/tree/'`. Use
`--diff-prefix ''` to explicitly preserve prefix-free paths, including real
directories named `b` or `w`.

### GitHub pull requests

```sh
maas-code-reviewer review-pr \
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
maas-code-reviewer review-mp \
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
| `--runtime-config FILE` | Optional explicit operator TOML overriding individual built-in runtime defaults. No repository/home/environment discovery. |
| `--model ID` | Exact identity authorized by the profile and verified against advertised harness support/current selection. An unsupported explicit override fails instead of fuzzy matching or falling back. |
| `--review-timeout SECONDS` | Positive finite total startup/agent deadline; defaults to the profile. Cleanup has a separate bounded grace period. |
| `--max-diff-chars N` | Bounds initial diff context; default 200000. Full-diff validation remains authoritative. |
| `--repo-dir DIR` | Local input checkout for `review-diff`/`review-pr`; defaults to cwd. Never a runtime-definition source. |
| `--metrics FILE` | Write reported usage and host context/diff metadata after a successful validated review. |

Whitespace-only diffs skip the runtime and model authentication. Structured empty
reviews retain their unmarked response; plain text retains the host review
marker/preamble. Empty-review usage is known zero. Runtime/result failures are
nonzero, use stderr, and do not publish or emit review/metrics artifacts.

## Review runtime configuration

Reviews run on built-in defaults owned by this package: no operator file is
required. The defaults select the host `workshop` executable found on `PATH`,
generate the Workshop definition, request the `omp` SDK, declare one inference
tunnel, and use the packaged harness profile, container layout, argv, and a
300-second deadline.

`--runtime-config FILE` overrides individual values. Relative host resources
resolve against the resolved configuration file's directory, not the reviewed
checkout or cwd. Unknown keys, malformed commands/paths, missing executables,
unauthorized models, and invalid deadlines fail before launching a job. Commands
are argv arrays, never shell-interpolated strings. Every setting is optional:

- `[workshop]`: `executable` (default: the `workshop` on `PATH`), `base`
  (default `ubuntu@26.04`), and absolute host `socket` selecting the management
  Unix socket (upstream default `/var/lib/workshop/workshop.socket`). Snap or
  custom hosts must supply their actual socket. The CLI and host change-control
  API use the same endpoint; the agent never receives the socket or its
  environment key. The canonical Workshop Snap declares the socket at
  `/var/snap/workshop/common/workshop/workshop.socket`. Its launcher fixes that
  environment value; for an explicitly different endpoint use the underlying
  `/snap/workshop/current/bin/workshop` binary, not the Snap dispatcher alias.
  Workshop's own Snap is classic-confined; this does not restore the retired
  strict-confined `maas-code-reviewer` package.
- `[omp]`: `channel` for the harness SDK. Omitted by default, which keeps
  Workshop's own channel resolution. Workshop's definition format has no
  revision field, so a channel is the only reproducibility control available;
  this application does not pretend otherwise by pinning observed versions.
- `[harness]`: container-local argv. Trusted placeholders supply the selected
  model, system instruction, protected paths/profile directory, and remaining
  deadline; replacement is per argument, not shell expansion.
- `[paths]`: absolute normalized repository, full-diff, system-instruction,
  session cwd, and agent-state paths. Overrides merge with the default layout
  and the whole result must stay distinct and nonoverlapping; the session cwd is
  outside repository data.
- `[model]`: `default` identity, and an optional exact `allowed` list. Without
  `allowed`, `--model` selects any identity the gateway serves; with it, both
  `default` and `--model` must appear in it. Registry advertisement and resolved
  model identity must agree with the requested identity before a turn.
- `[authentication]`: inference `plug` name (default `review-inference`) and
  tunnel `endpoint` (default `localhost:4000`). The SDK is always `omp` because
  this application writes the definition that declares the plug and its
  same-named `system` slot. Each job runs `workshop connect JOB/omp:PLUG` after
  readiness, then verifies that exactly that one tunnel is connected. Connection
  failure or timeout aborts startup and follows normal job cleanup.
- `[profile]`: `directory` holding the trusted harness policy; defaults to the
  packaged profile.

The generated definition is written into a fresh disposable project directory
and re-read immediately before `workshop launch`; any edit between preparation
and launch aborts the review. A `workshop.yaml` inside the reviewed checkout is
ordinary snapshot data and is never loaded.

Every host-backed SDK state mount Workshop reports is inventoried and remapped
to fresh job state automatically; no declaration is required and an unremappable
mount blocks the agent. Each replacement source directory is created before
invoking `workshop remount`; Workshop does not create it on the stopped
cross-filesystem path when the old source exists. Jobs with mounts are stopped
before remapping and restarted before bootstrap: Workshop cannot safely perform
every live remount across host filesystems (for example, SDK storage to a job
directory under `/tmp`). Failure to stop, remap, or restart aborts startup and
follows normal job cleanup. Raw lifecycle stderr is intentionally not echoed
because it may contain secrets. If remount still fails, obtain the underlying
diagnostic by running it directly against a disposable probe Workshop, never a
development or production job. Credential, device, and other non-tunnel
interfaces are rejected outright.

### Real Workshop host setup

Installed-wheel and live GitHub Actions runs were exercised on Ubuntu 26.04.1
with Workshop 0.9.9, LXD 6.10.0, the `omp` SDK's `latest/stable` channel
(observed omp 18.12.2 / revision 250), Python ACP 0.12.1, and Actions runner
2.338.0. Observed revisions are evidence, not enforced pins. See
[`PLAN.md` sections 8.4–8.5](PLAN.md#84-real-workshop-vm-execution-record).

On a dedicated Ubuntu host, install the host tools:

```sh
sudo snap install --channel=6/stable lxd
sudo snap install --classic workshop
sudo snap install --classic astral-uv
workshop --version
lxc version
sdk info omp
```

Create policy outside every reviewed checkout, using the Python interpreter
from the trusted coordinator installation. This example uses the verified VM's
installation path and creates root-owned policy for the workflow default:

```sh
sudo /home/ubuntu/reviewer-env/bin/python -I - <<'PY'
from importlib.resources import files
from pathlib import Path
import json
import shutil

root = Path("/etc/maas-code-reviewer")
root.mkdir(parents=True, exist_ok=True)
profile = root / "profile"
shutil.copytree(str(files("maas_code_reviewer").joinpath("runtime/profile")),
                profile, dirs_exist_ok=True)
(profile / "models.yml").write_text('''providers:
  openai-codex:
    baseUrl: http://127.0.0.1:4401
    transport: pi-native
    apiKey: review-inference
''')
(root / "runtime.toml").write_text('''[workshop]
executable = "/snap/bin/workshop"
socket = "/var/snap/workshop/common/workshop/workshop.socket"
[omp]
channel = "latest/stable"
[authentication]
endpoint = "127.0.0.1:4401"
[profile]
directory = ''' + json.dumps(str(profile)) + '''
[model]
default = "openai-codex/gpt-6.1-sol"
allowed = ["openai-codex/gpt-6.1-sol"]
''')
print(root / "runtime.toml")
PY
```

The example uses an authenticated native inference gateway at host loopback
port 4000, with its bearer in `$HOME/auth-gateway-token`. Install the maintained
inference-only front end from a **trusted coordinator source checkout**, never
from PR input:

```sh
sudo install -d -m 0755 /usr/local/lib/maas-code-reviewer
sudo install -m 0600 "$HOME/auth-gateway-token" /etc/maas-code-reviewer/auth-gateway-token
sudo install -m 0644 scripts/review_inference_proxy.py \
  /usr/local/lib/maas-code-reviewer/inference_proxy.py
sudo install -m 0644 scripts/maas-review-inference.service \
  /etc/systemd/system/maas-review-inference.service
sudo systemctl daemon-reload
sudo systemctl enable --now maas-review-inference.service
```

`maas-review-inference.service` listens only on loopback port 4401 and exposes
only `GET /v1/models` and `POST /v1/pi/stream`. It fixes the upstream endpoint,
rejects forwarding/management authority, removes caller provider headers and
credential/URL options, and loads the upstream bearer through systemd
`LoadCredential`. `review-inference` in the container profile is a nonsecret
literal; the real bearer stays on the host. Downstream cancellation closes the
upstream inference stream. Restart the service after changing its code or
installing a rotated credential file.

The operator explicitly accepted inference authority over all advertised models;
the filter preserves that catalog rather than pretending its bearer is model-
scoped. The review profile still selects/authorizes its exact model separately.
The provided upstream port 4000 is a user-managed SSH reverse forward: its
availability remains an operator dependency. The old assistant-owned temporary
port-4400 gateway/tunnel was retired after this deployment passed real reviews.
An existing equivalently restricted inference-only service may replace this
example, but never expose broker administration or credential export.

```sh
maas-code-reviewer review-diff \
  --runtime-config /etc/maas-code-reviewer/runtime.toml \
  --repo-dir /path/to/checkout changes.diff
```

Host network restrictions are separate from this TOML. The verified VM uses
`maas-review-network.service` and nftables tables `inet maas_review` / `bridge
maas_review` to deny unrelated host/private/peer services while permitting DNS
and public IPv4 traffic. Public egress still permits disclosure of review data;
neither Workshop nor this application claims an Internet allowlist.

Workshop 0.9.9 exposed a shared-SDK ETag conflict during concurrent provisioning.
Serialize provisioning on affected hosts; two already-running jobs still have
independent state. A failed launch/removal retains recovery metadata rather than
silently retrying or declaring cleanup. See the phase-8 record before removing
retained state.

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
Runtime preflight rejects control files, profiles, or executables inside the
reviewed checkout, even when reached through symlinks. The example targets this
development container's installed omp; change both omp executable references if
its installed location differs. The example overrides only the Workshop
executable and the test-shim block and relies on defaults for everything else:
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
fallback when Workshop is unavailable. Remove this section and use a real
trusted Workshop executable and policy for deployment.

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

The real Workshop job uses fresh identity/state, re-reads its own generated
definition before launch, and verifies readiness, connected interfaces, and
remapped mounts before the agent starts. Trusted
bootstrap copies staged data into container-private locations and removes the
host-backed `/project` view before any agent execution. It then read-only bind
mounts the copied repository, full diff, and system-instruction file; it does
not substitute chmod for the host boundary. Real-host mount inventories, write
probes, effective-tool registration, clean ACP stdout, and host/private/peer
reachability checks verify the recorded deployment. Recheck after host/SDK policy
changes; the shim deliberately performs no real mounts.
Bootstrap also unmounts the unused `/dev/lxd` guest API and unlinks the
container-local Workshop untrusted proxy socket before agent execution. These
operations remove guest access paths, not the host's daemon sockets.

Trusted bootstrap code is passed inline to `sh -c`, not executed from a file on
`/project`. Executing the script from that mount would leave a shell descriptor
open on it and prevent bootstrap from unmounting its own host project view.

ACP advertises no host filesystem, terminal, or interactive elicitation services;
unexpected permissions/services are rejected. Protocol stdout and diagnostics are
separate. Visible messages, thoughts, tools, and usage are tracked separately; only
the terminal assistant message after prompt completion may become review content.
Provider errors can arrive with `end_turn`; successful stop reasons/process exits
alone are never acceptance criteria. No review-repair/re-prompt retries,
transcript-wide JSON salvage, or partial-output publication are performed.

Cancellation sends `session/cancel`, settles bounded protocol work, and escalates
to job teardown. SDK shutdown is bounded even if a peer stops reading a large
prompt. EOF can drain an active turn rather than cancel immediately.

The SDK gets a separate bounded graceful-exit window so a valid review is not
lost merely because its remote launcher needs more than a permission-cancel
grace period to drain. Forced/nonzero process shutdown still fails the review.

Launches use `--no-wait`; ownership of pending daemon changes is retained independently
so abort and rollback settlement precede stop/remove after timeout or owner death.
An observed launch ID is retained even if its CLI times out after printing it.
Before removal, cleanup settles pending exec changes belonging to the launch's
verified project; closing a local pipe or stopping a container alone does not
settle the daemon's exec operation.
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

Container stop has a separate bounded budget covering Workshop's normal
60-second LXD shutdown allowance plus bookkeeping. The full guardian reserve
includes launch and exec settlement; cleanup can outlast the review deadline.

Workshop names use `review-` followed by the hexadecimal encoding of the unique
temporary-directory suffix, so underscores cannot produce trailing or repeated
hyphens. For retained jobs from older versions, an invalid-definition warning is
not proof that a container exists. Inspect the retained `control.json` and check
running and stopped Workshop/LXD state before removing only that job's directory.
`workshop okay` acknowledges warnings; it does not stop or remove workloads.

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
can reach a Workshop daemon. Both paths were exercised on authorized private
test PRs with real Workshop and official one-job runner 2.338.0; the shim is not
their deployment evidence.

The workflows check out coordinator code at the trusted workflow SHA separately
from PR input, disable persisted checkout credentials, install through its frozen
lockfile, and pass input values through quoted environment variables/argv. The
runtime definition is operator-owned, never read from PR HEAD. Set repository
variable `MAAS_REVIEW_RUNTIME_CONFIG` (default
`/etc/maas-code-reviewer/runtime.toml`) and optionally `MAAS_REVIEW_MODEL` for
automatic review. Publication uses the step-scoped `GITHUB_TOKEN`; model access
belongs to the runtime/gateway. Manual inputs preserve PR number, dry run, initial
diff size, model, and total review deadline. Metrics are uploaded when produced.

Install the official Linux x64 runner using the repository's **Settings →
Actions → Runners** instructions and verify its published checksum. Use a fresh
runner directory and unique runner name for each job. Supply its short-lived
registration token only to configuration, not as a workflow secret or argv value:

```sh
read -rp 'Repository URL: ' ACTIONS_RUNNER_INPUT_URL
read -rp 'Unique runner name: ' runner_name
read -rsp 'Runner registration token: ' ACTIONS_RUNNER_INPUT_TOKEN
printf '\n'
export ACTIONS_RUNNER_INPUT_URL ACTIONS_RUNNER_INPUT_TOKEN
./config.sh --unattended --ephemeral --labels maas-review-workshop --name "$runner_name"
unset runner_name ACTIONS_RUNNER_INPUT_TOKEN ACTIONS_RUNNER_INPUT_URL
./run.sh
```

The runner deregisters after one job. Remove its used workspace/credentials and
provision another ephemeral runner for the next job; this application does not
install a persistent privileged runner pool. The personal token used to provision
test fixtures/runners is not the step-scoped publication `GITHUB_TOKEN` and must
not be passed into jobs or review containers. The verified deployment used the
default `/etc/maas-code-reviewer/runtime.toml`, so repository-variable write
permission was unnecessary.

The live record includes automatic and manual publication, a manual dry run of
poisoned coordinator/Workshop/omp/MCP/hook/dotenv/symlink input, metrics downloads,
and a deliberately invalid deadline that failed with exit 2 and no publication
or metrics artifact. Checkout logs show the trusted coordinator SHA separately
from the PR SHA; host execution sentinels remained absent. See
[`PLAN.md` section 8.5](PLAN.md#85-authorized-live-github-and-persistent-gateway-proof).

Repeat the recorded host/mount/gateway/lifecycle and live workflow checks when
deploying a different host/profile/SDK combination. Publication testing must use
explicitly authorized disposable proposals. Live Launchpad API/publication
verification still requires a separately authorized MP and account; GitHub proof
is not relabeled as Launchpad proof.

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

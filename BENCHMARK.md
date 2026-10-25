# Benchmark the last three MAAS commits

Run these instructions on `ubuntu@10.5.129.70`, from a trusted checkout of
`maas-code-reviewer` containing the collection, replay, and run-ID changes. The
older installed wheel alone does not include these changes. Copy the updated
checkout to the VM first if necessary.

The gateway advertises `github-copilot/claude-opus-5`, not
`github-copilot/opus-5`. The DeepSeek identity is
`openrouter/deepseek/deepseek-v4.1-flash-1017`. Both were present in the gateway
catalog when these instructions were written; availability can change.

## 1. Install the checkout

```sh
cd /path/to/maas-code-reviewer
uv sync --frozen
```

The remaining commands assume this checkout is the current directory.

## 2. Allow all advertised models

Edit the runtime configuration:

```sh
sudoedit /etc/maas-code-reviewer/runtime.toml
```

Remove this line:

```toml
allowed = ["openai-codex/gpt-6.1-sol"]
```

Keep the rest unchanged. Omitting `allowed` permits any exact model identity
supported by omp and the gateway.

The VM's existing `models.yml` routes only `openai-codex` through the gateway.
Generate equivalent routes for every advertised provider, without a per-model
allowlist:

```sh
sudo /home/ubuntu/reviewer-env/bin/python -I - <<'PY'
import json
from pathlib import Path
from urllib.request import urlopen

gateway = "http://127.0.0.1:4401"
with urlopen(gateway + "/v1/models", timeout=30) as response:
    models = json.load(response)["data"]

providers = sorted({model["id"].split("/", 1)[0] for model in models})
configuration = {
    "providers": {
        provider: {
            "baseUrl": gateway,
            "transport": "pi-native",
            "apiKey": "review-inference",
        }
        for provider in providers
    }
}

# JSON is valid YAML. The API-key value here is a nonsecret placeholder.
Path("/etc/maas-code-reviewer/profile/models.yml").write_text(
    json.dumps(configuration, indent=2) + "\n"
)
print(f"Configured {len(providers)} providers through the inference gateway.")
PY
```

This removes model filtering, not the inference-only proxy or Workshop
isolation. These configuration changes also affect other reviews using this
runtime/profile. No service restart is needed for these profile changes; new
reviews load them.

Port `4401` is the host's inference-only proxy, forwarding to the gateway through
port `4000`. Keep this arrangement so the real gateway bearer stays on the host.

## 3. Configure Langfuse in this shell

Keep your existing `OTEL_EXPORTER_OTLP_TRACES_HEADERS` value exported privately.
Do not put its credentials in this document, the corpus, or the runtime TOML.

```sh
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT='http://10.5.128.253:3000/api/public/otel/v1/traces'
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL='http/protobuf'
export OTEL_TRACES_EXPORTER=otlp
export OTEL_SDK_DISABLED=false

: "${OTEL_EXPORTER_OTLP_TRACES_HEADERS:?Set OTEL_EXPORTER_OTLP_TRACES_HEADERS before continuing.}"
```

The header value supports the existing `Authorization=Basic%20...` form and
`x-langfuse-ingestion-version=4`. The exporter runs on the coordinator;
credentials are not forwarded to the reviewer or Workshop. Message-content
capture remains disabled inside Workshop regardless of ambient OTEL settings.
Only traces containing usage/cost attributes are uploaded; raw OTEL metrics
remain local.

## 4. Collect the last three commits

Use a fresh corpus directory:

```sh
RUN_ID="maas-last3-$(date -u +%Y%m%dT%H%M%SZ)"
CORPUS="$HOME/maas-review-runs/$RUN_ID"

uv run python scripts/collect_reviews.py \
  --count 3 \
  --output "$CORPUS"
```

The collector defaults to `canonical/maas` and inspects three first-parent
commits on its default branch. The number of collected reviews may differ if
commits lack bot reviews or have multiple reviewed revisions. See
`$CORPUS/manifest.json` for the selected commits and skip reasons.

Collection also saves submitted human reviews for those PRs, including inline
findings and approvals, under `human_reviews/<landed-sha>-pr<N>/review<ID>/`.
Each directory contains `review.json`, author/revision/state metadata, the
fetched description, and a reconstructed human-reviewed diff when available.
Human replies and ordinary PR conversation comments are retained separately in
that PR's `discussion.json`; they are context, not standalone findings.

The corpus contains reconstructed diffs at each published review's recorded
head, not the final landed patches. PR descriptions are fetched at collection
time and may have changed since the historical review. The original baseline
model label defaults to `gemini-3.1-pro-preview`; historical token/cost data is
not recovered from review text.

Collection does not overwrite changed inputs or baselines. Use a new corpus
when refreshing the selected commits or fetched data. Human-review collection
uses schema version 2: if the corpus was created before this feature, collect
again into a fresh directory rather than reusing it.

## 5. Run both models

```sh
uv run python scripts/replay_reviews.py "$CORPUS" \
  --run-id "$RUN_ID" \
  --runtime-config /etc/maas-code-reviewer/runtime.toml \
  --review-timeout 600 \
  --model github-copilot/claude-opus-5 \
  --model openrouter/deepseek/deepseek-v4.1-flash-1017
```

The reviewer uses the original reviewed checkout, saved diff, and saved
description for each case. It never publishes the benchmark reviews to GitHub.

## 6. Inspect results and resume

Results are under:

```text
$CORPUS/runs/$RUN_ID/reviews/<model>/<case>/attempts/<attempt-id>/
  attempt.json
  review.json
  metrics.json
  otel/traces.jsonl
  otel/metrics.jsonl
  otel/langfuse-traces.jsonl
```

Model directory names are URL-encoded, including their `/` separators.
`review.json` uses the structured `review-diff` format. `metrics.json` contains
aggregate reported token usage, cache usage, cost, currency, and model identity;
unavailable values remain null. Costs are harness estimates, not necessarily
provider invoices. `attempt.json` records correlation IDs and review/export
status. Failed attempts retain already received telemetry but do not become
successful-looking review results.

Compare generated results with both the original Gemini baseline under
`$CORPUS/reviews/` and the human reviews under `$CORPUS/human_reviews/`.
Human `review.json` files use the same structured review schema; unsupported
left-side/multiline findings remain in metadata. Metadata identifies the author,
whether they are the PR author, review state, and reviewed head SHA. Human and
model reviewers may have seen different revisions: inspect those SHAs and diffs
before calling a human finding an LLM miss. Human reviews do not add replay
cases or become model context, and approvals/comments are not automatically
treated as correctness labels.

In Langfuse, filter by **session ID = `$RUN_ID`**. Every span also carries
filterable metadata for `run_id`, `case_id`, `reviewed_commit`, `landed_commit`,
`repository`, `model`, and `attempt_id`.

To resume, rerun the command in step 5 with the same `CORPUS` and `RUN_ID`.
Successful reviews are reused; failed uploads retry without another model
invocation. If using a new shell, restore those two variables and the Langfuse
environment first. Do not rerun the timestamp assignment in step 4 when resuming.

Add `--overwrite` only when you intentionally want another model execution.
It creates a new attempt without deleting earlier evidence. Use a different
`--run-id` for an independent comparison.

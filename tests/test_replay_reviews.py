# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import quote, unquote

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from scripts import replay_reviews

BASELINE_MODEL = "google/gemini-3.1-pro-preview"
BASELINE_REVIEW = {"general_comment": "SECRET HISTORICAL ANSWER", "inline_comments": {}}
RUN_ID = "comparison A/?#%"

FAKE_REVIEWER = r"""
import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["review-diff"])
parser.add_argument("--repo-dir", type=Path, required=True)
parser.add_argument("--description-file", type=Path, required=True)
parser.add_argument("--diff-prefix", required=True)
parser.add_argument("--json-output", type=Path, required=True)
parser.add_argument("--metrics", type=Path, required=True)
parser.add_argument("--otel-output", type=Path, required=True)
parser.add_argument("--model", required=True)
parser.add_argument("--runtime-config", type=Path)
parser.add_argument("--review-timeout", type=float)
parser.add_argument("--max-diff-chars", type=int)
parser.add_argument("diff", type=Path)
args = parser.parse_args()
assert not any(key in os.environ for key in ("GITHUB_TOKEN", "GH_TOKEN"))
assert not any(key.startswith(("OTEL_", "LANGFUSE_")) for key in os.environ)
assert args.diff_prefix == "b/"
assert args.repo_dir.is_dir()
assert not args.json_output.is_relative_to(args.repo_dir)
assert not args.metrics.is_relative_to(args.repo_dir)
assert not args.otel_output.is_relative_to(args.repo_dir)
attempt_state = json.loads(args.otel_output.parent.joinpath("attempt.json").read_text())
assert attempt_state["review_status"] == "running"
assert attempt_state["export_status"] in ("pending", "disabled")
assert not args.description_file.is_relative_to(args.repo_dir)
assert not args.diff.is_relative_to(args.repo_dir)
assert not (args.repo_dir / "reviews").exists()
assert not (args.repo_dir / "repository.git").exists()
assert not (args.repo_dir / "manifest.json").exists()
assert not (args.repo_dir / "submodule" / "ran.txt").exists()
assert args.repo_dir.joinpath("context.txt").read_text() == "review-time context\n"
assert args.repo_dir.joinpath("code.py").read_text() == "new = True\n"
assert "SECRET HISTORICAL ANSWER" not in "".join(
    item.read_text() for item in args.repo_dir.rglob("*")
    if item.is_file() and item.name != ".git"
)
record = {
    "model": args.model,
    "repo_dir": str(args.repo_dir),
    "pid": os.getpid(),
    "cwd": str(Path.cwd()),
    "diff": args.diff.read_text(),
    "description": args.description_file.read_text(),
    "runtime_config": str(args.runtime_config) if args.runtime_config else None,
    "review_timeout": args.review_timeout,
    "max_diff_chars": args.max_diff_chars,
}
control_path = Path(os.environ["REPLAY_TEST_CONTROL"])
control = json.loads(control_path.read_text())
mode = control.get(args.model, "success")
print("timing: fake reviewer phase: 0.00s", file=sys.stderr)
args.otel_output.mkdir(parents=True, exist_ok=True)
spans = [
    {
        "traceId": "0123456789abcdef0123456789abcdef",
        "spanId": "0123456789abcdef",
        "name": "review",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": [
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "17"}}
        ],
    },
    {
        "traceId": "0123456789abcdef0123456789abcdef",
        "spanId": "fedcba9876543210",
        "parentSpanId": "0123456789abcdef",
        "name": "generation",
        "startTimeUnixNano": "1100000000",
        "endTimeUnixNano": "1900000000",
    },
]
batch = {"resourceSpans": [
    {"resource": {}, "scopeSpans": [{"scope": {}, "spans": spans}]}
]}
args.otel_output.joinpath("traces.jsonl").write_text(
    "" if mode == "no-spans" else json.dumps(batch) + "\n"
)
args.otel_output.joinpath("metrics.jsonl").write_text('{"resourceMetrics": []}\n')
with Path(os.environ["REPLAY_TEST_LOG"]).open("a") as stream:
    stream.write(json.dumps(record) + "\n")
if mode == "wait":
    import time
    time.sleep(60)
review = {
    "general_comment": record["description"] + "review-time context",
    "inline_comments": {"code.py": {"1": "Check the new value."}},
}
if mode == "invalid-review":
    review["inline_comments"] = {"code.py": {"999": "Not in the diff."}}
if mode == "extra-key":
    review["unexpected"] = "not the review schema"
if mode == "malformed-json":
    args.json_output.write_text("{not JSON")
else:
    args.json_output.write_text(json.dumps(review))
if mode != "missing-metrics":
    model = "provider/fallback" if mode == "mismatch" else args.model
    args.metrics.write_text(json.dumps({"model_name": model, "tokens_input": 17}))
if mode == "fail":
    print("deliberate reviewer failure after writing outputs", file=sys.stderr)
    raise SystemExit(9)
"""


@dataclass
class Corpus:
    path: Path
    case: Path
    bare: Path
    reviewer: Path
    log: Path
    control: Path

    def run(self, *options: str) -> int:
        return replay_reviews.main(
            [
                str(self.path),
                "--reviewer",
                str(self.reviewer),
                "--run-id",
                RUN_ID,
                *options,
            ]
        )

    def attempt_root(
        self, model: str, case: Path | None = None, run_id: str = RUN_ID
    ) -> Path:
        return (
            self.path
            / "runs"
            / quote(run_id, safe="")
            / "reviews"
            / quote(model, safe="")
            / (case or self.case).name
            / "attempts"
        )

    def attempts(
        self, model: str, case: Path | None = None, run_id: str = RUN_ID
    ) -> list[Path]:
        return [
            path.parent
            for path in sorted(
                self.attempt_root(model, case, run_id).glob("*/attempt.json"),
                key=lambda path: path.stat().st_mtime_ns,
            )
        ]

    def output(
        self, model: str, case: Path | None = None, run_id: str = RUN_ID
    ) -> Path:
        attempts = self.attempts(model, case, run_id)
        attempt = (
            attempts[-1]
            if attempts
            else self.attempt_root(model, case, run_id) / "missing"
        )
        return attempt / "review.json"

    def metrics(self, model: str, case: Path | None = None) -> Path:
        return self.output(model, case).with_name("metrics.json")

    def baseline(self, case: Path | None = None) -> Path:
        return (
            self.path
            / "reviews"
            / quote(BASELINE_MODEL, safe="")
            / f"{(case or self.case).name}.json"
        )

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def set_mode(self, **models: str) -> None:
        self.control.write_text(json.dumps(models))

    def add_case(self, suffix: str = "second") -> Path:
        second = self.case.with_name(self.case.name + "-" + suffix)
        second.mkdir()
        for name in ("metadata.json", "diff.patch", "description.md"):
            second.joinpath(name).write_bytes(self.case.joinpath(name).read_bytes())
        baseline = self.baseline(second)
        baseline.write_text(json.dumps(BASELINE_REVIEW))
        return second


def git(directory: Path, *args: str) -> str:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    return subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch) -> Corpus:
    for key in list(os.environ):
        if key.startswith(("OTEL_", "LANGFUSE_")):
            monkeypatch.delenv(key)
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Replay Test")
    git(source, "config", "user.email", "replay@example.invalid")
    source.joinpath("context.txt").write_text("review-time context\n")
    source.joinpath("code.py").write_text("old = True\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    source.joinpath("code.py").write_text("new = True\n")
    git(source, "commit", "-am", "reviewed head")
    head = git(source, "rev-parse", "HEAD")
    source.joinpath("context.txt").write_text("landed context, not review context\n")
    git(source, "commit", "-am", "landed commit")
    landed = git(source, "rev-parse", "HEAD")
    root = tmp_path / "corpus"
    root.mkdir()
    bare = root / "repository.git"
    git(tmp_path, "clone", "--bare", str(source), str(bare))
    hook = bare / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{tmp_path / 'HOOK_EXECUTED'}'\n")
    hook.chmod(0o755)
    case = root / "cases" / f"{landed}-pr7-review101"
    case.mkdir(parents=True)
    metadata = {
        "repository": "canonical/maas",
        "landed_commit": landed,
        "pr_number": 7,
        "review_id": 101,
        "head_sha": head,
        "base_sha": base,
        "original_model": BASELINE_MODEL,
        "description_source": "latest fetched PR body, not review-time body",
        "diff_source": "git diff merge-base reviewed-head",
    }
    case.joinpath("metadata.json").write_text(json.dumps(metadata))
    diff = git(source, "diff", base, head, "--src-prefix=a/", "--dst-prefix=b/")
    case.joinpath("diff.patch").write_text(diff + "\n")
    case.joinpath("description.md").write_text("Latest fetched PR body.\n")
    baseline = root / "reviews" / quote(BASELINE_MODEL, safe="") / f"{case.name}.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps(BASELINE_REVIEW))
    root.joinpath("manifest.json").write_text('{"commits": []}')
    reviewer = tmp_path / "fake reviewer"
    reviewer.write_text(f"#!{sys.executable}\n" + FAKE_REVIEWER)
    reviewer.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    control = tmp_path / "control.json"
    control.write_text("{}")
    monkeypatch.setenv("REPLAY_TEST_LOG", str(log))
    monkeypatch.setenv("REPLAY_TEST_CONTROL", str(control))
    monkeypatch.setenv("GITHUB_TOKEN", "never-forward-this")
    monkeypatch.setenv("GH_TOKEN", "never-forward-this-either")
    monkeypatch.setenv("LANGFUSE_AUTH_BASE64", "test-placeholder")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "test-placeholder")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic%20test-placeholder"
    )
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    return Corpus(root, case, bare, reviewer, log, control)


def assert_clean(corpus: Corpus) -> None:
    worktrees = git(
        corpus.path, "--git-dir", str(corpus.bare), "worktree", "list", "--porcelain"
    )
    assert worktrees.count("worktree ") == 1  # The bare repository only.
    assert not corpus.path.parent.joinpath("HOOK_EXECUTED").exists()
    for call in corpus.calls():
        assert not Path(call["repo_dir"]).exists()
        assert not Path(call["cwd"]).exists()
    assert not list(corpus.path.rglob(".replay-*"))


def test_replays_original_snapshot_and_description_without_baseline_answers(
    corpus: Corpus,
    tmp_path: Path,
) -> None:
    models = ["provider/model:preview", "other/model ?#%"]
    config = tmp_path / "runtime.toml"
    config.write_text("# passed to the real reviewer, not interpreted by replay\n")
    baseline = corpus.baseline().read_bytes()
    options = [
        "--runtime-config",
        str(config),
        "--review-timeout",
        "45",
        "--max-diff-chars",
        "1234",
    ]
    for model in models:
        options.extend(["--model", model])
    assert corpus.run(*options) == 0
    assert len(corpus.calls()) == 2
    for model in models:
        assert unquote(corpus.attempt_root(model).parent.parent.name) == model
    assert corpus.baseline().read_bytes() == baseline
    assert_clean(corpus)


def test_replay_logs_timings_for_each_review_phase(
    corpus: Corpus,
    capsys,
) -> None:
    model = "provider/timed"
    assert corpus.run("--model", model) == 0
    captured = capsys.readouterr()
    case_name = corpus.case.name
    assert f"Reviewing {case_name} [{model}]" in captured.out
    assert f"timing: load inputs for {case_name}" in captured.out
    assert f"timing: check previous attempts for {case_name} [{model}]" in captured.out
    assert "timing: git worktree add" in captured.out
    assert "timing: run reviewer" in captured.out
    assert "timing: validate review result" in captured.out
    assert "timing: git worktree remove" in captured.out
    assert "timing: publish review results" in captured.out
    assert f"timing: export traces for {case_name} [{model}]" in captured.out
    assert f"Reviewed {case_name} [{model}] in" in captured.out
    assert "Replay of 1 cases finished in" in captured.out
    assert "s with 0 failures" in captured.out
    assert "reviewer: timing: fake reviewer phase: 0.00s" in captured.err
    assert_clean(corpus)


def test_resume_overwrite_and_incomplete_metrics(corpus: Corpus) -> None:
    model = "provider/resume"
    assert corpus.run("--model", model) == 0
    result = corpus.output(model).read_bytes()
    assert corpus.run("--model", model) == 0
    assert len(corpus.calls()) == 1
    assert corpus.output(model).read_bytes() == result
    assert corpus.run("--model", model, "--overwrite") == 0
    assert len(corpus.calls()) == 2
    assert len(corpus.attempts(model)) == 2
    for attempt in corpus.attempts(model):
        (attempt / "metrics.json").unlink()
    assert corpus.run("--model", model) == 0
    assert len(corpus.calls()) == 3
    assert_clean(corpus)


def test_failure_continues_all_cases_and_models_then_resumes_only_failures(
    corpus: Corpus,
    capsys,
) -> None:
    second = corpus.add_case()
    bad, good = "provider/failing", "provider/good"
    corpus.set_mode(**{bad: "fail"})
    options = ["--model", bad, "--model", good]
    assert corpus.run(*options) == 1
    assert "deliberate reviewer failure" in capsys.readouterr().err
    assert len(corpus.calls()) == 4
    for case in (corpus.case, second):
        assert not corpus.output(bad, case).exists()
        assert not corpus.metrics(bad, case).exists()
        assert corpus.output(good, case).exists()
    assert_clean(corpus)
    corpus.set_mode()
    assert corpus.run(*options) == 0
    assert len(corpus.calls()) == 6
    for case in (corpus.case, second):
        assert corpus.output(bad, case).exists()
        assert corpus.output(good, case).exists()
    assert_clean(corpus)


@pytest.mark.parametrize(
    "mode",
    [
        "mismatch",
        "missing-metrics",
        "invalid-review",
        "extra-key",
        "malformed-json",
    ],
)
def test_rejects_failed_validation_without_final_artifacts(
    corpus: Corpus,
    mode: str,
    capsys,
) -> None:
    model = "provider/requested"
    corpus.set_mode(**{model: mode})
    assert corpus.run("--model", model) == 1
    errors = capsys.readouterr().err
    assert "Error:" in errors
    if mode == "mismatch":
        assert "provider/fallback" in errors
        assert model in errors
    assert not corpus.output(model).exists()
    assert not corpus.metrics(model).exists()
    assert_clean(corpus)


def test_failed_overwrite_preserves_success_and_resumes_it(corpus: Corpus) -> None:
    model = "provider/replaced"
    assert corpus.run("--model", model) == 0
    successful = corpus.output(model)
    result = successful.read_bytes()
    corpus.set_mode(**{model: "fail"})
    assert corpus.run("--model", model, "--overwrite") == 1
    assert len(corpus.attempts(model)) == 2
    assert successful.read_bytes() == result
    assert not corpus.output(model).exists()
    assert corpus.run("--model", model) == 0
    assert len(corpus.calls()) == 2
    assert successful.read_bytes() == result
    assert_clean(corpus)


def test_baseline_model_can_replay_without_modifying_baseline(corpus: Corpus) -> None:
    baseline = corpus.baseline().read_bytes()
    assert corpus.run("--model", BASELINE_MODEL, "--overwrite") == 0
    assert corpus.baseline().read_bytes() == baseline
    assert corpus.output(BASELINE_MODEL).exists()
    assert [call["model"] for call in corpus.calls()] == [BASELINE_MODEL]
    assert_clean(corpus)


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "../../outside"),
        ("repository", "../outside"),
        ("original_model", ".."),
        ("original_model", "provider/../outside"),
        ("case_id", "../../outside"),
    ],
)
def test_invalid_metadata_does_not_block_other_cases(
    corpus: Corpus,
    field: str,
    value: str,
    capsys,
) -> None:
    valid_case = corpus.add_case("valid")
    metadata_path = corpus.case / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata[field] = value
    metadata_path.write_text(json.dumps(metadata))
    model = "provider/safe"
    assert corpus.run("--model", model) == 1
    assert "Error:" in capsys.readouterr().err
    assert not corpus.output(model).exists()
    assert corpus.output(model, valid_case).exists()
    assert len(corpus.calls()) == 1
    assert_clean(corpus)


def test_symlink_case_cannot_escape_corpus(corpus: Corpus, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    corpus.case.rename(outside)
    corpus.case.symlink_to(outside, target_is_directory=True)
    assert corpus.run("--model", "provider/test") == 1
    assert corpus.calls() == []
    assert_clean(corpus)


def test_output_symlink_cannot_escape_corpus(corpus: Corpus, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    model = "provider/test"
    target = corpus.attempt_root(model).parent.parent
    target.parent.mkdir(parents=True)
    target.symlink_to(outside, target_is_directory=True)
    assert corpus.run("--model", model) == 1
    assert not list(outside.iterdir())
    assert corpus.calls() == []
    assert_clean(corpus)


@pytest.mark.parametrize("model", ["", " ", ".", "..", "provider/../outside"])
def test_rejects_empty_and_dot_models(corpus: Corpus, model: str) -> None:
    with pytest.raises(SystemExit) as error:
        corpus.run("--model", model)
    assert error.value.code == 2
    assert corpus.calls() == []


@pytest.mark.parametrize(
    "option,value",
    [
        ("--review-timeout", "0"),
        ("--review-timeout", "nan"),
        ("--review-timeout", "inf"),
        ("--parallel", "0"),
    ],
)
def test_rejects_invalid_numeric_options(
    corpus: Corpus,
    option: str,
    value: str,
) -> None:
    with pytest.raises(SystemExit) as error:
        corpus.run("--model", "provider/test", option, value)
    assert error.value.code == 2
    assert corpus.calls() == []


def test_unknown_commit_fails_without_leaking_worktree(corpus: Corpus) -> None:
    metadata_path = corpus.case / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["head_sha"] = "0" * 40
    metadata_path.write_text(json.dumps(metadata))
    assert corpus.run("--model", "provider/missing") == 1
    assert corpus.calls() == []
    assert not corpus.output("provider/missing").exists()
    assert not corpus.metrics("provider/missing").exists()
    assert_clean(corpus)


def test_interrupt_stops_reviewer_and_removes_worktree(corpus: Corpus) -> None:
    model = "provider/interrupted"
    corpus.set_mode(**{model: "wait"})
    command = [
        sys.executable,
        str(Path(replay_reviews.__file__)),
        str(corpus.path),
        "--run-id",
        RUN_ID,
        "--reviewer",
        str(corpus.reviewer),
        "--model",
        model,
    ]
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None and (
                not corpus.log.exists() or not corpus.log.stat().st_size
            ):
                if time.monotonic() >= deadline:
                    pytest.fail("Fake reviewer did not start")
                time.sleep(0.02)
            assert process.poll() is None
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 130
            assert "Interrupted" in stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert not corpus.output(model).exists()
    assert not corpus.metrics(model).exists()
    attempt = corpus.output(model).parent
    assert attempt_state(attempt)["review_status"] == "interrupted"
    assert attempt_state(attempt)["export_status"] == "disabled"
    assert len(trace_spans(attempt / "otel" / "traces.jsonl")) == 2
    assert len(trace_spans(attempt / "otel" / "langfuse-traces.jsonl")) == 2
    assert_clean(corpus)
    for call in corpus.calls():
        with pytest.raises(ProcessLookupError):
            os.kill(call["pid"], 0)


def test_parallel_attempts_run_concurrently_and_stop_on_interrupt(
    corpus: Corpus,
) -> None:
    models = ["provider/first-wait", "provider/second-wait"]
    corpus.set_mode(**{model: "wait" for model in models})
    command = [
        sys.executable,
        str(Path(replay_reviews.__file__)),
        str(corpus.path),
        "--run-id",
        RUN_ID,
        "--reviewer",
        str(corpus.reviewer),
        "--parallel",
        "2",
    ]
    for model in models:
        command.extend(["--model", model])
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            # Both waiting reviewers must be started at once: a sequential
            # replay cannot reach a second log entry while the first sleeps.
            deadline = time.monotonic() + 10
            while process.poll() is None and len(corpus.calls()) < 2:
                if time.monotonic() >= deadline:
                    pytest.fail("Two reviewers did not run concurrently")
                time.sleep(0.02)
            assert process.poll() is None
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=30)
            assert process.returncode == 130
            assert "Interrupted" in stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert len(corpus.calls()) == 2
    for model in models:
        attempt = corpus.attempts(model)[-1]
        assert attempt_state(attempt)["review_status"] == "interrupted"
        assert attempt_state(attempt)["export_status"] == "disabled"
        assert not corpus.output(model).exists()
        assert not corpus.metrics(model).exists()
    assert_clean(corpus)
    for call in corpus.calls():
        with pytest.raises(ProcessLookupError):
            os.kill(call["pid"], 0)


def attempt_state(attempt: Path) -> dict:
    return json.loads((attempt / "attempt.json").read_text())


def trace_spans(path: Path) -> list[dict]:
    return [
        span
        for line in path.read_text().splitlines()
        for resource in json.loads(line)["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


@pytest.fixture
def collector(corpus: Corpus, monkeypatch):
    requests = []
    status = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (
                    self.path,
                    self.headers,
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(status[0])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        f"http://127.0.0.1:{server.server_port}/api/public/otel/v1/traces",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "Authorization=Basic%20test-placeholder,ingestion-version=4",
    )
    try:
        yield requests, status
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_runs_are_isolated_and_encoded_reversibly(corpus: Corpus) -> None:
    model = "provider/model"
    assert corpus.run("--model", model) == 0
    original = corpus.output(model)
    original_bytes = original.read_bytes()
    other_run = "comparison B/?#%"
    assert corpus.run("--model", model, "--run-id", other_run) == 0
    other = corpus.output(model, run_id=other_run)
    assert other.exists()
    assert other != original
    assert unquote(other.parents[5].name) == other_run
    assert corpus.run("--model", model) == 0
    assert len(corpus.calls()) == 2
    assert original.read_bytes() == original_bytes
    assert len(corpus.attempts(model)) == 1
    assert len(corpus.attempts(model, run_id=other_run)) == 1
    assert_clean(corpus)


def test_disabled_export_still_annotates_every_span_without_changing_raw(
    corpus: Corpus,
) -> None:
    model = "provider/annotated"
    assert corpus.run("--model", model) == 0
    attempt = corpus.output(model).parent
    state = attempt_state(attempt)
    metadata = json.loads((corpus.case / "metadata.json").read_text())
    expected = {
        "run_id": RUN_ID,
        "case_id": corpus.case.name,
        "reviewed_commit": metadata["head_sha"],
        "landed_commit": metadata["landed_commit"],
        "repository": "canonical/maas",
        "model": model,
        "attempt_id": attempt.name,
    }
    assert state == {
        **expected,
        "review_status": "succeeded",
        "export_status": "disabled",
    }
    assert len(attempt.name) == 32
    int(attempt.name, 16)
    raw_path = attempt / "otel" / "traces.jsonl"
    annotated_path = attempt / "otel" / "langfuse-traces.jsonl"
    raw = trace_spans(raw_path)
    annotated = trace_spans(annotated_path)
    assert len(raw) == len(annotated) == 2
    for before, after in zip(raw, annotated, strict=True):
        attributes = {item["key"]: item["value"] for item in after["attributes"]}
        for key, value in expected.items():
            assert attributes[f"langfuse.trace.metadata.{key}"] == {
                "stringValue": value
            }
        assert "session.id" not in attributes
        assert attributes["langfuse.session.id"] == {"stringValue": RUN_ID}
        assert attributes["langfuse.trace.tags"] == {
            "arrayValue": {"values": [{"stringValue": "model-review-comparison"}]}
        }
        assert not any(
            item["key"].startswith("langfuse.") for item in before.get("attributes", [])
        )
        for key in (
            "traceId",
            "spanId",
            "parentSpanId",
            "startTimeUnixNano",
            "endTimeUnixNano",
        ):
            assert before.get(key) == after.get(key)
    assert (attempt / "otel" / "metrics.jsonl").is_file()
    assert "Authorization" not in json.dumps(state)
    assert_clean(corpus)


def test_upload_failure_resumes_export_only_with_identical_trace_ids(
    corpus: Corpus, collector, capsys
) -> None:
    requests, status = collector
    status[0] = 503
    model = "provider/upload-retry"
    assert corpus.run("--model", model) == 1
    attempt = corpus.output(model).parent
    review = corpus.output(model).read_bytes()
    raw = (attempt / "otel" / "traces.jsonl").read_bytes()
    assert attempt_state(attempt)["review_status"] == "succeeded"
    assert attempt_state(attempt)["export_status"] == "failed"
    status[0] = 200
    assert corpus.run("--model", model) == 0
    assert len(corpus.calls()) == 1
    assert corpus.attempts(model) == [attempt]
    assert corpus.output(model).read_bytes() == review
    assert (attempt / "otel" / "traces.jsonl").read_bytes() == raw
    assert attempt_state(attempt)["export_status"] == "succeeded"
    assert len(requests) == 2
    assert requests[0][2] == requests[1][2]
    path, headers, body = requests[-1]
    assert path == "/api/public/otel/v1/traces"
    assert headers["Content-Type"] == "application/x-protobuf"
    assert headers["Authorization"] == "Basic test-placeholder"
    assert headers["ingestion-version"] == "4"
    decoded = ExportTraceServiceRequest.FromString(body)
    spans = decoded.resource_spans[0].scope_spans[0].spans
    assert len(spans) == 2
    assert spans[0].trace_id.hex() == "0123456789abcdef0123456789abcdef"
    assert spans[1].parent_span_id == spans[0].span_id
    assert "test-placeholder" not in capsys.readouterr().err
    assert "test-placeholder" not in (attempt / "attempt.json").read_text()
    assert corpus.run("--model", model) == 0
    assert len(requests) == 2
    assert len(corpus.calls()) == 1
    assert_clean(corpus)


def test_enabling_export_reuses_previously_disabled_success(
    corpus: Corpus, monkeypatch
) -> None:
    model = "provider/enable-export"
    assert corpus.run("--model", model) == 0
    attempt = corpus.output(model).parent
    # Enabling a standard endpoint must not trigger another paid review.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    assert corpus.run("--model", model) == 1
    assert len(corpus.calls()) == 1
    assert corpus.attempts(model) == [attempt]
    assert attempt_state(attempt)["export_status"] == "failed"
    assert_clean(corpus)


def test_failed_review_exports_received_telemetry_without_publishing_results(
    corpus: Corpus, collector, capsys
) -> None:
    requests, _ = collector
    model = "provider/failed-review"
    corpus.set_mode(**{model: "fail"})
    assert corpus.run("--model", model) == 1
    attempt = corpus.output(model).parent
    assert attempt_state(attempt)["review_status"] == "failed"
    assert attempt_state(attempt)["export_status"] == "succeeded"
    assert not corpus.output(model).exists()
    assert not corpus.metrics(model).exists()
    assert len(trace_spans(attempt / "otel" / "traces.jsonl")) == 2
    assert len(trace_spans(attempt / "otel" / "langfuse-traces.jsonl")) == 2
    assert (attempt / "otel" / "metrics.jsonl").read_text()
    assert len(requests) == 1
    assert "deliberate reviewer failure" in capsys.readouterr().err
    assert_clean(corpus)


def test_failed_upload_does_not_replace_primary_review_failure(
    corpus: Corpus, collector, capsys
) -> None:
    _, status = collector
    status[0] = 503
    model = "provider/double-failure"
    corpus.set_mode(**{model: "fail"})
    assert corpus.run("--model", model) == 1
    attempt = corpus.output(model).parent
    assert attempt_state(attempt)["review_status"] == "failed"
    assert attempt_state(attempt)["export_status"] == "failed"
    assert "deliberate reviewer failure" in capsys.readouterr().err
    assert_clean(corpus)


def test_configured_export_with_no_spans_is_explicit_failure_without_upload(
    corpus: Corpus, collector
) -> None:
    requests, _ = collector
    model = "provider/no-spans"
    corpus.set_mode(**{model: "no-spans"})
    assert corpus.run("--model", model) == 1
    attempt = corpus.output(model).parent
    assert attempt_state(attempt)["review_status"] == "succeeded"
    assert attempt_state(attempt)["export_status"] == "no_spans"
    assert (attempt / "otel" / "langfuse-traces.jsonl").read_text() == ""
    assert requests == []
    assert corpus.run("--model", model) == 1
    assert len(corpus.calls()) == 1
    assert requests == []
    assert_clean(corpus)


def test_exporter_configuration_fails_before_inference(
    corpus: Corpus, monkeypatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    assert corpus.run("--model", "provider/preflight") == 1
    assert corpus.calls() == []
    assert not (corpus.path / "runs").exists()


@pytest.mark.parametrize("run_id", ["", " ", ".", "..", "\x00"])
def test_rejects_empty_and_dot_run_ids(corpus: Corpus, run_id: str) -> None:
    with pytest.raises(SystemExit) as error:
        corpus.run("--model", "provider/model", "--run-id", run_id)
    assert error.value.code == 2
    assert corpus.calls() == []


def test_requires_run_id(corpus: Corpus) -> None:
    with pytest.raises(SystemExit) as error:
        replay_reviews.main([str(corpus.path), "--model", "provider/model"])
    assert error.value.code == 2
    assert corpus.calls() == []

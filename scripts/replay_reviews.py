#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Replay collected historical reviews against explicitly selected reviewer models."""

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from maas_code_reviewer.otel_export import TraceExporter, annotate_traces
from maas_code_reviewer.review_schema import validate_review_json


class ReplayError(ValueError):
    """A corpus entry or reviewer result cannot safely be replayed."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--model", action="append", required=True, type=_model)
    parser.add_argument("--run-id", required=True, type=_run_id)
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--review-timeout", type=_positive_float)
    parser.add_argument("--max-diff-chars", type=_positive_int)
    parser.add_argument("--reviewer", default="maas-code-reviewer")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        return _replay(args)
    except KeyboardInterrupt:
        print("Interrupted; temporary review checkout removed.", file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _replay(args: argparse.Namespace) -> int:
    exporter = TraceExporter.from_environment()
    corpus = args.corpus.resolve(strict=True)
    bare = _inside(corpus / "repository.git", corpus)
    if not bare.is_dir():
        raise ReplayError(f"Missing bare repository: {bare}")
    if _git(bare, "rev-parse", "--is-bare-repository").strip() != "true":
        raise ReplayError(f"Not a bare repository: {bare}")
    cases_root = _inside(corpus / "cases", corpus)
    cases = sorted(cases_root.glob("*/metadata.json"))
    if not cases:
        raise ReplayError(f"No cases/*/metadata.json in {corpus}")
    if args.runtime_config is not None:
        args.runtime_config = args.runtime_config.resolve(strict=True)
    # A path containing a slash is an executable path, never a shell command.
    if "/" in args.reviewer:
        args.reviewer = str(Path(args.reviewer).resolve(strict=True))
    failures = 0
    for metadata_path in cases:
        try:
            case = _inside(metadata_path.parent, cases_root)
            metadata = _metadata(_inside(metadata_path, case), case.name)
            diff = _inside(case / "diff.patch", case)
            description = _inside(case / "description.md", case)
            if not diff.is_file() or not description.is_file():
                raise ReplayError("Case needs diff.patch and description.md")
            diff_text = diff.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            print(f"Error: {metadata_path}: {error}", file=sys.stderr)
            failures += 1
            continue
        for model in dict.fromkeys(args.model):
            try:
                attempts = _inside(
                    corpus
                    / "runs"
                    / quote(args.run_id, safe="")
                    / "reviews"
                    / quote(model, safe="")
                    / case.name
                    / "attempts",
                    corpus,
                )
                attempts.mkdir(parents=True, exist_ok=True)
                labels = {
                    "run_id": args.run_id,
                    "case_id": case.name,
                    "reviewed_commit": metadata["head_sha"],
                    "landed_commit": metadata["landed_commit"],
                    "repository": metadata["repository"],
                    "model": model,
                }
                existing = (
                    None
                    if args.overwrite
                    else _successful_attempt(attempts, labels, diff_text)
                )
                if existing is not None:
                    attempt, state = existing
                    if exporter is not None and state["export_status"] == "succeeded":
                        print(
                            f"Skipping {case.name} [{model}]: successful result exists"
                        )
                        continue
                    if not _export_attempt(attempt, state, exporter):
                        failures += 1
                    print(f"Resumed {case.name} [{model}] without inference")
                    continue
                attempt_id = uuid4().hex
                attempt = _inside(attempts / attempt_id, attempts)
                attempt.mkdir()
                otel = attempt / "otel"
                otel.mkdir()
                for name in ("traces.jsonl", "metrics.jsonl"):
                    (otel / name).touch()
                state = {
                    **labels,
                    "attempt_id": attempt_id,
                    "review_status": "running",
                    "export_status": "pending" if exporter is not None else "disabled",
                }
                _write_json(attempt / "attempt.json", state)
                try:
                    _review(
                        args,
                        bare,
                        metadata["head_sha"],
                        diff,
                        description,
                        model,
                        diff_text,
                        attempt / "review.json",
                        attempt / "metrics.json",
                        otel,
                    )
                    state["review_status"] = "succeeded"
                    _write_json(attempt / "attempt.json", state)
                    exported = _export_attempt(attempt, state, exporter)
                except (
                    OSError,
                    ValueError,
                    subprocess.SubprocessError,
                    KeyboardInterrupt,
                ) as error:
                    review_failed = state["review_status"] == "running"
                    if review_failed:
                        state["review_status"] = (
                            "interrupted"
                            if isinstance(error, KeyboardInterrupt)
                            else "failed"
                        )
                    elif state["export_status"] == "pending":
                        state["export_status"] = "failed"
                    # Finalization must not replace the primary failure. A review
                    # that finished remains resumable if export was interrupted.
                    try:
                        if review_failed:
                            _export_attempt(attempt, state, exporter)
                        else:
                            _write_json(attempt / "attempt.json", state)
                    except (OSError, ValueError):
                        print(
                            "Error: could not finalize review attempt",
                            file=sys.stderr,
                        )
                    raise
                if not exported:
                    failures += 1
                print(f"Reviewed {case.name} [{model}]")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"Error: {case.name} [{model}]: {error}", file=sys.stderr)
                failures += 1
    return 1 if failures else 0


def _successful_attempt(
    attempts: Path, labels: dict[str, str], diff_text: str
) -> tuple[Path, dict] | None:
    candidates = [_inside(path, attempts) for path in attempts.glob("*/attempt.json")]
    for path in sorted(
        candidates, key=lambda item: item.stat().st_mtime_ns, reverse=True
    ):
        attempt = path.parent
        try:
            state = _read_json(path)
            if (
                not isinstance(state, dict)
                or any(state.get(key) != value for key, value in labels.items())
                or state.get("attempt_id") != attempt.name
                or state.get("review_status") != "succeeded"
                or state.get("export_status")
                not in ("disabled", "pending", "succeeded", "failed", "no_spans")
            ):
                continue
            output = _inside(attempt / "review.json", attempt)
            metrics = _inside(attempt / "metrics.json", attempt)
            if _successful(output, metrics, labels["model"], diff_text):
                return attempt, state
        except (OSError, ValueError):
            continue
    return None


def _export_attempt(attempt: Path, state: dict, exporter: TraceExporter | None) -> bool:
    try:
        otel = _inside(attempt / "otel", attempt)
        source = _inside(otel / "traces.jsonl", attempt)
        destination = _inside(otel / "langfuse-traces.jsonl", attempt)
        labels = {
            key: state[key]
            for key in (
                "run_id",
                "case_id",
                "reviewed_commit",
                "landed_commit",
                "repository",
                "model",
                "attempt_id",
            )
        }
        spans = annotate_traces(source, destination, labels)
        if exporter is None:
            state["export_status"] = "disabled"
        elif not spans:
            state["export_status"] = "no_spans"
            print("Error: trace export received no spans", file=sys.stderr)
        else:
            state["export_status"] = "pending"
            _write_json(attempt / "attempt.json", state)
            exported = exporter.export_file(destination)
            if exported != spans:
                raise ReplayError("Trace export did not export all recorded spans")
            state["export_status"] = "succeeded"
    except (OSError, ValueError) as error:
        state["export_status"] = "failed"
        print(f"Error: trace export: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        state["export_status"] = "failed"
        raise
    finally:
        _write_json(attempt / "attempt.json", state)
    return state["export_status"] in {"disabled", "succeeded"}


def _review(
    args: argparse.Namespace,
    bare: Path,
    head_sha: str,
    diff: Path,
    description: Path,
    model: str,
    diff_text: str,
    output: Path,
    metrics: Path,
    otel: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="replay-review-") as temporary:
        root = Path(temporary)
        staged_output = root / "review.json"
        staged_metrics = root / "metrics.json"
        with _checkout(bare, root / "repository", head_sha) as checkout:
            command = [
                args.reviewer,
                "review-diff",
                "--repo-dir",
                str(checkout),
                "--description-file",
                str(description),
                "--diff-prefix",
                "b/",
                "--json-output",
                str(staged_output),
                "--metrics",
                str(staged_metrics),
                "--otel-output",
                str(otel),
                "--model",
                model,
            ]
            for option, value in (
                ("--runtime-config", args.runtime_config),
                ("--review-timeout", args.review_timeout),
                ("--max-diff-chars", args.max_diff_chars),
            ):
                if value is not None:
                    command.extend([option, str(value)])
            command.append(str(diff))
            _run_reviewer(command, root)
            review, reported_metrics = _result(
                staged_output, staged_metrics, model, diff_text
            )
        # Publish only after checkout teardown succeeds. Each file is replaced
        # atomically on its destination filesystem; the review is the commit marker.
        try:
            _write_json(metrics, reported_metrics)
            _write_json(output, review)
        except (OSError, ValueError, KeyboardInterrupt):
            output.unlink(missing_ok=True)
            metrics.unlink(missing_ok=True)
            raise


def _run_reviewer(command: list[str], cwd: Path) -> None:
    environment = _environment()
    environment.pop("GITHUB_TOKEN", None)
    environment.pop("GH_TOKEN", None)
    for key in list(environment):
        if key.startswith(("OTEL_", "LANGFUSE_")):
            del environment[key]
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate()
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
        if process.returncode:
            detail = stderr.strip() or stdout.strip() or "no diagnostic output"
            raise ReplayError(f"Reviewer exited {process.returncode}: {detail}")


@contextmanager
def _checkout(bare: Path, checkout: Path, head_sha: str) -> Iterator[Path]:
    added = False
    try:
        _git(bare, "worktree", "add", "--detach", str(checkout), head_sha)
        added = True
        yield checkout
    finally:
        # Also run after a failed add: Git can have registered a partial worktree.
        cleanup = _git_process(
            bare, "worktree", "remove", "--force", "--force", str(checkout)
        )
        if cleanup.returncode and (added or checkout.exists()):
            raise ReplayError(f"Worktree cleanup failed: {cleanup.stderr.strip()}")


def _successful(output: Path, metrics: Path, model: str, diff_text: str) -> bool:
    try:
        _result(output, metrics, model, diff_text)
    except (OSError, ValueError):
        return False
    return True


def _result(
    output: Path, metrics: Path, model: str, diff_text: str
) -> tuple[dict, dict]:
    review = _read_json(output)
    if not isinstance(review, dict) or set(review) != {
        "general_comment",
        "inline_comments",
    }:
        raise ReplayError(
            "Review must contain exactly general_comment and inline_comments"
        )
    errors = validate_review_json(review, diff_text, path_prefix="b/")
    if errors:
        raise ReplayError("Invalid review JSON: " + "; ".join(errors))
    reported_metrics = _read_json(metrics)
    if not isinstance(reported_metrics, dict):
        raise ReplayError("Metrics must be a JSON object")
    resolved = reported_metrics.get("model_name")
    if resolved != model:
        raise ReplayError(f"Requested model {model!r}, but metrics report {resolved!r}")
    return review, reported_metrics


def _metadata(path: Path, case_id: str) -> dict:
    metadata = _read_json(path)
    if not isinstance(metadata, dict):
        raise ReplayError("Metadata must be a JSON object")
    for key in ("landed_commit", "head_sha", "base_sha"):
        value = metadata.get(key)
        if (
            not isinstance(value, str)
            or len(value) not in (40, 64)
            or any(c not in "0123456789abcdefABCDEF" for c in value)
        ):
            raise ReplayError(f"Metadata {key} must be a full hexadecimal commit SHA")
    for key in ("pr_number", "review_id"):
        value = metadata.get(key)
        if type(value) is not int or value <= 0:
            raise ReplayError(f"Metadata {key} must be a positive integer")
    repository = metadata.get("repository")
    if (
        not isinstance(repository, str)
        or len(repository.split("/")) != 2
        or any(not _safe_component(part) for part in repository.split("/"))
    ):
        raise ReplayError("Metadata repository must be owner/name without traversal")
    original_model = metadata.get("original_model")
    try:
        _model(original_model)
    except argparse.ArgumentTypeError as error:
        raise ReplayError(f"Metadata original_model: {error}") from error
    for key in ("description_source", "diff_source"):
        provenance = metadata.get(key)
        if not isinstance(provenance, str) or not provenance.strip():
            raise ReplayError(f"Metadata {key} must be a nonempty provenance string")
    if not _safe_component(case_id):
        raise ReplayError("Unsafe case directory name")
    if metadata.get("case_id", case_id) != case_id:
        raise ReplayError("Metadata case_id does not match its directory")
    return metadata


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not path.is_relative_to(root):
        raise ReplayError(f"Path escapes its corpus location: {path}")
    current = path
    while current != root:
        if current.is_symlink():
            raise ReplayError(f"Refusing symlink corpus path: {current}")
        current = current.parent
    return resolved


def _read_json(path: Path) -> object:
    if path.is_symlink():
        raise ReplayError(f"Refusing symlink JSON file: {path}")
    return json.loads(
        path.read_text(encoding="utf-8"), parse_constant=_invalid_constant
    )


def _invalid_constant(value: str) -> object:
    raise ReplayError(f"Non-JSON numeric constant: {value}")


def _write_json(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".replay-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _environment() -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git_process(bare: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "submodule.recurse=false",
            "--git-dir",
            str(bare),
            *arguments,
        ],
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )


def _git(bare: Path, *arguments: str) -> str:
    result = _git_process(bare, *arguments)
    if result.returncode:
        raise ReplayError(f"Git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout


def _safe_component(value: str) -> bool:
    return (
        bool(value)
        and value not in (".", "..")
        and not any(character in value for character in ("/", "\\", "\x00", "\n", "\r"))
    )


def _model(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise argparse.ArgumentTypeError("Model must be a nonempty, non-dot identifier")
    return value


def _run_id(value: str) -> str:
    if not value.strip() or value in (".", "..") or "\x00" in value:
        raise argparse.ArgumentTypeError(
            "Run ID must be a nonempty, non-dot identifier"
        )
    return value


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected a positive finite number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive finite number")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return number


if __name__ == "__main__":
    raise SystemExit(main())

# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from scripts import compare_reviews

CORPUS_NAME = "maas-test-corpus"
RUN_ID = "run-one"
BASELINE_MODEL = "gemini-3.1-pro-preview"
MODELS = ["provider/alpha-model", "provider/beta-model"]

MANIFEST = {
    "schema_version": 1,
    "commits": [],
    "selection": {
        "count": 2,
        "ref": "master",
        "repository": "canonical/maas",
        "original_model": BASELINE_MODEL,
        "resolved_head": "c" * 40,
    },
}

CASE_METADATA = {
    "pr_number": 811,
    "review_id": 5630417338,
    "review_url": "https://github.com/canonical/maas/pull/811"
    "#pullrequestreview-5630417338",
    "description_url": "https://github.com/canonical/maas/pull/811",
    "review_submitted_at": "2026-10-23T15:18:13Z",
    "landed_commit": "l" * 40,
    "head_sha": "h" * 40,
    "base_sha": "b" * 40,
}


def spans(root_start: int, root_end: int, *, model_calls: int = 0) -> list[dict]:
    root = {
        "traceId": "0123456789abcdef0123456789abcdef",
        "spanId": "0123456789abcdef",
        "name": compare_reviews.ROOT_SPAN,
        "startTimeUnixNano": str(root_start),
        "endTimeUnixNano": str(root_end),
    }
    chats = [
        {
            "traceId": "0123456789abcdef0123456789abcdef",
            "spanId": f"0123456789abcd{index:02x}",
            "name": f"chat model-{index}",
            "startTimeUnixNano": str(root_start + index),
            "endTimeUnixNano": str(root_end - index),
        }
        for index in range(model_calls)
    ]
    return [root, *chats]


def write_attempt(
    attempts_root: Path,
    model: str,
    case_id: str,
    attempt_id: str,
    *,
    review_status: str = "succeeded",
    cost: float | None = 0.5,
    tokens_total: int = 100,
    root_start: int = 100_000_000_000,
    root_end: int | None = 260_000_000_000,
    with_traces: bool = True,
    with_review: bool = True,
    model_calls: int = 0,
    mtime_ns: int = 1,
) -> Path:
    attempt_dir = (
        attempts_root / quote(model, safe="") / case_id / "attempts" / attempt_id
    )
    attempt_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "run_id": RUN_ID,
        "case_id": case_id,
        "model": model,
        "attempt_id": attempt_id,
        "review_status": review_status,
        "export_status": "succeeded",
    }
    (attempt_dir / "attempt.json").write_text(json.dumps(state))
    os.utime(attempt_dir / "attempt.json", ns=(mtime_ns, mtime_ns))
    metrics = {
        "model_name": model,
        "agent_name": "omp",
        "agent_version": "18.12.2",
        "tokens_input": 10,
        "tokens_output": 20,
        "tokens_total": tokens_total,
        "cost": cost,
        "cost_currency": "USD",
    }
    (attempt_dir / "metrics.json").write_text(json.dumps(metrics))
    if with_review:
        review = {
            "general_comment": f"Review of {case_id} by {model}.",
            "inline_comments": {
                "src/code.py": {"12": "Check <this> & that.", "3": "First."}
            },
        }
        (attempt_dir / "review.json").write_text(json.dumps(review))
    if with_traces:
        otel = attempt_dir / "otel"
        otel.mkdir()
        batch = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": spans(
                                root_start, root_end or 0, model_calls=model_calls
                            )
                        }
                    ]
                }
            ]
        }
        (otel / "traces.jsonl").write_text(json.dumps(batch) + "\n")
    return attempt_dir


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / CORPUS_NAME
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(MANIFEST))
    case_ids = ["a" * 40 + "-pr811-review1", "2" * 40 + "-pr801-review2"]
    for case_id, pr_number in zip(case_ids, (811, 801), strict=True):
        case_dir = root / "cases" / case_id
        case_dir.mkdir(parents=True)
        metadata = dict(CASE_METADATA, pr_number=pr_number)
        (case_dir / "metadata.json").write_text(json.dumps(metadata))
        (case_dir / "diff.patch").write_text("diff --git a/x b/x\n+new\n")
    baseline_dir = root / "reviews" / BASELINE_MODEL
    baseline_dir.mkdir(parents=True)
    for case_id in case_ids:
        review = {
            "general_comment": "Original review with <script> markup & text.",
            "inline_comments": {"docs/guide.md": {"7": "Use straight quotes."}},
        }
        (baseline_dir / f"{case_id}.json").write_text(json.dumps(review))
    runs_root = root / "runs" / RUN_ID / "reviews"
    for model in MODELS:
        for case_id in case_ids:
            write_attempt(
                runs_root,
                model,
                case_id,
                "attempt-1",
                tokens_total=100,
                cost=0.5,
                root_start=100_000_000_000,
                root_end=260_000_000_000,
                model_calls=2,
            )
    return root


def test_report_compares_models_cases_and_baseline(corpus: Path) -> None:
    exit_code = compare_reviews.main([str(corpus)])

    assert exit_code == 0
    report = (corpus / "report.html").read_text()
    assert "provider/alpha-model" in report
    assert "provider/beta-model" in report
    assert BASELINE_MODEL in report
    assert "canonical/maas" in report
    # Two cases per model, all successful.
    assert "2/2" in report
    # 160 s wall time from the invoke_agent span, summed per model.
    assert "5m 20s" in report
    assert "2m 40s" in report
    # Token and cost sums across both cases.
    assert "200" in report
    assert "$1.0000" in report
    # Per-attempt values, links, and counts.
    assert "$0.5000" in report
    assert "2 model calls" in report
    assert 'href="https://github.com/canonical/maas/pull/811"' in report
    assert "PR #811" in report
    assert "PR #801" in report
    # Baseline row is marked by its collected status.
    assert ">collected</td>" in report
    # Inline comment counts (replayed: 2, baseline: 1) and sorted line order.
    assert ">2</td>" in report
    assert ">1</td>" in report
    assert "<b>line 3</b>" in report
    assert "Check &lt;this&gt; &amp; that." in report
    # Original review text is escaped, not interpreted as HTML.
    assert "&lt;script&gt;" in report


def test_report_prefers_latest_successful_attempt(corpus: Path) -> None:
    runs_root = corpus / "runs" / RUN_ID / "reviews"
    case_id = sorted(
        (path.parent.name for path in (corpus / "cases").glob("*/metadata.json")),
        reverse=True,
    )[0]
    write_attempt(
        runs_root,
        "provider/alpha-model",
        case_id,
        "old-failed",
        review_status="failed",
        cost=9.0,
        tokens_total=999,
        mtime_ns=1,
    )
    write_attempt(
        runs_root,
        "provider/alpha-model",
        case_id,
        "new-success",
        review_status="succeeded",
        cost=0.25,
        tokens_total=42,
        mtime_ns=5,
    )
    write_attempt(
        runs_root,
        "provider/alpha-model",
        case_id,
        "newest-failed",
        review_status="failed",
        cost=8.0,
        tokens_total=888,
        mtime_ns=9,
    )

    exit_code = compare_reviews.main(
        [str(corpus), "--output", str(corpus / "out.html")]
    )

    assert exit_code == 0
    report = (corpus / "out.html").read_text()
    assert "$0.2500" in report
    assert "$9.0000" not in report
    assert "$8.0000" not in report
    assert "succeeded (4 attempts)" in report
    assert (
        "Other attempts: attempt-1 (succeeded), "
        "newest-failed (failed), old-failed (failed)"
    ) in report


def test_report_marks_missing_traces_and_review(corpus: Path) -> None:
    runs_root = corpus / "runs" / RUN_ID / "reviews"
    case_id = sorted(
        (path.parent.name for path in (corpus / "cases").glob("*/metadata.json")),
    )[0]
    write_attempt(
        runs_root,
        "provider/gamma-model",
        case_id,
        "attempt-partial",
        with_traces=False,
        with_review=False,
    )

    exit_code = compare_reviews.main([str(corpus)])

    assert exit_code == 0
    report = (corpus / "report.html").read_text()
    assert "provider/gamma-model" in report
    assert "No review body was retained." in report


def test_report_renders_issue_classification(corpus: Path) -> None:
    case_ids = sorted(
        path.parent.name for path in (corpus / "cases").glob("*/metadata.json")
    )
    analysis_dir = corpus / "analysis"
    analysis_dir.mkdir()
    (analysis_dir / f"{case_ids[0]}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": case_ids[0],
                "classifier_model": "openrouter/moonshotai/kimi-k3",
                "reviewers": [
                    {"key": "alpha", "label": "Alpha (original)"},
                    {"key": "beta", "label": "Beta"},
                ],
                "issues": [
                    {
                        "summary": "Race <in> cache",
                        "detail": "Concurrent & access.",
                        "kind": "correctness",
                        "severity": "high",
                        "reviewers": ["alpha", "beta"],
                    },
                    {
                        "summary": "Missing test",
                        "kind": "testing",
                        "severity": "low",
                        "reviewers": ["beta"],
                    },
                ],
            }
        )
    )

    exit_code = compare_reviews.main([str(corpus)])

    assert exit_code == 0
    report = (corpus / "report.html").read_text()
    assert "Issues · classified by openrouter/moonshotai/kimi-k3" in report
    assert "<th>Alpha (original)</th>" in report
    assert "<th>Beta</th>" in report
    assert "Race &lt;in&gt; cache" in report
    assert "Concurrent &amp; access." in report
    assert report.count('<td class="center">✓</td>') == 3
    assert report.count('<td class="muted center">—</td>') == 1
    # The case without an analysis file renders no issues table.
    assert report.count("<h4>Issues") == 1


def test_report_rejects_unknown_issue_reviewer(corpus: Path, capsys) -> None:
    case_ids = sorted(
        path.parent.name for path in (corpus / "cases").glob("*/metadata.json")
    )
    analysis_dir = corpus / "analysis"
    analysis_dir.mkdir()
    (analysis_dir / f"{case_ids[0]}.json").write_text(
        json.dumps(
            {
                "reviewers": [{"key": "alpha", "label": "Alpha"}],
                "issues": [{"summary": "S", "reviewers": ["ghost"]}],
            }
        )
    )

    exit_code = compare_reviews.main([str(corpus)])

    assert exit_code == 1
    assert "unknown reviewer keys: ghost" in capsys.readouterr().err


def test_missing_manifest_exits_nonzero(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty-corpus"
    empty.mkdir()

    exit_code = compare_reviews.main([str(empty)])

    assert exit_code == 1
    assert "manifest.json" in capsys.readouterr().err


def test_missing_corpus_exits_nonzero(tmp_path: Path) -> None:
    exit_code = compare_reviews.main([str(tmp_path / "missing")])
    assert exit_code == 1

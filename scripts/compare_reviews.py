#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Compare collected and replayed reviews across a review corpus.

The deterministic comparison: wall time, reported token usage, harness cost
estimate, attempt status, and review comment counts for every case and model
in every run, next to the original baseline reviews collected with each case.
Semantic comparison of individual review points is deliberately out of scope;
the report embeds the full review text so a later pass can classify it.

Usage:

    uv run python scripts/compare_reviews.py "$CORPUS" [--output PATH]

Writes a single self-contained HTML file, by default "$CORPUS/report.html".
Wall time comes from the "invoke_agent" root span in otel/traces.jsonl, with
the span envelope as a fallback when that span is absent. Token usage and
cost come from metrics.json and are harness estimates, not provider invoices.
Historical timing, token, and cost data for original reviews was not
collected and is reported as unavailable.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from urllib.parse import unquote

DEFAULT_OUTPUT_NAME = "report.html"
ROOT_SPAN = "invoke_agent"

CSS = """
body { font-family: system-ui, sans-serif; line-height: 1.45; margin: 1.5rem auto;
       max-width: 74rem; padding: 0 1rem; }
h1, h2, h3 { line-height: 1.25; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid #888; padding-bottom: 0.2rem; }
h3 { margin-bottom: 0.3rem; }
h4, h5 { margin: 0.8rem 0 0.3rem; }
p { margin: 0.4rem 0; }
p.meta { color: #666; font-size: 0.9rem; }
span.detail { color: #666; font-size: 0.9rem; }
td.center { text-align: center; }
table { border-collapse: collapse; margin: 0.8rem 0; font-size: 0.9rem; }
th, td { border: 1px solid #888; padding: 0.3rem 0.6rem;
         text-align: left; vertical-align: top; }
th { background: rgba(127, 127, 127, 0.15); }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.muted { color: #666; }
pre { white-space: pre-wrap; overflow-wrap: anywhere;
      background: rgba(127, 127, 127, 0.12); padding: 0.75rem; border-radius: 4px; }
details { margin: 0.5rem 0; }
code { overflow-wrap: anywhere; }
section.case { margin-top: 3rem; }
"""


class ReportError(ValueError):
    """The corpus cannot be read as a review comparison input."""


@dataclass(frozen=True)
class Case:
    """One replayable review unit, collected with the original review."""

    case_id: str
    pr_number: int
    review_id: int
    review_url: str
    description_url: str
    submitted_at: str
    landed_commit: str
    head_sha: str
    base_sha: str
    diff_lines: int
    diff_bytes: int


@dataclass(frozen=True)
class Review:
    """A structured review body, replayed or originally collected."""

    general_comment: str
    inline_comments: dict[str, dict[str, str]]


@dataclass(frozen=True)
class AnalysisIssue:
    """One clustered review point and which reviewers raised it."""

    summary: str
    detail: str
    kind: str
    severity: str
    reviewers: list[str]


@dataclass(frozen=True)
class Analysis:
    """Issue classification of one case's reviews, produced by an LLM pass."""

    classifier: str
    reviewers: list[dict[str, str]]
    issues: list[AnalysisIssue]


@dataclass(frozen=True)
class Attempt:
    """One reviewer invocation against one case."""

    model: str
    case_id: str
    attempt_id: str
    review_status: str
    export_status: str
    metrics: dict | None
    review: Review | None
    duration_s: float | None
    started_at: str | None
    model_calls: int
    tool_calls: int
    mtime_ns: int


# run id -> model -> case id -> attempts, in sorted order.
_Runs = dict[str, dict[str, dict[str, list[Attempt]]]]
# case id -> original model label -> collected review.
_Baselines = dict[str, dict[str, Review]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help=f"report path (default: $CORPUS/{DEFAULT_OUTPUT_NAME})",
    )
    args = parser.parse_args(argv)
    try:
        output = _report(args)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Wrote report: {output}")
    return 0


def _report(args: argparse.Namespace) -> Path:
    root = args.corpus.resolve(strict=True)
    manifest = _read_json(root / "manifest.json")
    selection = manifest.get("selection")
    if not isinstance(selection, dict):
        raise ReportError("manifest.json has no selection object")
    cases = _cases(root)
    if not cases:
        raise ReportError(f"no cases/*/metadata.json under {root}")
    baselines = _baselines(root, cases)
    runs = _runs(root)
    analysis = _analysis(root, cases)
    output = args.output if args.output is not None else root / DEFAULT_OUTPUT_NAME
    page = _page(root.name, selection, cases, baselines, runs, analysis)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page)
    return output


def _cases(root: Path) -> list[Case]:
    cases = []
    for meta_path in sorted((root / "cases").glob("*/metadata.json")):
        meta = _read_json(meta_path)
        diff_path = meta_path.parent / "diff.patch"
        diff = diff_path.read_text() if diff_path.exists() else ""
        cases.append(
            Case(
                case_id=meta_path.parent.name,
                pr_number=_required_int(meta.get("pr_number"), "pr_number"),
                review_id=_required_int(meta.get("review_id"), "review_id"),
                review_url=str(meta.get("review_url", "")),
                description_url=str(meta.get("description_url", "")),
                submitted_at=str(meta.get("review_submitted_at", "")),
                landed_commit=str(meta.get("landed_commit", "")),
                head_sha=str(meta.get("head_sha", "")),
                base_sha=str(meta.get("base_sha", "")),
                diff_lines=diff.count("\n"),
                diff_bytes=len(diff.encode()),
            )
        )
    cases.sort(key=lambda case: (-case.pr_number, case.case_id))
    return cases


def _baselines(root: Path, cases: list[Case]) -> _Baselines:
    by_case: _Baselines = {case.case_id: {} for case in cases}
    reviews_root = root / "reviews"
    if not reviews_root.is_dir():
        return by_case
    for model_dir in sorted(reviews_root.iterdir()):
        if not model_dir.is_dir():
            continue
        for path in sorted(model_dir.glob("*.json")):
            if path.stem not in by_case:
                continue
            data = _read_json(path)
            by_case[path.stem][model_dir.name] = Review(
                general_comment=_text(data.get("general_comment")),
                inline_comments=_inline(data.get("inline_comments")),
            )
    return by_case


def _runs(root: Path) -> _Runs:
    runs: _Runs = {}
    runs_root = root / "runs"
    if not runs_root.is_dir():
        return runs
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        reviews_root = run_dir / "reviews"
        if not reviews_root.is_dir():
            continue
        for model_dir in sorted(reviews_root.iterdir()):
            if not model_dir.is_dir():
                continue
            model = unquote(model_dir.name)
            for case_dir in sorted(model_dir.iterdir()):
                if not case_dir.is_dir():
                    continue
                for state_path in sorted(
                    (case_dir / "attempts").glob("*/attempt.json")
                ):
                    attempt = _attempt(model, case_dir.name, state_path)
                    runs.setdefault(run_dir.name, {}).setdefault(model, {}).setdefault(
                        case_dir.name, []
                    ).append(attempt)
    return runs


def _analysis(root: Path, cases: list[Case]) -> dict[str, Analysis | None]:
    """Load per-case issue classification from analysis/<case-id>.json."""
    entries: dict[str, Analysis | None] = {}
    analysis_root = root / "analysis"
    for case in cases:
        path = analysis_root / f"{case.case_id}.json"
        if not path.exists():
            entries[case.case_id] = None
            continue
        data = _read_json(path)
        reviewers = _reviewer_entries(data.get("reviewers"), path)
        entries[case.case_id] = Analysis(
            classifier=str(data.get("classifier_model", "unknown")),
            reviewers=reviewers,
            issues=_issue_entries(
                data.get("issues"), {entry["key"] for entry in reviewers}, path
            ),
        )
    return entries


def _reviewer_entries(value: object, path: Path) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ReportError(f"{path}: reviewers must be a list")
    entries: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ReportError(f"{path}: reviewer entries must be objects")
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            raise ReportError(f"{path}: reviewer entry missing a key")
        label = entry.get("label", key)
        if not isinstance(label, str) or not label:
            label = key
        entries.append({"key": key, "label": label})
    return entries


def _issue_entries(
    value: object, reviewer_keys: set[str], path: Path
) -> list[AnalysisIssue]:
    if not isinstance(value, list):
        raise ReportError(f"{path}: issues must be a list")
    issues: list[AnalysisIssue] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ReportError(f"{path}: issue entries must be objects")
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary:
            raise ReportError(f"{path}: issue entry missing a summary")
        reviewers = entry.get("reviewers", [])
        if not isinstance(reviewers, list) or not all(
            isinstance(item, str) for item in reviewers
        ):
            raise ReportError(f"{path}: issue reviewers must be a list of strings")
        reviewers = [item for item in reviewers if isinstance(item, str)]
        unknown = [item for item in reviewers if item not in reviewer_keys]
        if unknown:
            raise ReportError(
                f"{path}: issue references unknown reviewer keys: " + ", ".join(unknown)
            )
        issues.append(
            AnalysisIssue(
                summary=summary,
                detail=_text(entry.get("detail")),
                kind=_text(entry.get("kind")),
                severity=_text(entry.get("severity")),
                reviewers=reviewers,
            )
        )
    return issues


def _attempt(model: str, case_id: str, state_path: Path) -> Attempt:
    attempt_dir = state_path.parent
    state = _read_json(state_path)
    metrics_path = attempt_dir / "metrics.json"
    metrics = _read_json(metrics_path) if metrics_path.exists() else None
    review_path = attempt_dir / "review.json"
    review = None
    if review_path.exists():
        data = _read_json(review_path)
        review = Review(
            general_comment=_text(data.get("general_comment")),
            inline_comments=_inline(data.get("inline_comments")),
        )
    duration, started, model_calls, tool_calls = _timing(attempt_dir / "otel")
    return Attempt(
        model=model,
        case_id=case_id,
        attempt_id=attempt_dir.name,
        review_status=str(state.get("review_status", "unknown")),
        export_status=str(state.get("export_status", "unknown")),
        metrics=metrics,
        review=review,
        duration_s=duration,
        started_at=started,
        model_calls=model_calls,
        tool_calls=tool_calls,
        mtime_ns=state_path.stat().st_mtime_ns,
    )


def _timing(otel_dir: Path) -> tuple[float | None, str | None, int, int]:
    """Wall time and span counts from an attempt's exported traces."""
    spans = _spans(otel_dir / "traces.jsonl")
    model_calls = sum(
        1 for span in spans if str(span.get("name", "")).startswith("chat")
    )
    tool_calls = sum(
        1 for span in spans if str(span.get("name", "")).startswith("execute_tool")
    )
    root_spans = [span for span in spans if span.get("name") == ROOT_SPAN]
    chosen = root_spans or spans
    starts = [
        nanos
        for span in chosen
        if (nanos := _nanos(span.get("startTimeUnixNano"))) is not None
    ]
    ends = [
        nanos
        for span in chosen
        if (nanos := _nanos(span.get("endTimeUnixNano"))) is not None
    ]
    if not starts or not ends:
        return None, None, model_calls, tool_calls
    start, end = min(starts), max(ends)
    started = datetime.fromtimestamp(start / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    return (end - start) / 1e9, started, model_calls, tool_calls


def _spans(path: Path) -> list[dict]:
    if not path.exists():
        return []
    spans = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            batch = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(batch, dict):
            continue
        for resource in _objects(batch.get("resourceSpans")):
            for scope in _objects(resource.get("scopeSpans")):
                for span in _objects(scope.get("spans")):
                    spans.append(span)
    return spans


def _page(
    corpus_name: str,
    selection: dict,
    cases: list[Case],
    baselines: _Baselines,
    runs: _Runs,
    analysis: dict[str, Analysis | None],
) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    repository = selection.get("repository", "unknown")
    original = selection.get("original_model", "unknown")
    run_ids = ", ".join(sorted(runs)) or "none"
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{_esc('Review comparison — ' + corpus_name)}</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>Review comparison — {_esc(corpus_name)}</h1>",
        '<p class="meta">',
        f"repository {_esc(repository)}"
        f" · ref {_esc(selection.get('ref', 'unknown'))}"
        f" · resolved head {_esc(_short(str(selection.get('resolved_head', ''))))}"
        f" · original model {_esc(original)}"
        f" · corpus {_esc(corpus_name)}"
        f" · {len(cases)} case(s)"
        f" · run(s): {_esc(run_ids)}"
        f" · generated {generated}",
        "</p>",
        '<p class="meta">Wall time is the invoke_agent span duration (or the span '
        "envelope when absent). Token usage and cost are harness estimates from "
        "metrics.json, not provider invoices. Historical timing, token, and cost "
        "data for original reviews was not collected.</p>",
        _summary(runs, cases),
        "<h2>Cases</h2>",
    ]
    for case in cases:
        parts.append(_case_section(case, baselines, runs, analysis))
    parts.append("</body>\n</html>\n")
    return "\n".join(parts)


def _summary(runs: _Runs, cases: list[Case]) -> str:
    parts = [
        "<h2>Summary per model</h2>",
        "<table>",
        "<tr>",
        "<th>Model</th><th>Cases</th><th>Wall total</th><th>Wall mean</th>"
        "<th>Input</th><th>Output</th><th>Cache read</th><th>Cache write</th>"
        "<th>Total tokens</th><th>Cost</th>",
        "</tr>",
    ]
    for run_id in sorted(runs):
        for model in sorted(runs[run_id]):
            representatives = []
            for case in cases:
                attempts = runs[run_id][model].get(case.case_id, [])
                if attempts:
                    representatives.append(_representative(attempts))
            succeeded = [
                rep for rep in representatives if rep.review_status == "succeeded"
            ]
            durations = [
                rep.duration_s for rep in succeeded if rep.duration_s is not None
            ]
            metrics = [rep.metrics for rep in succeeded]
            parts.append(
                "<tr>"
                f"<td>{_esc(model)}</td>"
                f'<td class="num">{len(succeeded)}/{len(representatives)}</td>'
                f'<td class="num">{_fmt_duration(_total(durations))}</td>'
                f'<td class="num">{_fmt_duration(_mean(durations))}</td>'
                + "".join(
                    f'<td class="num">{_fmt_int(_sum_metric(metrics, key))}</td>'
                    for key in (
                        "tokens_input",
                        "tokens_output",
                        "tokens_cache_read",
                        "tokens_cache_write",
                        "tokens_total",
                    )
                )
                + f'<td class="num">{_fmt_cost_total(metrics)}</td>'
                + "</tr>"
            )
    parts.append("</table>")
    return "\n".join(parts)


def _issues_table(analysis: Analysis | None) -> str:
    """Render the classified issue list with reviewer coverage columns."""
    if analysis is None:
        return ""
    parts = [
        f"<h4>Issues · classified by {_esc(analysis.classifier)}</h4>",
        "<table>",
        "<tr><th>Issue</th><th>Kind</th><th>Severity</th>"
        + "".join(f"<th>{_esc(entry['label'])}</th>" for entry in analysis.reviewers)
        + "</tr>",
    ]
    if not analysis.issues:
        parts.append(
            f'<tr><td colspan="{3 + len(analysis.reviewers)}">'
            '<span class="detail">No issues reported.</span></td></tr>'
        )
    for issue in analysis.issues:
        summary = f"<b>{_esc(issue.summary)}</b>"
        if issue.detail:
            summary += f'<br><span class="detail">{_esc(issue.detail)}</span>'
        marks = "".join(
            '<td class="center">✓</td>'
            if entry["key"] in issue.reviewers
            else '<td class="muted center">—</td>'
            for entry in analysis.reviewers
        )
        parts.append(
            "<tr>"
            f"<td>{summary}</td>"
            f"<td>{_esc(issue.kind)}</td>"
            f"<td>{_esc(issue.severity)}</td>"
            f"{marks}"
            "</tr>"
        )
    parts.append("</table>")
    return "\n".join(parts)


def _case_section(
    case: Case,
    baselines: _Baselines,
    runs: _Runs,
    analysis: dict[str, Analysis | None],
) -> str:
    meta = [
        _link(case.description_url, f"PR #{case.pr_number}"),
        _link(case.review_url, f"review {case.review_id}"),
        f"submitted {_esc(case.submitted_at)}",
        f"landed {_esc(_short(case.landed_commit))}",
        f"reviewed head {_esc(_short(case.head_sha))}",
        f"base {_esc(_short(case.base_sha))}",
        f"diff {_fmt_int(case.diff_lines)} lines / {_fmt_int(case.diff_bytes)} bytes",
    ]
    parts = [
        '<section class="case">',
        "<h3>",
        _link(case.description_url, f"PR #{case.pr_number}"),
        f" · {_esc(case.case_id)}",
        "</h3>",
        '<p class="meta">' + " · ".join(meta[1:]) + "</p>",
        _metrics_table(case, baselines, runs),
        _issues_table(analysis.get(case.case_id)),
        "<h4>Reviews</h4>",
    ]
    for model in sorted(baselines.get(case.case_id, {})):
        review = baselines[case.case_id][model]
        parts.append(
            _review_details(
                f"Original review — {model}",
                "collected with the case; no timing, token, or cost data",
                review,
            )
        )
    for run_id in sorted(runs):
        for model in sorted(runs[run_id]):
            attempts = runs[run_id][model].get(case.case_id, [])
            if not attempts:
                continue
            rep = _representative(attempts)
            others = [
                attempt for attempt in attempts if attempt.attempt_id != rep.attempt_id
            ]
            timing = " · ".join(
                value
                for value in (
                    f"wall {_fmt_duration(rep.duration_s)}",
                    f"started {rep.started_at}" if rep.started_at else "",
                    f"{rep.model_calls} model calls",
                    f"{rep.tool_calls} tool calls",
                )
                if value
            )
            extra = ""
            if others:
                listed = ", ".join(
                    f"{attempt.attempt_id} ({attempt.review_status})"
                    for attempt in others
                )
                extra = f"<br>Other attempts: {listed}"
            parts.append(
                _review_details(
                    f"Review — {rep.model}",
                    f"attempt {rep.attempt_id} · {rep.review_status} · {timing}{extra}",
                    rep.review,
                )
            )
    parts.append("</section>")
    return "\n".join(parts)


def _metrics_table(case: Case, baselines: _Baselines, runs: _Runs) -> str:
    parts = [
        "<table>",
        "<tr>",
        "<th>Model</th><th>Status</th><th>Wall</th>"
        "<th>Model calls</th><th>Tool calls</th><th>Input</th><th>Output</th>"
        "<th>Cache read</th><th>Cache write</th>"
        "<th>Total tokens</th><th>Cost</th><th>Comments</th>",
        "</tr>",
    ]
    for model in sorted(baselines.get(case.case_id, {})):
        review = baselines[case.case_id][model]
        parts.append(
            "<tr>"
            f"<td>{_esc(model)}</td>"
            '<td class="muted">collected</td>'
            '<td class="num muted">—</td>'
            '<td class="num muted">—</td>'
            '<td class="num muted">—</td>'
            '<td class="num muted">—</td>'
            '<td class="num muted">—</td>'
            '<td class="num muted">—</td>'
            '<td class="num muted">—</td>'
            '<td class="num muted">—</td>'
            '<td class="num muted">—</td>'
            f'<td class="num">{_fmt_int(_comment_count(review))}</td>'
            "</tr>"
        )
    for run_id in sorted(runs):
        for model in sorted(runs[run_id]):
            attempts = runs[run_id][model].get(case.case_id, [])
            if not attempts:
                continue
            rep = _representative(attempts)
            status = rep.review_status
            if len(attempts) > 1:
                status = f"{status} ({len(attempts)} attempts)"
            parts.append(
                "<tr>"
                f"<td>{_esc(model)}</td>"
                f"<td>{_esc(status)}</td>"
                f'<td class="num">{_fmt_duration(rep.duration_s)}</td>'
                f'<td class="num">{_fmt_int(rep.model_calls)}</td>'
                f'<td class="num">{_fmt_int(rep.tool_calls)}</td>'
                f'<td class="num">{_fmt_metric(rep.metrics, "tokens_input")}</td>'
                f'<td class="num">{_fmt_metric(rep.metrics, "tokens_output")}</td>'
                f'<td class="num">{_fmt_metric(rep.metrics, "tokens_cache_read")}</td>'
                f'<td class="num">{_fmt_metric(rep.metrics, "tokens_cache_write")}</td>'
                f'<td class="num">{_fmt_metric(rep.metrics, "tokens_total")}</td>'
                f'<td class="num">{_fmt_cost_metric(rep.metrics)}</td>'
                f'<td class="num">{_fmt_int(_comment_count(rep.review))}</td>'
                "</tr>"
            )
    parts.append("</table>")
    return "\n".join(parts)


def _review_details(heading: str, meta: str, review: Review | None) -> str:
    parts = [
        "<details>",
        f"<summary>{_esc(heading)}</summary>",
        f'<p class="meta">{_esc(meta)}</p>',
    ]
    if review is None:
        parts.append('<p class="meta">No review body was retained.</p>')
    else:
        parts.append("<h4>General comment</h4>")
        parts.append(f"<pre>{_esc(review.general_comment)}</pre>")
        count = _comment_count(review)
        parts.append(f"<h4>Inline comments ({count})</h4>")
        if review.inline_comments:
            for path in sorted(review.inline_comments):
                parts.append(f"<h5><code>{_esc(path)}</code></h5>")
                items = []
                for line in sorted(review.inline_comments[path], key=_line_sort_key):
                    comment = review.inline_comments[path][line]
                    items.append(f"<li><b>line {_esc(line)}</b>: {_esc(comment)}</li>")
                parts.append("<ul>" + "".join(items) + "</ul>")
        else:
            parts.append('<p class="meta">None.</p>')
    parts.append("</details>")
    return "\n".join(parts)


def _representative(attempts: list[Attempt]) -> Attempt:
    """Prefer the latest successful attempt, else the latest attempt."""
    return max(
        attempts,
        key=lambda attempt: (
            attempt.review_status == "succeeded",
            attempt.mtime_ns,
        ),
    )


def _line_sort_key(line: str) -> tuple[int, str]:
    return (int(line), line) if line.isdigit() else (2**30, line)


def _comment_count(review: Review | None) -> int | None:
    if review is None:
        return None
    return sum(len(lines) for lines in review.inline_comments.values())


def _total(values: list[float]) -> float | None:
    return sum(values) if values else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _sum_metric(metrics: list[dict | None], key: str) -> int | None:
    values = [
        value
        for entry in metrics
        if entry is not None and (value := _metric_int(entry, key)) is not None
    ]
    return sum(values) if values else None


def _metric_int(metrics: dict | None, key: str) -> int | None:
    if metrics is None:
        return None
    return _positive_int(metrics.get(key), key, optional=True)


def _fmt_metric(metrics: dict | None, key: str) -> str:
    return _fmt_int(_metric_int(metrics, key))


def _fmt_int(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    whole = int(seconds)
    if whole >= 3600:
        return f"{whole // 3600}h {(whole % 3600) // 60}m {whole % 60}s"
    if whole >= 60:
        return f"{whole // 60}m {whole % 60}s"
    return f"{seconds:.1f}s"


def _fmt_cost_metric(metrics: dict | None) -> str:
    if metrics is None or metrics.get("cost") is None:
        return "—"
    return _fmt_cost(float(metrics["cost"]), str(metrics.get("cost_currency", "")))


def _fmt_cost_total(metrics: list[dict | None]) -> str:
    costs = [
        (float(entry["cost"]), str(entry.get("cost_currency", "")))
        for entry in metrics
        if entry is not None and entry.get("cost") is not None
    ]
    if not costs:
        return "—"
    total = sum(cost for cost, _ in costs)
    currencies = {currency for _, currency in costs}
    if currencies == {"USD"}:
        return f"${total:.4f}"
    if len(currencies) == 1:
        return f"{total:.4f} {currencies.pop()}"
    return f"{total:.4f} mixed currencies"


def _fmt_cost(cost: float, currency: str) -> str:
    if currency == "USD":
        return f"${cost:.4f}"
    return f"{cost:.4f} {currency}".strip()


def _short(value: str) -> str:
    return value[:10]


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


def _link(url: str, text: str) -> str:
    if not url:
        return _esc(text)
    return f'<a href="{_esc(url)}">{_esc(text)}</a>'


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _inline(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    comments: dict[str, dict[str, str]] = {}
    for path, lines in value.items():
        if not isinstance(path, str) or not isinstance(lines, dict):
            continue
        comments[path] = {
            line: _text(comment)
            for line, comment in lines.items()
            if isinstance(line, str)
        }
    return comments


def _objects(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _nanos(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _positive_int(value: object, field: str, *, optional: bool = True) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReportError(f"{field} must be a nonnegative integer, not {value!r}")
    return value


def _required_int(value: object, field: str) -> int:
    result = _positive_int(value, field, optional=False)
    if result is None:
        raise ReportError(f"{field} must be present")
    return result


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ReportError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReportError(f"{path}: expected a JSON object")
    return value


if __name__ == "__main__":
    sys.exit(main())

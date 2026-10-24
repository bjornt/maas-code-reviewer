# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
from pathlib import Path

from maas_code_reviewer.agent_runner import (
    AgentRunResult,
    AgentUsage,
    CompletionStatus,
)
from maas_code_reviewer.metrics import ReviewMetrics, write_metrics
from maas_code_reviewer.reviewer import review_diff
from tests.fake_agent_runner import FakeAgentRunner

DIFF = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+café\n"


def test_review_metrics_persist_reported_identity_usage_and_host_metadata(
    tmp_path: Path,
) -> None:
    runner = FakeAgentRunner(
        AgentRunResult(
            terminal_text='{"general_comment": "Reviewed.", "inline_comments": {}}',
            status=CompletionStatus.COMPLETED,
            agent_name="portable-harness",
            agent_version="2.0",
            model="resolved/model",
            usage=AgentUsage(
                input_tokens=19,
                output_tokens=7,
                thinking_tokens=3,
                cache_read_tokens=5,
                cache_write_tokens=2,
                total_tokens=31,
                cost=0.125,
                cost_currency="USD",
            ),
        )
    )
    metrics = ReviewMetrics()
    review_diff(runner, DIFF, None, tmp_path, metrics=metrics, agents_md="Rules.")
    output = tmp_path / "metrics.json"
    write_metrics(metrics, output)
    assert json.loads(output.read_text()) == {
        "model_name": "resolved/model",
        "agent_name": "portable-harness",
        "agent_version": "2.0",
        "tokens_input": 19,
        "tokens_output": 7,
        "tokens_thinking": 3,
        "tokens_cache_read": 5,
        "tokens_cache_write": 2,
        "tokens_total": 31,
        "cost": 0.125,
        "cost_currency": "USD",
        "agents_md_read": True,
        "diff_lines": len(DIFF.splitlines()),
        "diff_size_bytes": len(DIFF.encode("utf-8")),
    }


def test_unreported_values_serialize_as_null_not_invented_zeros(
    tmp_path: Path,
) -> None:
    runner = FakeAgentRunner(
        AgentRunResult(
            terminal_text='{"general_comment": "Reviewed.", "inline_comments": {}}',
            status=CompletionStatus.COMPLETED,
            agent_name="portable-harness",
            usage=AgentUsage(output_tokens=0),
        )
    )
    metrics = ReviewMetrics()
    review_diff(runner, DIFF, None, tmp_path, metrics=metrics)
    output = tmp_path / "metrics.json"
    write_metrics(metrics, output)
    document = json.loads(output.read_text())
    assert document["tokens_output"] == 0
    for field in (
        "model_name",
        "agent_version",
        "tokens_input",
        "tokens_thinking",
        "tokens_cache_read",
        "tokens_cache_write",
        "tokens_total",
        "cost",
        "cost_currency",
    ):
        assert document[field] is None
    assert document["agents_md_read"] is False


def test_empty_review_persists_known_zero_usage_without_an_agent(
    tmp_path: Path,
) -> None:
    metrics = ReviewMetrics()
    review_diff(None, "", None, tmp_path, metrics=metrics)
    output = tmp_path / "metrics.json"
    write_metrics(metrics, output)
    document = json.loads(output.read_text())
    for field in (
        "tokens_input",
        "tokens_output",
        "tokens_thinking",
        "tokens_cache_read",
        "tokens_cache_write",
        "tokens_total",
        "cost",
        "diff_lines",
        "diff_size_bytes",
    ):
        assert document[field] == 0
    assert document["agent_name"] is None
    assert document["agent_version"] is None
    assert document["model_name"] is None
    assert document["agents_md_read"] is False
    assert document["cost_currency"] is None

# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
from pathlib import Path

from maas_code_reviewer.metrics import ReviewMetrics, write_metrics


class TestReviewMetrics:
    def test_default_field_values(self) -> None:
        metrics = ReviewMetrics()
        assert metrics.model_name == ""
        assert metrics.tokens_thinking == 0
        assert metrics.tokens_input == 0
        assert metrics.tokens_output == 0
        assert metrics.agents_md_read is False
        assert metrics.diff_lines == 0
        assert metrics.diff_size_bytes == 0
        assert metrics.agent_name is None
        assert metrics.agent_version is None
        assert metrics.tokens_cache_read is None
        assert metrics.tokens_cache_write is None
        assert metrics.tokens_total is None
        assert metrics.cost is None
        assert metrics.cost_currency is None

    def test_agent_fields_round_trip_and_none_serialises_as_null(
        self, tmp_path: Path
    ) -> None:
        metrics = ReviewMetrics(
            model_name=None,
            agent_name="omp",
            agent_version="18.12.2",
            tokens_cache_read=5,
            tokens_cache_write=2,
            tokens_total=31,
            cost=0.125,
            cost_currency="USD",
        )
        out = tmp_path / "metrics.json"
        write_metrics(metrics, out)

        data = json.loads(out.read_text())
        assert data["model_name"] is None
        assert data["agent_name"] == "omp"
        assert data["agent_version"] == "18.12.2"
        assert data["tokens_cache_read"] == 5
        assert data["tokens_cache_write"] == 2
        assert data["tokens_total"] == 31
        assert data["cost"] == 0.125
        assert data["cost_currency"] == "USD"

        default_out = tmp_path / "default-metrics.json"
        write_metrics(ReviewMetrics(), default_out)

        default_data = json.loads(default_out.read_text())
        assert default_data["agent_name"] is None
        assert default_data["cost"] is None
        assert '"agent_name": null' in default_out.read_text()
        assert '"cost": null' in default_out.read_text()


class TestWriteMetrics:
    def test_writes_valid_json_that_round_trips(self, tmp_path: Path) -> None:
        metrics = ReviewMetrics(
            model_name="test-model",
            tokens_thinking=100,
            tokens_input=200,
            tokens_output=300,
            agents_md_read=True,
            diff_lines=42,
            diff_size_bytes=2048,
        )
        out = tmp_path / "metrics.json"
        write_metrics(metrics, out)

        data = json.loads(out.read_text())
        assert data["model_name"] == "test-model"
        assert data["tokens_thinking"] == 100
        assert data["tokens_input"] == 200
        assert data["tokens_output"] == 300
        assert data["agents_md_read"] is True
        assert data["diff_lines"] == 42
        assert data["diff_size_bytes"] == 2048

    def test_agents_md_read_serialises_as_json_boolean(self, tmp_path: Path) -> None:
        metrics = ReviewMetrics(agents_md_read=True)
        out = tmp_path / "metrics.json"
        write_metrics(metrics, out)

        raw = out.read_text()
        data = json.loads(raw)
        # Verify it's a real JSON boolean, not an int
        assert data["agents_md_read"] is True
        assert isinstance(data["agents_md_read"], bool)

    def test_agents_md_read_false_serialises_as_json_boolean(
        self, tmp_path: Path
    ) -> None:
        metrics = ReviewMetrics(agents_md_read=False)
        out = tmp_path / "metrics.json"
        write_metrics(metrics, out)

        data = json.loads(out.read_text())
        assert data["agents_md_read"] is False
        assert isinstance(data["agents_md_read"], bool)

    def test_default_values_write_correctly(self, tmp_path: Path) -> None:
        metrics = ReviewMetrics()
        out = tmp_path / "metrics.json"
        write_metrics(metrics, out)

        data = json.loads(out.read_text())
        assert data["model_name"] == ""
        assert data["tokens_thinking"] == 0
        assert data["tokens_input"] == 0
        assert data["tokens_output"] == 0
        assert data["agents_md_read"] is False
        assert data["diff_lines"] == 0
        assert data["diff_size_bytes"] == 0

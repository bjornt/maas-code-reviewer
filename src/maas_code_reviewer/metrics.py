# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReviewMetrics:
    model_name: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    tokens_thinking: int | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_write: int | None = None
    tokens_total: int | None = None
    cost: float | None = None
    cost_currency: str | None = None
    agents_md_read: bool = False
    diff_lines: int = 0
    diff_size_bytes: int = 0


def write_metrics(metrics: ReviewMetrics, path: Path) -> None:
    """Serialise *metrics* as JSON to *path*."""
    path.write_text(json.dumps(dataclasses.asdict(metrics), indent=2))

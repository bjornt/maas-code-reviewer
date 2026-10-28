# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReviewMetrics:
    """One review's metrics: host-known diff facts plus the agent's report.

    Populated by the ``review`` agent path. ``None`` means unavailable,
    never a fabricated zero; the empty-diff short-circuit records known
    zeros because nothing ran.
    """

    model_name: str | None = ""
    tokens_thinking: int | None = 0
    tokens_input: int | None = 0
    tokens_output: int | None = 0
    agents_md_read: bool = False
    diff_lines: int = 0
    diff_size_bytes: int = 0
    agent_name: str | None = None
    agent_version: str | None = None
    tokens_cache_read: int | None = None
    tokens_cache_write: int | None = None
    tokens_total: int | None = None
    cost: float | None = None
    cost_currency: str | None = None


def write_metrics(metrics: ReviewMetrics, path: Path) -> None:
    """Serialise *metrics* as JSON to *path*."""
    path.write_text(json.dumps(dataclasses.asdict(metrics), indent=2))

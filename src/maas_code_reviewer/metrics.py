# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReviewMetrics:
    # google-genai (legacy) fields — populated by review-diff/review-mp/
    # review-pr; scheduled for removal in step 16.
    model_name: str | None = ""
    tokens_thinking: int | None = 0
    tokens_input: int | None = 0
    tokens_output: int | None = 0
    files_read: int = 0
    agents_md_read: bool = False
    diff_lines: int = 0
    diff_size_bytes: int = 0
    tool_call_limit: int = 0
    tool_call_limit_reached: bool = False
    resume_attempts: int = 0
    # Agent-path fields — populated by `review`; None = unavailable,
    # never a fabricated zero (a measured 0 stays 0).
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

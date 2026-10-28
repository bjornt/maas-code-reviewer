# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Synchronous application boundary for one isolated, non-resumable agent review.

No provider, protocol SDK, process, publication client, or filesystem callback
belongs in this contract. The reviewer validates terminal output against the
original full diff before decorating or publishing it.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class CompletionStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REFUSED = "refused"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True)
class ReviewRequest:
    """Host-owned inputs; repository contents must never select runtime policy.

    The caller keeps repository_path alive for the whole synchronous run. The
    timeout covers startup and the turn; cleanup may use a bounded grace period.
    task_prompt may be bounded, but full_diff must retain the original input.
    """

    repository_path: Path
    full_diff: str
    task_prompt: str
    system_instruction: str
    timeout_seconds: float
    model: str | None = None


@dataclass(frozen=True)
class AgentUsage:
    """Only reported measurements; None is unavailable, not a measured zero.

    Token fields are reported independently: input, cache, and total semantics
    can differ by harness. Do not derive totals or sum cumulative notifications.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None
    cost_currency: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    """One terminal message, never a concatenated protocol transcript.

    Only COMPLETED with nonempty, host-validated terminal_text is publishable.
    agent_name/version describe the initialized harness; model is the resolved
    identity when available, not merely the user's requested selector.
    """

    terminal_text: str
    status: CompletionStatus
    agent_name: str
    agent_version: str | None = None
    model: str | None = None
    usage: AgentUsage = field(default_factory=AgentUsage)


class AgentRunner(Protocol):
    def run(self, request: ReviewRequest) -> AgentRunResult:
        """Complete one job synchronously; return only after bounded teardown."""
        ...


class AgentRunnerError(RuntimeError):
    """A non-publishable failure, optionally carrying actually reported usage."""

    def __init__(self, message: str, *, usage: AgentUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class RunnerConfigurationError(AgentRunnerError):
    """Missing/invalid trusted configuration or an unsupported explicit model."""


class RunnerStartupError(AgentRunnerError):
    """The isolated job or harness could not start."""


class RunnerTransportError(AgentRunnerError):
    """Protocol/process failure, including unexpected EOF or malformed frames."""


class RunnerTimeoutError(AgentRunnerError):
    """The total startup/turn deadline expired."""


class RunnerCancelledError(AgentRunnerError):
    """The review was cancelled; partial output must not be published."""


class RunnerRefusedError(AgentRunnerError):
    """The harness refused the task."""


class RunnerLimitError(AgentRunnerError):
    """The harness exhausted its token/request limit without a complete review."""


class RunnerResultError(AgentRunnerError):
    """Missing/invalid terminal output or ambiguous completion."""

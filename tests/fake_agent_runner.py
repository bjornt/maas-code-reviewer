# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from maas_code_reviewer.agent_runner import (
    AgentRunnerError,
    AgentRunResult,
    ReviewRequest,
)


class FakeAgentRunner:
    """Return controlled terminal outcomes without importing a runtime SDK.

    Requests are exposed for policy tests (especially empty/skip paths). Failed
    runs can report partial usage through the same exception as a real runner.
    `outcomes` scripts a sequence of returns, one per run() call, for repair
    tests; later calls fall back to the mutable `outcome` attribute so tests
    can still adjust the outcome between invocations.
    """

    def __init__(
        self,
        outcome: AgentRunResult | AgentRunnerError | None = None,
        *,
        outcomes: list[AgentRunResult | AgentRunnerError] | None = None,
    ) -> None:
        self.outcomes = list(outcomes) if outcomes is not None else None
        self.outcome = self.outcomes[0] if self.outcomes else outcome
        self.requests: list[ReviewRequest] = []

    def run(self, request: ReviewRequest) -> AgentRunResult:
        self.requests.append(request)
        if self.outcomes is not None and len(self.requests) <= len(self.outcomes):
            outcome = self.outcomes[len(self.requests) - 1]
        else:
            outcome = self.outcome
        if outcome is None:
            raise AssertionError("FakeAgentRunner has no outcome to return")
        if isinstance(outcome, AgentRunnerError):
            raise outcome
        return outcome

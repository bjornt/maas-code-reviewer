# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from maas_code_reviewer.agent_runner import (
    AgentRunnerError,
    AgentRunResult,
    ReviewRequest,
)


class FakeAgentRunner:
    """Return a controlled terminal outcome without importing a runtime SDK.

    Requests are exposed for policy tests (especially empty/skip paths). Failed
    runs can report partial usage through the same exception as a real runner.
    """

    def __init__(self, outcome: AgentRunResult | AgentRunnerError) -> None:
        self.outcome = outcome
        self.requests: list[ReviewRequest] = []

    def run(self, request: ReviewRequest) -> AgentRunResult:
        self.requests.append(request)
        if isinstance(self.outcome, AgentRunnerError):
            raise self.outcome
        return self.outcome

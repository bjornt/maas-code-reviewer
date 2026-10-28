# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for edge cases in fake objects and factories."""

from pathlib import Path

import pytest

from maas_code_reviewer.agent_runner import (
    AgentRunnerError,
    AgentRunResult,
    AgentUsage,
    CompletionStatus,
    ReviewRequest,
    RunnerTransportError,
)
from maas_code_reviewer.launchpad_client import web_url_to_api_url
from tests.fake_agent_runner import FakeAgentRunner
from tests.fake_git import FakeGitClient
from tests.fake_launchpadlib import (
    FakeLaunchpad,
    make_fake_comment,
    make_fake_mp,
)


def _make_request() -> ReviewRequest:
    return ReviewRequest(
        repository_path=Path("/repo"),
        full_diff="diff --git a/x b/x\n",
        task_prompt="Review the diff.",
        system_instruction="You review diffs.",
        timeout_seconds=300.0,
    )


def _make_result(text: str = "{}") -> AgentRunResult:
    return AgentRunResult(
        terminal_text=text,
        status=CompletionStatus.COMPLETED,
        agent_name="omp",
    )


class TestFakeLaunchpadlibEdgeCases:
    def test_add_comment_raises_for_unknown_mp(self) -> None:
        fake_lp = FakeLaunchpad()
        comment = make_fake_comment(author="alice", body="hi")

        with pytest.raises(KeyError, match="No merge proposal"):
            fake_lp.add_comment("https://code.launchpad.net/unknown", comment)

    def test_load_raises_for_unknown_url(self) -> None:
        fake_lp = FakeLaunchpad()

        with pytest.raises(KeyError, match="Nothing loaded"):
            fake_lp.load("https://example.com/unknown")

    def test_web_url_to_api_url_returns_unchanged_for_non_lp_url(self) -> None:
        url = "https://example.com/something"
        assert web_url_to_api_url(url) == url

    def test_create_comment_on_fake_mp(self) -> None:
        fake_lp = FakeLaunchpad(bot_username="ci-bot")
        mp = make_fake_mp()
        fake_lp.add_merge_proposal("myproject", mp)

        mp.createComment(subject="Review", content="Looks good!")

        assert len(mp.all_comments) == 1
        assert mp.all_comments[0].message_body == "Looks good!"
        assert mp.all_comments[0].author.name == "ci-bot"


class TestFakeGitClientBareRepo:
    def test_create_bare_repo(self, tmp_path: Path) -> None:
        client = FakeGitClient()
        bare_repo = tmp_path / "bare.git"
        client.create_repo(bare_repo, bare=True)

        assert bare_repo.exists()
        # A bare repo has a HEAD file at the top level
        assert (bare_repo / "HEAD").exists()


class TestFakeAgentRunnerEdgeCases:
    def test_raises_when_no_outcome(self) -> None:
        runner = FakeAgentRunner()

        with pytest.raises(AssertionError, match="no outcome to return"):
            runner.run(_make_request())

    def test_returns_single_outcome_for_every_call(self) -> None:
        outcome = _make_result()
        runner = FakeAgentRunner(outcome)
        first, second = _make_request(), _make_request()

        assert runner.run(first) is outcome
        assert runner.run(second) is outcome
        assert runner.requests == [first, second]

    def test_scripts_one_outcome_per_call_then_falls_back(self) -> None:
        first, second = _make_result("one"), _make_result("two")
        runner = FakeAgentRunner(outcomes=[first, second])

        assert runner.run(_make_request()) is first
        assert runner.run(_make_request()) is second
        assert runner.run(_make_request()) is first

    def test_empty_outcomes_falls_back_to_outcome(self) -> None:
        outcome = _make_result()
        runner = FakeAgentRunner(outcome, outcomes=[])

        assert runner.run(_make_request()) is outcome

    def test_raises_scripted_error_and_keeps_partial_usage(self) -> None:
        usage = AgentUsage(input_tokens=5)
        runner = FakeAgentRunner(RunnerTransportError("connection lost", usage=usage))

        with pytest.raises(RunnerTransportError, match="connection lost") as info:
            runner.run(_make_request())

        assert info.value.usage is usage
        assert isinstance(info.value, AgentRunnerError)

    def test_outcome_attribute_adjustable_between_calls(self) -> None:
        runner = FakeAgentRunner(_make_result())
        replacement = _make_result("replaced")

        assert runner.run(_make_request()).terminal_text == "{}"
        runner.outcome = replacement
        assert runner.run(_make_request()) is replacement

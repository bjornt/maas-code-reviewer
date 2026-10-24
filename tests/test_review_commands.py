# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from maas_code_reviewer.agent_runner import (
    AgentRunResult,
    CompletionStatus,
    ReviewRequest,
    RunnerTransportError,
)
from maas_code_reviewer.github_client import GitHubClient
from maas_code_reviewer.metrics import ReviewMetrics
from maas_code_reviewer.models import Comment
from maas_code_reviewer.review_commands import (
    MergeProposalSummary,
    _lp_repo_url,
    _ref_to_branch,
    format_merge_proposals,
    has_existing_review,
    list_merge_proposals,
    post_pull_request_review,
    read_agents_context,
    review_merge_proposal,
    review_pull_request,
)
from maas_code_reviewer.reviewer import EMPTY_DIFF_GENERAL_COMMENT, REVIEW_MARKER
from maas_code_reviewer.workshop_runner import ConfiguredAgentRunner
from tests.factory import make_mp
from tests.fake_agent_runner import FakeAgentRunner
from tests.fake_git import FakeGitClient
from tests.fake_github import FakeGitHubClient
from tests.fake_launchpad import FakeLaunchpadClient


def make_outcome() -> AgentRunResult:
    return AgentRunResult(
        terminal_text=json.dumps(
            {
                "general_comment": "Check the changed value.",
                "inline_comments": {"file.txt": {"1": "[P1] Preserve the value."}},
            }
        ),
        status=CompletionStatus.COMPLETED,
        agent_name="test-agent",
        model="provider/review-model",
    )


class MemoryGitHub(FakeGitHubClient, GitHubClient):
    """Use the publication client's API with in-memory PR storage."""


def test_list_empty_and_format_empty() -> None:
    assert list_merge_proposals(FakeLaunchpadClient(), "project", "Needs review") == []
    assert format_merge_proposals([]) == ""


def test_listing_filters_project_and_status_and_uses_latest_bot_marker() -> None:
    client = FakeLaunchpadClient()
    reviewed = make_mp(url="https://code.launchpad.net/project/+merge/1")
    unreviewed = make_mp(url="https://code.launchpad.net/project/+merge/2")
    for mp in (
        reviewed,
        unreviewed,
        make_mp(target_git_repository="elsewhere"),
        make_mp(status="Merged"),
    ):
        client.add_merge_proposal(mp)
    early = datetime(2026, 1, 1, tzinfo=UTC)
    late = early + timedelta(days=1)
    for date in (late, early):
        client.add_comment(
            reviewed.api_url,
            Comment(author="review-bot", body=f"{REVIEW_MARKER}\nReview", date=date),
        )
    for author, body in (
        ("someone-else", REVIEW_MARKER),
        ("review-bot", "Ordinary comment"),
        ("review-bot", f"prefix {REVIEW_MARKER}"),
    ):
        client.add_comment(
            unreviewed.api_url, Comment(author=author, body=body, date=late)
        )

    summaries = list_merge_proposals(client, "myproject", "Needs review")

    assert summaries == [
        MergeProposalSummary(reviewed.url, "Needs review", late),
        MergeProposalSummary(unreviewed.url, "Needs review", None),
    ]
    assert format_merge_proposals(summaries).splitlines() == [
        f"{reviewed.url} Needs review {late.isoformat()}",
        f"{unreviewed.url} Needs review never",
    ]
    assert has_existing_review(client, reviewed)
    assert not has_existing_review(client, unreviewed)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "~user/project/+git/repo",
            "https://git.launchpad.net/~user/project/+git/repo",
        ),
        ("/tmp/repo", "/tmp/repo"),
        ("file:///tmp/repo", "file:///tmp/repo"),
        ("http://example/repo", "http://example/repo"),
        ("https://example/repo", "https://example/repo"),
    ],
)
def test_launchpad_repository_location(name: str, expected: str) -> None:
    assert _lp_repo_url(name) == expected


@pytest.mark.parametrize(
    ("ref", "branch"),
    [
        ("refs/heads/main", "main"),
        ("feature", "feature"),
        ("refs/tags/v1", "refs/tags/v1"),
    ],
)
def test_git_branch_reference(ref: str, branch: str) -> None:
    assert _ref_to_branch(ref) == branch


def test_repository_guidance_records_only_host_known_metric(tmp_path: Path) -> None:
    assert read_agents_context(tmp_path) is None
    (tmp_path / "AGENTS.md").write_text("Review data, not executable policy.")
    metrics = ReviewMetrics()
    assert (
        read_agents_context(tmp_path, metrics) == "Review data, not executable policy."
    )
    assert metrics.agents_md_read is True


@pytest.fixture
def acquired_mp(tmp_path: Path) -> tuple[FakeLaunchpadClient, FakeGitClient, Path, str]:
    git = FakeGitClient()
    target = tmp_path / "target"
    git.create_repo(target)
    git.add_commit(target, {"file.txt": "original\n", "AGENTS.md": "Review carefully."})
    source = tmp_path / "source"
    git.clone(str(target), source, "main")
    git.create_branch(source, "feature")
    git.checkout(source, "feature")
    git.add_commit(source, {"file.txt": "changed\n"})
    # A target-only change proves the review is of the merged tree, not source alone.
    git.add_commit(target, {"target-only.txt": "target context\n"})
    mp = make_mp(
        target_git_repository=str(target),
        source_git_repository=str(source),
        description="Proposal explanation",
        commit_message="Commit explanation",
    )
    lp = FakeLaunchpadClient()
    lp.add_merge_proposal(mp)
    return lp, git, source, mp.url


def test_merged_tree_lives_through_synchronous_review_and_is_then_deleted(
    acquired_mp: tuple[FakeLaunchpadClient, FakeGitClient, Path, str],
) -> None:
    lp, git, _, url = acquired_mp

    class InspectingRunner(FakeAgentRunner):
        def run(self, request: ReviewRequest) -> AgentRunResult:
            assert (request.repository_path / "target-only.txt").read_text() == (
                "target context\n"
            )
            assert (request.repository_path / "file.txt").read_text() == "changed\n"
            assert "+changed" in request.full_diff
            assert "target-only.txt" not in request.full_diff
            assert "Proposal explanation" in request.task_prompt
            assert "Review carefully." in request.task_prompt
            return super().run(request)

    runner = InspectingRunner(make_outcome())
    metrics = ReviewMetrics()
    review = review_merge_proposal(
        lp,
        git,
        runner,
        url,
        metrics=metrics,
        model="provider/review-model",
        timeout_seconds=18.5,
    )
    assert review is not None
    assert review.startswith(REVIEW_MARKER)
    assert "file.txt" in review and "Preserve the value" in review
    request = runner.requests[0]
    assert request.model == "provider/review-model"
    assert request.timeout_seconds == 18.5
    assert not request.repository_path.exists()
    assert lp.get_comments_for(lp.get_merge_proposal(url).api_url) == []
    assert metrics.agents_md_read is True
    assert metrics.diff_lines > 0
    assert metrics.model_name == "provider/review-model"


def test_merge_failure_result_cleans_acquired_tree(
    acquired_mp: tuple[FakeLaunchpadClient, FakeGitClient, Path, str],
) -> None:
    lp, git, _, url = acquired_mp
    runner = FakeAgentRunner(RunnerTransportError("connection lost"))
    with pytest.raises(RunnerTransportError, match="connection lost"):
        review_merge_proposal(lp, git, runner, url)
    assert not runner.requests[0].repository_path.exists()
    assert lp.get_comments_for(lp.get_merge_proposal(url).api_url) == []


def test_commit_message_context_without_description(
    acquired_mp: tuple[FakeLaunchpadClient, FakeGitClient, Path, str],
) -> None:
    lp, git, _, url = acquired_mp
    mp = lp.get_merge_proposal(url)
    lp = FakeLaunchpadClient()
    lp.add_merge_proposal(replace(mp, description=None))
    runner = FakeAgentRunner(make_outcome())
    review_merge_proposal(lp, git, runner, url)
    assert "Commit explanation" in runner.requests[0].task_prompt


def test_duplicate_does_not_acquire_repository_or_touch_metrics() -> None:
    lp = FakeLaunchpadClient()
    mp = make_mp(target_git_repository="/does-not-exist")
    lp.add_merge_proposal(mp)
    lp.add_comment(
        mp.api_url,
        Comment(author="review-bot", body=REVIEW_MARKER, date=datetime.now(UTC)),
    )
    metrics = ReviewMetrics()
    runner = FakeAgentRunner(RunnerTransportError("must not run"))
    assert (
        review_merge_proposal(lp, FakeGitClient(), runner, mp.url, metrics=metrics)
        is None
    )
    assert runner.requests == []
    assert metrics == ReviewMetrics()


def test_empty_merged_diff_needs_no_runtime_configuration(
    acquired_mp: tuple[FakeLaunchpadClient, FakeGitClient, Path, str],
) -> None:
    lp, git, source, url = acquired_mp
    git.add_commit(source, {"file.txt": "original\n"})
    metrics = ReviewMetrics()
    result = review_merge_proposal(
        lp, git, ConfiguredAgentRunner(None), url, metrics=metrics
    )
    assert result is not None and EMPTY_DIFF_GENERAL_COMMENT in result
    assert metrics.tokens_input == 0
    assert metrics.tokens_output == 0


def test_pr_acquisition_uses_checkout_context_and_publishes_every_validated_finding(
    tmp_path: Path,
) -> None:
    github = MemoryGitHub()
    pr = github.add_pull_request(
        "owner",
        "repo",
        7,
        "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1 +1 @@\n-original\n+changed\n",
        description="PR explanation",
    )
    (tmp_path / "AGENTS.md").write_text("Checkout guidance")
    runner = FakeAgentRunner(make_outcome())
    review = review_pull_request(github, runner, "owner", "repo", 7, tmp_path)
    assert not pr.posted_reviews
    assert runner.requests[0].repository_path == tmp_path.resolve()
    assert "PR explanation" in runner.requests[0].task_prompt
    assert "Checkout guidance" in runner.requests[0].task_prompt

    post_pull_request_review(github, "owner", "repo", 7, review)

    assert pr.posted_reviews == [
        {
            "body": review["general_comment"],
            "comments": [
                {"path": "file.txt", "line": 1, "body": "[P1] Preserve the value."}
            ],
        }
    ]


def test_empty_pr_can_publish_without_runtime_config(tmp_path: Path) -> None:
    github = MemoryGitHub()
    pr = github.add_pull_request("owner", "repo", 7, "")
    review = review_pull_request(
        github, ConfiguredAgentRunner(None), "owner", "repo", 7, tmp_path
    )
    assert review["general_comment"] == EMPTY_DIFF_GENERAL_COMMENT
    post_pull_request_review(github, "owner", "repo", 7, review)
    assert pr.posted_reviews == [{"body": EMPTY_DIFF_GENERAL_COMMENT, "comments": []}]

# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maas_code_reviewer.metrics import ReviewMetrics
from maas_code_reviewer.models import Comment
from maas_code_reviewer.review_commands import (
    InvalidReviewError,
    MergeProposalSummary,
    _detect_diff_prefix,
    _lp_repo_url,
    _ref_to_branch,
    fetch_merge_proposal,
    fetch_pull_request,
    format_merge_proposals,
    has_existing_review,
    list_merge_proposals,
    load_validated_review,
    render_merge_proposal_comment,
    render_pull_request_review,
    review_merge_proposal,
    review_pull_request,
)
from maas_code_reviewer.reviewer import (
    EMPTY_DIFF_GENERAL_COMMENT,
    REVIEW_MARKER,
    REVIEW_PREAMBLE,
)
from tests.factory import make_mp
from tests.fake_git import FakeGitClient
from tests.fake_github import FakeGitHubClient
from tests.fake_launchpad import FakeLaunchpadClient
from tests.fake_llm import FakeLLMClient, ScriptedResponse, ToolCall


def _prompt_text(parts: list) -> str:
    """Join Part/str prompt elements into a single string for assertions."""
    return "".join(getattr(p, "text", p) for p in parts)


def _recording_run() -> tuple[list[list[str]], object]:
    """Return a call list and a delegating subprocess.run recorder.

    The recorder calls the real ``subprocess.run`` and records each
    command's argv, so assertions can inspect the git invocations.
    """
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        calls.append(list(args))
        return real_run(args, **kwargs)

    return calls, recording_run


def _write_publish_inputs(
    tmp_path: Path, diff: str, diff_prefix: str | None = None
) -> Path:
    """Write a fetch-style input directory and return its path."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "diff.patch").write_text(diff)
    (input_dir / "metadata.json").write_text(json.dumps({"diff_prefix": diff_prefix}))
    return input_dir


_SIMPLE_STD_DIFF = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n"

_GITHUB_PR_DIFF = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "+import sys\n"
    "\n"
    " def main():\n"
)


class TestListMergeProposals:
    def test_no_proposals(self) -> None:
        client = FakeLaunchpadClient()

        result = list_merge_proposals(client, "myproject", "Needs review")

        assert result == []

    def test_proposal_without_review_comments(self) -> None:
        client = FakeLaunchpadClient()
        mp = make_mp()
        client.add_merge_proposal(mp)

        result = list_merge_proposals(client, "myproject", "Needs review")

        assert result == [
            MergeProposalSummary(url=mp.url, status="Needs review", last_reviewed=None)
        ]

    def test_proposal_with_review_comment(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)
        review_date = datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        client.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body="[maas-code-reviewer review]\n\nLooks good!",
                date=review_date,
            ),
        )

        result = list_merge_proposals(client, "myproject", "Needs review")

        assert result == [
            MergeProposalSummary(
                url=mp.url, status="Needs review", last_reviewed=review_date
            )
        ]

    def test_ignores_non_bot_comments_with_marker(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)
        client.add_comment(
            mp.api_url,
            Comment(
                author="human-user",
                body="[maas-code-reviewer review]\n\nFake review by human",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = list_merge_proposals(client, "myproject", "Needs review")

        assert result[0].last_reviewed is None

    def test_ignores_bot_comments_without_marker(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)
        client.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body="Just a regular comment, no marker",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = list_merge_proposals(client, "myproject", "Needs review")

        assert result[0].last_reviewed is None

    def test_uses_latest_review_date(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)
        early = datetime(2025, 6, 10, 8, 0, 0, tzinfo=UTC)
        late = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        client.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body="[maas-code-reviewer review]\n\nFirst review",
                date=early,
            ),
        )
        client.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body="[maas-code-reviewer review]\n\nSecond review",
                date=late,
            ),
        )

        result = list_merge_proposals(client, "myproject", "Needs review")

        assert result[0].last_reviewed == late

    def test_multiple_proposals_mixed_review_state(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp1 = make_mp(url="https://code.launchpad.net/~user/project/+git/repo/+merge/1")
        mp2 = make_mp(url="https://code.launchpad.net/~user/project/+git/repo/+merge/2")
        client.add_merge_proposal(mp1)
        client.add_merge_proposal(mp2)
        review_date = datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        client.add_comment(
            mp1.api_url,
            Comment(
                author="ci-bot",
                body="[maas-code-reviewer review]\n\nOK",
                date=review_date,
            ),
        )

        result = list_merge_proposals(client, "myproject", "Needs review")

        assert len(result) == 2
        assert result[0].last_reviewed == review_date
        assert result[1].last_reviewed is None

    def test_marker_must_be_at_start_of_body(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)
        client.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body="Some preamble [maas-code-reviewer review]\n\nContent",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = list_merge_proposals(client, "myproject", "Needs review")

        assert result[0].last_reviewed is None


class TestFormatMergeProposals:
    def test_empty_list(self) -> None:
        assert format_merge_proposals([]) == ""

    def test_single_unreviewed(self) -> None:
        summaries = [
            MergeProposalSummary(
                url="https://code.launchpad.net/~user/project/+git/repo/+merge/1",
                status="Needs review",
                last_reviewed=None,
            )
        ]

        output = format_merge_proposals(summaries)

        assert output == (
            "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
            " Needs review never"
        )

    def test_single_reviewed(self) -> None:
        review_date = datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        summaries = [
            MergeProposalSummary(
                url="https://code.launchpad.net/~user/project/+git/repo/+merge/1",
                status="Needs review",
                last_reviewed=review_date,
            )
        ]

        output = format_merge_proposals(summaries)

        assert output == (
            "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
            f" Needs review {review_date.isoformat()}"
        )

    def test_multiple_proposals(self) -> None:
        review_date = datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        summaries = [
            MergeProposalSummary(
                url="https://code.launchpad.net/~user/project/+git/repo/+merge/1",
                status="Needs review",
                last_reviewed=review_date,
            ),
            MergeProposalSummary(
                url="https://code.launchpad.net/~user/project/+git/repo/+merge/2",
                status="Needs review",
                last_reviewed=None,
            ),
        ]

        output = format_merge_proposals(summaries)

        lines = output.split("\n")
        assert len(lines) == 2
        assert lines[0].endswith(review_date.isoformat())
        assert lines[1].endswith("never")


class TestLpRepoUrl:
    def test_prepends_git_base(self) -> None:
        result = _lp_repo_url("~user/project/+git/repo")
        assert result == "https://git.launchpad.net/~user/project/+git/repo"

    def test_plain_name(self) -> None:
        result = _lp_repo_url("myproject")
        assert result == "https://git.launchpad.net/myproject"


class TestRefToBranch:
    def test_strips_refs_heads_prefix(self) -> None:
        assert _ref_to_branch("refs/heads/feature") == "feature"

    def test_strips_refs_heads_with_slashes(self) -> None:
        assert _ref_to_branch("refs/heads/user/my-feature") == "user/my-feature"

    def test_returns_unchanged_without_prefix(self) -> None:
        assert _ref_to_branch("main") == "main"

    def test_returns_unchanged_for_partial_prefix(self) -> None:
        assert _ref_to_branch("refs/tags/v1.0") == "refs/tags/v1.0"


class TestHasExistingReview:
    def test_returns_false_when_no_comments(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)

        assert has_existing_review(client, mp) is False

    def test_returns_true_when_bot_review_exists(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)
        client.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body="[maas-code-reviewer review]\n\nLooks good!",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        assert has_existing_review(client, mp) is True

    def test_returns_false_for_non_bot_comment_with_marker(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)
        client.add_comment(
            mp.api_url,
            Comment(
                author="human-user",
                body="[maas-code-reviewer review]\n\nFake review",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        assert has_existing_review(client, mp) is False

    def test_returns_false_for_bot_comment_without_marker(self) -> None:
        client = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp()
        client.add_merge_proposal(mp)
        client.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body="Just a regular comment",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        assert has_existing_review(client, mp) is False


def _setup_repos(
    tmp_path: Path,
    git: FakeGitClient,
    *,
    target_files: dict[str, str] | None = None,
    source_files: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Create a target and source repo for review tests.

    Returns (target_repo_path, source_repo_path).
    The source repo is cloned from target, with a ``feature`` branch
    containing the source changes.
    """
    if target_files is None:
        target_files = {"file.txt": "original\n"}
    if source_files is None:
        source_files = {"file.txt": "modified\n"}

    target_repo = tmp_path / "target"
    git.create_repo(target_repo)
    git.add_commit(target_repo, target_files, message="init")

    source_repo = tmp_path / "source"
    git.clone(str(target_repo), source_repo, "main")
    git.create_branch(source_repo, "feature")
    git.checkout(source_repo, "feature")
    git.add_commit(source_repo, source_files, message="feature work")

    return target_repo, source_repo


class TestReviewMergeProposal:
    def test_returns_review_body(self, tmp_path: Path) -> None:
        """MP exists, not yet reviewed — review body is returned."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient([ScriptedResponse(text="LGTM, no issues found.")])

        result = review_merge_proposal(lp, git, llm, mp.url)

        assert result is not None
        assert result.startswith(REVIEW_MARKER)
        assert "LGTM, no issues found." in result
        # Posting is the caller's responsibility — nothing posted here
        assert lp.get_comments_for(mp.api_url) == []

    def test_already_reviewed_mp_returns_none(self, tmp_path: Path) -> None:
        """An MP with an existing review is skipped."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)
        lp.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body="[maas-code-reviewer review]\n\nPrevious review.",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        llm = FakeLLMClient()  # no responses needed — should not be called

        result = review_merge_proposal(lp, git, llm, mp.url)

        assert result is None
        # No new comment should have been posted
        comments = lp.get_comments_for(mp.api_url)
        assert len(comments) == 1  # only the pre-existing one

    def test_returns_review_body_without_posting(self, tmp_path: Path) -> None:
        """review_merge_proposal returns the body without posting a comment."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient([ScriptedResponse(text="Some review text.")])

        result = review_merge_proposal(lp, git, llm, mp.url)

        assert result is not None
        assert "Some review text." in result
        # Posting is the caller's responsibility — nothing posted here
        comments = lp.get_comments_for(mp.api_url)
        assert len(comments) == 0

    def test_diff_is_passed_to_llm(self, tmp_path: Path) -> None:
        """The diff content is included in the prompt sent to the LLM."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(
            tmp_path,
            git,
            target_files={"code.py": "def hello():\n    pass\n"},
            source_files={"code.py": "def hello():\n    print('hi')\n"},
        )

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient([ScriptedResponse(text="Nice change.")])

        review_merge_proposal(lp, git, llm, mp.url)

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "pass" in prompt
        assert "print" in prompt

    def test_description_is_passed_to_llm(self, tmp_path: Path) -> None:
        """The MP description is included in the prompt."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
            description="Fix the widget rendering bug",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient([ScriptedResponse(text="Looks good.")])

        review_merge_proposal(lp, git, llm, mp.url)

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "Fix the widget rendering bug" in prompt

    def test_commit_message_used_when_no_description(self, tmp_path: Path) -> None:
        """Falls back to commit_message when description is None."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
            description=None,
            commit_message="Refactor auth module",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient([ScriptedResponse(text="OK.")])

        review_merge_proposal(lp, git, llm, mp.url)

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "Refactor auth module" in prompt

    def test_tools_provided_to_llm_can_read_files(self, tmp_path: Path) -> None:
        """The read_file tool provided to the LLM can read from the repo."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(
            tmp_path,
            git,
            target_files={"AGENTS.md": "# Rules\nBe nice.\n", "file.txt": "a\n"},
            source_files={"file.txt": "b\n"},
        )

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        from tests.fake_llm import ToolCall

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text="Reviewed with context.",
                    tool_calls=[
                        ToolCall(name="read_file", args={"path": "AGENTS.md"}),
                    ],
                ),
            ]
        )

        result = review_merge_proposal(lp, git, llm, mp.url)

        assert result is not None
        assert "Reviewed with context." in result

    def test_read_file_tool_returns_error_for_missing_file(
        self, tmp_path: Path
    ) -> None:
        """The read_file tool returns an error string for nonexistent files."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text="File missing, moving on.",
                    tool_calls=[
                        ToolCall(
                            name="read_file",
                            args={"path": "nonexistent.py"},
                        ),
                    ],
                ),
            ]
        )

        result = review_merge_proposal(lp, git, llm, mp.url)

        assert result is not None
        assert "File missing, moving on." in result

    def test_tools_provided_to_llm_can_list_directory(self, tmp_path: Path) -> None:
        """The list_directory tool provided to the LLM can list repo contents."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(
            tmp_path,
            git,
            target_files={"src/main.py": "print('hi')\n", "src/utils.py": "pass\n"},
            source_files={"src/main.py": "print('hello')\n"},
        )

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text="Checked src directory.",
                    tool_calls=[
                        ToolCall(name="list_directory", args={"path": "src"}),
                    ],
                ),
            ]
        )

        result = review_merge_proposal(lp, git, llm, mp.url)

        assert result is not None
        assert "Checked src directory." in result

    def test_list_directory_tool_returns_error_for_missing_dir(
        self, tmp_path: Path
    ) -> None:
        """The list_directory tool returns an error for nonexistent directories."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text="Dir not found, continuing.",
                    tool_calls=[
                        ToolCall(
                            name="list_directory",
                            args={"path": "nonexistent"},
                        ),
                    ],
                ),
            ]
        )

        result = review_merge_proposal(lp, git, llm, mp.url)

        assert result is not None
        assert "Dir not found, continuing." in result

    def test_review_comment_has_correct_format(self, tmp_path: Path) -> None:
        """The posted comment starts with the marker, then blank line, then review."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient([ScriptedResponse(text="Review body here.")])

        result = review_merge_proposal(lp, git, llm, mp.url)

        expected = (
            f"[maas-code-reviewer review]\n\n{REVIEW_PREAMBLE}\n\nReview body here."
        )
        assert result == expected

    def test_populates_metrics(self, tmp_path: Path) -> None:
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text="OK.", tokens_input=10, tokens_output=20, tokens_thinking=30
                )
            ]
        )

        metrics = ReviewMetrics()
        result = review_merge_proposal(lp, git, llm, mp.url, metrics=metrics)

        assert result is not None
        assert metrics.model_name == "gemini-3-flash-preview"
        assert metrics.tokens_input == 10
        assert metrics.tokens_output == 20
        assert metrics.tokens_thinking == 30
        assert metrics.diff_lines > 0
        assert metrics.diff_size_bytes > 0


def _make_fetch_mp(
    lp: FakeLaunchpadClient,
    target_repo: Path,
    source_repo: Path,
    **kwargs,
):
    """Build an MP wired to the fixture repos and register it with *lp*."""
    mp = make_mp(
        source_git_repository=str(source_repo),
        source_git_path="refs/heads/feature",
        target_git_repository=str(target_repo),
        target_git_path="refs/heads/main",
        **kwargs,
    )
    lp.add_merge_proposal(mp)
    return mp


class TestFetchMergeProposal:
    def test_skip_writes_status_and_clones_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An already-reviewed MP writes only status.json — no git command runs."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(lp, target_repo, source_repo)
        lp.add_comment(
            mp.api_url,
            Comment(
                author="ci-bot",
                body=f"{REVIEW_MARKER}\n\nPrevious review.",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )
        calls, recording_run = _recording_run()
        monkeypatch.setattr("maas_code_reviewer.git.subprocess.run", recording_run)

        result = fetch_merge_proposal(lp, git, mp.url, tmp_path / "out")

        assert result is False
        status = json.loads((tmp_path / "out" / "status.json").read_text())
        assert status == {"status": "skipped", "reason": "already-reviewed"}
        assert not (tmp_path / "out" / "repo").exists()
        assert not (tmp_path / "out" / "diff.patch").exists()
        assert not (tmp_path / "out" / "description.md").exists()
        assert not (tmp_path / "out" / "metadata.json").exists()
        assert calls == []

    def test_fetch_writes_repo_diff_description_metadata(self, tmp_path: Path) -> None:
        """A fresh MP writes the merged tree, diff, description, and metadata."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(lp, target_repo, source_repo, description="Fix the widget")
        out_dir = tmp_path / "out"

        result = fetch_merge_proposal(lp, git, mp.url, out_dir)

        assert result is True
        assert (out_dir / "repo" / "file.txt").read_text() == "modified\n"
        assert "+modified" in (out_dir / "diff.patch").read_text()
        assert (out_dir / "description.md").read_text() == "Fix the widget"
        metadata = json.loads((out_dir / "metadata.json").read_text())
        assert metadata == {
            "mp_url": mp.url,
            "target_url": str(target_repo),
            "target_branch": "main",
            "source_url": str(source_repo),
            "source_branch": "feature",
            "base_sha": git.rev_parse(target_repo, "main"),
            "head_sha": git.rev_parse(source_repo, "feature"),
            "diff_prefix": None,
        }

    def test_creates_missing_output_dir_with_parents(self, tmp_path: Path) -> None:
        """A nonexistent output directory is created with parents."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(lp, target_repo, source_repo)
        out_dir = tmp_path / "deep" / "nested" / "out"

        fetch_merge_proposal(lp, git, mp.url, out_dir)

        assert (out_dir / "diff.patch").exists()
        assert (out_dir / "description.md").exists()
        assert (out_dir / "metadata.json").exists()
        assert (out_dir / "repo").exists()

    def test_description_falls_back_to_commit_message(self, tmp_path: Path) -> None:
        """description.md falls back to the commit message."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(
            lp, target_repo, source_repo, commit_message="Refactor auth"
        )

        fetch_merge_proposal(lp, git, mp.url, tmp_path / "out")

        assert (tmp_path / "out" / "description.md").read_text() == "Refactor auth"

    def test_missing_description_and_commit_message_write_empty_file(
        self, tmp_path: Path
    ) -> None:
        """Both being None leaves description.md empty."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(lp, target_repo, source_repo)

        fetch_merge_proposal(lp, git, mp.url, tmp_path / "out")

        assert (tmp_path / "out" / "description.md").read_text() == ""

    def test_no_change_merge_writes_empty_diff(self, tmp_path: Path) -> None:
        """An up-to-date merge produces an empty diff and equal SHAs."""
        git = FakeGitClient()
        target_repo, _source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(target_repo),
            source_git_path="refs/heads/main",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
        )
        lp.add_merge_proposal(mp)
        out_dir = tmp_path / "out"

        result = fetch_merge_proposal(lp, git, mp.url, out_dir)

        assert result is True
        assert (out_dir / "diff.patch").read_text() == ""
        metadata = json.loads((out_dir / "metadata.json").read_text())
        assert metadata["base_sha"] == metadata["head_sha"]

    def test_git_token_applies_header_auth_to_clone_and_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token becomes a one-shot http.extraHeader on clone and fetch."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(lp, target_repo, source_repo)
        calls, recording_run = _recording_run()
        monkeypatch.setattr("maas_code_reviewer.git.subprocess.run", recording_run)
        expected_header = "Authorization: Basic " + base64.b64encode(
            b"ci-bot:tok"
        ).decode("ascii")

        result = fetch_merge_proposal(
            lp, git, mp.url, tmp_path / "out", git_token="tok"
        )

        assert result is True
        assert calls[0][:4] == [
            "git",
            "-c",
            f"http.extraHeader={expected_header}",
            "clone",
        ]
        fetch_call = next(call for call in calls if "fetch" in call)
        fetch_index = fetch_call.index("fetch")
        assert fetch_call[:fetch_index] == [
            "git",
            "-c",
            f"http.extraHeader={expected_header}",
        ]
        assert all("credential.helper" not in call for call in calls)
        # The header is passed exactly twice (clone + source fetch) as one-shot
        # config, and no remote URL argument carries credentials.
        header_args = [arg for call in calls for arg in call if "extraHeader=" in arg]
        assert header_args == [f"http.extraHeader={expected_header}"] * 2
        remote_args = [
            arg
            for call in calls
            for arg in call
            if arg.startswith(("http://", "https://", "/"))
        ]
        assert all("@" not in arg for arg in remote_args)
        assert all("Authorization" not in arg for arg in remote_args)
        # Nothing credential-bearing is persisted in the clone's config.
        config_text = (tmp_path / "out" / "repo" / ".git" / "config").read_text()
        assert "extraHeader" not in config_text
        assert "Authorization" not in config_text

    def test_no_token_omits_header(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a token, no -c/extraHeader is passed and fetch succeeds."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(lp, target_repo, source_repo)
        calls, recording_run = _recording_run()
        monkeypatch.setattr("maas_code_reviewer.git.subprocess.run", recording_run)

        result = fetch_merge_proposal(lp, git, mp.url, tmp_path / "out")

        assert result is True
        assert all("-c" not in call and "extraHeader" not in call for call in calls)

    def test_records_prefix_from_git_config_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom src/dst prefixes are recorded in metadata.json."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(lp, target_repo, source_repo)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.srcPrefix")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "XX/")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", "diff.dstPrefix")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "YY/")
        out_dir = tmp_path / "out"

        fetch_merge_proposal(lp, git, mp.url, out_dir)

        diff_text = (out_dir / "diff.patch").read_text()
        assert "+++ YY/file.txt" in diff_text
        metadata = json.loads((out_dir / "metadata.json").read_text())
        assert metadata["diff_prefix"] == "YY/"

    def test_fetch_prefix_round_trips_into_publish_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The recorded prefix lets publish validate repository-keyed candidates."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = _make_fetch_mp(lp, target_repo, source_repo)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.srcPrefix")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "XX/")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", "diff.dstPrefix")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "YY/")
        out_dir = tmp_path / "out"

        fetch_merge_proposal(lp, git, mp.url, out_dir)

        repository_candidate = tmp_path / "review_repo.json"
        repository_candidate.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"file.txt": {"1": "good"}},
                }
            )
        )
        prefixed_candidate = tmp_path / "review_prefixed.json"
        prefixed_candidate.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"YY/file.txt": {"1": "good"}},
                }
            )
        )

        validated = load_validated_review(repository_candidate, out_dir)
        assert validated == {
            "general_comment": "LGTM",
            "inline_comments": {"file.txt": {"1": "good"}},
        }
        with pytest.raises(InvalidReviewError):
            load_validated_review(prefixed_candidate, out_dir)


class TestDetectDiffPrefix:
    def test_standard_diff_returns_none(self) -> None:
        assert _detect_diff_prefix(_SIMPLE_STD_DIFF) is None

    def test_new_file_header_returns_none(self) -> None:
        diff = (
            "diff --git a/new.py b/new.py\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1 @@\n"
            "+print()\n"
        )
        assert _detect_diff_prefix(diff) is None

    def test_deletion_first_skips_to_next_header(self) -> None:
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "diff --git a/f.txt b/f.txt\n"
            "--- a/f.txt\n"
            "+++ b/f.txt\n"
            "@@ -1 +1 @@\n"
            "-one\n"
            "+two\n"
        )
        assert _detect_diff_prefix(diff) is None

    def test_identical_paths_return_empty_prefix(self) -> None:
        diff = "diff --git f.txt f.txt\n--- f.txt\n+++ f.txt\n@@ -1 +1 @@\n-one\n+two\n"
        assert _detect_diff_prefix(diff) == ""

    def test_custom_dst_prefix_is_returned(self) -> None:
        diff = (
            "diff --git XX/g.txt YY/g.txt\n"
            "--- XX/g.txt\n"
            "+++ YY/g.txt\n"
            "@@ -1 +1 @@\n"
            "-one\n"
            "+two\n"
        )
        assert _detect_diff_prefix(diff) == "YY/"

    def test_prefix_free_paths_without_slash_return_empty_prefix(self) -> None:
        diff = (
            "diff --git old.txt new.txt\n"
            "--- old.txt\n"
            "+++ new.txt\n"
            "@@ -1 +1 @@\n"
            "-one\n"
            "+two\n"
        )
        assert _detect_diff_prefix(diff) == ""

    def test_quoted_standard_path_returns_none(self) -> None:
        diff = (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1 +1 @@\n"
            "-one\n"
            "+two\n"
        )
        assert _detect_diff_prefix(diff) is None

    def test_empty_diff_returns_none(self) -> None:
        assert _detect_diff_prefix("") is None
        rename_only = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 100%\n"
            "rename from old.py\n"
            "rename to new.py\n"
        )
        assert _detect_diff_prefix(rename_only) is None


class TestLoadValidatedReview:
    def test_valid_candidate_returns_document(self, tmp_path: Path) -> None:
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        candidate = {
            "general_comment": "LGTM",
            "inline_comments": {"f.txt": {"1": "good"}},
        }
        review_file.write_text(json.dumps(candidate))

        result = load_validated_review(review_file, input_dir)

        assert result == candidate

    def test_line_not_in_diff_raises(self, tmp_path: Path) -> None:
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text(
            json.dumps(
                {"general_comment": "LGTM", "inline_comments": {"f.txt": {"9": "off"}}}
            )
        )

        with pytest.raises(InvalidReviewError) as exc_info:
            load_validated_review(review_file, input_dir)

        assert "does not appear in the diff" in str(exc_info.value)

    def test_unknown_file_raises(self, tmp_path: Path) -> None:
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"nope.py": {"1": "gone"}},
                }
            )
        )

        with pytest.raises(InvalidReviewError) as exc_info:
            load_validated_review(review_file, input_dir)

        message = str(exc_info.value)
        assert "inline_comments" in message
        assert "does not appear in the diff" in message

    def test_unparseable_candidate_raises(self, tmp_path: Path) -> None:
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text("not json {")

        with pytest.raises(InvalidReviewError) as exc_info:
            load_validated_review(review_file, input_dir)

        assert "not valid JSON" in str(exc_info.value)

    def test_recorded_prefix_mismatch_raises(self, tmp_path: Path) -> None:
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF, "w/")
        review_file = tmp_path / "review.json"
        review_file.write_text(
            json.dumps(
                {"general_comment": "LGTM", "inline_comments": {"f.txt": {"1": "good"}}}
            )
        )

        with pytest.raises(InvalidReviewError) as exc_info:
            load_validated_review(review_file, input_dir)

        assert "--diff-prefix" in str(exc_info.value)

    def test_recorded_prefix_resolves_coordinates(self, tmp_path: Path) -> None:
        diff = "--- i/f.txt\n+++ w/f.txt\n@@ -1 +1 @@\n-one\n+two\n"
        input_dir = _write_publish_inputs(tmp_path, diff, "w/")
        valid_file = tmp_path / "valid.json"
        valid_file.write_text(
            json.dumps(
                {"general_comment": "LGTM", "inline_comments": {"f.txt": {"1": "good"}}}
            )
        )
        prefixed_file = tmp_path / "prefixed.json"
        prefixed_file.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"w/f.txt": {"1": "good"}},
                }
            )
        )

        result = load_validated_review(valid_file, input_dir)
        assert result["inline_comments"] == {"f.txt": {"1": "good"}}

        with pytest.raises(InvalidReviewError):
            load_validated_review(prefixed_file, input_dir)

    def test_missing_general_comment_raises(self, tmp_path: Path) -> None:
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text(json.dumps({"inline_comments": {}}))

        with pytest.raises(InvalidReviewError) as exc_info:
            load_validated_review(review_file, input_dir)

        assert "Missing required key" in str(exc_info.value)

    def test_empty_diff_document_validates(self, tmp_path: Path) -> None:
        input_dir = _write_publish_inputs(tmp_path, "")
        review_file = tmp_path / "review.json"
        candidate = {
            "general_comment": EMPTY_DIFF_GENERAL_COMMENT,
            "inline_comments": {},
        }
        review_file.write_text(json.dumps(candidate))

        result = load_validated_review(review_file, input_dir)

        assert result == candidate


class TestRenderMergeProposalComment:
    def test_renders_marker_preamble_general_and_sections(self) -> None:
        review = {
            "general_comment": "LGTM",
            "inline_comments": {"f.txt": {"1": "Good"}, "g.py": {"2": "Also good"}},
        }

        result = render_merge_proposal_comment(review)

        assert result == (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nLGTM\n\n"
            "### f.txt:1\n\nGood\n\n### g.py:2\n\nAlso good"
        )

    def test_marker_in_candidate_is_not_duplicated(self) -> None:
        review = {
            "general_comment": (
                f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nSaid it twice"
            ),
            "inline_comments": {},
        }

        result = render_merge_proposal_comment(review)

        assert result.startswith(REVIEW_MARKER)
        assert result.count(REVIEW_MARKER) == 1
        assert result.count(REVIEW_PREAMBLE.strip()) == 1
        assert "Said it twice" in result

    def test_no_inline_comments_renders_general_only(self) -> None:
        review = {"general_comment": "LGTM", "inline_comments": {}}

        result = render_merge_proposal_comment(review)

        assert result == f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nLGTM"
        assert "###" not in result

    def test_empty_diff_comment_is_decorated(self) -> None:
        review = {
            "general_comment": EMPTY_DIFF_GENERAL_COMMENT,
            "inline_comments": {},
        }

        result = render_merge_proposal_comment(review)

        assert result == (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\n{EMPTY_DIFF_GENERAL_COMMENT}"
        )


class TestReviewPullRequestDecoration:
    def test_decorates_general_comment_for_nonempty_diff(self, tmp_path: Path) -> None:
        github = FakeGitHubClient()
        github.add_pull_request(
            "owner", "repo", 42, diff=_SIMPLE_STD_DIFF, description="Add a line"
        )
        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=json.dumps(
                        {
                            "general_comment": "Looks good overall.",
                            "inline_comments": {},
                        }
                    )
                )
            ]
        )

        result = review_pull_request(
            github, llm, "owner", "repo", 42, repo_dir=tmp_path
        )

        assert result["general_comment"] == (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nLooks good overall."
        )

    def test_empty_diff_document_stays_undecorated(self, tmp_path: Path) -> None:
        github = FakeGitHubClient()
        github.add_pull_request("owner", "repo", 42, diff="")
        llm = FakeLLMClient()

        result = review_pull_request(
            github, llm, "owner", "repo", 42, repo_dir=tmp_path
        )

        assert result["general_comment"] == EMPTY_DIFF_GENERAL_COMMENT


class TestFetchPullRequest:
    def test_writes_diff_description_and_metadata(self, tmp_path: Path) -> None:
        github = FakeGitHubClient()
        github.add_pull_request(
            "owner",
            "repo",
            42,
            diff=_GITHUB_PR_DIFF,
            description="Fix the widget",
            base_sha="b" * 40,
            head_sha="h" * 40,
        )

        fetch_pull_request(
            github, "https://github.com/owner/repo/pull/42", tmp_path / "out"
        )

        out = tmp_path / "out"
        assert (out / "diff.patch").read_text() == _GITHUB_PR_DIFF
        assert (out / "description.md").read_text() == "Fix the widget"
        metadata = json.loads((out / "metadata.json").read_text())
        assert metadata == {
            "pr_url": "https://github.com/owner/repo/pull/42",
            "owner": "owner",
            "repo": "repo",
            "number": 42,
            "base_sha": "b" * 40,
            "head_sha": "h" * 40,
            "diff_prefix": None,
        }
        assert (tmp_path / "out" / "repo").exists() is False

    def test_creates_missing_output_dir_with_parents(self, tmp_path: Path) -> None:
        github = FakeGitHubClient()
        github.add_pull_request("owner", "repo", 42, diff=_GITHUB_PR_DIFF)

        fetch_pull_request(
            github, "https://github.com/owner/repo/pull/42", tmp_path / "deep" / "out"
        )

        out = tmp_path / "deep" / "out"
        assert (out / "diff.patch").exists()
        assert (out / "description.md").exists()
        assert (out / "metadata.json").exists()

    def test_none_description_writes_empty_file(self, tmp_path: Path) -> None:
        github = FakeGitHubClient()
        github.add_pull_request("owner", "repo", 42, diff=_GITHUB_PR_DIFF)

        fetch_pull_request(
            github, "https://github.com/owner/repo/pull/42", tmp_path / "out"
        )

        assert (tmp_path / "out" / "description.md").read_text() == ""

    def test_records_none_prefix_for_github_diff_shapes(self, tmp_path: Path) -> None:
        diff = (
            "diff --git a/new.py b/new.py\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+new\n"
            "diff --git a/changed.py b/changed.py\n"
            "--- a/changed.py\n"
            "+++ b/changed.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+changed\n"
            "diff --git a/gone.py b/gone.py\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-gone\n"
        )
        github = FakeGitHubClient()
        github.add_pull_request("owner", "repo", 42, diff=diff)

        fetch_pull_request(
            github, "https://github.com/owner/repo/pull/42", tmp_path / "out"
        )

        metadata = json.loads((tmp_path / "out" / "metadata.json").read_text())
        assert metadata["diff_prefix"] is None

    def test_invalid_pr_url_raises_value_error(self, tmp_path: Path) -> None:
        github = FakeGitHubClient()

        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            fetch_pull_request(
                github, "https://github.com/owner/repo", tmp_path / "out"
            )

        assert not (tmp_path / "out").exists()


class TestRenderPullRequestReview:
    def test_decorates_general_comment_and_preserves_findings(self) -> None:
        doc = {
            "general_comment": "Looks good overall.",
            "inline_comments": {"src/foo.py": {"2": "Nice import."}},
        }

        rendered = render_pull_request_review(doc)

        assert rendered["general_comment"] == (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nLooks good overall."
        )
        assert rendered["inline_comments"] == {"src/foo.py": {"2": "Nice import."}}
        assert doc["general_comment"] == "Looks good overall."

    def test_marker_in_candidate_is_not_duplicated(self) -> None:
        doc = {
            "general_comment": (
                f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nSaid it twice"
            ),
            "inline_comments": {},
        }

        rendered = render_pull_request_review(doc)

        assert rendered["general_comment"].startswith(REVIEW_MARKER)
        assert rendered["general_comment"].count(REVIEW_MARKER) == 1
        assert rendered["general_comment"].count(REVIEW_PREAMBLE.strip()) == 1
        assert "Said it twice" in rendered["general_comment"]

    def test_empty_diff_comment_is_decorated(self) -> None:
        doc = {"general_comment": EMPTY_DIFF_GENERAL_COMMENT, "inline_comments": {}}

        rendered = render_pull_request_review(doc)

        assert rendered["general_comment"] == (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\n{EMPTY_DIFF_GENERAL_COMMENT}"
        )

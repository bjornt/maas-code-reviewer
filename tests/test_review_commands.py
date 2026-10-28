# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from datetime import UTC, datetime
from pathlib import Path

from maas_code_reviewer.metrics import ReviewMetrics
from maas_code_reviewer.models import Comment
from maas_code_reviewer.review_commands import (
    MergeProposalSummary,
    _lp_repo_url,
    _ref_to_branch,
    format_merge_proposals,
    has_existing_review,
    list_merge_proposals,
    review_merge_proposal,
)
from maas_code_reviewer.reviewer import REVIEW_MARKER, REVIEW_PREAMBLE
from tests.factory import make_mp
from tests.fake_git import FakeGitClient
from tests.fake_launchpad import FakeLaunchpadClient
from tests.fake_llm import FakeLLMClient, ScriptedResponse, ToolCall


def _prompt_text(parts: list) -> str:
    """Join Part/str prompt elements into a single string for assertions."""
    return "".join(getattr(p, "text", p) for p in parts)


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

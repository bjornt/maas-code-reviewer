# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from maas_code_reviewer.cli import (
    _build_parser,
    _get_gemini_api_key,
    handle_fetch_mp,
    handle_fetch_pr,
    handle_list_lp_mps,
    handle_publish_mp,
    handle_publish_pr,
    handle_review,
    handle_review_diff,
    handle_review_mp,
    handle_review_pr,
    main,
)
from maas_code_reviewer.llm_client import DEFAULT_MAX_TOOL_CALLS
from maas_code_reviewer.models import Comment
from maas_code_reviewer.review_commands import InvalidReviewError
from maas_code_reviewer.reviewer import (
    EMPTY_DIFF_GENERAL_COMMENT,
    REVIEW_MARKER,
    REVIEW_PREAMBLE,
)
from tests.factory import make_mp
from tests.fake_git import FakeGitClient
from tests.fake_github import FakeGitHubClient
from tests.fake_launchpad import FakeLaunchpadClient
from tests.fake_launchpadlib import FakeLaunchpad, make_fake_comment, make_fake_mp
from tests.fake_llm import FakeLLMClient, ScriptedResponse, ToolCall


def _prompt_text(parts: list) -> str:
    """Join Part/str prompt elements into a single string for assertions."""
    return "".join(getattr(p, "text", p) for p in parts)


class TestBuildParser:
    def test_list_lp_mps_defaults_status_to_needs_review(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["list-lp-mps", "myproject"])
        assert args.command == "list-lp-mps"
        assert args.status == "Needs review"
        assert args.project == "myproject"
        assert args.launchpad_credentials is None

    def test_list_lp_mps_parses_explicit_status(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["list-lp-mps", "--status", "Approved", "myproject"])
        assert args.command == "list-lp-mps"
        assert args.status == "Approved"
        assert args.project == "myproject"

    def test_list_lp_mps_parses_credentials(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "list-lp-mps",
                "--launchpad-credentials",
                "/path/to/creds",
                "--status",
                "Approved",
                "myproject",
            ]
        )
        assert args.launchpad_credentials == "/path/to/creds"

    def test_no_subcommand_gives_none(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.command is None


class TestBuildParserFetchMp:
    def test_parses_output_dir_and_url(self) -> None:
        parser = _build_parser()
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        args = parser.parse_args(["fetch-mp", "--output-dir", "out", url])
        assert args.command == "fetch-mp"
        assert args.output_dir == "out"
        assert args.mp_url == url
        assert args.launchpad_credentials is None

    def test_missing_output_dir_exits(self) -> None:
        parser = _build_parser()
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        with pytest.raises(SystemExit):
            parser.parse_args(["fetch-mp", url])


class TestBuildParserPublishMp:
    def test_parses_review_input_dir_and_url(self) -> None:
        parser = _build_parser()
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        args = parser.parse_args(
            ["publish-mp", "--review", "r.json", "--input-dir", "in", url]
        )
        assert args.command == "publish-mp"
        assert args.review == "r.json"
        assert args.input_dir == "in"
        assert args.mp_url == url
        assert args.dry_run is False
        assert args.launchpad_credentials is None

    def test_parses_dry_run(self) -> None:
        parser = _build_parser()
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        args = parser.parse_args(
            [
                "publish-mp",
                "--review",
                "r.json",
                "--input-dir",
                "in",
                "--dry-run",
                url,
            ]
        )
        assert args.dry_run is True

    def test_missing_review_exits(self) -> None:
        parser = _build_parser()
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        with pytest.raises(SystemExit):
            parser.parse_args(["publish-mp", "--input-dir", "in", url])

    def test_missing_input_dir_exits(self) -> None:
        parser = _build_parser()
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        with pytest.raises(SystemExit):
            parser.parse_args(["publish-mp", "--review", "r.json", url])

    def test_review_mp_parses_mp_url(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "-g",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.command == "review-mp"
        assert (
            args.mp_url == "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        )
        assert args.dry_run is False
        assert args.launchpad_credentials is None
        assert args.gemini_api_key_file == str(key_file)
        assert args.model == "gemini-3-flash-preview"

    def test_review_mp_parses_dry_run(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "--dry-run",
                "-g",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.dry_run is True

    def test_review_mp_parses_launchpad_credentials(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "--launchpad-credentials",
                "/path/to/creds",
                "-g",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.launchpad_credentials == "/path/to/creds"

    def test_review_mp_parses_gemini_api_key_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "gemini.key"
        key_file.write_text("my-secret-key\n")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "--gemini-api-key-file",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.gemini_api_key_file == str(key_file)

    def test_review_mp_model_defaults_to_gemini_3_flash_preview(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "-g",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.model == "gemini-3-flash-preview"

    def test_review_mp_parses_model(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "--model",
                "gemini-2.5-pro",
                "-g",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.model == "gemini-2.5-pro"

    def test_review_mp_metrics_defaults_to_none(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "-g",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.metrics is None

    def test_review_mp_parses_metrics(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "-g",
                str(key_file),
                "--metrics",
                "/tmp/m.json",
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.metrics == "/tmp/m.json"

    def test_review_mp_max_diff_chars_defaults_to_200000(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "-g",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.max_diff_chars == 200_000

    def test_review_mp_parses_max_diff_chars(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "-g",
                str(key_file),
                "--max-diff-chars",
                "50000",
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.max_diff_chars == 50_000

    def test_review_mp_max_tool_calls_defaults(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "-g",
                str(key_file),
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.max_tool_calls == DEFAULT_MAX_TOOL_CALLS

    def test_review_mp_parses_max_tool_calls(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-mp",
                "-g",
                str(key_file),
                "--max-tool-calls",
                "50",
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )
        assert args.max_tool_calls == 50


class TestBuildParserFetchPr:
    def test_parses_output_dir_and_url(self) -> None:
        parser = _build_parser()
        url = "https://github.com/owner/repo/pull/42"
        args = parser.parse_args(["fetch-pr", "--output-dir", "out", url])
        assert args.command == "fetch-pr"
        assert args.output_dir == "out"
        assert args.pr_url == url
        assert args.github_token is None

    def test_missing_output_dir_exits(self) -> None:
        parser = _build_parser()
        url = "https://github.com/owner/repo/pull/42"
        with pytest.raises(SystemExit):
            parser.parse_args(["fetch-pr", url])


class TestBuildParserPublishPr:
    def test_parses_review_input_dir_and_url(self) -> None:
        parser = _build_parser()
        url = "https://github.com/owner/repo/pull/42"
        args = parser.parse_args(
            ["publish-pr", "--review", "r.json", "--input-dir", "in", url]
        )
        assert args.command == "publish-pr"
        assert args.review == "r.json"
        assert args.input_dir == "in"
        assert args.pr_url == url
        assert args.dry_run is False
        assert args.github_token is None

    def test_parses_dry_run(self) -> None:
        parser = _build_parser()
        url = "https://github.com/owner/repo/pull/42"
        args = parser.parse_args(
            [
                "publish-pr",
                "--review",
                "r.json",
                "--input-dir",
                "in",
                "--dry-run",
                url,
            ]
        )
        assert args.dry_run is True

    def test_missing_review_exits(self) -> None:
        parser = _build_parser()
        url = "https://github.com/owner/repo/pull/42"
        with pytest.raises(SystemExit):
            parser.parse_args(["publish-pr", "--input-dir", "in", url])

    def test_missing_input_dir_exits(self) -> None:
        parser = _build_parser()
        url = "https://github.com/owner/repo/pull/42"
        with pytest.raises(SystemExit):
            parser.parse_args(["publish-pr", "--review", "r.json", url])


class TestMain:
    def test_no_command_exits_with_code_1(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1

    def test_no_review_text_exits_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n")
        api_key_file = tmp_path / "key"
        api_key_file.write_text("fake-key")

        no_text = ScriptedResponse(
            text=None,
            pending_function_call=ToolCall(
                name="list_directory", args={"path": "src"}
            ),
        )
        llm = FakeLLMClient([no_text, no_text, no_text, no_text])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "review-diff",
                        "-g",
                        str(api_key_file),
                        str(diff_file),
                    ]
                )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "no review text" in captured.err

    def test_list_lp_mps_delegates_to_handle_list_lp_mps(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_lp = FakeLaunchpad(bot_username="review-bot")
        lp_mp = make_fake_mp(status="Needs review")
        fake_lp.add_merge_proposal("myproject", lp_mp)
        with fake_lp.patch_login_with():
            main(["list-lp-mps", "--status", "Needs review", "myproject"])
        captured = capsys.readouterr()
        assert lp_mp.web_link in captured.out

    def test_review_mp_command_delegates_to_handle_review_mp(
        self, tmp_path: Path
    ) -> None:
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

        llm = FakeLLMClient([ScriptedResponse(text="Looks great.")])

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
        ):
            main(
                [
                    "review-mp",
                    "--gemini-api-key-file",
                    str(api_key_file),
                    mp.url,
                ]
            )

        comments = lp.get_comments_for(mp.api_url)
        assert len(comments) == 1
        assert "Looks great." in comments[0].body

    def test_fetch_mp_command_delegates_to_handle_fetch_mp(
        self, tmp_path: Path
    ) -> None:
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

        out_dir = tmp_path / "out"

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
        ):
            main(["fetch-mp", "--output-dir", str(out_dir), mp.url])

        assert (out_dir / "diff.patch").exists()
        assert (out_dir / "description.md").exists()
        assert (out_dir / "metadata.json").exists()
        assert (out_dir / "repo").exists()

    def test_publish_mp_command_delegates_to_handle_publish_mp(
        self, tmp_path: Path
    ) -> None:
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"f.txt": {"1": "Good"}},
                }
            )
        )
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(url=url)
        lp.add_merge_proposal(mp)

        with patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp):
            main(
                [
                    "publish-mp",
                    "--review",
                    str(review_file),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )

        comments = lp.get_comments_for(mp.api_url)
        assert len(comments) == 1
        assert comments[0].body.startswith(REVIEW_MARKER)

    def test_invalid_review_exits_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"f.txt": {"9": "off"}},
                }
            )
        )
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "publish-mp",
                    "--review",
                    str(review_file),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Invalid review document" in captured.err

    def test_fetch_pr_command_delegates_to_handle_fetch_pr(
        self, tmp_path: Path
    ) -> None:
        github_client = FakeGitHubClient()
        github_client.add_pull_request(
            "owner",
            "repo",
            42,
            diff=_PR_DIFF,
            description="Add sys import",
            base_sha="b" * 40,
            head_sha="h" * 40,
        )
        out = tmp_path / "out"
        url = "https://github.com/owner/repo/pull/42"

        with patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client):
            main(
                [
                    "fetch-pr",
                    "--github-token",
                    "ghp_test",
                    "--output-dir",
                    str(out),
                    url,
                ]
            )

        assert (out / "diff.patch").read_text() == _PR_DIFF
        assert (out / "description.md").read_text() == "Add sys import"
        metadata = json.loads((out / "metadata.json").read_text())
        assert metadata == {
            "pr_url": url,
            "owner": "owner",
            "repo": "repo",
            "number": 42,
            "base_sha": "b" * 40,
            "head_sha": "h" * 40,
            "diff_prefix": None,
        }

    def test_publish_pr_command_delegates_to_handle_publish_pr(
        self, tmp_path: Path
    ) -> None:
        input_dir, review_path = _write_pr_publish_inputs(
            tmp_path, json.loads(_PR_REVIEW_RESPONSE)
        )
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        url = "https://github.com/owner/repo/pull/42"

        with patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client):
            main(
                [
                    "publish-pr",
                    "--github-token",
                    "ghp_test",
                    "--review",
                    str(review_path),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )

        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        assert len(reviews) == 1
        assert reviews[0]["body"].startswith(REVIEW_MARKER)

    def test_publish_pr_invalid_review_exits_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        review = {
            "general_comment": "LGTM",
            "inline_comments": {"src/foo.py": {"9": "off"}},
        }
        input_dir, review_path = _write_pr_publish_inputs(tmp_path, review)
        url = "https://github.com/owner/repo/pull/42"

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "publish-pr",
                    "--review",
                    str(review_path),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Invalid review document" in captured.err


class TestHandleListLpMps:
    def test_prints_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        fake_lp = FakeLaunchpad(bot_username="review-bot")
        lp_mp = make_fake_mp(status="Needs review")
        fake_lp.add_merge_proposal("myproject", lp_mp)
        with fake_lp.patch_login_with():
            args = _build_parser().parse_args(
                ["list-lp-mps", "--status", "Needs review", "myproject"]
            )
            handle_list_lp_mps(args)
        captured = capsys.readouterr()
        assert lp_mp.web_link in captured.out
        assert "Needs review" in captured.out
        assert "never" in captured.out

    def test_no_output_when_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        fake_lp = FakeLaunchpad()
        fake_lp.add_project("myproject")
        with fake_lp.patch_login_with():
            args = _build_parser().parse_args(
                ["list-lp-mps", "--status", "Needs review", "myproject"]
            )
            handle_list_lp_mps(args)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_passes_credentials(self) -> None:
        fake_lp = FakeLaunchpad()
        fake_lp.add_project("myproject")
        with fake_lp.patch_login_with():
            args = _build_parser().parse_args(
                [
                    "list-lp-mps",
                    "--launchpad-credentials",
                    "/path/to/creds",
                    "--status",
                    "Needs review",
                    "myproject",
                ]
            )
            handle_list_lp_mps(args)
        assert fake_lp.credentials_file == "/path/to/creds"

    def test_shows_last_review_date(self, capsys: pytest.CaptureFixture[str]) -> None:
        fake_lp = FakeLaunchpad(bot_username="ci-bot")
        lp_mp = make_fake_mp(status="Needs review")
        fake_lp.add_merge_proposal("myproject", lp_mp)
        review_date = datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        fake_lp.add_comment(
            lp_mp.web_link,
            make_fake_comment(
                author="ci-bot",
                body="[maas-code-reviewer review]\n\nLooks good!",
                date=review_date,
            ),
        )
        with fake_lp.patch_login_with():
            args = _build_parser().parse_args(
                ["list-lp-mps", "--status", "Needs review", "myproject"]
            )
            handle_list_lp_mps(args)
        captured = capsys.readouterr()
        assert lp_mp.web_link in captured.out
        assert review_date.isoformat() in captured.out


class TestHandleReviewMp:
    def test_posts_review_comment(self, tmp_path: Path) -> None:
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

        llm = FakeLLMClient([ScriptedResponse(text="Looks great.")])

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
        ):
            args = _build_parser().parse_args(
                ["review-mp", "--gemini-api-key-file", str(api_key_file), mp.url]
            )
            handle_review_mp(args)

        comments = lp.get_comments_for(mp.api_url)
        assert len(comments) == 1
        assert "Looks great." in comments[0].body

    def test_prints_already_reviewed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
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
                body="[maas-code-reviewer review]\n\nAlready done.",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        llm = FakeLLMClient()

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
        ):
            args = _build_parser().parse_args(
                ["review-mp", "--gemini-api-key-file", str(api_key_file), mp.url]
            )
            handle_review_mp(args)

        captured = capsys.readouterr()
        assert "Already reviewed, skipping." in captured.out

    def test_dry_run_prints_review_without_posting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
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

        llm = FakeLLMClient([ScriptedResponse(text="Dry run review.")])

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
        ):
            args = _build_parser().parse_args(
                [
                    "review-mp",
                    "--dry-run",
                    "--gemini-api-key-file",
                    str(api_key_file),
                    mp.url,
                ]
            )
            handle_review_mp(args)

        captured = capsys.readouterr()
        assert "Dry run review." in captured.out
        assert len(lp.get_comments_for(mp.api_url)) == 0

    def test_missing_gemini_api_key_exits_with_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When no Gemini API key is available, the command exits with an error."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        args = _build_parser().parse_args(
            [
                "review-mp",
                "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
            ]
        )

        with pytest.raises(SystemExit) as exc_info:
            handle_review_mp(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GEMINI_API_KEY" in captured.err


def _write_publish_inputs(
    tmp_path: Path, diff: str, diff_prefix: str | None = None
) -> Path:
    """Write a fetch-style input directory and return its path."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "diff.patch").write_text(diff)
    (input_dir / "metadata.json").write_text(
        json.dumps({"diff_prefix": diff_prefix})
    )
    return input_dir


_SIMPLE_STD_DIFF = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n"


def _recording_run() -> tuple[list[list[str]], object]:
    """Return a call list and a delegating subprocess.run recorder.

    The recorder calls the real ``subprocess.run`` and records each
    command's argv, so assertions can inspect the git invocations.
    """
    import subprocess

    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        calls.append(list(args))
        return real_run(args, **kwargs)

    return calls, recording_run


class TestHandleFetchMp:
    def test_writes_fetch_outputs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The handler writes diff.patch, description.md, metadata.json, repo/."""
        git = FakeGitClient()
        target_repo, source_repo = _setup_repos(tmp_path, git)

        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(
            source_git_repository=str(source_repo),
            source_git_path="refs/heads/feature",
            target_git_repository=str(target_repo),
            target_git_path="refs/heads/main",
            description="Fix the widget",
        )
        lp.add_merge_proposal(mp)
        out_dir = tmp_path / "out"

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
        ):
            args = _build_parser().parse_args(
                ["fetch-mp", "--output-dir", str(out_dir), mp.url]
            )
            handle_fetch_mp(args)

        assert (out_dir / "diff.patch").exists()
        assert (out_dir / "description.md").read_text() == "Fix the widget"
        assert (out_dir / "metadata.json").exists()
        assert (out_dir / "repo").exists()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_prints_already_reviewed_and_writes_status(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An already-reviewed MP prints the skip message and writes status.json."""
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
                body=f"{REVIEW_MARKER}\n\nAlready done.",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )
        out_dir = tmp_path / "out"

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
        ):
            args = _build_parser().parse_args(
                ["fetch-mp", "--output-dir", str(out_dir), mp.url]
            )
            handle_fetch_mp(args)

        captured = capsys.readouterr()
        assert "Already reviewed, skipping." in captured.out
        assert (out_dir / "status.json").exists()
        assert not (out_dir / "repo").exists()

    def test_reads_lp_git_token_from_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LP_GIT_TOKEN becomes an Authorization header on the clone."""
        import base64

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
        calls, recording_run = _recording_run()
        monkeypatch.setattr(
            "maas_code_reviewer.git.subprocess.run", recording_run
        )
        monkeypatch.setenv("LP_GIT_TOKEN", "tok")
        expected_header = (
            "Authorization: Basic "
            + base64.b64encode(b"ci-bot:tok").decode("ascii")
        )

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
        ):
            args = _build_parser().parse_args(
                [
                    "fetch-mp",
                    "--output-dir",
                    str(tmp_path / "out"),
                    mp.url,
                ]
            )
            handle_fetch_mp(args)

        assert calls[0][:4] == [
            "git",
            "-c",
            f"http.extraHeader={expected_header}",
            "clone",
        ]


class TestHandlePublishMp:
    def test_posts_rendered_comment(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A valid candidate is rendered, decorated, and posted."""
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"f.txt": {"1": "Good"}},
                }
            )
        )
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(url=url)
        lp.add_merge_proposal(mp)

        with patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp):
            args = _build_parser().parse_args(
                [
                    "publish-mp",
                    "--review",
                    str(review_file),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )
            handle_publish_mp(args)

        comments = lp.get_comments_for(mp.api_url)
        assert len(comments) == 1
        assert comments[0].body.startswith(REVIEW_MARKER)
        assert "LGTM" in comments[0].body
        assert "### f.txt:1" in comments[0].body
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_dry_run_prints_comment_without_posting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--dry-run prints the rendered comment and never constructs a client."""
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"f.txt": {"1": "Good"}},
                }
            )
        )
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(url=url)
        lp.add_merge_proposal(mp)

        with patch(
            "maas_code_reviewer.cli.LaunchpadClient",
            side_effect=AssertionError("must not construct"),
        ):
            args = _build_parser().parse_args(
                [
                    "publish-mp",
                    "--review",
                    str(review_file),
                    "--input-dir",
                    str(input_dir),
                    "--dry-run",
                    url,
                ]
            )
            handle_publish_mp(args)

        captured = capsys.readouterr()
        assert captured.out.startswith(REVIEW_MARKER)
        assert "LGTM" in captured.out
        assert "### f.txt:1" in captured.out
        assert lp.get_comments_for(mp.api_url) == []

    def test_invalid_candidate_raises_without_posting(self, tmp_path: Path) -> None:
        """An invalid candidate raises InvalidReviewError before any client use."""
        input_dir = _write_publish_inputs(tmp_path, _SIMPLE_STD_DIFF)
        review_file = tmp_path / "review.json"
        review_file.write_text(
            json.dumps(
                {
                    "general_comment": "LGTM",
                    "inline_comments": {"f.txt": {"9": "off"}},
                }
            )
        )
        url = "https://code.launchpad.net/~user/project/+git/repo/+merge/1"
        lp = FakeLaunchpadClient(bot_username="ci-bot")
        mp = make_mp(url=url)
        lp.add_merge_proposal(mp)

        with patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp):
            args = _build_parser().parse_args(
                [
                    "publish-mp",
                    "--review",
                    str(review_file),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )
            with pytest.raises(InvalidReviewError):
                handle_publish_mp(args)

        assert lp.get_comments_for(mp.api_url) == []


class TestHandleFetchPr:
    def test_writes_fetch_outputs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        github_client = FakeGitHubClient()
        github_client.add_pull_request(
            "owner",
            "repo",
            42,
            diff=_PR_DIFF,
            description="Add sys import",
            base_sha="b" * 40,
            head_sha="h" * 40,
        )
        out = tmp_path / "out"
        url = "https://github.com/owner/repo/pull/42"

        with patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client):
            args = _build_parser().parse_args(
                [
                    "fetch-pr",
                    "--github-token",
                    "ghp_test",
                    "--output-dir",
                    str(out),
                    url,
                ]
            )
            handle_fetch_pr(args)

        assert (out / "diff.patch").read_text() == _PR_DIFF
        assert (out / "description.md").read_text() == "Add sys import"
        metadata = json.loads((out / "metadata.json").read_text())
        assert metadata == {
            "pr_url": url,
            "owner": "owner",
            "repo": "repo",
            "number": 42,
            "base_sha": "b" * 40,
            "head_sha": "h" * 40,
            "diff_prefix": None,
        }
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_token_read_from_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        out = tmp_path / "out"
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        url = "https://github.com/owner/repo/pull/42"

        captured_tokens: list[str] = []

        def fake_github_client(token: str) -> FakeGitHubClient:
            captured_tokens.append(token)
            return github_client

        with patch(
            "maas_code_reviewer.cli.GitHubClient", side_effect=fake_github_client
        ):
            args = _build_parser().parse_args(
                [
                    "fetch-pr",
                    "--output-dir",
                    str(out),
                    url,
                ]
            )
            handle_fetch_pr(args)

        assert captured_tokens == ["env-token"]

    def test_missing_token_exits_with_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        out = tmp_path / "out"
        url = "https://github.com/owner/repo/pull/42"
        args = _build_parser().parse_args(
            [
                "fetch-pr",
                "--output-dir",
                str(out),
                url,
            ]
        )

        with pytest.raises(SystemExit) as exc_info:
            handle_fetch_pr(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GITHUB_TOKEN" in captured.err


class TestHandlePublishPr:
    def test_posts_validated_and_decorated_review(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        input_dir, review_path = _write_pr_publish_inputs(
            tmp_path, json.loads(_PR_REVIEW_RESPONSE)
        )
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        url = "https://github.com/owner/repo/pull/42"

        with patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client):
            args = _build_parser().parse_args(
                [
                    "publish-pr",
                    "--github-token",
                    "ghp_test",
                    "--review",
                    str(review_path),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )
            handle_publish_pr(args)

        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        assert len(reviews) == 1
        assert reviews[0]["body"] == (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nLooks good overall."
        )
        assert reviews[0]["comments"] == [
            {"path": "src/foo.py", "line": 2, "body": "Nice import."}
        ]
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_dry_run_prints_json_without_posting(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        input_dir, review_path = _write_pr_publish_inputs(
            tmp_path, json.loads(_PR_REVIEW_RESPONSE)
        )
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        url = "https://github.com/owner/repo/pull/42"
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)

        with patch(
            "maas_code_reviewer.cli.GitHubClient",
            side_effect=AssertionError("must not construct"),
        ):
            args = _build_parser().parse_args(
                [
                    "publish-pr",
                    "--review",
                    str(review_path),
                    "--input-dir",
                    str(input_dir),
                    "--dry-run",
                    url,
                ]
            )
            handle_publish_pr(args)

        expected = json.loads(_PR_REVIEW_RESPONSE)
        expected["general_comment"] = (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nLooks good overall."
        )
        captured = capsys.readouterr()
        assert json.loads(captured.out) == expected
        assert github_client.get_posted_reviews("owner", "repo", 42) == []

    def test_invalid_candidate_raises_without_posting(self, tmp_path: Path) -> None:
        review = {
            "general_comment": "LGTM",
            "inline_comments": {"src/foo.py": {"9": "off"}},
        }
        input_dir, review_path = _write_pr_publish_inputs(tmp_path, review)
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        url = "https://github.com/owner/repo/pull/42"

        with patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client):
            args = _build_parser().parse_args(
                [
                    "publish-pr",
                    "--github-token",
                    "ghp_test",
                    "--review",
                    str(review_path),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )
            with pytest.raises(InvalidReviewError):
                handle_publish_pr(args)

        assert github_client.get_posted_reviews("owner", "repo", 42) == []

    def test_missing_token_exits_without_posting(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        input_dir, review_path = _write_pr_publish_inputs(
            tmp_path, json.loads(_PR_REVIEW_RESPONSE)
        )
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        url = "https://github.com/owner/repo/pull/42"

        with patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client):
            args = _build_parser().parse_args(
                [
                    "publish-pr",
                    "--review",
                    str(review_path),
                    "--input-dir",
                    str(input_dir),
                    url,
                ]
            )
            with pytest.raises(SystemExit) as exc_info:
                handle_publish_pr(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GITHUB_TOKEN" in captured.err
        assert github_client.get_posted_reviews("owner", "repo", 42) == []


class TestGetGeminiApiKey:
    def _make_args(
        self, gemini_api_key_file: str | None = None
    ) -> argparse.Namespace:
        return argparse.Namespace(gemini_api_key_file=gemini_api_key_file)

    def test_reads_key_from_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("my-secret-key\n")
        assert _get_gemini_api_key(self._make_args(str(key_file))) == "my-secret-key"

    def test_strips_whitespace_from_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("   my-key   \n\n")
        assert _get_gemini_api_key(self._make_args(str(key_file))) == "my-key"

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("")
        assert _get_gemini_api_key(self._make_args(str(key_file))) is None

    def test_whitespace_only_file_returns_none(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("   \n  \t\n")
        assert _get_gemini_api_key(self._make_args(str(key_file))) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert _get_gemini_api_key(self._make_args(str(missing))) is None

    def test_reads_from_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        assert _get_gemini_api_key(self._make_args()) == "env-key"

    def test_strips_whitespace_from_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "  env-key\n")
        assert _get_gemini_api_key(self._make_args()) == "env-key"

    def test_empty_env_var_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "")
        assert _get_gemini_api_key(self._make_args()) is None

    def test_whitespace_only_env_var_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "   \n")
        assert _get_gemini_api_key(self._make_args()) is None

    def test_no_file_no_env_var_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert _get_gemini_api_key(self._make_args()) is None

    def test_file_takes_precedence_over_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        key_file = tmp_path / "key"
        key_file.write_text("file-key")
        assert _get_gemini_api_key(self._make_args(str(key_file))) == "file-key"


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



class TestBuildParserReviewDiff:
    def test_review_diff_parses_diff_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("--- a/f\n+++ b/f\n")
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "-g", str(key_file), str(diff_file)])
        assert args.command == "review-diff"
        assert args.diff_file == str(diff_file)

    def test_review_diff_parses_stdin_dash(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "-g", str(key_file), "-"])
        assert args.diff_file == "-"

    def test_review_diff_repo_dir_defaults_to_none(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "-g", str(key_file), "-"])
        assert args.repo_dir is None

    def test_review_diff_parses_repo_dir(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            ["review-diff", "-g", str(key_file), "--repo-dir", str(tmp_path), "-"]
        )
        assert args.repo_dir == str(tmp_path)

    def test_review_diff_model_defaults_to_gemini_3_flash_preview(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "-g", str(key_file), "-"])
        assert args.model == "gemini-3-flash-preview"

    def test_review_diff_parses_model(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            ["review-diff", "--model", "gemini-2.5-pro", "-g", str(key_file), "-"]
        )
        assert args.model == "gemini-2.5-pro"

    def test_review_diff_gemini_api_key_file_defaults_to_none(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "patch.diff"])
        assert args.gemini_api_key_file is None

    def test_review_diff_json_output_defaults_to_none(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "-g", str(key_file), "-"])
        assert args.json_output is None

    def test_review_diff_parses_json_output(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        output_file = tmp_path / "review.json"
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-diff",
                "-g",
                str(key_file),
                "--json-output",
                str(output_file),
                "-",
            ]
        )
        assert args.json_output == str(output_file)

    def test_review_diff_metrics_defaults_to_none(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "-g", str(key_file), "-"])
        assert args.metrics is None

    def test_review_diff_parses_metrics(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            ["review-diff", "-g", str(key_file), "--metrics", "/tmp/m.json", "-"]
        )
        assert args.metrics == "/tmp/m.json"

    def test_review_diff_max_diff_chars_defaults_to_200000(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "-g", str(key_file), "-"])
        assert args.max_diff_chars == 200_000

    def test_review_diff_parses_max_diff_chars(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-diff",
                "-g",
                str(key_file),
                "--max-diff-chars",
                "100000",
                "-",
            ]
        )
        assert args.max_diff_chars == 100_000

    def test_review_diff_max_tool_calls_defaults(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(["review-diff", "-g", str(key_file), "-"])
        assert args.max_tool_calls == DEFAULT_MAX_TOOL_CALLS

    def test_review_diff_parses_max_tool_calls(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review-diff",
                "-g",
                str(key_file),
                "--max-tool-calls",
                "15",
                "-",
            ]
        )
        assert args.max_tool_calls == 15


_STRUCTURED_DIFF = (
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "+import sys\n"
    "\n"
    " def main():\n"
)

_VALID_STRUCTURED_RESPONSE = json.dumps(
    {
        "general_comment": "Looks good.",
        "inline_comments": {
            "src/foo.py": {
                "2": "Good addition.",
            }
        },
    }
)

_EMPTY_STRUCTURED_RESPONSE = json.dumps(
    {
        "general_comment": "No issues.",
        "inline_comments": {},
    }
)

# A diff whose new-side path has two suffixes present in a checkout holding
# both "src/foo.py" and "after/common/src/foo.py" — unresolvable without a
# prefix, so validation raises.
_AMBIGUOUS_DIFF = (
    "--- before/common/src/foo.py\n"
    "+++ after/common/src/foo.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


class TestHandleReviewDiff:
    def test_reads_diff_from_file_and_prints_review(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """review-diff reads a diff from a file path and prints the LLM review."""
        diff_content = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(diff_content)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text="All good.")])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        captured = capsys.readouterr()
        assert "All good." in captured.out
        assert REVIEW_MARKER in captured.out

    def test_reads_diff_from_stdin(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """review-diff reads a diff from stdin when diff_file is '-'."""
        import io

        diff_content = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(diff_content))

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text="Stdin review.")])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "-",
                ]
            )
            handle_review_diff(args)

        captured = capsys.readouterr()
        assert "Stdin review." in captured.out

    def test_uses_custom_repo_dir_for_tools(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The --repo-dir is used as the base for read_file and list_directory tools."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "AGENTS.md").write_text("# Rules\nBe nice.\n")

        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text="Used context.",
                    tool_calls=[ToolCall(name="read_file", args={"path": "AGENTS.md"})],
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(repo_dir),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        captured = capsys.readouterr()
        assert "Used context." in captured.out

    def test_default_repo_dir_is_cwd(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When --repo-dir is omitted, the current working directory is used."""
        monkeypatch.chdir(tmp_path)

        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text="CWD review.")])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                ["review-diff", "-g", str(api_key_file), str(diff_file)]
            )
            handle_review_diff(args)

        captured = capsys.readouterr()
        assert "CWD review." in captured.out

    def test_diff_content_passed_to_llm(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The diff text from the file is included in the prompt sent to the LLM."""
        diff_content = (
            "--- a/widget.py\n+++ b/widget.py\n@@ -1 +1 @@\n-broken\n+fixed\n"
        )
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(diff_content)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text="Nice fix.")])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "broken" in prompt
        assert "fixed" in prompt


class TestHandleReviewDiffJsonOutput:
    def test_writes_json_file_when_json_output_given(self, tmp_path: Path) -> None:
        """When --json-output is provided, a JSON file is written instead of stdout."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--json-output",
                    str(output_file),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "general_comment" in data
        assert "inline_comments" in data

    def test_json_output_contains_general_comment(self, tmp_path: Path) -> None:
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--json-output",
                    str(output_file),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        data = json.loads(output_file.read_text())
        assert data["general_comment"] == "Looks good."

    def test_json_output_contains_inline_comments(self, tmp_path: Path) -> None:
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--json-output",
                    str(output_file),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        data = json.loads(output_file.read_text())
        assert data["inline_comments"]["src/foo.py"]["2"] == "Good addition."

    def test_json_output_does_not_print_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When --json-output is given, nothing is printed to stdout."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_EMPTY_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--json-output",
                    str(output_file),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_no_json_output_still_prints_plain_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When --json-output is absent, plain text review is printed to stdout."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text="Plain text review.")])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        captured = capsys.readouterr()
        assert "Plain text review." in captured.out
        assert REVIEW_MARKER in captured.out

    def test_json_output_uses_review_diff_structured(self, tmp_path: Path) -> None:
        """When --json-output is set, review_diff_structured is called."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--json-output",
                    str(output_file),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        tool_names = {t.__name__ for t in llm._client.received_tools[0]}
        assert "validate_review" in tool_names

    def test_json_output_from_stdin(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json-output works when reading the diff from stdin."""
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(_STRUCTURED_DIFF))

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_EMPTY_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--json-output",
                    str(output_file),
                    "-",
                ]
            )
            handle_review_diff(args)

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["general_comment"] == "No issues."

    def test_missing_gemini_api_key_exits_with_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When no Gemini API key is available, the command exits with an error."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("--- a/foo\n+++ b/foo\n")

        args = _build_parser().parse_args(["review-diff", str(diff_file)])

        with pytest.raises(SystemExit) as exc_info:
            handle_review_diff(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GEMINI_API_KEY" in captured.err


class TestHandleReviewDiffJsonOutputWiring:
    def _checkout_with_ambiguous_suffixes(self, tmp_path: Path) -> None:
        """Create a checkout where the diff's new-side path matches twice."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("code\n")
        (tmp_path / "after" / "common" / "src").mkdir(parents=True)
        (tmp_path / "after" / "common" / "src" / "foo.py").write_text("code\n")

    def test_json_output_validates_against_real_checkout_listing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The JSON branch resolves diff paths against the real checkout."""
        self._checkout_with_ambiguous_suffixes(tmp_path)
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_AMBIGUOUS_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        tool_input = json.dumps({"general_comment": "ok", "inline_comments": {}})
        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_EMPTY_STRUCTURED_RESPONSE,
                    tool_calls=[
                        ToolCall(
                            name="validate_review",
                            args={"json_text": tool_input},
                        )
                    ],
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--json-output",
                    str(output_file),
                    str(diff_file),
                ]
            )
            # Two listing entries match the new-side path's suffixes, so
            # validation raises instead of guessing a repository path.
            with pytest.raises(ValueError):
                handle_review_diff(args)

        assert not output_file.exists()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_text_output_is_unchanged_by_threading(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The text branch never validates, so the ambiguous diff still reviews."""
        self._checkout_with_ambiguous_suffixes(tmp_path)
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_AMBIGUOUS_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text="Plain text review.")])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        captured = capsys.readouterr()
        assert "Plain text review." in captured.out
        assert REVIEW_MARKER in captured.out


class TestMainReviewDiff:
    def test_review_diff_command_delegates_to_handle_review_diff(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        diff_content = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(diff_content)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text="Main dispatch works.")])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            main(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    str(diff_file),
                ]
            )

        captured = capsys.readouterr()
        assert "Main dispatch works." in captured.out


_PR_DIFF = (
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "+import sys\n"
    "\n"
    " def main():\n"
)

_PR_REVIEW_RESPONSE = json.dumps(
    {
        "general_comment": "Looks good overall.",
        "inline_comments": {
            "src/foo.py": {
                "2": "Nice import.",
            }
        },
    }
)

_PR_EMPTY_RESPONSE = json.dumps(
    {
        "general_comment": "No issues.",
        "inline_comments": {},
    }
)


def _write_pr_publish_inputs(
    tmp_path: Path, review: dict, diff: str = _PR_DIFF
) -> tuple[Path, Path]:
    """Write a fetch-pr-style input dir and candidate review; return paths."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "diff.patch").write_text(diff)
    (input_dir / "metadata.json").write_text(json.dumps({"diff_prefix": None}))
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review))
    return input_dir, review_path


class TestBuildParserReviewPr:
    def test_review_pr_parses_pr_url(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "https://github.com/owner/repo/pull/42",
            ]
        )
        assert args.pr_url == "https://github.com/owner/repo/pull/42"

    def test_review_pr_requires_pr_url(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["review-pr", "-g", "key.txt"])

    def test_review_pr_gemini_api_key_file_defaults_to_none(self) -> None:
        args = _build_parser().parse_args(
            ["review-pr", "https://github.com/owner/repo/pull/1"]
        )
        assert args.gemini_api_key_file is None

    def test_review_pr_github_token_defaults_to_none(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.github_token is None

    def test_review_pr_parses_github_token(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "--github-token",
                "ghp_abc123",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.github_token == "ghp_abc123"

    def test_review_pr_model_defaults_to_gemini_3_flash_preview(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.model == "gemini-3-flash-preview"

    def test_review_pr_parses_model(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "--model",
                "gemini-pro",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.model == "gemini-pro"

    def test_review_pr_repo_dir_defaults_to_none(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.repo_dir is None

    def test_review_pr_parses_repo_dir(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "--repo-dir",
                "/some/path",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.repo_dir == "/some/path"

    def test_review_pr_dry_run_defaults_to_false(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.dry_run is False

    def test_review_pr_parses_dry_run(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "--dry-run",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.dry_run is True

    def test_review_pr_metrics_defaults_to_none(self) -> None:
        args = _build_parser().parse_args(
            ["review-pr", "-g", "key.txt", "https://github.com/owner/repo/pull/1"]
        )
        assert args.metrics is None

    def test_review_pr_parses_metrics(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "--metrics",
                "/tmp/m.json",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.metrics == "/tmp/m.json"

    def test_review_pr_max_diff_chars_defaults_to_200000(self) -> None:
        args = _build_parser().parse_args(
            ["review-pr", "-g", "key.txt", "https://github.com/owner/repo/pull/1"]
        )
        assert args.max_diff_chars == 200_000

    def test_review_pr_parses_max_diff_chars(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "--max-diff-chars",
                "300000",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.max_diff_chars == 300_000

    def test_review_pr_max_tool_calls_defaults(self) -> None:
        args = _build_parser().parse_args(
            ["review-pr", "-g", "key.txt", "https://github.com/owner/repo/pull/1"]
        )
        assert args.max_tool_calls == DEFAULT_MAX_TOOL_CALLS

    def test_review_pr_parses_max_tool_calls(self) -> None:
        args = _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                "key.txt",
                "--max-tool-calls",
                "60",
                "https://github.com/owner/repo/pull/1",
            ]
        )
        assert args.max_tool_calls == 60


class TestHandleReviewPr:
    def _make_args(
        self,
        tmp_path: Path,
        *,
        pr_url: str = "https://github.com/owner/repo/pull/42",
        github_token: str | None = "ghp_test",
        gemini_api_key_file: str | None = "__default__",
        repo_dir: str | None = None,
        dry_run: bool = False,
        max_diff_chars: int | None = None,
    ) -> argparse.Namespace:
        if gemini_api_key_file == "__default__":
            api_key_file = tmp_path / "api_key.txt"
            api_key_file.write_text("fake-key\n")
            gemini_api_key_file = str(api_key_file)
        return _build_parser().parse_args(
            [
                "review-pr",
                *(["-g", gemini_api_key_file] if gemini_api_key_file else []),
                *(["--github-token", github_token] if github_token else []),
                *(["--repo-dir", repo_dir] if repo_dir else []),
                *(["--dry-run"] if dry_run else []),
                *(
                    ["--max-diff-chars", str(max_diff_chars)]
                    if max_diff_chars is not None
                    else []
                ),
                pr_url,
            ]
        )

    def test_posts_review_after_fetching_diff(self, tmp_path: Path) -> None:
        """End-to-end: diff is fetched, reviewed, and posted to GitHub."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request(
            "owner", "repo", 42, diff=_PR_DIFF, description="Add sys import"
        )
        llm = FakeLLMClient([ScriptedResponse(text=_PR_REVIEW_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        assert len(reviews) == 1
        assert reviews[0]["body"] == (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nLooks good overall."
        )

    def test_inline_comments_are_posted(self, tmp_path: Path) -> None:
        """Inline comments from the LLM response are included in the posted review."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_REVIEW_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        comments = reviews[0]["comments"]
        assert len(comments) == 1
        assert comments[0]["path"] == "src/foo.py"
        assert comments[0]["line"] == 2
        assert comments[0]["body"] == "Nice import."

    def test_no_inline_comments_when_empty(self, tmp_path: Path) -> None:
        """When the LLM returns no inline comments, an empty list is posted."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        assert reviews[0]["comments"] == []

    def test_empty_diff_posts_default_no_changes_review(self, tmp_path: Path) -> None:
        """When the PR diff is empty, post a deterministic no-changes review."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff="")
        llm = FakeLLMClient()

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        assert len(reviews) == 1
        assert reviews[0]["body"] == EMPTY_DIFF_GENERAL_COMMENT
        assert reviews[0]["comments"] == []
        assert llm._client.received_prompts == []

    def test_dry_run_prints_json_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With --dry-run, the JSON review is printed to stdout, not posted."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_REVIEW_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(
                self._make_args(tmp_path, dry_run=True, repo_dir=str(tmp_path))
            )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["general_comment"] == (
            f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\nLooks good overall."
        )

    def test_dry_run_does_not_post_review(self, tmp_path: Path) -> None:
        """With --dry-run, no review is posted to GitHub."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(
                self._make_args(tmp_path, dry_run=True, repo_dir=str(tmp_path))
            )

        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        assert reviews == []

    def test_token_read_from_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When --github-token is absent, GITHUB_TOKEN env var is used."""
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        captured_token: list[str] = []

        def fake_github_client(token: str) -> FakeGitHubClient:
            captured_token.append(token)
            return github_client

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch(
                "maas_code_reviewer.cli.GitHubClient", side_effect=fake_github_client
            ),
        ):
            handle_review_pr(
                self._make_args(tmp_path, github_token=None, repo_dir=str(tmp_path))
            )

        assert captured_token == ["env-token"]

    def test_explicit_token_overrides_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--github-token takes precedence over GITHUB_TOKEN env var."""
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        captured_token: list[str] = []

        def fake_github_client(token: str) -> FakeGitHubClient:
            captured_token.append(token)
            return github_client

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch(
                "maas_code_reviewer.cli.GitHubClient", side_effect=fake_github_client
            ),
        ):
            handle_review_pr(
                self._make_args(
                    tmp_path, github_token="explicit-token", repo_dir=str(tmp_path)
                )
            )

        assert captured_token == ["explicit-token"]

    def test_missing_token_exits_with_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When no token is available, the command exits with an error message."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            handle_review_pr(
                self._make_args(tmp_path, github_token=None, repo_dir=str(tmp_path))
            )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GITHUB_TOKEN" in captured.err

    def test_gemini_api_key_read_from_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When --gemini-api-key-file is absent, GEMINI_API_KEY env var is used."""
        monkeypatch.setenv("GEMINI_API_KEY", "env-gemini-key")
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        captured_keys: list[str] = []

        def fake_gemini_client(*, api_key: str, model: str) -> object:
            captured_keys.append(api_key)
            return llm

        with (
            patch(
                "maas_code_reviewer.cli.GeminiClient", side_effect=fake_gemini_client
            ),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(
                self._make_args(
                    tmp_path, gemini_api_key_file=None, repo_dir=str(tmp_path)
                )
            )

        assert captured_keys == ["env-gemini-key"]

    def test_missing_gemini_api_key_exits_with_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When no Gemini API key is available, the command exits with an error."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            handle_review_pr(
                self._make_args(
                    tmp_path, gemini_api_key_file=None, repo_dir=str(tmp_path)
                )
            )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GEMINI_API_KEY" in captured.err

    def test_description_passed_to_llm(self, tmp_path: Path) -> None:
        """The PR description is included in the LLM prompt."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request(
            "owner", "repo", 42, diff=_PR_DIFF, description="Fix the widget bug"
        )
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "Fix the widget bug" in prompt

    def test_diff_passed_to_llm(self, tmp_path: Path) -> None:
        """The PR diff is included in the LLM prompt."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "import sys" in prompt

    def test_max_diff_chars_truncates_diff_sent_to_llm(
        self, tmp_path: Path
    ) -> None:
        """A low --max-diff-chars truncates the diff before it reaches the LLM."""
        truncated_diff = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import sys\n"
            "\n"
            " def main():\n"
        )
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=truncated_diff)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(
                self._make_args(
                    tmp_path, repo_dir=str(tmp_path), max_diff_chars=10
                )
            )

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "import sys" not in prompt
        assert "truncated" in prompt.lower()

    def test_default_repo_dir_is_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When --repo-dir is omitted, the current working directory is used."""
        monkeypatch.chdir(tmp_path)
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            # No --repo-dir; args.repo_dir will be None
            handle_review_pr(self._make_args(tmp_path, repo_dir=None))

        # If we get here without error, cwd was used successfully.
        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        assert len(reviews) == 1

    def test_validate_review_tool_provided_to_llm(self, tmp_path: Path) -> None:
        """The validate_review tool is provided to the LLM for structured output."""
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        tool_names = {t.__name__ for t in llm._client.received_tools[0]}
        assert "validate_review" in tool_names

    def test_review_pull_request_validates_against_real_checkout_listing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """review-pr resolves diff paths against the real checkout listing."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("code\n")
        (tmp_path / "after" / "common" / "src").mkdir(parents=True)
        (tmp_path / "after" / "common" / "src" / "foo.py").write_text("code\n")
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_AMBIGUOUS_DIFF)

        tool_input = json.dumps({"general_comment": "ok", "inline_comments": {}})
        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_PR_EMPTY_RESPONSE,
                    tool_calls=[
                        ToolCall(
                            name="validate_review",
                            args={"json_text": tool_input},
                        )
                    ],
                )
            ]
        )

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            # Two listing entries match the new-side path's suffixes, so
            # validation raises instead of guessing a repository path.
            with pytest.raises(ValueError):
                handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        assert github_client.get_posted_reviews("owner", "repo", 42) == []
        captured = capsys.readouterr()
        assert captured.out == ""


class TestHandleReviewDiffMetrics:
    def test_no_metrics_flag_no_file_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n")
        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text="All good.")])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        # No metrics file should exist
        assert not (tmp_path / "metrics.json").exists()

    def test_metrics_written_with_correct_fields(self, tmp_path: Path) -> None:
        diff_content = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(diff_content)
        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")
        metrics_file = tmp_path / "metrics.json"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text="All good.",
                    tokens_input=10,
                    tokens_output=20,
                    tokens_thinking=30,
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--metrics",
                    str(metrics_file),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        assert metrics_file.exists()
        data = json.loads(metrics_file.read_text())
        assert data["model_name"] == "gemini-3-flash-preview"
        assert data["tokens_input"] == 10
        assert data["tokens_output"] == 20
        assert data["tokens_thinking"] == 30
        assert data["diff_lines"] == diff_content.count("\n")
        assert data["diff_size_bytes"] == len(diff_content.encode("utf-8"))
        assert data["files_read"] == 0
        assert data["agents_md_read"] is False

    def test_metrics_tracks_files_read(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "AGENTS.md").write_text("# Rules\nBe nice.\n")

        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")
        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")
        metrics_file = tmp_path / "metrics.json"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text="Used context.",
                    tool_calls=[ToolCall(name="read_file", args={"path": "AGENTS.md"})],
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review-diff",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(repo_dir),
                    "--metrics",
                    str(metrics_file),
                    str(diff_file),
                ]
            )
            handle_review_diff(args)

        data = json.loads(metrics_file.read_text())
        assert data["files_read"] == 1
        assert data["agents_md_read"] is True


class TestHandleReviewPrMetrics:
    def _make_args(
        self,
        tmp_path: Path,
        *,
        pr_url: str = "https://github.com/owner/repo/pull/42",
        github_token: str | None = "ghp_test",
        repo_dir: str | None = None,
        dry_run: bool = False,
        metrics: str | None = None,
    ) -> argparse.Namespace:
        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")
        return _build_parser().parse_args(
            [
                "review-pr",
                "-g",
                str(api_key_file),
                *(["--github-token", github_token] if github_token else []),
                *(["--repo-dir", repo_dir] if repo_dir else []),
                *(["--dry-run"] if dry_run else []),
                *(["--metrics", metrics] if metrics else []),
                pr_url,
            ]
        )

    def test_no_metrics_flag_no_file_written(self, tmp_path: Path) -> None:
        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(self._make_args(tmp_path, repo_dir=str(tmp_path)))

        assert not (tmp_path / "metrics.json").exists()

    def test_metrics_written_with_correct_fields(self, tmp_path: Path) -> None:
        github_client = FakeGitHubClient()
        github_client.add_pull_request(
            "owner", "repo", 42, diff=_PR_DIFF, description="Add sys import"
        )
        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_PR_REVIEW_RESPONSE,
                    tokens_input=100,
                    tokens_output=200,
                    tokens_thinking=300,
                )
            ]
        )
        metrics_file = tmp_path / "metrics.json"

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            handle_review_pr(
                self._make_args(
                    tmp_path, repo_dir=str(tmp_path), metrics=str(metrics_file)
                )
            )

        assert metrics_file.exists()
        data = json.loads(metrics_file.read_text())
        assert data["model_name"] == "gemini-3-flash-preview"
        assert data["tokens_input"] == 100
        assert data["tokens_output"] == 200
        assert data["tokens_thinking"] == 300
        assert data["diff_lines"] == _PR_DIFF.count("\n")
        assert data["diff_size_bytes"] == len(_PR_DIFF.encode("utf-8"))
        assert data["files_read"] == 0
        assert data["agents_md_read"] is False


class TestHandleReviewMpMetrics:
    def test_metrics_written_with_correct_fields(self, tmp_path: Path) -> None:
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
                    text="Looks great.",
                    tokens_input=50,
                    tokens_output=60,
                    tokens_thinking=70,
                )
            ]
        )

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")
        metrics_file = tmp_path / "metrics.json"

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
        ):
            args = _build_parser().parse_args(
                [
                    "review-mp",
                    "--dry-run",
                    "--gemini-api-key-file",
                    str(api_key_file),
                    "--metrics",
                    str(metrics_file),
                    mp.url,
                ]
            )
            handle_review_mp(args)

        assert metrics_file.exists()
        data = json.loads(metrics_file.read_text())
        assert data["model_name"] == "gemini-3-flash-preview"
        assert data["tokens_input"] == 50
        assert data["tokens_output"] == 60
        assert data["tokens_thinking"] == 70
        assert data["diff_lines"] > 0
        assert data["diff_size_bytes"] > 0

    def test_no_metrics_flag_no_file_written(self, tmp_path: Path) -> None:
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

        llm = FakeLLMClient([ScriptedResponse(text="Looks great.")])

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
        ):
            args = _build_parser().parse_args(
                [
                    "review-mp",
                    "--dry-run",
                    "--gemini-api-key-file",
                    str(api_key_file),
                    mp.url,
                ]
            )
            handle_review_mp(args)

        assert not (tmp_path / "metrics.json").exists()

    def test_no_metrics_written_when_already_reviewed(self, tmp_path: Path) -> None:
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
                body="[maas-code-reviewer review]\n\nAlready done.",
                date=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        llm = FakeLLMClient()

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")
        metrics_file = tmp_path / "metrics.json"

        with (
            patch("maas_code_reviewer.cli.LaunchpadClient", return_value=lp),
            patch("maas_code_reviewer.cli.GitClient", return_value=git),
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
        ):
            args = _build_parser().parse_args(
                [
                    "review-mp",
                    "--gemini-api-key-file",
                    str(api_key_file),
                    "--metrics",
                    str(metrics_file),
                    mp.url,
                ]
            )
            handle_review_mp(args)

        assert not metrics_file.exists()


class TestMainReviewPr:
    def test_review_pr_command_delegates_to_handle_review_pr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        github_client = FakeGitHubClient()
        github_client.add_pull_request("owner", "repo", 42, diff=_PR_DIFF)
        llm = FakeLLMClient([ScriptedResponse(text=_PR_EMPTY_RESPONSE)])

        with (
            patch("maas_code_reviewer.cli.GeminiClient", return_value=llm),
            patch("maas_code_reviewer.cli.GitHubClient", return_value=github_client),
        ):
            main(
                [
                    "review-pr",
                    "-g",
                    str(api_key_file),
                    "--github-token",
                    "ghp_test",
                    "--repo-dir",
                    str(tmp_path),
                    "https://github.com/owner/repo/pull/42",
                ]
            )

        reviews = github_client.get_posted_reviews("owner", "repo", 42)
        assert len(reviews) == 1


_REVIEW_AMBIGUOUS_DIFF = (
    "diff --git before/common/src/foo.py after/common/src/foo.py\n"
    "--- before/common/src/foo.py\n"
    "+++ after/common/src/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    " context\n"
    "-old\n"
    "+new\n"
)


class TestBuildParserReview:
    def test_review_defaults(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("test-key")
        parser = _build_parser()
        args = parser.parse_args(
            ["review", "-g", str(key_file), "--diff-file", "patch.diff"]
        )
        assert args.command == "review"
        assert args.diff_file == "patch.diff"
        assert args.description_file is None
        assert args.diff_prefix is None
        assert args.repo_dir is None
        assert args.json_output is None
        assert args.metrics is None
        assert args.model == "gemini-3-flash-preview"
        assert args.gemini_api_key_file == str(key_file)

    def test_review_gemini_api_key_file_defaults_to_none(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["review", "--diff-file", "patch.diff"])
        assert args.gemini_api_key_file is None

    def test_review_requires_diff_file(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(["review"])

        assert exc_info.value.code == 2
        assert "--diff-file" in capsys.readouterr().err

    def test_review_diff_file_accepts_dash_for_stdin(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["review", "--diff-file", "-"])
        assert args.diff_file == "-"

    def test_review_parses_description_file(self, tmp_path: Path) -> None:
        desc_file = tmp_path / "description.md"
        desc_file.write_text("Adds the widget API.")
        parser = _build_parser()
        args = parser.parse_args(
            [
                "review",
                "--diff-file",
                "patch.diff",
                "--description-file",
                str(desc_file),
            ]
        )
        assert args.description_file == str(desc_file)

    @pytest.mark.parametrize("prefix", ["w/", ""])
    def test_review_parses_diff_prefix(self, prefix: str) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["review", "--diff-file", "patch.diff", "--diff-prefix", prefix]
        )
        assert args.diff_prefix == prefix

    def test_review_parses_repo_dir(self, tmp_path: Path) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["review", "--diff-file", "patch.diff", "--repo-dir", str(tmp_path)]
        )
        assert args.repo_dir == str(tmp_path)

    def test_review_parses_json_output(self, tmp_path: Path) -> None:
        output_file = tmp_path / "review.json"
        parser = _build_parser()
        args = parser.parse_args(
            ["review", "--diff-file", "patch.diff", "--json-output", str(output_file)]
        )
        assert args.json_output == str(output_file)

    def test_review_parses_metrics(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["review", "--diff-file", "patch.diff", "--metrics", "/tmp/m.json"]
        )
        assert args.metrics == "/tmp/m.json"

    def test_review_parses_model(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["review", "--diff-file", "patch.diff", "--model", "gemini-2.5-pro"]
        )
        assert args.model == "gemini-2.5-pro"


class TestHandleReview:
    def test_writes_raw_json_document(self, tmp_path: Path) -> None:
        """--json-output writes the raw, undecorated document."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                    "--json-output",
                    str(output_file),
                ]
            )
            handle_review(args)

        assert json.loads(output_file.read_text()) == {
            "general_comment": "Looks good.",
            "inline_comments": {"src/foo.py": {"2": "Good addition."}},
        }
        assert REVIEW_MARKER not in output_file.read_text()

    def test_json_output_prints_nothing_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When --json-output is given, nothing is printed to stdout."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_EMPTY_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                    "--json-output",
                    str(output_file),
                ]
            )
            handle_review(args)

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_text_mode_renders_document_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without --json-output the rendered document is printed, undecorated."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                ]
            )
            handle_review(args)

        captured = capsys.readouterr()
        assert captured.out == "Looks good.\n\nsrc/foo.py:2: Good addition.\n"
        assert REVIEW_MARKER not in captured.out
        assert not (tmp_path / "review.json").exists()

    def test_reads_diff_from_stdin(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--diff-file - reads the diff from stdin."""
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(_STRUCTURED_DIFF))

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_EMPTY_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    "-",
                    "--json-output",
                    str(output_file),
                ]
            )
            handle_review(args)

        assert json.loads(output_file.read_text()) == {
            "general_comment": "No issues.",
            "inline_comments": {},
        }

    def test_description_file_content_reaches_prompt(self, tmp_path: Path) -> None:
        """The --description-file content lands in the LLM prompt."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        description_file = tmp_path / "description.md"
        description_file.write_text("Adds the widget API.")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_EMPTY_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                    "--description-file",
                    str(description_file),
                    "--json-output",
                    str(output_file),
                ]
            )
            handle_review(args)

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "## Description" in prompt
        assert "Adds the widget API." in prompt

    def test_no_description_file_passes_none_to_prompt(self, tmp_path: Path) -> None:
        """Without --description-file the prompt has no Description section."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_EMPTY_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                    "--json-output",
                    str(output_file),
                ]
            )
            handle_review(args)

        prompt = _prompt_text(llm._client.received_prompts[0])
        assert "## Description" not in prompt

    def test_diff_prefix_resolves_ambiguous_diff(self, tmp_path: Path) -> None:
        """--diff-prefix threads into the validate_review tool and resolves paths."""
        repo_dir = tmp_path / "checkout"
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "foo.py").write_text("code\n")
        (repo_dir / "common" / "src").mkdir(parents=True)
        (repo_dir / "common" / "src" / "foo.py").write_text("code\n")

        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_REVIEW_AMBIGUOUS_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_VALID_STRUCTURED_RESPONSE,
                    tool_calls=[
                        ToolCall(
                            name="validate_review",
                            args={"json_text": _VALID_STRUCTURED_RESPONSE},
                        )
                    ],
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(repo_dir),
                    "--diff-file",
                    str(diff_file),
                    "--diff-prefix",
                    "after/common/",
                    "--json-output",
                    str(output_file),
                ]
            )
            handle_review(args)

        assert json.loads(output_file.read_text()) == {
            "general_comment": "Looks good.",
            "inline_comments": {"src/foo.py": {"2": "Good addition."}},
        }

    def test_ambiguous_diff_fails_with_clean_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unresolvable diff path raises InvalidReviewError naming --diff-prefix."""
        repo_dir = tmp_path / "checkout"
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "foo.py").write_text("code\n")
        (repo_dir / "common" / "src").mkdir(parents=True)
        (repo_dir / "common" / "src" / "foo.py").write_text("code\n")

        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_REVIEW_AMBIGUOUS_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_VALID_STRUCTURED_RESPONSE,
                    tool_calls=[
                        ToolCall(
                            name="validate_review",
                            args={"json_text": _VALID_STRUCTURED_RESPONSE},
                        )
                    ],
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(repo_dir),
                    "--diff-file",
                    str(diff_file),
                    "--json-output",
                    str(output_file),
                ]
            )
            with pytest.raises(InvalidReviewError) as exc_info:
                handle_review(args)

        assert "--diff-prefix" in str(exc_info.value)
        assert not output_file.exists()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_diff_prefix_mismatch_fails_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A diff path not matching --diff-prefix raises InvalidReviewError."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_VALID_STRUCTURED_RESPONSE,
                    tool_calls=[
                        ToolCall(
                            name="validate_review",
                            args={"json_text": _VALID_STRUCTURED_RESPONSE},
                        )
                    ],
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                    "--diff-prefix",
                    "wrong/",
                    "--json-output",
                    str(output_file),
                ]
            )
            with pytest.raises(InvalidReviewError) as exc_info:
                handle_review(args)

        assert "--diff-prefix" in str(exc_info.value)
        assert not output_file.exists()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_empty_diff_writes_empty_document(self, tmp_path: Path) -> None:
        """An empty diff produces the standard empty document with no LLM call."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("")

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                    "--json-output",
                    str(output_file),
                ]
            )
            handle_review(args)

        assert json.loads(output_file.read_text()) == {
            "general_comment": EMPTY_DIFF_GENERAL_COMMENT,
            "inline_comments": {},
        }
        assert llm._client.received_prompts == []

    def test_empty_diff_text_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty diff renders the standard empty document, undecorated."""
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text("")

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                ]
            )
            handle_review(args)

        captured = capsys.readouterr()
        assert EMPTY_DIFF_GENERAL_COMMENT in captured.out
        assert REVIEW_MARKER not in captured.out

    def test_missing_gemini_api_key_exits_with_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When no Gemini API key is available, the command exits with an error."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        args = _build_parser().parse_args(
            ["review", "--repo-dir", str(tmp_path), "--diff-file", str(diff_file)]
        )

        with pytest.raises(SystemExit) as exc_info:
            handle_review(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GEMINI_API_KEY" in captured.err


class TestHandleReviewMetrics:
    def test_no_metrics_flag_no_file_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)
        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                ]
            )
            handle_review(args)

        # No metrics file should exist
        assert not (tmp_path / "metrics.json").exists()

    def test_metrics_written_with_correct_fields(self, tmp_path: Path) -> None:
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)
        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")
        metrics_file = tmp_path / "metrics.json"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_VALID_STRUCTURED_RESPONSE,
                    tokens_input=10,
                    tokens_output=20,
                    tokens_thinking=30,
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                    "--metrics",
                    str(metrics_file),
                ]
            )
            handle_review(args)

        assert metrics_file.exists()
        data = json.loads(metrics_file.read_text())
        assert data["model_name"] == "gemini-3-flash-preview"
        assert data["tokens_input"] == 10
        assert data["tokens_output"] == 20
        assert data["tokens_thinking"] == 30
        assert data["diff_lines"] == _STRUCTURED_DIFF.count("\n")
        assert data["diff_size_bytes"] == len(_STRUCTURED_DIFF.encode("utf-8"))
        assert data["files_read"] == 0
        assert data["agents_md_read"] is False

    def test_metrics_tracks_files_read(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "AGENTS.md").write_text("# Rules\nBe nice.\n")

        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)
        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")
        metrics_file = tmp_path / "metrics.json"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_VALID_STRUCTURED_RESPONSE,
                    tool_calls=[
                        ToolCall(name="read_file", args={"path": "AGENTS.md"})
                    ],
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            args = _build_parser().parse_args(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(repo_dir),
                    "--diff-file",
                    str(diff_file),
                    "--metrics",
                    str(metrics_file),
                ]
            )
            handle_review(args)

        data = json.loads(metrics_file.read_text())
        assert data["files_read"] == 1
        assert data["agents_md_read"] is True


class TestMainReview:
    def test_review_command_delegates_to_handle_review(self, tmp_path: Path) -> None:
        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_STRUCTURED_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient([ScriptedResponse(text=_VALID_STRUCTURED_RESPONSE)])

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            main(
                [
                    "review",
                    "-g",
                    str(api_key_file),
                    "--repo-dir",
                    str(tmp_path),
                    "--diff-file",
                    str(diff_file),
                    "--json-output",
                    str(output_file),
                ]
            )

        assert output_file.exists()
        assert json.loads(output_file.read_text()) == {
            "general_comment": "Looks good.",
            "inline_comments": {"src/foo.py": {"2": "Good addition."}},
        }

    def test_invalid_diff_exits_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unresolvable diff path exits 1 with an error naming --diff-prefix."""
        repo_dir = tmp_path / "checkout"
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "foo.py").write_text("code\n")
        (repo_dir / "common" / "src").mkdir(parents=True)
        (repo_dir / "common" / "src" / "foo.py").write_text("code\n")

        diff_file = tmp_path / "patch.diff"
        diff_file.write_text(_REVIEW_AMBIGUOUS_DIFF)

        api_key_file = tmp_path / "api_key.txt"
        api_key_file.write_text("fake-key\n")

        output_file = tmp_path / "review.json"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    text=_VALID_STRUCTURED_RESPONSE,
                    tool_calls=[
                        ToolCall(
                            name="validate_review",
                            args={"json_text": _VALID_STRUCTURED_RESPONSE},
                        )
                    ],
                )
            ]
        )

        with patch("maas_code_reviewer.cli.GeminiClient", return_value=llm):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "review",
                        "-g",
                        str(api_key_file),
                        "--repo-dir",
                        str(repo_dir),
                        "--diff-file",
                        str(diff_file),
                        "--json-output",
                        str(output_file),
                    ]
                )

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "--diff-prefix" in captured.err
        assert not output_file.exists()

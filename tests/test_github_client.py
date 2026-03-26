# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import pytest

from maas_code_reviewer.github_client import GitHubClient, parse_pr_url
from tests.fake_github import FakeGitHubClient
from tests.fake_pygithub import FakeGithubFile, FakePyGithub


class TestParsePrUrl:
    def test_parses_standard_url(self) -> None:
        owner, repo, number = parse_pr_url("https://github.com/owner/repo/pull/42")
        assert owner == "owner"
        assert repo == "repo"
        assert number == 42

    def test_parses_large_pr_number(self) -> None:
        owner, repo, number = parse_pr_url("https://github.com/org/project/pull/9999")
        assert number == 9999

    def test_parses_pr_number_1(self) -> None:
        _, _, number = parse_pr_url("https://github.com/a/b/pull/1")
        assert number == 1

    def test_parses_owner_with_hyphens(self) -> None:
        owner, repo, number = parse_pr_url("https://github.com/my-org/my-repo/pull/7")
        assert owner == "my-org"
        assert repo == "my-repo"
        assert number == 7

    def test_rejects_non_github_url(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://gitlab.com/owner/repo/pull/1")

    def test_rejects_http_url(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("http://github.com/owner/repo/pull/1")

    def test_rejects_missing_pull_segment(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://github.com/owner/repo/issues/1")

    def test_rejects_non_integer_pr_number(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://github.com/owner/repo/pull/abc")

    def test_rejects_zero_pr_number(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://github.com/owner/repo/pull/0")

    def test_rejects_negative_pr_number(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://github.com/owner/repo/pull/-5")

    def test_rejects_empty_owner(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://github.com//repo/pull/1")

    def test_rejects_url_with_no_path(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://github.com/")

    def test_rejects_url_too_short(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://github.com/owner/repo")


def _make_client(fake_gh: FakePyGithub, token: str = "test-token") -> GitHubClient:
    with fake_gh.patch_github():
        return GitHubClient(token=token)


class TestGitHubClientGetPrDiff:
    def test_returns_reconstructed_diff(self) -> None:
        fake_gh = FakePyGithub()
        fake_gh.add_pull(
            "owner/repo",
            1,
            files=[
                FakeGithubFile("src/foo.py", "@@ -1 +1,2 @@\n+import sys"),
            ],
        )
        client = _make_client(fake_gh)

        diff = client.get_pr_diff("owner", "repo", 1)

        assert "--- a/src/foo.py" in diff
        assert "+++ b/src/foo.py" in diff
        assert "@@ -1 +1,2 @@\n+import sys" in diff

    def test_multiple_files(self) -> None:
        fake_gh = FakePyGithub()
        fake_gh.add_pull(
            "owner/repo",
            1,
            files=[
                FakeGithubFile("a.py", "@@ -1 +1 @@\n+a"),
                FakeGithubFile("b.py", "@@ -1 +1 @@\n+b"),
            ],
        )
        client = _make_client(fake_gh)

        diff = client.get_pr_diff("owner", "repo", 1)

        assert "--- a/a.py" in diff
        assert "--- a/b.py" in diff

    def test_skips_files_with_no_patch(self) -> None:
        fake_gh = FakePyGithub()
        fake_gh.add_pull(
            "owner/repo",
            1,
            files=[
                FakeGithubFile("binary.png", None),
                FakeGithubFile("text.py", "@@ -1 +1 @@\n+ok"),
            ],
        )
        client = _make_client(fake_gh)

        diff = client.get_pr_diff("owner", "repo", 1)

        assert "binary.png" not in diff
        assert "text.py" in diff

    def test_no_files_returns_empty_string(self) -> None:
        fake_gh = FakePyGithub()
        fake_gh.add_pull("owner/repo", 1, files=[])
        client = _make_client(fake_gh)

        diff = client.get_pr_diff("owner", "repo", 1)

        assert diff == ""

    def test_records_token(self) -> None:
        fake_gh = FakePyGithub()
        fake_gh.add_pull("owner/repo", 1)
        _make_client(fake_gh, token="my-secret-token")

        assert fake_gh.token == "my-secret-token"


class TestGitHubClientGetPrDescription:
    def test_returns_body(self) -> None:
        fake_gh = FakePyGithub()
        fake_gh.add_pull("owner/repo", 1, body="Fix the bug")
        client = _make_client(fake_gh)

        desc = client.get_pr_description("owner", "repo", 1)

        assert desc == "Fix the bug"

    def test_returns_none_for_none_body(self) -> None:
        fake_gh = FakePyGithub()
        fake_gh.add_pull("owner/repo", 1, body=None)
        client = _make_client(fake_gh)

        desc = client.get_pr_description("owner", "repo", 1)

        assert desc is None

    def test_returns_none_for_empty_body(self) -> None:
        fake_gh = FakePyGithub()
        fake_gh.add_pull("owner/repo", 1, body="")
        client = _make_client(fake_gh)

        desc = client.get_pr_description("owner", "repo", 1)

        assert desc is None


class TestGitHubClientPostReview:
    def test_posts_review_with_correct_args(self) -> None:
        fake_gh = FakePyGithub()
        pr = fake_gh.add_pull("owner/repo", 1)
        client = _make_client(fake_gh)

        client.post_review("owner", "repo", 1, body="Looks good.", comments=[])

        assert len(pr.posted_reviews) == 1
        assert pr.posted_reviews[0]["body"] == "Looks good."
        assert pr.posted_reviews[0]["event"] == "COMMENT"
        assert pr.posted_reviews[0]["comments"] == []

    def test_posts_inline_comments(self) -> None:
        fake_gh = FakePyGithub()
        pr = fake_gh.add_pull("owner/repo", 1)
        client = _make_client(fake_gh)
        inline = [{"path": "src/foo.py", "line": 10, "body": "Nit."}]

        client.post_review("owner", "repo", 1, body="See inline.", comments=inline)

        posted = pr.posted_reviews[0]["comments"]
        assert len(posted) == 1
        assert posted[0] == {"path": "src/foo.py", "line": 10, "body": "Nit."}


class TestFakeGitHubClientGetPrDiff:
    def test_returns_registered_diff(self) -> None:
        client = FakeGitHubClient()
        client.add_pull_request("owner", "repo", 1, diff="--- a/f.py\n+++ b/f.py\n")

        diff = client.get_pr_diff("owner", "repo", 1)

        assert diff == "--- a/f.py\n+++ b/f.py\n"

    def test_different_prs_return_different_diffs(self) -> None:
        client = FakeGitHubClient()
        client.add_pull_request("owner", "repo", 1, diff="diff-one")
        client.add_pull_request("owner", "repo", 2, diff="diff-two")

        assert client.get_pr_diff("owner", "repo", 1) == "diff-one"
        assert client.get_pr_diff("owner", "repo", 2) == "diff-two"

    def test_raises_for_unknown_pr(self) -> None:
        client = FakeGitHubClient()

        with pytest.raises(KeyError):
            client.get_pr_diff("owner", "repo", 99)


class TestFakeGitHubClientGetPrDescription:
    def test_returns_registered_description(self) -> None:
        client = FakeGitHubClient()
        client.add_pull_request("owner", "repo", 1, diff="", description="My PR")

        desc = client.get_pr_description("owner", "repo", 1)

        assert desc == "My PR"

    def test_returns_none_when_no_description(self) -> None:
        client = FakeGitHubClient()
        client.add_pull_request("owner", "repo", 1, diff="")

        desc = client.get_pr_description("owner", "repo", 1)

        assert desc is None

    def test_raises_for_unknown_pr(self) -> None:
        client = FakeGitHubClient()

        with pytest.raises(KeyError):
            client.get_pr_description("owner", "repo", 99)


class TestFakeGitHubClientPostReview:
    def test_records_posted_review(self) -> None:
        client = FakeGitHubClient()
        client.add_pull_request("owner", "repo", 1, diff="")

        client.post_review("owner", "repo", 1, body="Looks good.", comments=[])

        reviews = client.get_posted_reviews("owner", "repo", 1)
        assert len(reviews) == 1
        assert reviews[0]["body"] == "Looks good."
        assert reviews[0]["comments"] == []

    def test_records_inline_comments(self) -> None:
        client = FakeGitHubClient()
        client.add_pull_request("owner", "repo", 1, diff="")
        inline = [{"path": "src/foo.py", "line": 10, "body": "Consider this."}]

        client.post_review("owner", "repo", 1, body="See inline.", comments=inline)

        reviews = client.get_posted_reviews("owner", "repo", 1)
        assert reviews[0]["comments"] == inline

    def test_records_multiple_reviews(self) -> None:
        client = FakeGitHubClient()
        client.add_pull_request("owner", "repo", 1, diff="")

        client.post_review("owner", "repo", 1, body="First.", comments=[])
        client.post_review("owner", "repo", 1, body="Second.", comments=[])

        reviews = client.get_posted_reviews("owner", "repo", 1)
        assert len(reviews) == 2
        assert reviews[0]["body"] == "First."
        assert reviews[1]["body"] == "Second."

    def test_no_reviews_initially(self) -> None:
        client = FakeGitHubClient()
        client.add_pull_request("owner", "repo", 1, diff="")

        reviews = client.get_posted_reviews("owner", "repo", 1)

        assert reviews == []

    def test_raises_for_unknown_pr(self) -> None:
        client = FakeGitHubClient()

        with pytest.raises(KeyError):
            client.post_review("owner", "repo", 99, body="x", comments=[])

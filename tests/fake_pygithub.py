# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Fake that mimics the PyGithub object model.

``GitHubClient`` talks to PyGithub objects (``github.Github``,
``Repository``, ``PullRequest``, ``File``).  This module provides
in-memory fakes for all of those so that tests can exercise
``GitHubClient`` without hitting the network.

Usage in tests::

    fake_gh = FakePyGithub()
    fake_gh.add_pull("owner/repo", 1, body="Fix bug", files=[
        FakeGithubFile("src/foo.py", "@@ -1 +1,2 @@\\n+import sys"),
    ])

    with fake_gh.patch_github():
        client = GitHubClient(token="test-token")
        diff = client.get_pr_diff("owner", "repo", 1)

    assert fake_gh.token == "test-token"
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import patch


@dataclass
class FakeGithubFile:
    """In-memory representation of a changed file in a pull request."""

    filename: str
    patch: str | None = None


@dataclass
class FakeGithubPull:
    """In-memory representation of a GitHub pull request."""

    number: int
    body: str | None = None
    _files: list[FakeGithubFile] = field(default_factory=list)
    posted_reviews: list[dict] = field(default_factory=list)

    def get_files(self) -> list[FakeGithubFile]:
        return list(self._files)

    def create_review(self, body: str, event: str, comments: list[dict]) -> None:
        self.posted_reviews.append({"body": body, "event": event, "comments": comments})


@dataclass
class FakeGithubRepo:
    """In-memory representation of a GitHub repository."""

    full_name: str
    _pulls: dict[int, FakeGithubPull] = field(default_factory=dict)

    def get_pull(self, number: int) -> FakeGithubPull:
        if number not in self._pulls:
            raise KeyError(f"No pull request #{number} in {self.full_name}")
        return self._pulls[number]


class FakePyGithub:
    """In-memory replacement for a ``github.Github`` instance.

    Populate it with ``add_pull``, then pass it to ``GitHubClient``
    (by patching ``github.Github`` to return this object via
    ``patch_github()``).
    """

    def __init__(self) -> None:
        self.token: str | None = None
        self._repos: dict[str, FakeGithubRepo] = {}

    def add_pull(
        self,
        full_name: str,
        number: int,
        body: str | None = None,
        files: list[FakeGithubFile] | None = None,
    ) -> FakeGithubPull:
        """Register a fake pull request.

        Parameters
        ----------
        full_name:
            Repository full name in ``"owner/repo"`` format.
        number:
            The pull request number.
        body:
            The PR description body text.
        files:
            List of changed files with optional patches.
        """
        if full_name not in self._repos:
            self._repos[full_name] = FakeGithubRepo(full_name=full_name)
        repo = self._repos[full_name]
        pr = FakeGithubPull(number=number, body=body, _files=files or [])
        repo._pulls[number] = pr
        return pr

    def get_repo(self, full_name: str) -> FakeGithubRepo:
        if full_name not in self._repos:
            raise KeyError(f"No repository {full_name!r}")
        return self._repos[full_name]

    @contextmanager
    def patch_github(self) -> Iterator[None]:
        """Patch ``github.Github`` to return this fake.

        The token passed by the caller is recorded in ``self.token``
        so tests can assert on it.
        """

        def _fake_github(token: str) -> "FakePyGithub":
            self.token = token
            return self

        with patch(
            "maas_code_reviewer.github_client.github.Github",
            side_effect=_fake_github,
        ):
            yield

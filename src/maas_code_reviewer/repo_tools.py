# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from pathlib import Path


class RepoTools:
    """Host-side access to a repository's AGENTS.md guidance.

    The repository directory is resolved at construction and the AGENTS.md
    path is checked against it before reading, so a symlinked AGENTS.md
    cannot pull content in from outside the repository tree. The review
    agent never uses this class: it reads the repository with its own
    native tools inside the untrusted container.
    """

    def __init__(self, repo_dir: Path) -> None:
        self._repo_dir = repo_dir.resolve()

    def read_agents_md(self) -> str | None:
        """Return the contents of the repo's top-level AGENTS.md, or None.

        Used by the host to proactively include the repository's
        conventions in the review context.
        """
        target = (self._repo_dir / "AGENTS.md").resolve()
        if not target.is_relative_to(self._repo_dir):
            return None
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

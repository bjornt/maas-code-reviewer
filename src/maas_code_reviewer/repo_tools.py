# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from pathlib import Path


class RepoTools:
    """Load host-side repository conventions, never agent filesystem callbacks."""

    def __init__(self, repo_dir: Path) -> None:
        self._repo_dir = repo_dir.resolve()
        self.agents_md_read: bool = False

    def read_agents_md(self) -> str | None:
        """Read top-level conventions only when they stay inside the checkout."""
        target = (self._repo_dir / "AGENTS.md").resolve()
        if not target.is_relative_to(self._repo_dir):
            return None
        if not target.is_file():
            return None
        content = target.read_text(encoding="utf-8")
        self.agents_md_read = True
        return content

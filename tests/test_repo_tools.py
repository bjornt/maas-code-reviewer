# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from pathlib import Path

from maas_code_reviewer.repo_tools import RepoTools


class TestReadAgentsMd:
    def test_returns_none_when_no_agents_md(self, tmp_path: Path) -> None:
        tools = RepoTools(tmp_path)
        assert tools.read_agents_md() is None

    def test_returns_content_when_agents_md_present(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Rules\nBe nice.\n")
        tools = RepoTools(tmp_path)
        assert tools.read_agents_md() == "# Rules\nBe nice.\n"

    def test_rejects_agents_md_symlinked_outside_repo(self, tmp_path: Path) -> None:
        """A symlinked AGENTS.md pointing outside the repo must not be read."""
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "AGENTS.md"
        secret.write_text("super-secret-api-key")

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "AGENTS.md").symlink_to(secret)

        tools = RepoTools(repo_dir)
        assert tools.read_agents_md() is None

    def test_repo_dir_resolved_at_construction(self, tmp_path: Path) -> None:
        """RepoTools resolves symlinks in repo_dir at construction time."""
        real_repo = tmp_path / "real"
        real_repo.mkdir()
        (real_repo / "AGENTS.md").write_text("# Rules\n")
        link = tmp_path / "link"
        link.symlink_to(real_repo)

        tools = RepoTools(link)

        assert tools.read_agents_md() == "# Rules\n"

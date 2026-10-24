# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from pathlib import Path

import pytest

from maas_code_reviewer.repo_tools import RepoTools


def test_reads_top_level_utf8_conventions_and_records_actual_inclusion(
    tmp_path: Path,
) -> None:
    conventions = "# Conventions\nReview café changes carefully.\n"
    (tmp_path / "AGENTS.md").write_text(conventions, encoding="utf-8")
    tools = RepoTools(tmp_path)
    assert tools.read_agents_md() == conventions
    assert tools.agents_md_read is True


def test_empty_conventions_are_still_a_successful_read(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("")
    tools = RepoTools(tmp_path)
    assert tools.read_agents_md() == ""
    assert tools.agents_md_read is True


def test_missing_top_level_file_does_not_use_nested_conventions(tmp_path: Path) -> None:
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("Not top-level conventions.")
    tools = RepoTools(tmp_path)
    assert tools.read_agents_md() is None
    assert tools.agents_md_read is False


def test_directory_named_agents_md_is_not_read(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").mkdir()
    tools = RepoTools(tmp_path)
    assert tools.read_agents_md() is None
    assert tools.agents_md_read is False


def test_symlinked_conventions_cannot_escape_repository(tmp_path: Path) -> None:
    outside = tmp_path / "private"
    outside.write_text("private information")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").symlink_to(outside)
    tools = RepoTools(repo)
    assert tools.read_agents_md() is None
    assert tools.agents_md_read is False


def test_internal_conventions_symlink_stays_inside_resolved_checkout(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "conventions").write_text("Local rules.")
    (repo / "AGENTS.md").symlink_to("conventions")
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(repo, target_is_directory=True)
    tools = RepoTools(alias)
    assert tools.read_agents_md() == "Local rules."
    assert tools.agents_md_read is True


def test_failed_read_does_not_claim_conventions_were_included(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_bytes(b"\xff")
    tools = RepoTools(tmp_path)
    with pytest.raises(UnicodeDecodeError):
        tools.read_agents_md()
    assert tools.agents_md_read is False

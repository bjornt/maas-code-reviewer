# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import subprocess
from pathlib import Path


class GitClient:
    """GitClient implementation that wraps subprocess calls to git."""

    def clone(
        self,
        repo_url: str,
        dest: Path,
        branch: str,
        *,
        http_extra_header: str | None = None,
    ) -> None:
        """Clone *repo_url* (one branch) into *dest*.

        When *http_extra_header* is given it is passed to git as one-shot
        ``http.extraHeader`` config (header auth), so no credential helper or
        credential-bearing URL is ever used or persisted.
        """
        command = ["git"]
        if http_extra_header is not None:
            command += ["-c", f"http.extraHeader={http_extra_header}"]
        command += ["clone", "--branch", branch, repo_url, str(dest)]
        subprocess.run(command, check=True, capture_output=True)

    def diff(self, repo_dir: Path, base_ref: str, head_ref: str) -> str:
        result = subprocess.run(
            ["git", "diff", base_ref, head_ref],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def merge_into(
        self,
        repo_dir: Path,
        source_url: str,
        source_branch: str,
        *,
        http_extra_header: str | None = None,
    ) -> None:
        """Fetch *source_branch* from *source_url* and merge it (no editor)."""
        command = ["git"]
        if http_extra_header is not None:
            command += ["-c", f"http.extraHeader={http_extra_header}"]
        command += ["fetch", source_url, source_branch]
        subprocess.run(command, cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "merge", "FETCH_HEAD", "--no-edit"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

    def rev_parse(self, repo_dir: Path, ref: str) -> str:
        """Resolve *ref* to its commit SHA inside *repo_dir*."""
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def read_file(self, repo_dir: Path, path: str) -> str | None:
        file_path = repo_dir / path
        if not file_path.is_file():
            return None
        return file_path.read_text()

    def list_changed_files(
        self, repo_dir: Path, base_ref: str, head_ref: str
    ) -> list[str]:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_ref, head_ref],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            return []
        return result.stdout.strip().split("\n")

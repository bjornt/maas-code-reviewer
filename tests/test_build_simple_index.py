# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import io
import json
import sys
from pathlib import Path

import pytest

from scripts import build_simple_index

WHEEL = "maas_code_reviewer-0.3.0.dev202610240900-py3-none-any.whl"
SDIST = "maas_code_reviewer-0.3.0.dev202610240900.tar.gz"


@pytest.fixture
def run_index(monkeypatch, tmp_path: Path):
    def run(assets: list[dict[str, object]]) -> int:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(assets)))
        return build_simple_index.main(
            ["--project", "maas-code-reviewer", "--output", str(tmp_path / "simple")]
        )

    return run


def asset(name: str, digest: str | None = None) -> dict[str, object]:
    return {
        "name": name,
        "url": f"https://example.invalid/download/{name}",
        "digest": digest,
    }


def test_links_distributions_with_hashes(run_index, tmp_path: Path):
    exit_code = run_index(
        [
            asset(WHEEL, "sha256:" + "a" * 64),
            asset(SDIST),
            asset("SHA256SUMS", "sha256:" + "b" * 64),
        ]
    )

    assert exit_code == 0
    page = (tmp_path / "simple" / "maas-code-reviewer" / "index.html").read_text()
    assert f'href="https://example.invalid/download/{WHEEL}#sha256={"a" * 64}"' in page
    assert f'href="https://example.invalid/download/{SDIST}"' in page
    assert "SHA256SUMS" not in page


def test_root_index_links_to_project(run_index, tmp_path: Path):
    assert run_index([asset(WHEEL)]) == 0

    root = (tmp_path / "simple" / "index.html").read_text()
    assert 'href="maas-code-reviewer/"' in root


def test_no_distributions_fails(run_index, tmp_path: Path):
    assert run_index([asset("SHA256SUMS")]) == 1
    assert not (tmp_path / "simple").exists()


def test_normalizes_project_name():
    assert build_simple_index.normalize("Maas_Code.Reviewer") == "maas-code-reviewer"

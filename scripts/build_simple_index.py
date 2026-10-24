#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Generate a PEP 503 simple index from published release assets.

Reads a JSON array of release assets on stdin, each with ``name``,
``url``, and an optional ``digest`` of the form ``sha256:<hex>``, and
writes a static index tree that ``uv``/``pip`` can install from.
"""

import argparse
import html
import json
import sys
from pathlib import Path

DISTRIBUTION_SUFFIXES = (".whl", ".tar.gz")
SEPARATORS = "-_."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="distribution name")
    parser.add_argument(
        "--output", required=True, type=Path, help="index root directory"
    )
    args = parser.parse_args(argv)

    assets = json.load(sys.stdin)
    links = _links(assets)
    if not links:
        print("no distribution assets found", file=sys.stderr)
        return 1

    project = normalize(args.project)
    root = args.output
    (root / project).mkdir(parents=True, exist_ok=True)
    _write(
        root / "index.html",
        f"Simple index for {project}",
        [(f"{project}/", project)],
    )
    _write(root / project / "index.html", f"Links for {project}", links)
    return 0


def normalize(name: str) -> str:
    """Return the PEP 503 normalized form of a distribution name."""
    normalized: list[str] = []
    for character in name.lower():
        if character not in SEPARATORS:
            normalized.append(character)
        elif normalized and normalized[-1] != "-":
            normalized.append("-")
    return "".join(normalized).strip("-")


def _links(assets: list[dict[str, object]]) -> list[tuple[str, str]]:
    links: dict[str, str] = {}
    for asset in assets:
        name = str(asset["name"])
        if not name.endswith(DISTRIBUTION_SUFFIXES):
            continue
        url = str(asset["url"])
        digest = asset.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:"):
            url = f"{url}#sha256={digest.split(':', 1)[1]}"
        links[name] = url
    return [(links[name], name) for name in sorted(links)]


def _write(path: Path, title: str, links: list[tuple[str, str]]) -> None:
    anchors = "\n".join(
        f'    <a href="{html.escape(url, quote=True)}">{html.escape(text)}</a><br>'
        for url, text in links
    )
    path.write_text(
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8">\n'
        '    <meta name="pypi:repository-version" content="1.0">\n'
        f"    <title>{html.escape(title)}</title>\n"
        "  </head>\n"
        "  <body>\n"
        f"{anchors}\n"
        "  </body>\n"
        "</html>\n"
    )


if __name__ == "__main__":
    sys.exit(main())

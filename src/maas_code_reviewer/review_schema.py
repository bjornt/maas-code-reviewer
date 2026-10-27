# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import ast
import json
import os
from collections.abc import Set
from pathlib import Path
from typing import cast

REVIEW_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["general_comment", "inline_comments"],
    "properties": {
        "general_comment": {"type": "string"},
        "inline_comments": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
    },
}


def parse_diff_files_and_lines(
    diff_text: str,
    *,
    path_prefix: str | None = None,
    repository_files: Set[str] = frozenset(),
) -> dict[str, set[int]]:
    """Map repository-relative new-side paths to lines in unified diff hunks.

    Resolve arbitrary prefixes against original checkout filenames, never model
    output. An explicit new-side prefix disambiguates identical suffixes.
    """
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    current_line = 0
    old_remaining = new_remaining = 0
    old_path: str | None = None
    git_header: str | None = None
    renamed_path: str | None = None

    for raw_line in diff_text.removesuffix("\n").split("\n"):
        raw_line = raw_line.removesuffix("\r")
        if old_remaining or new_remaining:
            # Interpret hunk content before headers: an added line may itself
            # look like +++ or diff --git metadata.
            if raw_line.startswith("+") and new_remaining:
                if current_file is not None:
                    result[current_file].add(current_line)
                current_line += 1
                new_remaining -= 1
                continue
            if raw_line.startswith("-") and old_remaining:
                old_remaining -= 1
                continue
            if (
                (raw_line.startswith(" ") or raw_line == "")
                and old_remaining
                and new_remaining
            ):
                if current_file is not None:
                    result[current_file].add(current_line)
                current_line += 1
                old_remaining -= 1
                new_remaining -= 1
                continue
            if raw_line.startswith("\\"):
                continue
            old_remaining = new_remaining = 0

        if raw_line.startswith("diff --git "):
            current_file = old_path = renamed_path = None
            git_header = raw_line.removeprefix("diff --git ")
        elif raw_line.startswith(("rename to ", "copy to ")):
            renamed_path = _decode_diff_path(raw_line.split(" to ", 1)[1])
        elif raw_line.startswith("--- "):
            current_file = None
            old_path = _decode_diff_path(raw_line[4:])
        elif raw_line.startswith("+++ "):
            encoded_path = raw_line[4:].partition("\t")[0]
            path = _decode_diff_path(encoded_path)
            if old_path == "/dev/null" and git_header is not None:
                suffix = " " + encoded_path
                if git_header.endswith(suffix):
                    old_path = _decode_diff_path(git_header[: -len(suffix)])
            current_file = (
                _repository_path(
                    path, old_path, renamed_path, path_prefix, repository_files
                )
                if path is not None and path != "/dev/null"
                else None
            )
            git_header = renamed_path = None
            if current_file is not None:
                result.setdefault(current_file, set())
        elif raw_line.startswith("@@ "):
            parts = raw_line.split(" ")
            if (
                len(parts) < 4
                or not parts[1].startswith("-")
                or not parts[2].startswith("+")
                or parts[3] != "@@"
            ):
                continue
            try:
                old_start, old_remaining = _hunk_range(parts[1][1:])
                current_line, new_remaining = _hunk_range(parts[2][1:])
            except ValueError:
                old_remaining = new_remaining = 0
                continue
            if (
                old_start < 0
                or current_line < 0
                or old_remaining < 0
                or new_remaining < 0
                or (old_remaining and old_start == 0)
                or (new_remaining and current_line == 0)
            ):
                old_remaining = new_remaining = 0

    return result


def validate_review_json(
    data: object,
    diff_text: str,
    *,
    path_prefix: str | None = None,
    repository_files: Set[str] = frozenset(),
) -> list[str]:
    """Validate *data* against the review JSON schema and against the diff.

    Returns a list of error strings.  An empty list means the data is valid.

    Checks performed:

    1. ``data`` must be a ``dict``.
    2. ``general_comment`` must be present and be a ``str``.
    3. ``inline_comments`` must be present and be a ``dict``.
    4. Each value in ``inline_comments`` must be a ``dict``.
    5. Each key in each per-file dict must be a string that represents an
       integer (the line number).
    6. Each value in each per-file dict must be a ``str``.
    7. Each file path in ``inline_comments`` must appear in the diff.
    8. Each line number in ``inline_comments`` must appear in the diff for
       that file.
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        got = type(data).__name__
        errors.append(f"Review must be a JSON object (dict), got {got}.")
        return errors
    data = cast(dict[object, object], data)

    if "general_comment" not in data:
        errors.append("Missing required key: 'general_comment'.")
    elif not isinstance(data["general_comment"], str):
        got = type(data["general_comment"]).__name__
        errors.append(f"'general_comment' must be a string, got {got}.")

    if "inline_comments" not in data:
        errors.append("Missing required key: 'inline_comments'.")
        return errors

    inline = data["inline_comments"]
    if not isinstance(inline, dict):
        got = type(inline).__name__
        errors.append(f"'inline_comments' must be a JSON object (dict), got {got}.")
        return errors

    diff_files = parse_diff_files_and_lines(
        diff_text, path_prefix=path_prefix, repository_files=repository_files
    )

    for file_path, line_comments in inline.items():
        if (
            not isinstance(file_path, str)
            or not file_path
            or "\\" in file_path
            or "\x00" in file_path
            or ":" in file_path.split("/")[0]
            or any(part in ("", ".", "..") for part in file_path.split("/"))
        ):
            errors.append(
                f"File path {file_path!r} must be repository-relative "
                "without traversal."
            )
            continue
        if not isinstance(line_comments, dict):
            errors.append(
                f"inline_comments[{file_path!r}] must be a JSON object (dict), "
                f"got {type(line_comments).__name__}."
            )
            continue

        if file_path not in diff_files:
            errors.append(
                f"File path {file_path!r} in 'inline_comments' does not"
                " appear in the diff."
            )
            # Still validate line/comment types even for unknown paths.

        valid_lines = diff_files.get(file_path, set())

        for line_key, comment in line_comments.items():
            if (
                not isinstance(line_key, str)
                or not line_key.isascii()
                or not line_key.isdecimal()
                or line_key.startswith("0")
            ):
                errors.append(
                    f"inline_comments[{file_path!r}]: line key {line_key!r} is not "
                    "a positive decimal integer string."
                )
                continue
            try:
                line_number = int(line_key)
            except ValueError:
                errors.append(f"Line number {line_key!r} is too large.")
                continue

            if not isinstance(comment, str):
                errors.append(
                    f"inline_comments[{file_path!r}][{line_key!r}] must be a string, "
                    f"got {type(comment).__name__}."
                )

            if file_path in diff_files and line_number not in valid_lines:
                errors.append(
                    f"Line {line_number} of {file_path!r} does not appear in the diff."
                )

    return errors


def repository_files(root: Path) -> frozenset[str]:
    """List repository-relative files and symlinked directories under *root*."""
    paths: set[str] = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name.casefold() != ".git"]
        relative = Path(directory).relative_to(root)
        paths.update(
            (relative / name).as_posix() for name in files if name.casefold() != ".git"
        )
        paths.update(
            (relative / name).as_posix()
            for name in directories
            if (Path(directory) / name).is_symlink()
        )
    return frozenset(paths)


def parse_review_document(
    text: str,
    *,
    diff_text: str,
    path_prefix: str | None = None,
    repository_files: Set[str] = frozenset(),
) -> tuple[dict | None, str | None]:
    """Parse one terminal review document and validate it against the diff.

    Returns (document, None) when the document is publishable, otherwise
    (None, failure) with a message describing every reason it is not.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0] not in ("```", "```json") or lines[-1] != "```":
            return None, "Review must contain one JSON object or exact JSON fence."
        text = "\n".join(lines[1:-1])
    try:
        result = json.loads(text, parse_constant=_reject_json_constant)
    except ValueError as exc:
        return None, f"Invalid review JSON: {exc}"
    errors = validate_review_json(
        result, diff_text, path_prefix=path_prefix, repository_files=repository_files
    )
    if errors:
        return None, "Invalid review document: " + "\n".join(errors)
    return result, None


def _decode_diff_path(value: str) -> str | None:
    value = value.partition("\t")[0]
    if not value.startswith('"'):
        return value
    # Git quotes C byte strings, including octal UTF-8 and literal Unicode.
    encoded = value.encode("utf-8").decode("ascii", "backslashreplace")
    try:
        path = ast.literal_eval("b" + encoded)
    except (SyntaxError, ValueError):
        return None
    return os.fsdecode(path) if isinstance(path, bytes) else None


def _repository_path(
    path: str,
    old_path: str | None,
    renamed_path: str | None,
    prefix: str | None,
    repository_files: Set[str],
) -> str:
    if prefix is not None:
        if not path.startswith(prefix) or not path[len(prefix) :]:
            raise ValueError(
                f"Diff path {path!r} does not match --diff-prefix {prefix!r}"
            )
        return path[len(prefix) :]
    if renamed_path is not None and path.endswith(renamed_path):
        return renamed_path
    if old_path is not None and old_path.startswith("a/") and path.startswith("b/"):
        return path[2:]

    candidates = [
        path[index:] for index in range(len(path)) if path[index:] in repository_files
    ]
    if len(candidates) > 1:
        raise ValueError(
            f"Ambiguous diff path {path!r}; specify --diff-prefix PREFIX "
            "(use an empty string for prefix-free paths)"
        )
    if candidates:
        return candidates[0]

    if old_path == path:
        return path
    if old_path is not None and old_path != "/dev/null":
        old_parts, new_parts = old_path.split("/"), path.split("/")
        count = 0
        for old, new in zip(reversed(old_parts), reversed(new_parts)):
            if old != new:
                break
            count += 1
        if count:
            return "/".join(new_parts[-count:])
    return path.removeprefix("b/")


def _hunk_range(value: str) -> tuple[int, int]:
    start, separator, count = value.partition(",")
    return int(start), int(count) if separator else 1


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-JSON numeric constant: {value}")

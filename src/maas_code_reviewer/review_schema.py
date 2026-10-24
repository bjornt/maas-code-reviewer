# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

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


def parse_diff_files_and_lines(diff_text: str) -> dict[str, set[int]]:
    """Map new-side paths to lines actually present inside unified diff hunks."""
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    current_line = 0
    old_remaining = new_remaining = 0

    for raw_line in diff_text.splitlines():
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

        if raw_line.startswith("diff --git ") or raw_line.startswith("--- "):
            current_file = None
        elif raw_line.startswith("+++ "):
            path = raw_line[4:]
            if path.startswith("b/"):
                path = path[2:]
            current_file = None if path == "/dev/null" else path
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


def validate_review_json(data: object, diff_text: str) -> list[str]:
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

    diff_files = parse_diff_files_and_lines(diff_text)

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


def _hunk_range(value: str) -> tuple[int, int]:
    start, separator, count = value.partition(",")
    return int(start), int(count) if separator else 1

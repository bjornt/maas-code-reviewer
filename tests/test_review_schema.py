# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import pytest

from maas_code_reviewer.review_schema import (
    parse_diff_files_and_lines,
    validate_review_json,
)

DIFF = (
    "diff --git a/src/file.py b/src/file.py\n"
    "--- a/src/file.py\n+++ b/src/file.py\n"
    "@@ -1,3 +1,4 @@\n context\n-old\n+replacement\n\n+addition\n"
    "@@ -10 +11 @@\n later context\n"
    "diff --git a/new.py b/new.py\n"
    "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+new file\n"
    "diff --git a/deleted.py b/deleted.py\n"
    "--- a/deleted.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-deleted file\n"
)


def test_new_side_coordinates_include_context_and_additions_not_deletions() -> None:
    assert parse_diff_files_and_lines(DIFF) == {
        "src/file.py": {1, 2, 3, 4, 11},
        "new.py": {1},
    }


def test_multifile_inline_findings_accept_only_present_new_side_lines() -> None:
    document: dict = {
        "general_comment": "Fix both files.",
        "inline_comments": {
            "src/file.py": {
                "2": "Replacement needs a check.",
                "11": "Context matters.",
            },
            "new.py": {"1": "New file needs a check."},
        },
    }
    assert validate_review_json(document, DIFF) == []
    document["inline_comments"]["src/file.py"]["5"] = "Outside all hunks."
    assert validate_review_json(document, DIFF)


def test_plain_relative_paths_without_git_prefix_are_preserved() -> None:
    diff = "--- source.py\n+++ source.py\n@@ -1 +1 @@\n-old\n+new\n"
    assert parse_diff_files_and_lines(diff) == {"source.py": {1}}


def test_empty_diff_has_no_inline_coordinates() -> None:
    assert parse_diff_files_and_lines("") == {}
    assert (
        validate_review_json(
            {"general_comment": "No inline findings.", "inline_comments": {}},
            "",
        )
        == []
    )


def test_header_shaped_added_content_does_not_create_a_fictitious_file() -> None:
    diff = (
        "--- a/real.py\n+++ b/real.py\n@@ -0,0 +1,3 @@\n"
        "+++ b/fictitious.py\n+diff --git a/fake b/fake\n+final\n"
    )
    assert parse_diff_files_and_lines(diff) == {"real.py": {1, 2, 3}}
    assert validate_review_json(
        {
            "general_comment": "A finding.",
            "inline_comments": {"fictitious.py": {"2": "Not a changed path."}},
        },
        diff,
    )


def test_lines_outside_declared_hunk_ranges_do_not_become_coordinates() -> None:
    diff = (
        "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n unchanged\n"
        "+not actually in a hunk\n metadata\n"
    )
    assert parse_diff_files_and_lines(diff) == {"file.py": {1}}


def test_no_newline_markers_do_not_advance_the_new_side_line() -> None:
    diff = (
        "--- a/file.py\n+++ b/file.py\n@@ -1,2 +1,2 @@\n"
        "-old\n\\ No newline at end of file\n+new\n"
        "\\ No newline at end of file\n context\n"
    )
    assert parse_diff_files_and_lines(diff) == {"file.py": {1, 2}}


@pytest.mark.parametrize(
    "header",
    [
        "@@ malformed",
        "@@ 1 +1 @@",
        "@@ -1 1 @@",
        "@@ -1 +1 invalid",
        "@@ -abc +1 @@",
        "@@ -1 +abc @@",
        "@@ --1 +1 @@",
        "@@ -1 +-1 @@",
        "@@ -1,-1 +1 @@",
        "@@ -1 +1,-1 @@",
        "@@ -0 +1 @@",
        "@@ -1 +0 @@",
    ],
)
def test_malformed_hunks_never_authorize_inline_coordinates(header: str) -> None:
    diff = f"--- a/file.py\n+++ b/file.py\n{header}\n+untrusted\n context\n"
    assert parse_diff_files_and_lines(diff) == {"file.py": set()}
    assert validate_review_json(
        {
            "general_comment": "A finding.",
            "inline_comments": {"file.py": {"1": "Not an authorized coordinate."}},
        },
        diff,
    )


def test_incomplete_hunk_resets_at_metadata_then_accepts_the_next_hunk() -> None:
    diff = (
        "--- a/file.py\n+++ b/file.py\n@@ -1,3 +1,3 @@\n first\n"
        "index metadata\n+not inside a hunk\n@@ -9 +9 @@\n last\n"
    )
    assert parse_diff_files_and_lines(diff) == {"file.py": {1, 9}}


def test_orphan_hunks_cannot_authorize_any_repository_path() -> None:
    diff = "@@ -1 +1,2 @@\n context\n+addition\n"
    assert parse_diff_files_and_lines(diff) == {}


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "review",
        1,
        {},
        {"inline_comments": {}},
        {"general_comment": "review"},
        {"general_comment": None, "inline_comments": {}},
        {"general_comment": 1, "inline_comments": {}},
        {"general_comment": "review", "inline_comments": []},
        {"general_comment": "review", "inline_comments": None},
        {"general_comment": "review", "inline_comments": {"new.py": []}},
    ],
)
def test_malformed_document_shapes_are_rejected_without_raising(document) -> None:
    assert validate_review_json(document, DIFF)


@pytest.mark.parametrize(
    "path",
    [
        "unknown.py",
        "deleted.py",
        "/project/src/file.py",
        "/src/file.py",
        "../src/file.py",
        "src/../src/file.py",
        "./src/file.py",
        "src//file.py",
        "src/file.py/",
        "",
        "C:/src/file.py",
        "C:\\src\\file.py",
        "src\x00/file.py",
        7,
    ],
)
def test_unknown_or_noncanonical_paths_are_rejected(path) -> None:
    document = {
        "general_comment": "Review.",
        "inline_comments": {path: {"1": "Invalid path."}},
    }
    assert validate_review_json(document, DIFF)


@pytest.mark.parametrize(
    "path",
    [
        "/project/file.py",
        "../file.py",
        "./file.py",
        "a//file.py",
        "C:/file.py",
        "dir\\file.py",
    ],
)
def test_unsafe_paths_are_invalid_even_when_a_diff_header_mentions_them(
    path: str,
) -> None:
    diff = f"--- old\n+++ b/{path}\n@@ -1 +1 @@\n+changed\n"
    document = {
        "general_comment": "Review.",
        "inline_comments": {path: {"1": "Still an unsafe coordinate."}},
    }
    assert validate_review_json(document, diff)


@pytest.mark.parametrize(
    "line",
    [
        "999",
        "0",
        "-1",
        "+1",
        "01",
        " 1",
        "1 ",
        "1.0",
        "abc",
        "١",
        "",
        1,
        None,
        "9" * 5000,
    ],
)
def test_invalid_line_coordinates_are_rejected_without_raising(line) -> None:
    document = {
        "general_comment": "Review.",
        "inline_comments": {"new.py": {line: "Invalid line."}},
    }
    assert validate_review_json(document, DIFF)


@pytest.mark.parametrize("comment", [None, 7, [], {}, False])
def test_inline_findings_must_be_strings(comment) -> None:
    document = {
        "general_comment": "Review.",
        "inline_comments": {"new.py": {"1": comment}},
    }
    assert validate_review_json(document, DIFF)


def test_unknown_file_still_reports_bad_line_and_comment_types() -> None:
    errors = validate_review_json(
        {
            "general_comment": "Review.",
            "inline_comments": {"unknown.py": {"bad": "x", "1": None}},
        },
        DIFF,
    )
    assert len(errors) == 3

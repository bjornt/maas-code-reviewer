# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json

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


@pytest.mark.parametrize(
    ("source_prefix", "destination_prefix"),
    [
        ("i/", "w/"),
        ("old/tree/", "new/tree/"),
        ("left_", "right_"),
        ("same/", "same/"),
        ("", ""),
        ("old prefix/", "new prefix/"),
        ('source/é"/', 'target/é"/'),
        ("before\u2028/", "after\u2028/"),
        ("before\n/", "after\n/"),
    ],
)
def test_arbitrary_prefixes_authorize_only_repository_coordinates(
    source_prefix: str, destination_prefix: str
) -> None:
    old = json.dumps(source_prefix + "src/file.py", ensure_ascii=False)
    new = json.dumps(destination_prefix + "src/file.py", ensure_ascii=False)
    diff = (
        f"diff --git {old} {new}\n--- {old}\n+++ {new}\n"
        "@@ -1,2 +1,2 @@\n context\n-old\n+new\n"
    )
    files = frozenset({"src/file.py"})
    document: dict = {
        "general_comment": "Review.",
        "inline_comments": {"src/file.py": {"2": "Check this change."}},
    }
    assert validate_review_json(document, diff, repository_files=files) == []
    document["inline_comments"]["src/file.py"]["3"] = "Outside the hunk."
    assert validate_review_json(document, diff, repository_files=files)
    document["inline_comments"] = {"file.py": {"2": "Wrong repository path."}}
    assert validate_review_json(document, diff, repository_files=files)


def test_mnemonic_prefixes_and_prefixed_additions_work_without_checkout_hints() -> None:
    diff = (
        "diff --git before/src/file.py after/src/file.py\n"
        "--- /dev/null\n+++ after/src/file.py\n@@ -0,0 +1 @@\n+new\n"
        "diff --git i/other.py w/other.py\n"
        "--- i/other.py\n+++ w/other.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    assert parse_diff_files_and_lines(diff) == {"src/file.py": {1}, "other.py": {1}}


@pytest.mark.parametrize("path", ["b/src/file.py", "w/src/file.py"])
def test_prefix_free_directory_names_are_not_stripped(path: str) -> None:
    diff = (
        f"diff --git {path} {path}\n--- {path}\n+++ {path}\n@@ -1 +1 @@\n-old\n+new\n"
    )
    assert parse_diff_files_and_lines(diff) == {path: {1}}


@pytest.mark.parametrize("operation", ["rename", "copy"])
def test_extended_paths_disambiguate_renamed_and_copied_files(operation: str) -> None:
    diff = (
        "diff --git previous/old/name.py current/new/name.py\n"
        f"{operation} from old/name.py\n{operation} to new/name.py\n"
        "--- previous/old/name.py\n+++ current/new/name.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    files = frozenset({"new/name.py", "name.py"})
    assert parse_diff_files_and_lines(diff, repository_files=files) == {
        "new/name.py": {1}
    }


@pytest.mark.parametrize("old_path", ["b/file.py", "old/file.py"])
def test_standard_prefixes_preserve_nested_files_with_matching_basenames(
    old_path: str,
) -> None:
    diff = f"--- a/{old_path}\n+++ b/b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
    assert parse_diff_files_and_lines(
        diff, repository_files=frozenset({"b/file.py", "file.py"})
    ) == {"b/file.py": {1}}


def test_ambiguous_suffixes_require_an_explicit_prefix() -> None:
    diff = (
        "--- before/common/src/file.py\n+++ after/common/src/file.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    files = frozenset({"src/file.py", "file.py"})
    with pytest.raises(ValueError):
        parse_diff_files_and_lines(diff, repository_files=files)
    assert parse_diff_files_and_lines(
        diff, path_prefix="after/common/", repository_files=files
    ) == {"src/file.py": {1}}


@pytest.mark.parametrize("prefix", ["wrong/", "b/file.py"])
def test_an_explicit_prefix_must_leave_a_nonempty_matching_path(prefix: str) -> None:
    diff = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
    with pytest.raises(ValueError):
        parse_diff_files_and_lines(diff, path_prefix=prefix)


def test_an_empty_explicit_prefix_preserves_a_real_b_directory() -> None:
    diff = "--- b/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
    assert parse_diff_files_and_lines(
        diff, path_prefix="", repository_files=frozenset({"b/file.py", "file.py"})
    ) == {"b/file.py": {1}}


@pytest.mark.parametrize("encoded", ['"unterminated', '"x" + "y"', '"x", "y"'])
def test_malformed_quoted_paths_do_not_authorize_findings(encoded: str) -> None:
    diff = f"--- old\n+++ {encoded}\n@@ -1 +1 @@\n-old\n+new\n"
    assert parse_diff_files_and_lines(diff) == {}


def test_c_quoted_unicode_and_timestamp_headers_are_decoded() -> None:
    diff = (
        '--- "before/caf\\303\\251.py"\told timestamp\n'
        '+++ "after/caf\\303\\251.py"\tnew timestamp\n'
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    assert parse_diff_files_and_lines(diff) == {"café.py": {1}}


def test_missing_or_inconsistent_old_metadata_does_not_authorize_extra_lines() -> None:
    diff = (
        '--- "unterminated\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n'
        "diff --git unrelated metadata\n--- /dev/null\n+++ next.py\n"
        "@@ -0,0 +1 @@\n+new\n"
    )
    assert parse_diff_files_and_lines(diff) == {"file.py": {1}, "next.py": {1}}


def test_crlf_diff_headers_preserve_repository_coordinates() -> None:
    diff = "--- i/file.py\n+++ w/file.py\n@@ -1 +1 @@\n-old\n+new\n"
    assert parse_diff_files_and_lines(diff.replace("\n", "\r\n")) == {"file.py": {1}}


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

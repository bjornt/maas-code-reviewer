# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
from pathlib import Path

import pytest

from maas_code_reviewer.review_schema import (
    parse_diff_files_and_lines,
    parse_review_document,
    repository_files,
    validate_review_json,
)

# ---------------------------------------------------------------------------
# Sample diffs used across multiple tests
# ---------------------------------------------------------------------------

SIMPLE_DIFF = """\
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,4 +1,5 @@
 import os
+import sys

 def main():
-    pass
+    print("hello")
"""

# A diff touching two files
TWO_FILE_DIFF = """\
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,4 @@
 import os
+import sys

 def main():
--- a/src/bar.py
+++ b/src/bar.py
@@ -10,4 +10,5 @@
 def helper():
+    # new comment
     return 42

 def other():
"""

# A diff that deletes a file (target is /dev/null)
DELETE_FILE_DIFF = """\
--- a/old.py
+++ /dev/null
@@ -1,3 +0,0 @@
-import os
-
-def gone(): pass
"""

# A diff that creates a new file
NEW_FILE_DIFF = """\
--- /dev/null
+++ b/new_module.py
@@ -0,0 +1,3 @@
+import os
+
+def fresh(): pass
"""


# ---------------------------------------------------------------------------
# TestParseDiffFilesAndLines
# ---------------------------------------------------------------------------


class TestParseDiffFilesAndLines:
    def test_returns_dict(self) -> None:
        result = parse_diff_files_and_lines(SIMPLE_DIFF)
        assert isinstance(result, dict)

    def test_identifies_correct_file(self) -> None:
        result = parse_diff_files_and_lines(SIMPLE_DIFF)
        assert "src/foo.py" in result

    def test_strips_b_prefix_from_path(self) -> None:
        result = parse_diff_files_and_lines(SIMPLE_DIFF)
        # Should NOT have the raw "b/src/foo.py" key
        assert "b/src/foo.py" not in result

    def test_line_numbers_are_integers(self) -> None:
        result = parse_diff_files_and_lines(SIMPLE_DIFF)
        for lines in result.values():
            for ln in lines:
                assert isinstance(ln, int)

    def test_simple_diff_line_numbers(self) -> None:
        # Hunk: @@ -1,4 +1,5 @@
        # new-file lines: 1 (context "import os"), 2 (added "import sys"),
        #                 3 (context blank), 4 (context "def main():"),
        #                 5 (added "    print(...)")
        # deletion ("-    pass") does NOT advance new-file counter
        result = parse_diff_files_and_lines(SIMPLE_DIFF)
        lines = result["src/foo.py"]
        assert 1 in lines  # context: import os
        assert 2 in lines  # added: import sys
        assert 3 in lines  # context: blank line
        assert 4 in lines  # context: def main():
        assert 5 in lines  # added: print(...)

    def test_deletion_lines_not_included(self) -> None:
        # The "-    pass" line is a deletion; it must not appear in the set
        result = parse_diff_files_and_lines(SIMPLE_DIFF)
        lines = result["src/foo.py"]
        # Line 5 in the OLD file is the deletion but new-file line 5 is the addition.
        # The important thing is that we never include a new-file line number
        # purely from a deletion.  The deletion in this hunk is at old line 4;
        # its new-file equivalent would be between new lines 4 and 5.
        # We verify by checking that we have exactly the expected count.
        # (5 new-side lines total: 3 context + 2 added)
        assert len(lines) == 5

    def test_two_file_diff(self) -> None:
        result = parse_diff_files_and_lines(TWO_FILE_DIFF)
        assert "src/foo.py" in result
        assert "src/bar.py" in result

    def test_two_file_diff_bar_lines(self) -> None:
        # Hunk for bar.py: @@ -10,4 +10,5 @@
        # new-file lines starting at 10:
        #   10: context "def helper():"
        #   11: added "    # new comment"
        #   12: context "    return 42"
        #   13: context blank
        #   14: context "def other():"
        result = parse_diff_files_and_lines(TWO_FILE_DIFF)
        lines = result["src/bar.py"]
        assert 10 in lines
        assert 11 in lines
        assert 12 in lines
        assert 13 in lines
        assert 14 in lines

    def test_deleted_file_not_in_result(self) -> None:
        # When +++ is /dev/null the file should not appear
        result = parse_diff_files_and_lines(DELETE_FILE_DIFF)
        assert "old.py" not in result
        assert "/dev/null" not in result

    def test_new_file_included(self) -> None:
        result = parse_diff_files_and_lines(NEW_FILE_DIFF)
        assert "new_module.py" in result

    def test_new_file_line_numbers(self) -> None:
        # @@ -0,0 +1,3 @@ — new file, 3 added lines starting at 1
        result = parse_diff_files_and_lines(NEW_FILE_DIFF)
        lines = result["new_module.py"]
        assert 1 in lines
        assert 2 in lines
        assert 3 in lines

    def test_empty_diff_returns_empty_dict(self) -> None:
        result = parse_diff_files_and_lines("")
        assert result == {}

    def test_multiple_hunks_same_file(self) -> None:
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\n"
            "+added_a\n"
            " line2\n"
            "@@ -10,2 +11,3 @@\n"
            " line10\n"
            "+added_b\n"
            " line11\n"
        )
        result = parse_diff_files_and_lines(diff)
        lines = result["x.py"]
        # First hunk: 1, 2, 3
        assert 1 in lines
        assert 2 in lines
        assert 3 in lines
        # Second hunk: 11, 12, 13
        assert 11 in lines
        assert 12 in lines
        assert 13 in lines

    def test_path_without_b_prefix_kept_as_is(self) -> None:
        diff = "--- a/plain.py\n+++ plain.py\n@@ -1 +1 @@\n+x\n"
        result = parse_diff_files_and_lines(diff)
        assert "plain.py" in result

    def test_diff_git_header_alone_records_no_file(self) -> None:
        # A header without a patch authorizes nothing.
        result = parse_diff_files_and_lines("diff --git a/x.py b/x.py\n")
        assert result == {}

    def test_rename_to_target_is_the_repository_path(self) -> None:
        diff = (
            "diff --git previous/old/name.py current/new/name.py\n"
            "rename from old/name.py\n"
            "rename to new/name.py\n"
            "--- previous/old/name.py\n"
            "+++ current/new/name.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        result = parse_diff_files_and_lines(
            diff, repository_files=frozenset({"new/name.py", "name.py"})
        )
        assert result == {"new/name.py": {1}}

    def test_copy_to_target_is_the_repository_path(self) -> None:
        diff = (
            "diff --git previous/old/name.py current/new/name.py\n"
            "copy from old/name.py\n"
            "copy to new/name.py\n"
            "--- previous/old/name.py\n"
            "+++ current/new/name.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        result = parse_diff_files_and_lines(
            diff, repository_files=frozenset({"new/name.py", "name.py"})
        )
        assert result == {"new/name.py": {1}}

    def test_dev_null_source_with_git_header_resolves_prefixed_new_file(self) -> None:
        # The old-side name is recovered from the "diff --git" header, so the
        # common-suffix ladder can resolve the prefixed new-file path.
        diff = (
            "diff --git before/x.py after/x.py\n"
            "--- /dev/null\n"
            "+++ after/x.py\n"
            "@@ -0,0 +1 @@\n"
            "+new\n"
        )
        result = parse_diff_files_and_lines(diff)
        assert result == {"x.py": {1}}

    def test_crlf_diff_parses_like_lf(self) -> None:
        diff = "--- i/file.py\n+++ w/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse_diff_files_and_lines(diff.replace("\n", "\r\n"))
        assert result == {"file.py": {1}}

    def test_tab_timestamp_after_path_is_ignored(self) -> None:
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\t2026-10-27 10:00:00\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        result = parse_diff_files_and_lines(diff)
        assert result == {"x.py": {1}}

    @pytest.mark.parametrize(
        ("diff", "expected"),
        [
            (
                '--- "before/caf\\303\\251.py"\told stamp\n'
                '+++ "after/caf\\303\\251.py"\tnew stamp\n'
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n",
                {"café.py": {1}},
            ),
            (
                '--- "target/é.py"\n'
                '+++ "target/é.py"\n'
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n",
                {"target/é.py": {1}},
            ),
        ],
    )
    def test_quoted_c_style_paths_are_decoded(
        self, diff: str, expected: dict[str, set[int]]
    ) -> None:
        assert parse_diff_files_and_lines(diff) == expected

    @pytest.mark.parametrize(
        "encoded_path", ['"unterminated', '"x" + "y"', '"x", "y"']
    )
    def test_malformed_quoted_path_records_no_file(self, encoded_path: str) -> None:
        diff = f"--- a/x.py\n+++ {encoded_path}\n@@ -1 +1 @@\n-old\n+new\n"
        assert parse_diff_files_and_lines(diff) == {}

    def test_hunk_content_looking_like_a_header_stays_content(self) -> None:
        # While the declared hunk budgets remain, content-shaped lines are
        # content, not headers — the old parser could be hijacked this way.
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1 +1,2 @@\n"
            "-old\n"
            "+diff --git a/evil.py b/evil.py\n"
            "+second\n"
        )
        result = parse_diff_files_and_lines(diff)
        assert result == {"x.py": {1, 2}}
        assert "evil.py" not in result

    def test_no_newline_marker_is_skipped(self) -> None:
        # "\ No newline at end of file" consumes no budget and no line number.
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,2 +1,2 @@\n"
            " line1\n"
            "\\ No newline at end of file\n"
            " line2\n"
        )
        result = parse_diff_files_and_lines(diff)
        assert result == {"x.py": {1, 2}}

    def test_truncated_hunk_recovers_at_next_file_header(self) -> None:
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,5 +1,5 @@\n"
            " line1\n"
            " line2\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1 +1 @@\n"
            "+x\n"
        )
        result = parse_diff_files_and_lines(diff)
        assert result == {"x.py": {1, 2}, "b.py": {1}}

    def test_malformed_hunk_header_records_no_lines(self) -> None:
        # A non-integer new-file start sets no budgets; its content is not
        # recorded (the old parser recorded a bogus line 0).
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +abc @@\n+added\n"
        result = parse_diff_files_and_lines(diff)
        assert "f.py" in result
        assert result["f.py"] == set()
        assert 0 not in result["f.py"]

    def test_short_hunk_header_is_rejected(self) -> None:
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +1\n+added\n"
        result = parse_diff_files_and_lines(diff)
        assert result == {"f.py": set()}

    def test_hunk_with_function_context_suffix_is_parsed(self) -> None:
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@ def main():\n one\n+two\n"
        result = parse_diff_files_and_lines(diff)
        assert result == {"f.py": {1, 2}}

    def test_zero_start_with_nonzero_count_is_rejected(self) -> None:
        diff = "--- a/f.py\n+++ b/f.py\n@@ -0,1 +1,2 @@\n+added\n context\n"
        result = parse_diff_files_and_lines(diff)
        assert result == {"f.py": set()}

    def test_negative_start_is_rejected(self) -> None:
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +-1,2 @@\n+added\n context\n"
        result = parse_diff_files_and_lines(diff)
        assert result == {"f.py": set()}

    def test_path_prefix_strips_matching_prefix(self) -> None:
        diff = "--- i/src/foo.py\n+++ w/src/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse_diff_files_and_lines(diff, path_prefix="w/")
        assert result == {"src/foo.py": {1}}

    def test_path_prefix_mismatch_raises_value_error(self) -> None:
        diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(ValueError, match="--diff-prefix"):
            parse_diff_files_and_lines(diff, path_prefix="wrong/")

    def test_path_prefix_requires_nonempty_suffix(self) -> None:
        # The whole path being the prefix leaves nothing to resolve.
        diff = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(ValueError):
            parse_diff_files_and_lines(diff, path_prefix="b/file.py")

    def test_empty_path_prefix_preserves_prefix_free_paths(self) -> None:
        # An empty prefix legitimately preserves a real directory named "b".
        diff = "--- b/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse_diff_files_and_lines(diff, path_prefix="")
        assert result == {"b/file.py": {1}}

    def test_real_directory_named_b_not_stripped_without_a_prefix(self) -> None:
        # Without a/… on the old side, "b/" is not stripped.
        diff = "--- b/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse_diff_files_and_lines(diff)
        assert result == {"b/file.py": {1}}

    def test_repository_files_listing_disambiguates_prefixed_path(self) -> None:
        diff = "--- i/src/file.py\n+++ w/src/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse_diff_files_and_lines(
            diff, repository_files=frozenset({"src/file.py"})
        )
        assert result == {"src/file.py": {1}}

    def test_ambiguous_suffix_raises_value_error_naming_diff_prefix(self) -> None:
        diff = (
            "--- before/common/src/file.py\n"
            "+++ after/common/src/file.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        files = frozenset({"src/file.py", "file.py"})
        with pytest.raises(ValueError) as exc_info:
            parse_diff_files_and_lines(diff, repository_files=files)
        assert "Ambiguous diff path" in str(exc_info.value)
        assert "--diff-prefix" in str(exc_info.value)
        result = parse_diff_files_and_lines(
            diff, path_prefix="after/common/", repository_files=files
        )
        assert result == {"src/file.py": {1}}

    def test_common_suffix_resolution_without_listing(self) -> None:
        diff = (
            "--- before/src/file.py\n"
            "+++ after/src/file.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        result = parse_diff_files_and_lines(diff)
        assert result == {"src/file.py": {1}}

    def test_unprefixed_identical_paths_are_preserved(self) -> None:
        diff = "--- src/foo.py\n+++ src/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse_diff_files_and_lines(diff)
        assert result == {"src/foo.py": {1}}


# ---------------------------------------------------------------------------
# TestValidateReviewJson
# ---------------------------------------------------------------------------

_VALID_DATA = {
    "general_comment": "Looks good overall.",
    "inline_comments": {
        "src/foo.py": {
            "2": "Good use of sys here.",
        }
    },
}


class TestValidateReviewJson:
    def test_valid_data_returns_no_errors(self) -> None:
        errors = validate_review_json(_VALID_DATA, SIMPLE_DIFF)
        assert errors == []

    def test_empty_inline_comments_is_valid(self) -> None:
        data = {"general_comment": "Fine.", "inline_comments": {}}
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert errors == []

    def test_non_dict_root_returns_error(self) -> None:
        errors = validate_review_json(["not", "a", "dict"], SIMPLE_DIFF)  # type: ignore[arg-type]
        assert len(errors) == 1
        assert "dict" in errors[0].lower() or "object" in errors[0].lower()

    def test_missing_general_comment(self) -> None:
        data: dict = {"inline_comments": {}}
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("general_comment" in e for e in errors)

    def test_general_comment_wrong_type(self) -> None:
        data = {"general_comment": 42, "inline_comments": {}}
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("general_comment" in e for e in errors)

    def test_missing_inline_comments(self) -> None:
        data: dict = {"general_comment": "ok"}
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("inline_comments" in e for e in errors)

    def test_inline_comments_wrong_type(self) -> None:
        data = {"general_comment": "ok", "inline_comments": "not a dict"}
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("inline_comments" in e for e in errors)

    def test_per_file_value_not_dict(self) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {"src/foo.py": "not a dict"},
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("src/foo.py" in e for e in errors)

    def test_file_path_not_in_diff(self) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {
                "nonexistent/file.py": {"1": "some comment"},
            },
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("nonexistent/file.py" in e for e in errors)

    def test_line_number_not_in_diff(self) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {
                "src/foo.py": {"999": "way out of range"},
            },
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("999" in e for e in errors)

    def test_line_key_not_an_integer_string(self) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {
                "src/foo.py": {"abc": "bad key"},
            },
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("abc" in e for e in errors)

    def test_comment_value_wrong_type(self) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {
                "src/foo.py": {"2": 123},
            },
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert any("src/foo.py" in e for e in errors)

    def test_multiple_errors_returned(self) -> None:
        # Both missing keys should produce errors
        errors = validate_review_json({}, SIMPLE_DIFF)
        assert len(errors) >= 2

    def test_valid_two_file_inline_comments(self) -> None:
        data = {
            "general_comment": "Some issues found.",
            "inline_comments": {
                "src/foo.py": {"2": "Nice import."},
                "src/bar.py": {"11": "Good comment."},
            },
        }
        errors = validate_review_json(data, TWO_FILE_DIFF)
        assert errors == []

    def test_line_1_valid_for_new_file(self) -> None:
        data = {
            "general_comment": "Fresh file.",
            "inline_comments": {
                "new_module.py": {"1": "First line of new file."},
            },
        }
        errors = validate_review_json(data, NEW_FILE_DIFF)
        assert errors == []

    def test_deleted_file_path_rejected(self) -> None:
        # old.py was deleted (target is /dev/null) so it has no new-file lines
        data = {
            "general_comment": "ok",
            "inline_comments": {
                "old.py": {"1": "this file was deleted"},
            },
        }
        errors = validate_review_json(data, DELETE_FILE_DIFF)
        assert any("old.py" in e for e in errors)

    def test_context_lines_are_valid_targets(self) -> None:
        # Line 1 in SIMPLE_DIFF is a context line ("import os") — still valid
        data = {
            "general_comment": "ok",
            "inline_comments": {
                "src/foo.py": {"1": "comment on context line"},
            },
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert errors == []

    @pytest.mark.parametrize(
        "file_path",
        [
            "",
            ".",
            "..",
            "/etc/passwd",
            "src//foo.py",
            "./src/foo.py",
            "src/./foo.py",
            "src/../foo.py",
            "src/foo.py/",
            "a\\b.py",
            "a\x00b.py",
            "C:/src/foo.py",
            "host:src/foo.py",
        ],
    )
    def test_unsafe_file_paths_are_rejected(self, file_path: str) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {file_path: {"1": "some comment"}},
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert errors == [
            f"File path {file_path!r} must be repository-relative "
            "without traversal."
        ]

    def test_non_string_file_path_is_rejected(self) -> None:
        data: dict = {"general_comment": "ok", "inline_comments": {2: {"1": "x"}}}
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert len(errors) == 1
        assert "must be repository-relative" in errors[0]

    def test_legal_but_unusual_paths_still_validate(self) -> None:
        diff = (
            "--- a/src/foo bar.py\n"
            "+++ b/src/foo bar.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        data = {
            "general_comment": "ok",
            "inline_comments": {"src/foo bar.py": {"1": "comment"}},
        }
        assert validate_review_json(data, diff) == []

    @pytest.mark.parametrize(
        "line_key", ["", "0", "02", "+2", "-2", " 2", "2 ", "1_0", "1e2", "٢"]
    )
    def test_non_canonical_line_keys_are_rejected(self, line_key: str) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {"src/foo.py": {line_key: "some comment"}},
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert len(errors) == 1
        assert "positive decimal integer string" in errors[0]

    @pytest.mark.parametrize(
        ("diff", "file_path", "line_key"),
        [
            (SIMPLE_DIFF, "src/foo.py", "1"),
            (SIMPLE_DIFF, "src/foo.py", "2"),
            (TWO_FILE_DIFF, "src/bar.py", "10"),
            (TWO_FILE_DIFF, "src/bar.py", "14"),
            (NEW_FILE_DIFF, "new_module.py", "3"),
        ],
    )
    def test_canonical_line_keys_are_valid(
        self, diff: str, file_path: str, line_key: str
    ) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {file_path: {line_key: "some comment"}},
        }
        assert validate_review_json(data, diff) == []

    def test_oversized_line_key_reports_too_large(self) -> None:
        # CPython refuses int() conversion beyond 4300 digits.
        data = {
            "general_comment": "ok",
            "inline_comments": {"src/foo.py": {"1" * 5000: "some comment"}},
        }
        errors = validate_review_json(data, SIMPLE_DIFF)
        assert len(errors) == 1
        assert "too large" in errors[0]

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
    def test_arbitrary_prefix_pairs_authorize_only_repository_coordinates(
        self, source_prefix: str, destination_prefix: str
    ) -> None:
        old_path = json.dumps(source_prefix + "src/file.py", ensure_ascii=False)
        new_path = json.dumps(destination_prefix + "src/file.py", ensure_ascii=False)
        diff = (
            f"diff --git {old_path} {new_path}\n"
            f"--- {old_path}\n"
            f"+++ {new_path}\n"
            "@@ -1,2 +1,2 @@\n"
            " context\n"
            "-old\n"
            "+new\n"
        )
        files = frozenset({"src/file.py"})
        data = {
            "general_comment": "ok",
            "inline_comments": {"src/file.py": {"2": "some comment"}},
        }
        assert validate_review_json(data, diff, repository_files=files) == []
        data["inline_comments"]["src/file.py"]["3"] = "outside the hunk"
        assert validate_review_json(data, diff, repository_files=files)
        data["inline_comments"] = {"file.py": {"2": "wrong repository path"}}
        assert validate_review_json(data, diff, repository_files=files)

    def test_path_prefix_and_repository_files_reach_the_parser(self) -> None:
        diff = "--- i/src/foo.py\n+++ w/src/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        data = {
            "general_comment": "ok",
            "inline_comments": {"src/foo.py": {"1": "some comment"}},
        }
        assert validate_review_json(
            data,
            diff,
            path_prefix="w/",
            repository_files=frozenset({"src/foo.py"}),
        ) == []
        # The parser resolved the diff coordinates; the model must use
        # repository coordinates, not the prefixed diff path.
        data["inline_comments"] = {"w/src/foo.py": {"1": "prefixed path"}}
        assert validate_review_json(
            data,
            diff,
            path_prefix="w/",
            repository_files=frozenset({"src/foo.py"}),
        )

    def test_ambiguity_during_validation_raises_value_error(self) -> None:
        diff = (
            "--- before/common/src/file.py\n"
            "+++ after/common/src/file.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        data = {
            "general_comment": "ok",
            "inline_comments": {"src/file.py": {"1": "some comment"}},
        }
        with pytest.raises(ValueError):
            validate_review_json(
                data, diff, repository_files=frozenset({"src/file.py", "file.py"})
            )


PARSE_DOCUMENT = {
    "general_comment": "ok",
    "inline_comments": {"src/foo.py": {"2": "some comment"}},
}


class TestParseReviewDocument:
    @pytest.mark.parametrize(
        "wrapper",
        [
            lambda text: text,
            lambda text: "```\n" + text + "\n```",
            lambda text: "```json\n" + text + "\n```",
        ],
    )
    def test_accepts_bare_and_fenced_objects(
        self, wrapper, tmp_path: Path
    ) -> None:
        text = wrapper(json.dumps(PARSE_DOCUMENT))
        document, failure = parse_review_document(
            text, diff_text=SIMPLE_DIFF, repository_files=repository_files(tmp_path)
        )
        assert document == PARSE_DOCUMENT
        assert failure is None

    @pytest.mark.parametrize(
        "text",
        [
            "```python\n" + json.dumps(PARSE_DOCUMENT) + "\n```",
            "```\n" + json.dumps(PARSE_DOCUMENT) + "\n",
            "```\n" + json.dumps(PARSE_DOCUMENT) + "\n```\nTrailing prose.",
        ],
    )
    def test_rejects_inexact_fences(self, text: str) -> None:
        document, failure = parse_review_document(text, diff_text=SIMPLE_DIFF)
        assert document is None
        assert failure == "Review must contain one JSON object or exact JSON fence."

    def test_rejects_prose_around_the_object(self) -> None:
        document, failure = parse_review_document(
            "prose " + json.dumps(PARSE_DOCUMENT) + " prose",
            diff_text=SIMPLE_DIFF,
        )
        assert document is None
        assert failure is not None
        assert failure.startswith("Invalid review JSON: ")

    @pytest.mark.parametrize("constant", ["NaN", "Infinity"])
    def test_rejects_non_json_constants(self, constant: str) -> None:
        text = '{"general_comment": ' + constant + ', "inline_comments": {}}'
        document, failure = parse_review_document(text, diff_text=SIMPLE_DIFF)
        assert document is None
        assert failure is not None
        assert "Invalid review JSON" in failure
        assert f"Non-JSON numeric constant: {constant}" in failure

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("true", "Review must be a JSON object (dict), got bool."),
            ("[1, 2]", "Review must be a JSON object (dict), got list."),
        ],
    )
    def test_rejects_non_object_documents(self, text: str, expected: str) -> None:
        document, failure = parse_review_document(text, diff_text=SIMPLE_DIFF)
        assert document is None
        assert failure == f"Invalid review document: {expected}"

    def test_reports_every_schema_error(self) -> None:
        data = {
            "general_comment": "ok",
            "inline_comments": {"unknown.py": {"0": "x"}, "src/foo.py": {"2": 5}},
        }
        expected_errors = validate_review_json(data, SIMPLE_DIFF)
        assert len(expected_errors) == 3
        document, failure = parse_review_document(
            json.dumps(data), diff_text=SIMPLE_DIFF
        )
        assert document is None
        assert failure == "Invalid review document: " + "\n".join(expected_errors)

    def test_threads_prefix_and_repository_listing(self, tmp_path: Path) -> None:
        prefixed_diff = (
            "--- a/updated/tree/src/foo.py\n"
            "+++ b/updated/tree/src/foo.py\n"
            "@@ -1 +1,2 @@\n"
            " context\n+new\n"
        )
        files = frozenset({"src/foo.py"})
        document, failure = parse_review_document(
            json.dumps(PARSE_DOCUMENT),
            diff_text=prefixed_diff,
            path_prefix="b/updated/tree/",
            repository_files=files,
        )
        assert document == PARSE_DOCUMENT
        assert failure is None

        # Without the prefix the prefixed diff paths resolve to prefixed
        # repository coordinates the document never uses.
        document, failure = parse_review_document(
            json.dumps(PARSE_DOCUMENT), diff_text=prefixed_diff, repository_files=files
        )
        assert document is None
        assert failure is not None
        assert "'src/foo.py'" in failure

    def test_valid_document_round_trips(self) -> None:
        text = "```json\n" + json.dumps(PARSE_DOCUMENT) + "\n```"
        document, failure = parse_review_document(text, diff_text=SIMPLE_DIFF)
        assert document == PARSE_DOCUMENT
        assert failure is None


class TestRepositoryFiles:
    def test_walks_files_recursively_as_posix_paths(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("rules")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("code")
        (tmp_path / "src" / "pkg").mkdir()
        (tmp_path / "src" / "pkg" / "bar.py").write_text("code")
        assert repository_files(tmp_path) == frozenset(
            {"AGENTS.md", "src/foo.py", "src/pkg/bar.py"}
        )

    @pytest.mark.parametrize("git_dir_name", [".git", ".GIT"])
    def test_skips_git_directories(self, tmp_path: Path, git_dir_name: str) -> None:
        (tmp_path / git_dir_name / "objects").mkdir(parents=True)
        (tmp_path / git_dir_name / "objects" / "foo").write_text("object")
        (tmp_path / "tracked.py").write_text("code")
        assert repository_files(tmp_path) == frozenset({"tracked.py"})

    def test_skips_git_worktree_pointer_file(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x")
        (tmp_path / "tracked.py").write_text("code")
        assert repository_files(tmp_path) == frozenset({"tracked.py"})

    def test_symlinked_directories_are_entries_without_descent(
        self, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "private.py").write_text("secret")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "link").symlink_to(outside, target_is_directory=True)
        assert repository_files(repo) == frozenset({"link"})

    def test_symlinked_files_are_entries(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_text("content")
        (tmp_path / "alias").symlink_to(tmp_path / "real.txt")
        assert repository_files(tmp_path) == frozenset({"real.txt", "alias"})

    def test_missing_root_yields_empty_listing(self, tmp_path: Path) -> None:
        assert repository_files(tmp_path / "nope") == frozenset()

You MUST produce your review as a JSON object matching this schema:

{
  "general_comment": "<overall review as a string>",
  "inline_comments": {
    "<repository-relative file path>": {
      "<line number as string>": "<comment text>",
      ...
    },
    ...
  }
}

Rules for inline_comments:
- Only include repository-relative paths that appear in the full original
  diff. Never use absolute paths, path traversal, or the diff header's
  prefixed paths.
- Only include positive new-file line numbers present in that file's diff
  hunks.
- Line numbers must be decimal JSON string keys (e.g. "42", not 42).
- Each inline comment must be a string. If none are needed, use {}.

Your terminal answer MUST be exactly one JSON object and nothing else. An
optional enclosing code fence must open with exactly ```json or ``` and
close with exactly ``` on its own line. Do not output prose before or after
the JSON, multiple JSON objects, or non-JSON constants. The host validates
your terminal answer against this schema and the full original diff before
anything is published; the in-session `validate_review` tool can check a
draft, but its verdict is advisory — only host validation decides what is
published. Do not add a review marker or generated-review preamble; the
host adds those at publish time.

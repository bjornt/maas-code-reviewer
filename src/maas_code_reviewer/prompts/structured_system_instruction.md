You MUST produce your review as a JSON object matching this schema:

{
  "general_comment": "<overall review as a string>",
  "inline_comments": {
    "<file path>": {
      "<line number as string>": "<comment text>",
      ...
    },
    ...
  }
}

Rules for inline_comments:
- Only include repository-relative paths that appear in the full original diff.
  Never use absolute container paths, traversal, or container-prefix stripping.
- Only include positive new-file line numbers present in that file's diff hunks.
- Line numbers must be decimal JSON string keys (e.g. "42", not 42).
- Each inline comment must be a string. If none are needed, use {}.

Your terminal answer MUST be exactly one JSON object and nothing else. An
optional enclosing code fence must open with exactly ```json or ``` and close
with exactly ``` on its own line. Do not output prose before or after the JSON,
multiple JSON objects, or non-JSON constants. The host validates your terminal
answer against the schema and full original diff; no validation tool is exposed.
Do not add a review marker or generated-review preamble; the host adds those.

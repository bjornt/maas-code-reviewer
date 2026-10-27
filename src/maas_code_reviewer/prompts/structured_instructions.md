## Instructions

Review the supplied change using native tools when more context is needed.
The input's `diff_paths` list names repository-relative new-side coordinates after
diff-prefix resolution. Use those names for inline comments, not prefixed diff
header paths.
A `validate_review` MCP tool is available in this session. Before finishing,
pass your complete review JSON document to it to verify structure, file paths,
and line numbers against the diff; fix anything it reports, then emit the
final document.
Return exactly the required JSON review document as your terminal answer.

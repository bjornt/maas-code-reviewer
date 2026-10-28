## Instructions

Review the change described by the untrusted JSON input above, using the
native tools when more context is needed. The input's `diff_paths` list
names the repository-relative new-side coordinates of the changed files
after diff-prefix resolution; use those names — not the prefixed paths from
the diff headers — for inline comments. When the initial diff is truncated,
read the omitted files and the authoritative full diff named in the task
before raising concerns that could be resolved from them.

Return exactly one JSON review document as your terminal answer, following
the schema and output rules in the system instructions. Do not call any
validation tool first; the host validates your terminal answer against the
schema and the full original diff.

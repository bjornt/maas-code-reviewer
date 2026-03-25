# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from __future__ import annotations

from google.genai import types

from maas_code_reviewer.llm_client import _print_thoughts


class TestPrintThoughts:
    # candidates is None when the *prompt* is blocked by a safety filter before
    # any generation starts.  The API sets prompt_feedback.block_reason instead
    # and returns no candidates at all.
    def test_none_candidates_prints_nothing(self, capsys: object) -> None:
        response = types.GenerateContentResponse(candidates=None)
        _print_thoughts(response)
        assert capsys.readouterr().err == ""  # type: ignore[union-attr]

    # candidates=[] is the same safety-block scenario reached via a slightly
    # different API path; both are falsy, so the same guard covers them.
    def test_empty_candidates_prints_nothing(self, capsys: object) -> None:
        response = types.GenerateContentResponse(candidates=[])
        _print_thoughts(response)
        assert capsys.readouterr().err == ""  # type: ignore[union-attr]

    # content is None when a candidate was started but its *output* was then
    # safety-filtered during generation (finish_reason=SAFETY).  The candidate
    # object exists but carries no returnable content.
    def test_none_content_prints_nothing(self, capsys: object) -> None:
        response = types.GenerateContentResponse(
            candidates=[types.Candidate(content=None)]
        )
        _print_thoughts(response)
        assert capsys.readouterr().err == ""  # type: ignore[union-attr]

    def test_thought_part_is_printed_to_stderr(self, capsys: object) -> None:
        response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[
                            types.Part(text="I am reasoning.", thought=True),
                            types.Part(text="The code looks good."),
                        ],
                        role="model",
                    )
                )
            ]
        )
        _print_thoughts(response)
        assert capsys.readouterr().err == "[Thinking]\nI am reasoning.\n\n"  # type: ignore[union-attr]

    def test_afc_history_thoughts_are_printed_to_stderr(self, capsys: object) -> None:
        # model turn: thought produced while deciding to call a tool
        model_content = types.Content(
            parts=[types.Part(text="I need to call read_file.", thought=True)],
            role="model",
        )
        # user turn: function response — should be skipped
        user_content = types.Content(
            parts=[types.Part(text="file contents here")],
            role="user",
        )
        response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part(text="Review complete.")],
                        role="model",
                    )
                )
            ],
            automatic_function_calling_history=[model_content, user_content],
        )
        _print_thoughts(response)
        captured = capsys.readouterr()  # type: ignore[union-attr]
        # AFC-step thought is printed; user-role entry and non-thought final
        # part are both skipped.
        assert captured.err == "[Thinking (tool step)]\nI need to call read_file.\n\n"

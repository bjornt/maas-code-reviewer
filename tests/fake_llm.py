from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lp_ci_tools.llm_client import LLMClient


@dataclass
class ToolCall:
    """A tool invocation the fake should perform before returning."""

    name: str
    args: dict[str, str]


@dataclass
class ScriptedResponse:
    """A single scripted response, optionally preceded by tool calls."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class FakeLLMClient(LLMClient):
    """LLMClient fake that returns scripted responses and records calls.

    Each call to ``review()`` pops the next ``ScriptedResponse`` from the
    queue.  If the response includes ``tool_calls``, the fake invokes the
    corresponding tools (by name) with the given arguments before returning
    the response text.

    After the test, ``received_prompts`` and ``received_tools`` expose what
    was passed to each ``review()`` call for assertion.
    """

    def __init__(self, responses: list[ScriptedResponse] | None = None) -> None:
        self._responses: list[ScriptedResponse] = list(responses) if responses else []
        self.received_prompts: list[str] = []
        self.received_tools: list[list[Callable[..., str]]] = []

    def review(self, prompt: str, tools: list[Callable[..., str]]) -> str:
        self.received_prompts.append(prompt)
        self.received_tools.append(tools)

        if not self._responses:
            raise RuntimeError("FakeLLMClient: no more scripted responses")

        response = self._responses.pop(0)

        tools_by_name: dict[str, Callable[..., str]] = {t.__name__: t for t in tools}
        for tc in response.tool_calls:
            fn = tools_by_name.get(tc.name)
            if fn is None:
                raise RuntimeError(
                    f"FakeLLMClient: tool '{tc.name}' not found in provided tools"
                )
            fn(**tc.args)

        return response.text

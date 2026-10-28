# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Protocol behavior through real SDK pipes and a controllable child process."""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import FrozenInstanceError, replace
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError
from acp.connection import StreamDirection, StreamEvent

from maas_code_reviewer.acp_client import (
    _LOG_OWNER,
    AgentProcess,
    _Conversation,
    _SDKLogFilter,
    run_acp,
)
from maas_code_reviewer.agent_runner import (
    AgentUsage,
    CompletionStatus,
    ReviewRequest,
    RunnerCancelledError,
    RunnerConfigurationError,
    RunnerLimitError,
    RunnerRefusedError,
    RunnerResultError,
    RunnerStartupError,
    RunnerTimeoutError,
    RunnerTransportError,
)

MODEL = "provider/exact-model"
FINAL = '{"general_comment":"Final only","inline_comments":[]}'
USAGE = {
    "totalTokens": 101,
    "inputTokens": 70,
    "outputTokens": 20,
    "thoughtTokens": 11,
    "cachedReadTokens": 7,
    "cachedWriteTokens": 3,
}


def visible(text: str = FINAL, identity: str | None = "final") -> dict[str, Any]:
    return {
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "messageId": identity,
            "content": {"type": "text", "text": text},
        }
    }


def tool(
    status: str = "in_progress", *, start: bool = True, **fields: Any
) -> dict[str, Any]:
    update = {
        "sessionUpdate": "tool_call" if start else "tool_call_update",
        "toolCallId": "read-1",
        "status": status,
    }
    if start:
        update["title"] = "Read repository"
    return {"update": update | fields}


def configured(
    tmp_path: Path, **script: Any
) -> tuple[AgentProcess, ReviewRequest, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    trace = tmp_path / "trace.json"
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps({"trace": str(trace), "actions": [visible()]} | script)
    )
    process = AgentProcess(
        command=(
            sys.executable,
            str(Path(__file__).with_name("fake_acp.py")),
            str(scenario),
        ),
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": "", "USER": "review-job"},
        session_cwd="/review/session",
    )
    request = ReviewRequest(
        repository_path=tmp_path / "repo",
        full_diff="original full diff",
        task_prompt=(
            "Review the supplied diff using the repository at /review/repository."
        ),
        system_instruction="Installed by the trusted launch profile, not task text.",
        timeout_seconds=10,
        model=MODEL,
    )
    return process, request, trace


def execute(tmp_path: Path, **script: Any):
    process, request, _ = configured(tmp_path, **script)
    return asyncio.run(run_acp(process, request, deadline=time.monotonic() + 10))


def test_split_frames_chunks_and_real_session_contract(tmp_path):
    process, request, trace = configured(
        tmp_path,
        split_bytes=3,
        actions=[visible(FINAL[:21]), visible(FINAL[21:])],
        usage=USAGE,
    )
    result = asyncio.run(run_acp(process, request, deadline=time.monotonic() + 10))
    assert result.terminal_text == FINAL
    assert result.status is CompletionStatus.COMPLETED
    assert (result.agent_name, result.agent_version, result.model) == (
        "isolated-peer",
        "1.2.3",
        MODEL,
    )
    assert result.usage == AgentUsage(
        input_tokens=70,
        output_tokens=20,
        thinking_tokens=11,
        cache_read_tokens=7,
        cache_write_tokens=3,
        total_tokens=101,
    )
    requests = json.loads(trace.read_text())
    assert [event["method"] for event in requests] == [
        "initialize",
        "session/new",
        "session/prompt",
    ]
    capabilities = requests[0]["params"]["clientCapabilities"]
    assert not capabilities.get("fs")
    assert not capabilities.get("terminal")
    assert not capabilities.get("elicitation")
    assert not capabilities.get("auth")
    assert requests[1]["params"] == {"cwd": "/review/session", "mcpServers": []}
    assert requests[2]["params"]["prompt"] == [
        {"type": "text", "text": request.task_prompt}
    ]
    with pytest.raises(FrozenInstanceError):
        setattr(process, "cwd", Path("/elsewhere"))


@pytest.mark.parametrize(
    ("title", "arguments"),
    [
        (
            "read: Inspecting repository",
            {"path": "src/reviewer.py:50-200", "i": "Reading review code"},
        ),
        (
            "web_search: Checking API behavior",
            {"query": 'site:docs.example.com "API behavior"', "limit": 7},
        ),
    ],
)
def test_tool_identity_and_full_parameters_are_visible_only_on_stderr(
    tmp_path, capsys, title, arguments
):
    arguments = arguments | {
        "nested": {"all": [True, False, None, 1.5, "x" * 5000]},
    }
    result = execute(
        tmp_path,
        actions=[tool("completed", title=title, rawInput=arguments), visible()],
    )
    assert result.terminal_text == FINAL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert json.loads(captured.err) == {
        "toolCallId": "read-1",
        "title": title,
        "rawInput": arguments,
    }


def test_streamed_tool_inputs_escape_controls_and_skip_status_or_output_noise(
    tmp_path, capsys
):
    identity = 'read-1\n\x1b[31m\r\t"\\'
    title = 'read: Inspecting\n\x1b[2J\r\t"\\\u2028\u202e'
    updated_title = "read: Inspecting updated input"
    first_input = {
        "path": 'src/a.py\n\x1b[31m\r\t"\\\x00',
        "range": {"start": 1, "end": 20},
    }
    updated_input = {"path": "src/b.py", "literal": "é\u2028\u2029"}
    result = execute(
        tmp_path,
        actions=[
            tool(toolCallId=identity, title=title),
            tool(start=False, toolCallId=identity),
            tool(start=False, toolCallId=identity, rawInput=None),
            tool(start=False, toolCallId=identity, rawInput=first_input),
            tool(
                start=False,
                toolCallId=identity,
                rawInput={
                    "range": {"end": 20, "start": 1},
                    "path": first_input["path"],
                },
            ),
            tool(start=False, toolCallId=identity, title=updated_title),
            tool(
                start=False,
                toolCallId=identity,
                rawOutput={"secret-tool-output": "must stay private"},
                content=[
                    {
                        "type": "content",
                        "content": {"type": "text", "text": "secret-tool-content"},
                    }
                ],
            ),
            tool(start=False, toolCallId=identity, rawInput=updated_input),
            tool(start=False, toolCallId=identity, rawInput=None),
            tool(start=False, toolCallId=identity, rawInput=None),
            tool("completed", start=False, toolCallId=identity),
            {"stderr": 10},
            visible(),
        ],
    )
    assert result.terminal_text == FINAL
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert [json.loads(line) for line in lines] == [
        {"toolCallId": identity, "title": title, "rawInput": None},
        {"toolCallId": identity, "title": title, "rawInput": None},
        {"toolCallId": identity, "title": title, "rawInput": first_input},
        {"toolCallId": identity, "title": updated_title, "rawInput": updated_input},
        {"toolCallId": identity, "title": updated_title, "rawInput": None},
    ]
    assert all(32 <= ord(character) < 127 for line in lines for character in line)
    assert "secret" not in captured.err


@pytest.mark.parametrize(
    "raw_input", [None, {}, [], [True, None, 3.5], "text", False, 0]
)
def test_tool_logging_flushes_each_complete_json_record(
    tmp_path, monkeypatch, raw_input
):
    stream = StringIO()
    flushed = []
    monkeypatch.setattr(stream, "flush", lambda: flushed.append(stream.getvalue()))
    monkeypatch.setattr(sys, "stderr", stream)
    result = execute(
        tmp_path,
        actions=[tool("completed", rawInput=raw_input), visible()],
    )
    assert result.terminal_text == FINAL
    assert flushed == [stream.getvalue()]
    assert stream.getvalue().endswith("\n")
    assert json.loads(flushed[0])["rawInput"] == raw_input


def test_thoughts_progress_narration_and_late_terminal_message(tmp_path, capsys):
    result = execute(
        tmp_path,
        actions=[
            {
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "messageId": "thought",
                    "content": {"type": "text", "text": '{"false":"thought JSON"}'},
                }
            },
            visible('{"false":"narration JSON"}', "narration"),
            tool(),
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "read-1",
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "text",
                                "text": '{"false":"tool JSON"}',
                            },
                        }
                    ],
                }
            },
            tool("completed", start=False),
            visible("Still working", "progress"),
            {"sleep": 0.03},
            visible(FINAL[:17]),
            {"sleep": 0.03},
            visible(FINAL[17:]),
        ],
    )
    assert result.terminal_text == FINAL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "toolCallId": "read-1",
        "title": "Read repository",
        "rawInput": None,
    }


def test_completion_prose_is_not_error_matched_or_json_salvaged(tmp_path):
    text = "Provider failure: no valid JSON. error refusal limit"
    assert execute(tmp_path, actions=[visible(text)]).terminal_text == text


def test_unknown_usage_is_not_zero_or_context_size(tmp_path):
    result = execute(
        tmp_path,
        actions=[
            {"update": {"sessionUpdate": "usage_update", "used": 42, "size": 1000}},
            visible(),
        ],
    )
    assert result.usage == AgentUsage()


def test_cumulative_cost_and_tokens_are_not_summed(tmp_path):
    result = execute(
        tmp_path,
        usage=USAGE,
        actions=[
            {
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": 12,
                    "size": 1000,
                    "cost": {"amount": 0.1, "currency": "USD"},
                }
            },
            {
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": 29,
                    "size": 1000,
                    "cost": {"amount": 0.25, "currency": "USD"},
                }
            },
            visible(),
        ],
    )
    assert result.usage.total_tokens == 101
    assert result.usage.input_tokens == 70
    assert result.usage.cost == 0.25
    assert result.usage.cost_currency == "USD"


@pytest.mark.parametrize(
    "stop,error",
    [
        ("cancelled", RunnerCancelledError),
        ("refusal", RunnerRefusedError),
        ("max_tokens", RunnerLimitError),
        ("max_turn_requests", RunnerLimitError),
    ],
)
def test_nonpublishable_stops_preserve_partial_usage(tmp_path, stop, error):
    with pytest.raises(error) as caught:
        execute(tmp_path, stop=stop, usage=USAGE)
    assert caught.value.usage is not None
    assert caught.value.usage.total_tokens == 101


@pytest.mark.parametrize(
    "actions,message",
    [
        ([], "no terminal message"),
        ([visible("")], "empty"),
        ([visible("   ")], "empty"),
        ([visible(identity=None)], "identity"),
        ([visible(identity="")], "identity"),
        ([visible("a", "one"), visible("b", "two"), visible("c", "one")], "Ambiguous"),
        (
            [visible("before", "one"), tool("completed"), visible("after", "one")],
            "Ambiguous",
        ),
        ([visible(), tool("completed")], "after tool activity"),
        ([tool(), visible()], "unfinished tools"),
        ([tool(), tool(), visible()], "Duplicate"),
        ([tool("completed", start=False), visible()], "Unknown"),
        (
            [
                {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "messageId": "image",
                        "content": {
                            "type": "image",
                            "data": "aA==",
                            "mimeType": "image/png",
                        },
                    }
                }
            ],
            "text message identity",
        ),
    ],
)
def test_ambiguous_or_missing_terminal_output_is_rejected(tmp_path, actions, message):
    with pytest.raises(RunnerResultError, match=message):
        execute(tmp_path, actions=actions)


def test_failed_tool_can_be_followed_by_a_new_terminal_message(tmp_path):
    result = execute(tmp_path, actions=[tool(), tool("failed", start=False), visible()])
    assert result.terminal_text == FINAL


@pytest.mark.parametrize(
    "session",
    [
        {"sessionId": "fresh-session"},
        {
            "sessionId": "fresh-session",
            "models": {
                "currentModelId": "exact-model",
                "availableModels": [{"modelId": MODEL}],
            },
        },
        {
            "sessionId": "fresh-session",
            "models": {
                "currentModelId": MODEL,
                "availableModels": [{"modelId": "provider/other"}],
            },
        },
        {"sessionId": "fresh-session", "configOptions": []},
    ],
)
def test_model_selection_requires_exact_advertised_active_identity(tmp_path, session):
    with pytest.raises(RunnerConfigurationError):
        execute(tmp_path, session=session)


def model_option(model=MODEL, *, grouped=False):
    values = [{"value": MODEL, "name": "Exact model"}]
    return {
        "id": "model",
        "name": "Model",
        "type": "select",
        "category": "model",
        "currentValue": model,
        "options": (
            [{"group": "provider", "name": "Provider", "options": values}]
            if grouped
            else values
        ),
    }


@pytest.mark.parametrize("grouped", [False, True])
def test_standard_model_config_advertisement_is_supported(tmp_path, grouped):
    result = execute(
        tmp_path,
        session={
            "sessionId": "fresh-session",
            "configOptions": [model_option(grouped=grouped)],
        },
    )
    assert result.model == MODEL


@pytest.mark.parametrize(
    "initialize,error",
    [
        (
            {"protocolVersion": 2, "agentInfo": {"name": "peer", "version": "1"}},
            RunnerStartupError,
        ),
        ({"protocolVersion": 1}, RunnerStartupError),
        (
            {"protocolVersion": 1, "agentInfo": {"name": " ", "version": "1"}},
            RunnerStartupError,
        ),
        ({"protocolVersion": "invalid"}, RunnerTransportError),
    ],
)
def test_bad_initialization_has_no_success_fallback(tmp_path, initialize, error):
    with pytest.raises(error):
        execute(tmp_path, initialize=initialize)


@pytest.mark.parametrize(
    "script,error",
    [
        ({"startup_exit": True}, RunnerTransportError),
        (
            {"startup_error": {"code": -32000, "message": "secret-auth"}},
            RunnerStartupError,
        ),
        (
            {"prompt_error": {"code": -32603, "message": "secret-provider"}},
            RunnerTransportError,
        ),
        ({"actions": [{"exit": 17}]}, RunnerTransportError),
        ({"session": {"sessionId": ""}}, RunnerTransportError),
        (
            {"actions": [{"malformed": "secret-not-json"}, {"hang": True}]},
            RunnerTransportError,
        ),
        ({"actions": [{"malformed": "[]"}, {"hang": True}]}, RunnerTransportError),
        (
            {
                "actions": [
                    {"update": {"sessionUpdate": "unknown-invalid"}},
                    {"hang": True},
                ]
            },
            RunnerTransportError,
        ),
        ({"actions": [visible() | {"session": "wrong-session"}]}, RunnerTransportError),
    ],
)
def test_transport_and_startup_failures_are_private(tmp_path, caplog, script, error):
    with pytest.raises(error) as caught:
        execute(tmp_path, **script)
    assert "secret" not in str(caught.value)
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "method,params",
    [
        ("fs/read_text_file", {"sessionId": "fresh-session", "path": "/host/secret"}),
        (
            "fs/write_text_file",
            {
                "sessionId": "fresh-session",
                "path": "/host/file",
                "content": "bad",
            },
        ),
        (
            "terminal/create",
            {
                "sessionId": "fresh-session",
                "command": "touch",
                "args": ["/host/file"],
            },
        ),
        ("terminal/output", {"sessionId": "fresh-session", "terminalId": "host"}),
        ("terminal/release", {"sessionId": "fresh-session", "terminalId": "host"}),
        (
            "terminal/wait_for_exit",
            {"sessionId": "fresh-session", "terminalId": "host"},
        ),
        ("terminal/kill", {"sessionId": "fresh-session", "terminalId": "host"}),
        (
            "elicitation/create",
            {
                "message": "Unattended",
                "mode": "form",
                "requestId": "r",
                "requestedSchema": {"type": "object", "properties": {}},
            },
        ),
        ("_unexpected_host_service", {}),
    ],
)
def test_no_host_services_are_granted(tmp_path, method, params):
    process, request, trace = configured(
        tmp_path,
        actions=[
            {"request": {"method": method, "params": params}},
            visible(),
        ],
    )
    with pytest.raises(RunnerTransportError, match="host service"):
        asyncio.run(run_acp(process, request, deadline=time.monotonic() + 10))
    replies = [
        event for event in json.loads(trace.read_text()) if event.get("id") == "peer-0"
    ]
    assert len(replies) == 1
    assert "error" in replies[0] and "result" not in replies[0]
    assert not (tmp_path / "file").exists()


def permission(session="fresh-session"):
    return {
        "request": {
            "method": "session/request_permission",
            "params": {
                "sessionId": session,
                "toolCall": {"toolCallId": "danger", "title": "Dangerous action"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_always"}
                ],
            },
        }
    }


def test_permission_is_rejected_not_implicitly_approved(tmp_path):
    process, request, trace = configured(tmp_path, actions=[permission(), visible()])
    result = asyncio.run(run_acp(process, request, deadline=time.monotonic() + 10))
    assert result.terminal_text == FINAL
    reply = next(
        event for event in json.loads(trace.read_text()) if event.get("id") == "peer-0"
    )
    assert reply["result"] == {"outcome": {"outcome": "cancelled"}}


def test_permission_for_unknown_session_fails(tmp_path):
    with pytest.raises(RunnerTransportError, match="unknown session"):
        execute(tmp_path, actions=[permission("another-session"), visible()])


@pytest.mark.parametrize(
    "peer_request,code",
    [
        (
            {
                "method": "session/request_permission",
                "params": {"sessionId": "fresh-session"},
            },
            -32602,
        ),
        (
            {
                "method": "session/update",
                "params": {"sessionId": "fresh-session", **visible()},
            },
            -32601,
        ),
    ],
)
def test_sdk_error_replies_cannot_be_followed_by_publishable_output(
    tmp_path, peer_request, code
):
    process, request, trace = configured(
        tmp_path,
        actions=[{"request": peer_request}, visible()],
        usage=USAGE,
    )
    with pytest.raises(RunnerTransportError, match="ACP protocol"):
        asyncio.run(run_acp(process, request, deadline=time.monotonic() + 10))
    reply = next(
        event for event in json.loads(trace.read_text()) if event.get("id") == "peer-0"
    )
    assert reply["error"]["code"] == code
    assert "result" not in reply


async def wait_for_prompt(trace: Path) -> None:
    # A handshake, not a timing guess: cancellation begins only after startup.
    async with asyncio.timeout(5):
        while not trace.exists() or "session/prompt" not in trace.read_text():
            await asyncio.sleep(0.005)


def test_external_cancellation_settles_permission_and_preserves_usage(tmp_path):
    process, request, trace = configured(
        tmp_path,
        actions=[permission(), {"wait_cancel": True}],
        usage=USAGE,
    )

    async def scenario():
        task = asyncio.create_task(
            run_acp(process, request, deadline=time.monotonic() + 10)
        )
        await wait_for_prompt(trace)
        task.cancel()
        with pytest.raises(RunnerCancelledError) as caught:
            await task
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == 101

    asyncio.run(scenario())
    events = json.loads(trace.read_text())
    assert any(event.get("method") == "session/cancel" for event in events)
    assert any(
        event.get("result") == {"outcome": {"outcome": "cancelled"}} for event in events
    )


@pytest.mark.parametrize("action", [{"wait_cancel": True}, {"hang": True}])
def test_deadline_cancels_then_bounds_uncooperative_agent(tmp_path, action):
    process, request, trace = configured(
        tmp_path, actions=[permission(), action], usage=USAGE
    )
    started = time.monotonic()
    with pytest.raises(RunnerTimeoutError) as caught:
        asyncio.run(run_acp(process, request, deadline=started + 0.5))
    assert time.monotonic() - started < 4
    assert any(
        event.get("method") == "session/cancel"
        for event in json.loads(trace.read_text())
    )
    if "wait_cancel" in action:
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == 101


@pytest.mark.parametrize("cancel", [False, True])
def test_backpressured_prompt_cannot_park_cleanup_or_asyncio_shutdown(
    tmp_path, monkeypatch, cancel
):
    marker = tmp_path / "backpressured"
    monkeypatch.setattr("maas_code_reviewer.acp_client._SHUTDOWN_GRACE", 0.25)
    process, request, trace = configured(tmp_path, backpressure_marker=str(marker))
    request = replace(
        request, task_prompt="Review this diff.\n" + "x" * (2 * 1024 * 1024)
    )
    started = time.monotonic()

    async def scenario():
        task = asyncio.create_task(run_acp(process, request, deadline=started + 1))
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.005)
        # The peer has seen prompt bytes, but deliberately cannot parse or read
        # the full request. Its usage notification is drained during settlement.
        if cancel:
            task.cancel()
        error = RunnerCancelledError if cancel else RunnerTimeoutError
        with pytest.raises(error) as caught:
            await task
        assert caught.value.usage is not None
        assert caught.value.usage.cost == 0.25
        assert caught.value.usage.cost_currency == "USD"
        assert not [
            pending
            for pending in asyncio.all_tasks()
            if pending is not asyncio.current_task()
        ]

    asyncio.run(scenario())
    assert time.monotonic() - started < 4
    assert [event["method"] for event in json.loads(trace.read_text())] == [
        "initialize",
        "session/new",
    ]
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_valid_review_survives_delayed_graceful_launcher_exit(tmp_path):
    marker = tmp_path / "closing"
    process, request, _ = configured(
        tmp_path, usage=USAGE, shutdown_marker=str(marker), shutdown_delay=0.75
    )
    result = asyncio.run(run_acp(process, request, deadline=time.monotonic() + 10))
    assert marker.exists()
    assert result.terminal_text == FINAL
    assert result.status is CompletionStatus.COMPLETED
    assert result.usage.total_tokens == 101


def test_successful_turn_with_failed_process_cleanup_is_not_publishable(
    tmp_path, monkeypatch
):
    marker = tmp_path / "closing"
    process, request, _ = configured(
        tmp_path, usage=USAGE, shutdown_marker=str(marker), shutdown_delay=6
    )
    monkeypatch.setattr("maas_code_reviewer.acp_client._SHUTDOWN_GRACE", 0.25)
    started = time.monotonic()
    with pytest.raises(RunnerTransportError, match="cleanup") as caught:
        asyncio.run(run_acp(process, request, deadline=started + 10))
    assert marker.exists()
    assert time.monotonic() - started < 2
    assert caught.value.usage is not None
    assert caught.value.usage.total_tokens == 101


def test_stderr_is_drained_without_exposing_or_retaining_diagnostics(
    tmp_path, caplog, capsys
):
    result = execute(tmp_path, actions=[{"stderr": 50000}, visible()])
    assert result.terminal_text == FINAL
    assert "secret-diagnostic" not in caplog.text
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "change",
    [
        {"command": ()},
        {"cwd": Path("relative")},
        {"session_cwd": "relative"},
        {"session_cwd": "/review/../repository"},
    ],
)
def test_invalid_process_configuration_never_launches(tmp_path, change):
    process, request, trace = configured(tmp_path)
    with pytest.raises(RunnerConfigurationError):
        asyncio.run(
            run_acp(replace(process, **change), request, deadline=time.monotonic() + 10)
        )
    assert not trace.exists()


@pytest.mark.parametrize(
    "model,deadline,error",
    [
        (None, 10, RunnerConfigurationError),
        (MODEL, float("nan"), RunnerConfigurationError),
        (MODEL, -1, RunnerTimeoutError),
    ],
)
def test_invalid_or_expired_request_never_launches(tmp_path, model, deadline, error):
    process, request, trace = configured(tmp_path)
    with pytest.raises(error):
        asyncio.run(
            run_acp(
                process,
                replace(request, model=model),
                deadline=time.monotonic() + deadline,
            )
        )
    assert not trace.exists()


def test_missing_executable_is_a_private_startup_error(tmp_path):
    process, request, _ = configured(tmp_path)
    with pytest.raises(RunnerStartupError, match="Could not launch"):
        asyncio.run(
            run_acp(
                replace(process, command=(str(tmp_path / "absent"),)),
                request,
                deadline=time.monotonic() + 10,
            )
        )


def test_sdk_logging_filter_is_task_scoped_and_redacts_errors():
    owner = _Conversation(MODEL, "/review/session")
    other = _Conversation(MODEL, "/review/other")
    log_filter = _SDKLogFilter(owner)
    record = logging.LogRecord(
        "root",
        logging.ERROR,
        "/site-packages/acp/_transport.py",
        1,
        "secret-frame",
        (),
        None,
    )
    token = _LOG_OWNER.set(other)
    try:
        assert log_filter.filter(record)
        assert owner.failure is None
        _LOG_OWNER.set(owner)
        assert not log_filter.filter(record)
        assert isinstance(owner.failure, RunnerTransportError)
        warning = logging.LogRecord(
            "root", logging.WARNING, record.pathname, 1, "private", (), None
        )
        assert not log_filter.filter(warning)
        application = logging.LogRecord(
            "app", logging.ERROR, "/app/reviewer.py", 1, "app", (), None
        )
        assert log_filter.filter(application)
    finally:
        _LOG_OWNER.reset(token)


def test_unexpected_notifications_and_invalid_session_updates_fail(tmp_path):
    with pytest.raises(RunnerTransportError):
        execute(
            tmp_path,
            actions=[
                {"notification": {"method": "_unexpected", "params": {}}},
                visible(),
            ],
        )
    with pytest.raises(RunnerTransportError):
        execute(
            tmp_path / "elicitation",
            actions=[
                {
                    "notification": {
                        "method": "elicitation/complete",
                        "params": {"elicitationId": "unknown"},
                    }
                },
                visible(),
            ],
        )


def test_concurrent_protocol_errors_are_not_misattributed(tmp_path):
    good, good_request, _ = configured(
        tmp_path / "good", actions=[{"sleep": 0.1}, visible()]
    )
    bad, bad_request, _ = configured(
        tmp_path / "bad", actions=[{"malformed": "private"}, {"hang": True}]
    )

    async def scenario():
        outcomes = await asyncio.gather(
            run_acp(good, good_request, deadline=time.monotonic() + 10),
            run_acp(bad, bad_request, deadline=time.monotonic() + 10),
            return_exceptions=True,
        )
        assert not isinstance(outcomes[0], BaseException)
        assert outcomes[0].terminal_text == FINAL
        assert isinstance(outcomes[1], RunnerTransportError)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw",
    [
        {"models": {"currentModelId": MODEL, "availableModels": None}},
        {"configOptions": None},
        {"configOptions": [{"category": "model", "currentValue": MODEL}]},
        {
            "configOptions": [
                {"category": "model", "currentValue": MODEL, "options": [3]}
            ]
        },
        {
            "configOptions": [
                {
                    "category": "model",
                    "currentValue": MODEL,
                    "options": [{"group": "provider", "options": None}],
                }
            ]
        },
    ],
)
def test_malformed_model_advertisements_are_configuration_failures(raw):
    client = _Conversation(MODEL, "/review/session")
    with pytest.raises(RunnerConfigurationError):
        client.validate_model(raw)


def test_config_updates_cannot_change_the_pinned_model(tmp_path):
    changed = model_option("provider/other")
    with pytest.raises(RunnerTransportError, match="configured model"):
        execute(
            tmp_path,
            actions=[
                {
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [changed],
                    }
                },
                visible(),
            ],
        )
    assert (
        execute(
            tmp_path / "same",
            actions=[
                {
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [model_option()],
                    }
                },
                visible(),
            ],
        ).terminal_text
        == FINAL
    )


def test_non_model_configuration_updates_are_not_reviews(tmp_path):
    assert (
        execute(
            tmp_path,
            actions=[
                {
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [
                            {
                                "id": "mode",
                                "name": "Mode",
                                "type": "select",
                                "category": "mode",
                                "currentValue": "review",
                                "options": [{"value": "review", "name": "Review"}],
                            }
                        ],
                    }
                },
                visible(),
            ],
        ).terminal_text
        == FINAL
    )


@pytest.mark.parametrize(
    "options",
    [
        [],
        [
            {
                "id": "mode",
                "name": "Mode",
                "type": "select",
                "category": "mode",
                "currentValue": "review",
                "options": [{"value": "review", "name": "Review"}],
            }
        ],
        [model_option(), model_option()],
        [model_option("provider/other")],
    ],
)
def test_standard_model_full_updates_must_retain_one_exact_selector(tmp_path, options):
    with pytest.raises(RunnerTransportError, match="configured model"):
        execute(
            tmp_path,
            session={"sessionId": "fresh-session", "configOptions": [model_option()]},
            actions=[
                {
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": options,
                    }
                },
                visible(),
            ],
        )


def test_standard_model_updates_and_verified_legacy_model_remain_supported(tmp_path):
    unrelated = {
        "id": "mode",
        "name": "Mode",
        "type": "select",
        "category": "mode",
        "currentValue": "review",
        "options": [{"value": "review", "name": "Review"}],
    }
    result = execute(
        tmp_path,
        session={"sessionId": "fresh-session", "configOptions": [model_option()]},
        actions=[
            {
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [unrelated, model_option()],
                }
            },
            visible(),
        ],
    )
    assert result.model == MODEL and result.terminal_text == FINAL
    legacy = execute(
        tmp_path / "legacy",
        actions=[
            {
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": options,
                }
            }
            for options in ([model_option()], [unrelated], [])
        ]
        + [visible()],
    )
    assert legacy.model == MODEL and legacy.terminal_text == FINAL


def test_eof_does_not_promote_preliminary_json_to_a_result(tmp_path):
    with pytest.raises(RunnerTransportError):
        execute(tmp_path, actions=[visible(), {"exit": 0}])


def test_cancellation_during_sdk_shutdown_still_waits_for_cleanup(tmp_path):
    marker = tmp_path / "closing"
    process, request, _ = configured(
        tmp_path,
        usage=USAGE,
        shutdown_marker=str(marker),
        shutdown_delay=0.2,
    )

    async def scenario():
        task = asyncio.create_task(
            run_acp(process, request, deadline=time.monotonic() + 10)
        )
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(RunnerCancelledError) as caught:
            await task
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == 101

    asyncio.run(scenario())


def test_terminal_aggregator_keeps_the_first_protocol_failure():
    client = _Conversation(MODEL, "/review/session")
    client.usage = AgentUsage(cost=0.2, cost_currency="USD")
    first = RunnerResultError("First identity failure")
    client.fail(first)
    client.fail(RunnerTransportError("Later transport failure"))
    with pytest.raises(RunnerResultError, match="First identity") as caught:
        client.terminal_text()
    assert caught.value is first
    assert caught.value.usage == client.usage


def test_startup_metadata_is_not_terminal_content():
    from acp.schema import AvailableCommandsUpdate

    client = _Conversation(MODEL, "/review/session")
    asyncio.run(
        client.session_update(
            "fresh-session",
            AvailableCommandsUpdate(
                session_update="available_commands_update", available_commands=[]
            ),
        )
    )
    assert client.failure is None


def test_out_of_turn_text_and_rejected_services_are_not_success():
    from acp import update_agent_message_text

    client = _Conversation(MODEL, "/review/session")
    asyncio.run(
        client.session_update("fresh-session", update_agent_message_text(FINAL))
    )
    assert isinstance(client.failure, RunnerTransportError)


def test_observer_does_not_accept_a_nonobject_session_result():
    client = _Conversation(MODEL, "/review/session")
    client.observe(
        StreamEvent(
            StreamDirection.OUTGOING,
            {
                "id": 8,
                "method": "session/new",
                "params": {},
            },
        )
    )
    client.observe(StreamEvent(StreamDirection.INCOMING, {"id": 8, "result": None}))
    assert client.new_session_result == {}


def test_rejection_handlers_do_not_return_implicit_success():
    client = _Conversation(MODEL, "/review/session")

    async def scenario():
        with pytest.raises(RequestError):
            await client.complete_elicitation(elicitation_id="unknown")
        with pytest.raises(RequestError):
            await client.ext_notification("unexpected", {})

    asyncio.run(scenario())


def test_child_environment_is_exactly_the_configured_env(tmp_path, monkeypatch):
    marker = tmp_path / "child-env.json"
    monkeypatch.setenv("MAAS_REVIEW_CANARY", "ambient-leak")
    process, request, _ = configured(tmp_path, environment_marker=str(marker))
    environment = {
        "PATH": "/usr/bin:/bin",
        "LC_CTYPE": "C.UTF-8",
        "HOME": str(tmp_path),
    }
    process = replace(process, env=environment)
    asyncio.run(run_acp(process, request, deadline=time.monotonic() + 10))
    assert json.loads(marker.read_text()) == environment


def test_process_group_teardown_kills_agent_descendants(tmp_path, monkeypatch):
    marker = tmp_path / "grandchild"
    process, request, _ = configured(tmp_path, actions=[{"grandchild": str(marker)}])
    monkeypatch.setattr("maas_code_reviewer.acp_client._SHUTDOWN_GRACE", 0.25)
    started = time.monotonic()
    with pytest.raises(RunnerTimeoutError):
        asyncio.run(run_acp(process, request, deadline=started + 0.4))
    assert time.monotonic() - started < 4
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_deadline_during_startup_is_bounded(tmp_path):
    process, request, trace = configured(tmp_path, startup_sleep=5)
    started = time.monotonic()
    with pytest.raises(RunnerTimeoutError):
        asyncio.run(run_acp(process, request, deadline=started + 0.4))
    assert time.monotonic() - started < 4
    events = json.loads(trace.read_text())
    assert [event["method"] for event in events] == ["initialize"]

# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""One unattended ACP conversation over the pinned SDK's stdio transport.

The trusted runtime profile (argv, environment, model) is supplied by the
caller; this client grants the agent nothing on the host and verifies the
selection the profile made. The agent subprocess runs in its own session so
teardown can bound and terminate the whole local process group.
"""

import asyncio
import contextvars
import json
import logging
import math
import os
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

from acp import PROTOCOL_VERSION, RequestError, text_block
from acp.client.connection import ClientSideConnection
from acp.connection import StreamDirection, StreamEvent
from acp.interfaces import Client
from acp.schema import (
    AgentMessageChunk,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
)
from pydantic import ValidationError

from .agent_runner import (
    AgentRunResult,
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

_CLEANUP_GRACE = 0.5
_SHUTDOWN_GRACE = 2.0
_LOG_OWNER: contextvars.ContextVar["_Conversation | None"] = contextvars.ContextVar(
    "acp_log_owner", default=None
)


@dataclass(frozen=True)
class AgentProcess:
    """An explicitly configured launcher, not a local execution fallback."""

    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    session_cwd: str


async def run_acp(
    process: AgentProcess, request: ReviewRequest, *, deadline: float
) -> AgentRunResult:
    """Run one fresh session before an absolute monotonic host deadline.

    Teardown closes the connection and bounds local process-group shutdown.
    The workflow owner remains responsible for disposing of the container.
    """
    if (
        not process.command
        or not process.cwd.is_absolute()
        or not PurePosixPath(process.session_cwd).is_absolute()
        or ".." in PurePosixPath(process.session_cwd).parts
        or not request.model
        or not math.isfinite(deadline)
    ):
        raise RunnerConfigurationError(
            "Invalid ACP process, session cwd, model or deadline"
        )
    if deadline <= time.monotonic():
        raise RunnerTimeoutError("Agent review deadline expired")
    client = _Conversation(request.model, process.session_cwd)
    token = _LOG_OWNER.set(client)
    log_filter = _SDKLogFilter(client)
    logger = logging.getLogger()
    logger.addFilter(log_filter)
    drain: asyncio.Task[None] | None = None
    try:
        child = await _spawn(process)
        assert child.stdin is not None and child.stdout is not None
        connection = ClientSideConnection(
            cast(Client, client),
            child.stdin,
            child.stdout,
            observers=[client.observe],
            use_unstable_protocol=True,
        )
        returned = False
        try:
            assert child.stderr is not None
            drain = asyncio.create_task(_discard_stderr(child.stderr))
            result = await _run_connected(connection, client, request, deadline)
            returned = True
            return result
        finally:
            primary = None if returned else sys.exception()
            cleanup = asyncio.create_task(_teardown(connection, child, client))
            interrupted = False
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    interrupted = True
                except RunnerTransportError as error:
                    if primary is None and not interrupted:
                        raise
                    if primary is not None:
                        primary.add_note(str(error))
                    break
            if interrupted:
                raise asyncio.CancelledError
    except asyncio.CancelledError:
        raise RunnerCancelledError(
            "Agent review was cancelled", usage=client.usage
        ) from None
    except OSError:
        raise RunnerStartupError(
            "Could not launch ACP process", usage=client.usage
        ) from None
    finally:
        try:
            if drain is not None:
                await asyncio.wait((drain,), timeout=_CLEANUP_GRACE)
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
        finally:
            logger.removeFilter(log_filter)
            _LOG_OWNER.reset(token)


async def _spawn(process: AgentProcess) -> asyncio.subprocess.Process:
    # The child becomes a session and process-group leader, its environment
    # is exactly the configured one, and it inherits no ambient file
    # descriptors, so teardown can terminate the whole local process group.
    return await asyncio.create_subprocess_exec(
        *process.command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=process.env,
        cwd=str(process.cwd),
        start_new_session=True,
    )


class _Conversation:
    def __init__(self, model: str, session_cwd: str) -> None:
        self.model = model
        self.session_cwd = session_cwd
        self.session_id: str | None = None
        self.active = False
        self.usage = AgentUsage()
        self.failure: RunnerResultError | RunnerTransportError | None = None
        self.failed = asyncio.Event()
        self.new_session_request: int | str | None = None
        self.standard_model_config = False
        self.new_session_result: dict[str, Any] = {}
        self.messages: dict[str, list[str]] = {}
        self.last_message: str | None = None
        self.message_epoch: dict[str, int] = {}
        self.tool_epoch = 0
        self.tools: dict[str, str | None] = {}
        self.tool_inputs: dict[str, tuple[str, str | None]] = {}

    def observe(self, event: StreamEvent) -> None:
        """Retain the raw session advertisement and refuse host services."""
        message = event.message
        if event.direction == StreamDirection.OUTGOING:
            if "error" in message:
                self.fail(
                    RunnerTransportError("Agent request violated the ACP protocol")
                )
            elif message.get("method") == "session/new":
                self.new_session_request = message["id"]
        elif message.get("id") == self.new_session_request and "result" in message:
            result = message["result"]
            if isinstance(result, dict):
                self.new_session_result = result
        if event.direction == StreamDirection.INCOMING and "method" in message:
            method = message["method"]
            if method not in ("session/update", "session/request_permission"):
                # The SDK sends a deterministic method-not-found reply. Also
                # fail the turn, even if the agent ignores the refusal.
                self.fail(
                    RunnerTransportError("Agent requested an unavailable host service")
                )

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        if session_id != self.session_id:
            self.fail(RunnerTransportError("Permission request has an unknown session"))
        # Returning immediately settles every request, including during cancel.
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if not self.active and update.session_update in (
            "available_commands_update",
            "current_mode_update",
            "session_info_update",
        ):
            return
        if session_id != self.session_id or not self.active:
            self.fail(RunnerTransportError("Unexpected ACP session update"))
            return
        if isinstance(update, UsageUpdate):
            if update.cost is not None:
                self.usage = replace(
                    self.usage,
                    cost=update.cost.amount,
                    cost_currency=update.cost.currency,
                )
        elif isinstance(update, AgentMessageChunk):
            self._message(update)
        elif isinstance(update, ToolCallStart):
            if update.tool_call_id in self.tools:
                self.fail(RunnerResultError("Duplicate ACP tool identity"))
            self.tools[update.tool_call_id] = update.status
            self.tool_inputs[update.tool_call_id] = (
                update.title,
                json.dumps(update.raw_input, sort_keys=True)
                if "raw_input" in update.model_fields_set
                else None,
            )
            self._log_tool_input(update.tool_call_id)
            self.tool_epoch += 1
        elif isinstance(update, ToolCallProgress):
            if update.tool_call_id not in self.tools:
                self.fail(RunnerResultError("Unknown ACP tool identity"))
            if update.tool_call_id in self.tool_inputs:
                title, previous_input = self.tool_inputs[update.tool_call_id]
                raw_input = (
                    json.dumps(update.raw_input, sort_keys=True)
                    if "raw_input" in update.model_fields_set
                    else previous_input
                )
                self.tool_inputs[update.tool_call_id] = (
                    update.title if update.title is not None else title,
                    raw_input,
                )
                if raw_input != previous_input:
                    self._log_tool_input(update.tool_call_id)
            if update.status is not None:
                self.tools[update.tool_call_id] = update.status
            self.tool_epoch += 1
        elif update.session_update == "config_option_update":
            options = [
                option.model_dump(by_alias=True)
                for option in update.config_options
                if option.category == "model"
            ]
            if options or self.standard_model_config:
                try:
                    self.validate_model({"configOptions": options})
                except RunnerConfigurationError:
                    self.fail(
                        RunnerTransportError("Agent changed the configured model")
                    )
        # Thoughts, plans, commands, mode changes and tool content are not output.

    async def read_text_file(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/write_text_file")

    async def create_terminal(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/kill")

    async def create_elicitation(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("elicitation/create")

    async def complete_elicitation(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("elicitation/complete")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found("_" + method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RequestError.method_not_found("_" + method)

    def fail(self, error: RunnerResultError | RunnerTransportError) -> None:
        if self.failure is None:
            self.failure = error
            self.failed.set()

    def validate_model(self, session: dict[str, Any]) -> None:
        # omp 18.12.2 advertises the older models field; other conforming
        # harnesses can advertise the standard model-category config option.
        models = session.get("models")
        if isinstance(models, dict):
            available = models.get("availableModels")
            if not isinstance(available, list):
                raise RunnerConfigurationError("Invalid advertised model catalog")
            current = models.get("currentModelId")
            choices = [
                item.get("modelId") for item in available if isinstance(item, dict)
            ]
        else:
            config = session.get("configOptions", [])
            if not isinstance(config, list):
                raise RunnerConfigurationError("Invalid advertised model configuration")
            options = [
                item
                for item in config
                if isinstance(item, dict) and item.get("category") == "model"
            ]
            if len(options) != 1:
                raise RunnerConfigurationError(
                    "Agent did not advertise one exact model selector"
                )
            option = options[0]
            current = option.get("currentValue")
            choices = []
            values = option.get("options")
            if not isinstance(values, list):
                raise RunnerConfigurationError("Invalid advertised model choices")
            for item in values:
                if not isinstance(item, dict):
                    raise RunnerConfigurationError("Invalid advertised model choice")
                if "group" in item:
                    grouped = item.get("options")
                    if not isinstance(grouped, list):
                        raise RunnerConfigurationError("Invalid advertised model group")
                    choices.extend(
                        choice.get("value")
                        for choice in grouped
                        if isinstance(choice, dict)
                    )
                else:
                    choices.append(item.get("value"))
        if current != self.model or self.model not in choices:
            raise RunnerConfigurationError(
                "Agent did not select the exact configured model"
            )

    def terminal_text(self) -> str:
        if self.failure is not None:
            self.failure.usage = self.usage
            raise self.failure
        if any(status not in ("completed", "failed") for status in self.tools.values()):
            raise RunnerResultError(
                "ACP completed with unfinished tools", usage=self.usage
            )
        identity = self.last_message
        if identity is None or self.message_epoch[identity] != self.tool_epoch:
            raise RunnerResultError(
                "ACP has no terminal message after tool activity", usage=self.usage
            )
        text = "".join(self.messages[identity])
        if not text.strip():
            raise RunnerResultError("ACP terminal message is empty", usage=self.usage)
        return text

    def _log_tool_input(self, identity: str) -> None:
        # Keep every untrusted value JSON-escaped, including IDs and titles.
        title, raw_input = self.tool_inputs[identity]
        print(
            f'{{"toolCallId": {json.dumps(identity)}, '
            f'"title": {json.dumps(title)}, '
            f'"rawInput": {raw_input if raw_input is not None else "null"}}}',
            file=sys.stderr,
            flush=True,
        )

    def _message(self, update: AgentMessageChunk) -> None:
        identity = update.message_id
        if not identity or update.content.type != "text":
            self.fail(
                RunnerResultError("ACP visible output lacks a text message identity")
            )
            return
        if identity in self.messages and (
            identity != self.last_message
            or self.message_epoch[identity] != self.tool_epoch
        ):
            self.fail(
                RunnerResultError("Ambiguous ACP message identity across boundaries")
            )
            return
        self.messages.setdefault(identity, []).append(update.content.text)
        self.message_epoch[identity] = self.tool_epoch
        self.last_message = identity


class _SDKLogFilter(logging.Filter):
    """SDK 0.12.1 logs and skips malformed frames; make those fatal and private.

    SDK tasks inherit the conversation ContextVar. Concurrent unrelated runs and
    application logging are untouched, and raw payloads never reach handlers.
    """

    def __init__(self, owner: _Conversation) -> None:
        super().__init__()
        self.owner = owner

    def filter(self, record: logging.LogRecord) -> bool:
        if _LOG_OWNER.get() is self.owner and "acp" in Path(record.pathname).parts:
            if record.levelno >= logging.ERROR:
                self.owner.fail(
                    RunnerTransportError(
                        "Malformed ACP frame or protocol handler failure"
                    )
                )
            return False
        return True


async def _run_connected(
    connection: ClientSideConnection,
    client: _Conversation,
    request: ReviewRequest,
    deadline: float,
) -> AgentRunResult:
    exchange = asyncio.create_task(_exchange(connection, client, request))
    failed = asyncio.create_task(client.failed.wait())
    try:
        done, _ = await asyncio.wait(
            (exchange, failed),
            timeout=max(0, deadline - time.monotonic()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await _settle(connection, client, exchange)
            raise RunnerTimeoutError(
                "Agent review deadline expired", usage=client.usage
            )
        if client.failure is not None:
            await _settle(connection, client, exchange)
            client.failure.usage = client.usage
            raise client.failure
        return await exchange
    except asyncio.CancelledError:
        await _settle(connection, client, exchange)
        raise RunnerCancelledError(
            "Agent review was cancelled", usage=client.usage
        ) from None
    finally:
        failed.cancel()
        await asyncio.gather(failed, return_exceptions=True)


async def _exchange(
    connection: ClientSideConnection, client: _Conversation, request: ReviewRequest
) -> AgentRunResult:
    startup = True
    try:
        initialized = await connection.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(
                fs=None, terminal=False, auth=None, elicitation=None
            ),
            client_info=Implementation(name="maas-code-reviewer", version="0.2.0"),
        )
        if initialized.protocol_version != PROTOCOL_VERSION:
            raise RunnerStartupError("Agent does not support the requested ACP version")
        info = initialized.agent_info
        if info is None or not info.name.strip():
            raise RunnerStartupError("Agent did not advertise its identity")
        session = await connection.new_session(cwd=client.session_cwd)
        client.session_id = session.session_id
        if not client.session_id:
            raise RunnerTransportError("Agent returned an empty session identity")
        client.validate_model(client.new_session_result)
        client.standard_model_config = not isinstance(
            client.new_session_result.get("models"), dict
        )
        startup = False
        client.active = True
        response = await connection.prompt(
            session_id=client.session_id, prompt=[text_block(request.task_prompt)]
        )
        client.active = False
        if response.usage is not None:
            usage = response.usage
            client.usage = replace(
                client.usage,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                thinking_tokens=usage.thought_tokens,
                cache_read_tokens=usage.cached_read_tokens,
                cache_write_tokens=usage.cached_write_tokens,
                total_tokens=usage.total_tokens,
            )
        stop = response.stop_reason
        errors = {
            "cancelled": RunnerCancelledError,
            "refusal": RunnerRefusedError,
            "max_tokens": RunnerLimitError,
            "max_turn_requests": RunnerLimitError,
        }
        if stop in errors:
            raise errors[stop](f"ACP turn ended with {stop}", usage=client.usage)
        return AgentRunResult(
            terminal_text=client.terminal_text(),
            status=CompletionStatus.COMPLETED,
            agent_name=info.name,
            agent_version=info.version,
            model=client.model,
            usage=client.usage,
        )
    except RequestError as error:
        kind = RunnerStartupError if startup else RunnerTransportError
        raise kind(
            f"ACP request failed (code {error.code})", usage=client.usage
        ) from None
    except (ConnectionError, ValidationError):
        raise RunnerTransportError(
            "ACP connection closed or returned invalid data", usage=client.usage
        ) from None


async def _settle(
    connection: ClientSideConnection,
    client: _Conversation,
    exchange: asyncio.Task[AgentRunResult],
) -> None:
    if client.session_id is not None:
        try:
            await asyncio.wait_for(
                connection.cancel(session_id=client.session_id), _CLEANUP_GRACE
            )
        except (ConnectionError, OSError, RequestError, TimeoutError):
            pass
    try:
        await asyncio.wait_for(
            asyncio.gather(exchange, return_exceptions=True), _CLEANUP_GRACE
        )
    except TimeoutError:
        pass


async def _teardown(
    connection: ClientSideConnection,
    child: asyncio.subprocess.Process,
    client: _Conversation,
) -> None:
    failed = False
    # stdin EOF is the graceful ACP shutdown signal for a cooperative agent.
    assert child.stdin is not None
    if not child.stdin.is_closing():
        with suppress(OSError, RuntimeError):
            child.stdin.write_eof()
    try:
        if await asyncio.wait_for(child.wait(), _SHUTDOWN_GRACE) != 0:
            failed = True
    except TimeoutError:
        # The agent outlived its grace: terminate the whole local process
        # group, descendants included. Closing the child's pipes releases any
        # backpressured SDK sender blocked in writer.drain().
        failed = True
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        with suppress(TimeoutError):
            await asyncio.wait_for(child.wait(), _CLEANUP_GRACE)
    try:
        await asyncio.wait_for(connection.close(), _CLEANUP_GRACE)
    except (
        ConnectionError,
        OSError,
        RuntimeError,
        RequestError,
        ValueError,
        TimeoutError,
    ):
        failed = True
    if failed or client.failure is not None:
        raise RunnerTransportError("ACP process cleanup failed", usage=client.usage)


async def _discard_stderr(stream: asyncio.StreamReader) -> None:
    # Do not retain or reproduce diagnostics that may contain provider secrets,
    # repository content or tool output. Keep draining even during teardown.
    while await stream.read(65536):
        pass

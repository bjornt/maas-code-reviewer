# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""The production AgentRunner: one omp acp process per synchronous review.

Policy loading is lazy (constructing the runner starts nothing); the
scrubbed agent environment carries only the gateway token by name, and the
trusted runtime profile supplies argv, model, timeout, and inference
endpoint. Telemetry collection wraps exactly the ACP run.
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from maas_code_reviewer.acp_client import AgentProcess, run_acp
from maas_code_reviewer.agent_runner import (
    AgentRunResult,
    ReviewRequest,
    RunnerConfigurationError,
)
from maas_code_reviewer.runtime_config import (
    RuntimeConfig,
    load_runtime_config,
)
from maas_code_reviewer.telemetry import TelemetryCollector

GATEWAY_TOKEN_ENV = "GATEWAY_TOKEN"
_NATIVE_TRANSPORT = "pi-native"


class ConfiguredAgentRunner:
    """Run one review through omp over ACP using trusted runtime policy."""

    def __init__(
        self,
        configuration_path: Path | None,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
        otel_output: Path | None = None,
    ) -> None:
        self.configuration_path = configuration_path
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.otel_output = otel_output

    def run(self, request: ReviewRequest) -> AgentRunResult:
        """Load policy lazily, spawn one omp acp session, and join it."""
        config = load_runtime_config(self.configuration_path)
        request = replace(
            request,
            model=config.resolve_model(
                self.model if self.model is not None else request.model
            ),
            timeout_seconds=config.resolve_timeout(self.timeout_seconds),
        )
        token = os.environ.get(GATEWAY_TOKEN_ENV, "").strip()
        if not token:
            raise RunnerConfigurationError(
                f"The {GATEWAY_TOKEN_ENV} environment variable is required"
                " for an agent review"
            )
        root = Path(tempfile.mkdtemp(prefix="maas-review-agent-"))
        try:
            for name in ("home", "tmp", "run"):
                (root / name).mkdir()
            (root / "models.yml").write_text(
                _provider_configuration(
                    request.model or "", config.inference_endpoint
                ),
                encoding="utf-8",
            )
            deadline = time.monotonic() + request.timeout_seconds
            command = _harness_command(config, request, deadline)
            collector = (
                TelemetryCollector(self.otel_output)
                if self.otel_output is not None
                else nullcontext(None)
            )
            with collector as telemetry:
                environment = _agent_environment(root, token, telemetry)
                process = AgentProcess(
                    command=command,
                    cwd=root / "run",
                    env=environment,
                    session_cwd=str(root / "run"),
                )
                return asyncio.run(
                    run_acp(process, request, deadline=deadline)
                )
        finally:
            shutil.rmtree(root, ignore_errors=True)


def _provider_configuration(model: str, endpoint: str) -> str:
    """Write the per-run provider override; the token stays a name."""
    provider, separator, _ = model.partition("/")
    if not separator or not provider:
        raise RunnerConfigurationError(
            "The model identity must be an exact provider/model pair"
        )
    return json.dumps(
        {
            "providers": {
                provider: {
                    "baseUrl": endpoint,
                    "transport": _NATIVE_TRANSPORT,
                    "apiKey": GATEWAY_TOKEN_ENV,
                }
            }
        }
    )


def _harness_command(
    config: RuntimeConfig, request: ReviewRequest, deadline: float
) -> tuple[str, ...]:
    """Substitute the trusted placeholders per argument; never a shell."""
    substitutions = {
        "model": request.model or "",
        "system_instruction_text": request.system_instruction,
        "profile_dir": str(config.profile_directory),
        "timeout_seconds": str(deadline - time.monotonic()),
    }
    try:
        return tuple(
            argument.format_map(substitutions)
            for argument in config.harness_argv
        )
    except (KeyError, ValueError) as exc:
        raise RunnerConfigurationError(
            "Unknown or invalid harness argv placeholder"
        ) from exc


def _agent_environment(
    root: Path,
    token: str,
    telemetry: TelemetryCollector | None,
) -> dict[str, str]:
    """Build the complete agent env: token by name, nothing else ambient."""
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "TERM": "dumb",
        "SHELL": "/bin/sh",
        "PI_CODING_AGENT_DIR": str(root),
        "OMP_PROFILE": "",
        GATEWAY_TOKEN_ENV: token,
    }
    if telemetry is not None:
        environment.update(telemetry.environment)
    return environment

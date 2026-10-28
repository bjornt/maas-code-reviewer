# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""ConfiguredAgentRunner against the wire-level fake ACP peer, plus units."""

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from maas_code_reviewer.acp_runner import (
    ConfiguredAgentRunner,
    _harness_command,
    _provider_configuration,
)
from maas_code_reviewer.agent_runner import (
    CompletionStatus,
    ReviewRequest,
    RunnerConfigurationError,
)
from maas_code_reviewer.runtime_config import load_runtime_config

FAKE_ACP = Path(__file__).with_name("fake_acp.py")

FINAL_DOCUMENT = json.dumps(
    {"general_comment": "Final only.", "inline_comments": {}}
)
USAGE = {
    "totalTokens": 31,
    "inputTokens": 20,
    "outputTokens": 8,
    "thoughtTokens": 2,
    "cachedReadTokens": 1,
    "cachedWriteTokens": 0,
}
TEST_SESSION = {
    "sessionId": "fresh-session",
    "models": {
        "currentModelId": "test/model",
        "availableModels": [{"modelId": "test/model"}],
    },
}


def _visible(text: str = FINAL_DOCUMENT, identity: str = "final") -> dict[str, Any]:
    return {
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "messageId": identity,
            "content": {"type": "text", "text": text},
        }
    }


def _request(tmp_path: Path) -> ReviewRequest:
    return ReviewRequest(
        repository_path=tmp_path / "repo",
        full_diff="original full diff",
        task_prompt="Review the change described by the untrusted JSON envelope.",
        system_instruction="You MUST produce your review as a JSON object.",
        timeout_seconds=60,
    )


def _scenario(tmp_path: Path, name: str = "scenario.json", **script: Any) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"trace": str(tmp_path / "trace.json")} | script))
    return path


def _runtime_file(
    tmp_path: Path, scenario: Path, *, extra_argv: tuple[str, ...] = ()
) -> Path:
    """Write one explicit runtime override pointing the harness at the peer."""
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    (profile / "policy.yml").write_text("tools:\n  xdev: false\n")
    argv = [
        sys.executable,
        str(FAKE_ACP),
        str(scenario),
        "{model}",
        "--profile={profile_dir}",
        "--max-time={timeout_seconds}s",
        "{system_instruction_text}",
        *extra_argv,
    ]
    quoted = ",\n  ".join(json.dumps(argument) for argument in argv)
    config = tmp_path / "runtime.toml"
    config.write_text(
        """\
        [model]
        default = "test/model"
        allowed = ["test/model"]

        [harness]
        argv = [
          {quoted},
        ]

        [profile]
        directory = "./profile"
        """.replace("{quoted}", quoted)
    )
    return config


@pytest.fixture
def gateway_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_TOKEN", "test-token")


def test_construction_is_lazy(tmp_path: Path) -> None:
    runner = ConfiguredAgentRunner(Path("/no/such"))

    with pytest.raises(
        RunnerConfigurationError, match="Cannot read runtime configuration"
    ):
        runner.run(_request(tmp_path))


def test_missing_gateway_token_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    runner = ConfiguredAgentRunner(None)

    with pytest.raises(RunnerConfigurationError, match="GATEWAY_TOKEN"):
        runner.run(_request(tmp_path))


def test_run_returns_terminal_result_end_to_end(
    tmp_path: Path, gateway_token: None
) -> None:
    scenario = _scenario(
        tmp_path,
        session=TEST_SESSION,
        actions=[
            {
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": 12,
                    "size": 1000,
                    "cost": {"amount": 0.25, "currency": "USD"},
                }
            },
            _visible(),
        ],
        usage=USAGE,
    )
    runner = ConfiguredAgentRunner(_runtime_file(tmp_path, scenario))

    result = runner.run(_request(tmp_path))

    assert result.status is CompletionStatus.COMPLETED
    assert result.agent_name == "isolated-peer"
    assert result.agent_version == "1.2.3"
    assert result.terminal_text == FINAL_DOCUMENT
    assert result.model == "test/model"
    assert result.usage.total_tokens == 31
    assert result.usage.input_tokens == 20
    assert result.usage.cost == 0.25
    assert result.usage.cost_currency == "USD"


def test_agent_environment_is_scrubbed(
    tmp_path: Path, gateway_token: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "env.json"
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-gemini")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github")
    monkeypatch.setenv("MAAS_REVIEW_CANARY", "ambient-junk")
    scenario = _scenario(
        tmp_path,
        session=TEST_SESSION,
        actions=[_visible()],
        environment_marker=str(marker),
    )
    runner = ConfiguredAgentRunner(_runtime_file(tmp_path, scenario))

    runner.run(_request(tmp_path))

    environment = json.loads(marker.read_text())
    assert environment["GATEWAY_TOKEN"] == "test-token"
    assert environment["OMP_PROFILE"] == ""
    agent_dir = environment["PI_CODING_AGENT_DIR"]
    assert "maas-review-agent-" in agent_dir
    assert Path(environment["HOME"]).parent == Path(agent_dir)
    assert Path(environment["TMPDIR"]).parent == Path(agent_dir)
    assert "GEMINI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "MAAS_REVIEW_CANARY" not in environment
    assert "PATH" in environment


def test_otel_output_starts_collector_around_run(
    tmp_path: Path, gateway_token: None
) -> None:
    otel_dir = tmp_path / "otel"
    marker = tmp_path / "otel-env.json"
    scenario = _scenario(
        tmp_path,
        "otel-scenario.json",
        session=TEST_SESSION,
        actions=[_visible()],
        environment_marker=str(marker),
    )
    runner = ConfiguredAgentRunner(
        _runtime_file(tmp_path, scenario), otel_output=otel_dir
    )

    runner.run(_request(tmp_path))

    assert (otel_dir / "traces.jsonl").exists()
    assert (otel_dir / "metrics.jsonl").exists()
    environment = json.loads(marker.read_text())
    assert environment["OTEL_EXPORTER_OTLP_ENDPOINT"].startswith(
        "http://127.0.0.1:"
    )
    assert environment["OTEL_SERVICE_NAME"] == "maas-code-reviewer"

    bare_marker = tmp_path / "bare-env.json"
    bare_scenario = _scenario(
        tmp_path,
        "bare-scenario.json",
        session=TEST_SESSION,
        actions=[_visible()],
        environment_marker=str(bare_marker),
    )
    bare = ConfiguredAgentRunner(_runtime_file(tmp_path, bare_scenario))
    bare.run(_request(tmp_path))
    bare_environment = json.loads(bare_marker.read_text())
    assert not [key for key in bare_environment if key.startswith("OTEL_")]


def test_provider_configuration_references_token_by_name() -> None:
    document = json.loads(
        _provider_configuration("test/model", "http://127.0.0.1:4000")
    )

    assert document == {
        "providers": {
            "test": {
                "baseUrl": "http://127.0.0.1:4000",
                "transport": "pi-native",
                "apiKey": "GATEWAY_TOKEN",
            }
        }
    }
    with pytest.raises(RunnerConfigurationError):
        _provider_configuration("plainmodel", "http://127.0.0.1:4000")


def test_harness_command_substitutes_trusted_placeholders(tmp_path: Path) -> None:
    config = load_runtime_config(None)
    request = ReviewRequest(
        repository_path=tmp_path / "repo",
        full_diff="original full diff",
        task_prompt="Review the change.",
        system_instruction="SYSTEM TEXT MARKER",
        timeout_seconds=60,
        model="test/model",
    )

    command = _harness_command(config, request, time.monotonic() + 30)

    assert command[0] == "/var/lib/workshop/sdk/omp/bin/omp"
    assert "--model=test/model" in command
    config_arguments = [item for item in command if item.startswith("--config=")]
    assert len(config_arguments) == 1
    policy = Path(config_arguments[0].removeprefix("--config="))
    assert policy.name == "policy.yml"
    assert policy.is_absolute()
    max_time = [item for item in command if item.startswith("--max-time=")]
    assert len(max_time) == 1
    seconds = max_time[0].removeprefix("--max-time=").removesuffix("s")
    assert float(seconds) > 0
    assert "--append-system-prompt=SYSTEM TEXT MARKER" in command


def test_harness_command_rejects_unknown_placeholder(
    tmp_path: Path, gateway_token: None
) -> None:
    scenario = _scenario(
        tmp_path, "bogus-scenario.json", session=TEST_SESSION, actions=[_visible()]
    )
    runner = ConfiguredAgentRunner(
        _runtime_file(tmp_path, scenario, extra_argv=("{bogus}",))
    )

    with pytest.raises(
        RunnerConfigurationError, match="Unknown or invalid harness argv placeholder"
    ):
        runner.run(_request(tmp_path))


def test_disallowed_model_fails_closed(tmp_path: Path, gateway_token: None) -> None:
    scenario = _scenario(
        tmp_path, "denied-scenario.json", session=TEST_SESSION, actions=[_visible()]
    )
    runner = ConfiguredAgentRunner(
        _runtime_file(tmp_path, scenario), model="other/model"
    )

    with pytest.raises(RunnerConfigurationError, match="not allowed"):
        runner.run(_request(tmp_path))


def test_temporary_agent_root_is_removed(tmp_path: Path, gateway_token: None) -> None:
    temp_root = Path(tempfile.gettempdir())

    def agent_roots() -> set[str]:
        return {
            path.name
            for path in temp_root.iterdir()
            if path.name.startswith("maas-review-agent-")
        }

    before = agent_roots()
    scenario = _scenario(
        tmp_path, "teardown-scenario.json", session=TEST_SESSION, actions=[_visible()]
    )
    runner = ConfiguredAgentRunner(_runtime_file(tmp_path, scenario))

    result = runner.run(_request(tmp_path))

    assert result.status is CompletionStatus.COMPLETED
    assert agent_roots() == before

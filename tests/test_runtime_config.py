# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from maas_code_reviewer import review_mcp, runtime_config
from maas_code_reviewer.agent_runner import RunnerConfigurationError
from maas_code_reviewer.reviewer import _review_validation_server
from maas_code_reviewer.runtime_config import (
    DEFAULT_ALLOWED_MODELS,
    DEFAULT_HARNESS_ARGV,
    DEFAULT_INFERENCE_ENDPOINT,
    DEFAULT_MODEL,
    DEFAULT_REVIEW_TIMEOUT_SECONDS,
    RuntimeConfig,
    load_runtime_config,
)

PACKAGED_POLICY = """\
# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

# Trusted omp 18.12.2 review profile; never sourced from the reviewed
# checkout. The packaged omp acp argv template enforces the native tool
# allowlist (read,grep,glob,web_search) through --tools; this overlay pins
# the switchable allowlist members on, turns the ambient extras off, fixes
# the web-search role, and pre-approves the one in-session validation tool
# (tools.approval overrides are honored in every approval mode, keyed by
# omp's mcp__<server>_<tool> identity for the review-validation descriptor),
# so its calls are not denied by the host client's deterministic deny-all
# permission policy. Every other permission request still reaches the host
# client, which denies it.
tools:
  xdev: false
  approval:
    mcp__review_validation_validate_review: allow
dev:
  autoqa: false
vault:
  enabled: false
security:
  enabled: false
modelRoles:
  web: web/duckduckgo
autolearn:
  enabled: false
grep:
  enabled: true
glob:
  enabled: true
web_search:
  enabled: true
"""


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    directory = tmp_path / "profile"
    directory.mkdir()
    (directory / "policy.yml").write_text("tools:\n  xdev: false\n")
    return directory


@pytest.fixture
def runtime_file(tmp_path: Path, profile: Path) -> Path:
    config = tmp_path / "runtime.toml"
    config.write_text(
        textwrap.dedent(
            """\
            review_timeout_seconds = 127.5

            [model]
            default = "provider/selected"
            allowed = ["provider/selected", "provider/other"]

            [harness]
            argv = [
              "/opt/agent",
              "acp",
              "--model={model}",
              "--append-system-prompt={system_instruction_text}",
            ]

            [inference]
            endpoint = "http://127.0.0.1:4400"

            [profile]
            directory = "./profile"
            """
        )
    )
    return config


def _write_config(tmp_path: Path, toml: str) -> Path:
    """Write one explicit override file; never a searched-for or default name."""
    config = tmp_path / "runtime.toml"
    config.write_text(textwrap.dedent(toml))
    return config


class TestBuiltInDefaults:
    def test_defaults_are_exact_and_pinned(self) -> None:
        assert DEFAULT_MODEL == "openai-codex/gpt-6.1-sol"
        assert DEFAULT_ALLOWED_MODELS == (DEFAULT_MODEL,)
        assert DEFAULT_INFERENCE_ENDPOINT == "http://127.0.0.1:4000"
        assert DEFAULT_REVIEW_TIMEOUT_SECONDS == 300.0
        assert DEFAULT_HARNESS_ARGV[0] == "/var/lib/workshop/sdk/omp/bin/omp"
        assert DEFAULT_HARNESS_ARGV[1] == "acp"
        assert "--tools=read,grep,glob,web_search" in DEFAULT_HARNESS_ARGV
        assert "--approval-mode=always-ask" in DEFAULT_HARNESS_ARGV
        assert "--config={profile_dir}/policy.yml" in DEFAULT_HARNESS_ARGV
        assert "--max-time={timeout_seconds}s" in DEFAULT_HARNESS_ARGV

    def test_no_override_file_returns_built_in_defaults(self) -> None:
        config = load_runtime_config()

        assert isinstance(config, RuntimeConfig)
        assert config.harness_argv == DEFAULT_HARNESS_ARGV
        assert config.default_model == DEFAULT_MODEL
        assert config.allowed_models == DEFAULT_ALLOWED_MODELS
        assert config.inference_endpoint == DEFAULT_INFERENCE_ENDPOINT
        assert config.review_timeout_seconds == DEFAULT_REVIEW_TIMEOUT_SECONDS
        assert config.configuration_path is None
        packaged = Path(runtime_config.__file__).parent / "runtime" / "profile"
        assert config.profile_directory == packaged
        assert (packaged / "policy.yml").is_file()

    def test_default_model_resolves_without_a_request(self) -> None:
        config = load_runtime_config()

        assert config.resolve_model() == DEFAULT_MODEL
        assert config.resolve_model(DEFAULT_MODEL) == DEFAULT_MODEL

    def test_unlisted_requested_model_is_rejected(self) -> None:
        config = load_runtime_config()

        with pytest.raises(
            RunnerConfigurationError, match="not allowed by runtime policy"
        ):
            config.resolve_model("provider/other")

    def test_timeout_defaults_and_validates_overrides(self) -> None:
        config = load_runtime_config()

        assert config.resolve_timeout() == 300.0
        assert config.resolve_timeout(9.25) == 9.25
        for requested in (0, -1, float("inf"), float("nan")):
            with pytest.raises(
                RunnerConfigurationError, match="positive finite number of seconds"
            ):
                config.resolve_timeout(requested)

    def test_loading_starts_no_process(self) -> None:
        code = (
            "import sys\n"
            "from maas_code_reviewer.runtime_config import load_runtime_config\n"
            "load_runtime_config()\n"
            "assert 'subprocess' not in sys.modules\n"
        )

        subprocess.run([sys.executable, "-c", code], check=True)


class TestOverrideFile:
    def test_override_file_merges_every_value(
        self, runtime_file: Path, profile: Path
    ) -> None:
        config = load_runtime_config(runtime_file)

        assert config.review_timeout_seconds == 127.5
        assert config.default_model == "provider/selected"
        assert config.allowed_models == ("provider/selected", "provider/other")
        assert config.harness_argv == (
            "/opt/agent",
            "acp",
            "--model={model}",
            "--append-system-prompt={system_instruction_text}",
        )
        assert config.inference_endpoint == "http://127.0.0.1:4400"
        assert config.profile_directory == profile.resolve()
        assert config.configuration_path == runtime_file.resolve()
        assert config.resolve_model() == "provider/selected"
        assert config.resolve_model("provider/other") == "provider/other"
        assert config.resolve_timeout() == 127.5

    def test_partial_override_keeps_unlisted_defaults(self, tmp_path: Path) -> None:
        config = load_runtime_config(
            _write_config(tmp_path, "review_timeout_seconds = 42\n")
        )

        assert config.review_timeout_seconds == 42.0
        assert config.default_model == DEFAULT_MODEL
        assert config.allowed_models == DEFAULT_ALLOWED_MODELS
        assert config.harness_argv == DEFAULT_HARNESS_ARGV
        assert config.inference_endpoint == DEFAULT_INFERENCE_ENDPOINT
        assert config.profile_directory == (
            Path(runtime_config.__file__).parent / "runtime" / "profile"
        )

    def test_relative_profile_resolves_against_the_configuration_file(
        self,
        tmp_path: Path,
        profile: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "profile").mkdir()
        (checkout / "profile" / "policy.yml").write_text("tools:\n  xdev: true\n")
        monkeypatch.chdir(checkout)
        config = load_runtime_config(
            _write_config(
                tmp_path,
                """\
                [profile]
                directory = "./profile"
                """,
            )
        )

        assert config.profile_directory == profile.resolve()
        assert config.profile_directory != checkout / "profile"


class TestUnknownKeysFailClosed:
    @pytest.mark.parametrize(
        "toml",
        [
            pytest.param("unrecognized_control = true\n", id="root"),
            pytest.param("[model]\nunrecognized = true\n", id="model"),
            pytest.param("[harness]\nunrecognized = true\n", id="harness"),
            pytest.param("[inference]\nunrecognized = true\n", id="inference"),
            pytest.param("[profile]\nunrecognized = true\n", id="profile"),
        ],
    )
    def test_unknown_keys_are_rejected(self, tmp_path: Path, toml: str) -> None:
        with pytest.raises(RunnerConfigurationError, match="unknown keys"):
            load_runtime_config(_write_config(tmp_path, toml))

    @pytest.mark.parametrize(
        "toml",
        [
            pytest.param('model = "not-a-table"\n', id="model"),
            pytest.param("harness = 3\n", id="harness"),
        ],
    )
    def test_toplevel_values_must_be_tables(self, tmp_path: Path, toml: str) -> None:
        with pytest.raises(RunnerConfigurationError, match="must be a TOML table"):
            load_runtime_config(_write_config(tmp_path, toml))


class TestUnreadableConfiguration:
    def test_missing_file_never_falls_back_to_a_searched_default(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(
            RunnerConfigurationError, match="Cannot read runtime configuration"
        ):
            load_runtime_config(tmp_path / "missing.toml")

    def test_directory_is_not_a_configuration_file(self, tmp_path: Path) -> None:
        with pytest.raises(
            RunnerConfigurationError, match="Cannot read runtime configuration"
        ):
            load_runtime_config(tmp_path)

    @pytest.mark.parametrize("content", [b"[broken", b"\xff"])
    def test_malformed_content_is_a_configuration_error(
        self, tmp_path: Path, content: bytes
    ) -> None:
        config = tmp_path / "runtime.toml"
        config.write_bytes(content)

        with pytest.raises(
            RunnerConfigurationError, match="Cannot read runtime configuration"
        ):
            load_runtime_config(config)


class TestValidationRules:
    @pytest.mark.parametrize("value", ["0", "-3", "inf", "nan", '"300"', "true"])
    def test_review_timeout_must_be_positive_finite_seconds(
        self, tmp_path: Path, value: str
    ) -> None:
        config = _write_config(tmp_path, f"review_timeout_seconds = {value}\n")

        with pytest.raises(
            RunnerConfigurationError, match="positive finite number of seconds"
        ):
            load_runtime_config(config)

    def test_default_model_must_appear_in_the_allowlist(self, tmp_path: Path) -> None:
        config = _write_config(
            tmp_path,
            """\
            [model]
            default = "provider/other"
            allowed = ["provider/selected"]
            """,
        )

        with pytest.raises(
            RunnerConfigurationError, match="must appear in model.allowed"
        ):
            load_runtime_config(config)

    @pytest.mark.parametrize(
        "toml",
        [
            pytest.param(
                '[model]\ndefault = "no-slash"\nallowed = ["no-slash"]\n',
                id="default-shape",
            ),
            pytest.param(
                '[model]\ndefault = "provider/"\nallowed = ["provider/"]\n',
                id="default-empty-model",
            ),
            pytest.param("[model]\nallowed = []\n", id="allowed-empty"),
            pytest.param('[model]\nallowed = "provider/model"\n', id="allowed-scalar"),
            pytest.param("[model]\nallowed = [123]\n", id="allowed-not-string"),
            pytest.param('[model]\nallowed = ["noshape"]\n', id="allowed-entry-shape"),
        ],
    )
    def test_model_identities_must_be_exact(self, tmp_path: Path, toml: str) -> None:
        with pytest.raises(RunnerConfigurationError, match="model\\."):
            load_runtime_config(_write_config(tmp_path, toml))

    @pytest.mark.parametrize(
        "toml",
        [
            pytest.param("[harness]\nargv = []\n", id="empty"),
            pytest.param('[harness]\nargv = "/opt/agent acp"\n', id="shell-string"),
            pytest.param("[harness]\nargv = [123]\n", id="not-string"),
            pytest.param(
                '[harness]\nargv = ["\\u0000x", "--model={model}"]\n',
                id="nul",
            ),
            pytest.param(
                '[harness]\nargv = ["", "--model={model}"]\n',
                id="empty-argv0",
            ),
            pytest.param('[harness]\nargv = ["/opt/agent", "acp"]\n', id="no-model"),
            pytest.param(
                '[harness]\nargv = ["/opt/agent", "acp", "--model={model}"]\n',
                id="no-system-instruction",
            ),
        ],
    )
    def test_harness_argv_must_be_a_complete_template(
        self, tmp_path: Path, toml: str
    ) -> None:
        with pytest.raises(RunnerConfigurationError, match="harness\\.argv"):
            load_runtime_config(_write_config(tmp_path, toml))

    @pytest.mark.parametrize(
        "endpoint",
        [
            "127.0.0.1:4000",
            "ftp://127.0.0.1:4000",
            "http://user:password@127.0.0.1:4000",
            "http://127.0.0.1:4000/?q=1",
            "http://127.0.0.1:4000/#fragment",
            "http://127.0.0.1:notaport",
            "http://",
            "",
        ],
    )
    def test_inference_endpoint_must_be_a_clean_http_url(
        self, tmp_path: Path, endpoint: str
    ) -> None:
        config = _write_config(
            tmp_path, f"[inference]\nendpoint = {json.dumps(endpoint)}\n"
        )

        with pytest.raises(RunnerConfigurationError, match="inference\\.endpoint"):
            load_runtime_config(config)

    def test_profile_directory_must_exist(self, tmp_path: Path) -> None:
        config = _write_config(tmp_path, '[profile]\ndirectory = "./missing"\n')

        with pytest.raises(
            RunnerConfigurationError, match="Cannot resolve profile\\.directory"
        ):
            load_runtime_config(config)

    def test_profile_directory_must_be_a_directory(self, tmp_path: Path) -> None:
        a_file = tmp_path / "a-file"
        a_file.write_text("not a directory\n")
        config = _write_config(tmp_path, f'[profile]\ndirectory = "{a_file.name}"\n')

        with pytest.raises(
            RunnerConfigurationError, match="must be an existing directory"
        ):
            load_runtime_config(config)

    def test_profile_directory_must_contain_policy_yml(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty-profile"
        empty.mkdir()
        config = _write_config(tmp_path, f'[profile]\ndirectory = "{empty.name}"\n')

        with pytest.raises(
            RunnerConfigurationError, match="must contain the harness policy\\.yml"
        ):
            load_runtime_config(config)


class TestPackagedProfile:
    def test_packaged_policy_yml_is_the_expected_document(self) -> None:
        policy = (
            Path(runtime_config.__file__).parent / "runtime" / "profile" / "policy.yml"
        )

        assert policy.read_text() == PACKAGED_POLICY

    def test_packaged_policy_pre_approves_exactly_the_validation_tool(self) -> None:
        # omp reports MCP tools under the mangled identity
        # mcp__<server>_<tool> (QA trace: mcp__review_validation_validate_review)
        # and honors per-tool tools.approval overrides in every approval
        # mode. The overlay must name exactly the tool the review
        # descriptor configures: renaming the server or the tool breaks
        # this test, forcing the policy to follow.
        policy = (
            Path(runtime_config.__file__).parent / "runtime" / "profile" / "policy.yml"
        )
        text = policy.read_text()
        server = _review_validation_server(Path("/repo"), Path("/repo"), None)["name"]
        expected = f"mcp__{server.replace('-', '_')}_{review_mcp.TOOL_NAME}: allow"

        assert expected == "mcp__review_validation_validate_review: allow"
        assert text.count("\n    mcp__") == 1
        assert f"  approval:\n    {expected}\n" in text
        assert "--approval-mode=always-ask" in DEFAULT_HARNESS_ARGV

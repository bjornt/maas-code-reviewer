# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
import subprocess
import venv
from pathlib import Path, PurePosixPath

import pytest

from maas_code_reviewer.agent_runner import RunnerConfigurationError
from maas_code_reviewer.runtime_config import load_runtime_config


@pytest.fixture
def runtime_file(tmp_path: Path) -> Path:
    executable = tmp_path / "workshop"
    executable.write_text('#!/bin/sh\ntouch "$(dirname "$0")/launched"\nexit 33\n')
    executable.chmod(0o700)
    template = tmp_path / "template"
    template.mkdir()
    (template / "workshop.yaml").write_text("name: test\n")
    config = tmp_path / "runtime.toml"
    config.write_text("""\
review_timeout_seconds = 127.5

[workshop]
executable = "./workshop"
project_template = "./template"

[harness]
argv = ["/opt/agent", "acp"]

[paths]
repository = "/review/repository"
full_diff = "/review/inputs/full.diff"
system_instruction = "/review/control/system.md"
cwd = "/review/run"
agent_dir = "/review/state"

[model]
default = "provider/selected"
allowed = ["provider/selected", "another/model"]

[authentication]
interface = "agent:review-inference"
""")
    return config


def test_explicit_policy_and_host_paths_do_not_follow_checkout_cwd(
    runtime_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "runtime.toml").write_text("review_timeout_seconds = 1\n")
    monkeypatch.chdir(checkout)

    config = load_runtime_config(runtime_file)

    assert config.workshop_executable == tmp_path / "workshop"
    assert config.project_template == tmp_path / "template"
    assert config.paths.repository == PurePosixPath("/review/repository")
    assert config.resolve_model() == "provider/selected"
    assert config.resolve_model("another/model") == "another/model"
    assert config.resolve_timeout() == 127.5
    assert config.resolve_timeout(9.25) == 9.25
    assert not (tmp_path / "launched").exists()


def test_missing_explicit_config_never_falls_back_to_cwd_config(
    runtime_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RunnerConfigurationError, match="Cannot read"):
        load_runtime_config(tmp_path / "missing.toml")
    assert not (tmp_path / "launched").exists()


@pytest.mark.parametrize("content", [b"[broken", b"\xff"])
def test_unreadable_configuration_content_fails_as_configuration_error(
    runtime_file: Path, content: bytes
) -> None:
    runtime_file.write_bytes(content)
    with pytest.raises(RunnerConfigurationError, match="Cannot read"):
        load_runtime_config(runtime_file)


def test_directory_is_not_a_config_file(tmp_path: Path) -> None:
    with pytest.raises(RunnerConfigurationError, match="Cannot read"):
        load_runtime_config(tmp_path)


def test_config_symlink_loop_fails_before_any_job(runtime_file: Path) -> None:
    runtime_file.unlink()
    runtime_file.symlink_to(runtime_file.name)
    with pytest.raises(RunnerConfigurationError, match="Cannot read"):
        load_runtime_config(runtime_file)


def test_missing_authentication_policy_is_not_defaulted(runtime_file: Path) -> None:
    text = runtime_file.read_text().partition("[authentication]")[0]
    runtime_file.write_text(text)
    with pytest.raises(RunnerConfigurationError, match="missing.*authentication"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize("section", ["root", "harness", "authentication"])
def test_unknown_policy_keys_cannot_silently_disable_controls(
    runtime_file: Path, section: str
) -> None:
    text = runtime_file.read_text()
    addition = "unrecognized_control = true\n"
    if section == "root":
        text = addition + text
    else:
        text = text.replace(f"[{section}]\n", f"[{section}]\n{addition}")
    runtime_file.write_text(text)
    with pytest.raises(RunnerConfigurationError, match="unknown keys"):
        load_runtime_config(runtime_file)


def test_section_requires_a_table(runtime_file: Path) -> None:
    text = runtime_file.read_text().replace(
        '[harness]\nargv = ["/opt/agent", "acp"]\n', ""
    )
    runtime_file.write_text('harness = "agent acp"\n' + text)
    with pytest.raises(RunnerConfigurationError, match="harness must be a TOML table"):
        load_runtime_config(runtime_file)


def test_missing_harness_command_is_not_defaulted(runtime_file: Path) -> None:
    text = runtime_file.read_text().replace('argv = ["/opt/agent", "acp"]\n', "")
    runtime_file.write_text(text)
    with pytest.raises(RunnerConfigurationError, match="harness is missing.*argv"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize(
    "value",
    [
        '"agent acp"',
        "[]",
        '["/opt/agent", 5]',
        '["", "acp"]',
        '["/opt/agent", "\\u0000"]',
    ],
)
def test_harness_requires_exec_argv_not_a_shell_command(
    runtime_file: Path, value: str
) -> None:
    _set_value(runtime_file, "argv", value)
    with pytest.raises(RunnerConfigurationError, match="harness.argv"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize(
    "value",
    [
        '"repo"',
        '"/"',
        '"//review/repo"',
        '"/review/../repo"',
        '"/review/./repo"',
        '"/review//repo"',
        '"/review/repo/"',
    ],
)
def test_container_paths_cannot_be_relative_root_or_normalized_silently(
    runtime_file: Path, value: str
) -> None:
    _set_value(runtime_file, "repository", value)
    with pytest.raises(RunnerConfigurationError, match="paths.repository"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cwd", "/review/repository/run"),
        ("system_instruction", "/review/repository/policy.md"),
        ("full_diff", "/review/state/full.diff"),
        ("agent_dir", "/review/control"),
        ("full_diff", "/review/control/system.md"),
        ("repository", "/review"),
    ],
)
def test_repository_state_and_control_paths_must_not_overlap(
    runtime_file: Path, key: str, value: str
) -> None:
    _set_value(runtime_file, key, json.dumps(value))
    with pytest.raises(RunnerConfigurationError, match="overlaps"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize(
    ("key", "value"),
    [("repository", "7"), ("interface", '" "'), ("default", '"\\u0000"')],
)
def test_required_policy_strings_reject_wrong_types_blanks_and_nul(
    runtime_file: Path, key: str, value: str
) -> None:
    _set_value(runtime_file, key, value)
    with pytest.raises(RunnerConfigurationError, match="nonempty string"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize(
    "value", ["0", "-1", "nan", "inf", "-inf", "true", '"10"', "[10]", "1" + "0" * 400]
)
def test_configured_timeout_requires_positive_finite_seconds(
    runtime_file: Path, value: str
) -> None:
    _set_value(runtime_file, "review_timeout_seconds", value)
    with pytest.raises(RunnerConfigurationError, match="positive finite"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_timeout_override_cannot_bypass_deadline_validation(
    runtime_file: Path, value: float
) -> None:
    config = load_runtime_config(runtime_file)
    with pytest.raises(RunnerConfigurationError, match="positive finite"):
        config.resolve_timeout(value)


@pytest.mark.parametrize("value", ['"provider/selected"', "[]", "[3]"])
def test_model_allowlist_is_explicit_and_typed(runtime_file: Path, value: str) -> None:
    _set_value(runtime_file, "allowed", value)
    with pytest.raises(RunnerConfigurationError, match="model.allowed"):
        load_runtime_config(runtime_file)


def test_default_model_must_be_authorized(runtime_file: Path) -> None:
    _set_value(runtime_file, "default", '"not-authorized/model"')
    with pytest.raises(RunnerConfigurationError, match="model.default"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize(
    "requested",
    [
        "selected",
        "Provider/selected",
        "provider/selected ",
        "another/model; echo unexpected",
        "",
    ],
)
def test_model_override_never_fuzzy_matches_or_falls_back(
    runtime_file: Path, requested: str
) -> None:
    config = load_runtime_config(runtime_file)
    with pytest.raises(RunnerConfigurationError, match="not allowed"):
        config.resolve_model(requested)


@pytest.mark.parametrize(
    "value", ["agent", "agent:", ":plug", "agent:plug:other", "agent:plug with space"]
)
def test_authentication_requires_an_explicit_sdk_plug(
    runtime_file: Path, value: str
) -> None:
    _set_value(runtime_file, "interface", json.dumps(value))
    with pytest.raises(RunnerConfigurationError, match="SDK:plug"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize("key", ["executable", "project_template"])
def test_missing_host_resources_fail_before_launch(
    runtime_file: Path, key: str
) -> None:
    _set_value(runtime_file, key, '"./does-not-exist"')
    with pytest.raises(
        RunnerConfigurationError, match=f"Cannot resolve workshop.{key}"
    ):
        load_runtime_config(runtime_file)


def test_executable_must_be_a_file(runtime_file: Path) -> None:
    _set_value(runtime_file, "executable", '"./template"')
    with pytest.raises(RunnerConfigurationError, match="must be a file"):
        load_runtime_config(runtime_file)


def test_executable_must_be_runnable(runtime_file: Path, tmp_path: Path) -> None:
    (tmp_path / "workshop").chmod(0o600)
    with pytest.raises(RunnerConfigurationError, match="not executable"):
        load_runtime_config(runtime_file)


def test_project_template_must_be_a_directory(runtime_file: Path) -> None:
    _set_value(runtime_file, "project_template", '"./runtime.toml"')
    with pytest.raises(RunnerConfigurationError, match="must be a directory"):
        load_runtime_config(runtime_file)


def test_project_template_requires_its_own_definition(
    runtime_file: Path, tmp_path: Path
) -> None:
    (tmp_path / "template" / "workshop.yaml").unlink()
    with pytest.raises(RunnerConfigurationError, match="must contain workshop.yaml"):
        load_runtime_config(runtime_file)


def test_relative_resources_follow_resolved_config_file(
    runtime_file: Path, tmp_path: Path
) -> None:
    other_directory = tmp_path / "links"
    other_directory.mkdir()
    link = other_directory / "operator.toml"
    link.symlink_to(runtime_file)

    config = load_runtime_config(link)

    assert config.workshop_executable == tmp_path / "workshop"
    assert config.project_template == tmp_path / "template"


def test_artifact_and_interface_policy_is_explicit_and_secret_free(
    runtime_file: Path,
    tmp_path: Path,
) -> None:
    with runtime_file.open("a") as stream:
        stream.write("""
[artifacts]
workshop_version = "1.2.3"
sdk_revisions = {system = "2", omp = "250"}
profile_directory = "./template"
[interfaces]
allowed = ["omp:review-inference"]
mounts = ["omp:home"]
[test_shim]
omp_executable = "./workshop"
gateway_url = "http://localhost:4000"
token_env = "SCOPED_INFERENCE_BEARER"
""")
    config = load_runtime_config(runtime_file)
    assert config.workshop_version == "1.2.3"
    assert config.sdk_revisions == {"system": "2", "omp": "250"}
    assert config.allowed_interfaces == ("omp:review-inference",)
    assert config.mount_interfaces == ("omp:home",)
    assert config.profile_directory == tmp_path / "template"
    assert config.test_shim is not None
    assert config.test_shim.token_env == "SCOPED_INFERENCE_BEARER"
    assert config.test_shim.omp_executable == tmp_path / "workshop"
    assert not (tmp_path / "launched").exists()


@pytest.mark.parametrize(
    "section",
    [
        'artifacts = "bad"\n',
        "interfaces = []\n",
        '[artifacts]\nworkshop_version = "1"\nsdk_revisions = []\n'
        'profile_directory = "./template"\n',
        '[artifacts]\nworkshop_version = "1"\nsdk_revisions = {omp=1}\n'
        'profile_directory = "./template"\n',
        '[interfaces]\nallowed = "omp:gateway"\nmounts = []\n',
        "[interfaces]\nallowed = [1]\nmounts = []\n",
    ],
)
def test_invalid_artifact_and_interface_extensions_fail_closed(
    runtime_file: Path,
    section: str,
) -> None:
    text = runtime_file.read_text()
    runtime_file.write_text(
        section + text if not section.startswith("[") else text + "\n" + section
    )
    with pytest.raises(RunnerConfigurationError):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize("token_env", ["not a variable", "5TOKEN", "éTOKEN"])
def test_test_shim_bearer_is_an_explicit_environment_name(
    runtime_file: Path,
    token_env: str,
) -> None:
    with runtime_file.open("a") as stream:
        stream.write(
            '\n[test_shim]\nomp_executable = "./workshop"\n'
            'gateway_url = "http://localhost:4000"\n'
            f"token_env = {json.dumps(token_env)}\n"
        )
    with pytest.raises(RunnerConfigurationError, match="environment variable"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "http://user:password@localhost:4000",
        "http://localhost:4000/?token=secret",
        "http://localhost:4000/#secret",
        "http://[bad",
        "http://localhost:invalid",
    ],
)
def test_test_shim_gateway_url_cannot_store_credentials(
    runtime_file: Path,
    url: str,
) -> None:
    with runtime_file.open("a") as stream:
        stream.write(
            '\n[test_shim]\nomp_executable = "./workshop"\n'
            f'gateway_url = {json.dumps(url)}\ntoken_env = "TOKEN"\n'
        )
    with pytest.raises(RunnerConfigurationError, match="gateway URL"):
        load_runtime_config(runtime_file)


@pytest.mark.parametrize(
    "value", ['"relative/workshop.socket"', '" "', "3", '"/run/\\u0000socket"']
)
def test_management_socket_requires_an_explicit_absolute_host_path(
    runtime_file: Path,
    value: str,
) -> None:
    text = runtime_file.read_text().replace(
        "[workshop]\n",
        f"[workshop]\nsocket = {value}\n",
    )
    runtime_file.write_text(text)
    with pytest.raises(RunnerConfigurationError):
        load_runtime_config(runtime_file)


def test_executable_validation_preserves_its_virtual_environment(
    runtime_file: Path,
    tmp_path: Path,
) -> None:
    environment = tmp_path / "host-tool"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    _set_value(runtime_file, "executable", json.dumps(str(environment / "bin/python")))
    config = load_runtime_config(runtime_file)
    subprocess.run(
        [
            str(config.workshop_executable),
            "-c",
            "import sys; assert sys.prefix == sys.argv[1]",
            str(environment),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _set_value(path: Path, key: str, toml_value: str) -> None:
    lines = [
        f"{key} = {toml_value}" if line.partition("=")[0].strip() == key else line
        for line in path.read_text().splitlines()
    ]
    path.write_text("\n".join(lines) + "\n")

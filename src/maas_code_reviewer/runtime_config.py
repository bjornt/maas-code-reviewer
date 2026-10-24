# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Load explicitly selected operator TOML; never discover policy in a checkout.

Validation here is preflight, not a sandbox: the Workshop runner must separately
verify mounts, connected interfaces, harness/model support, and effective policy.
No command or authentication interface is invoked while loading configuration.
"""

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from maas_code_reviewer.agent_runner import RunnerConfigurationError


@dataclass(frozen=True)
class TestShimConfig:
    """Explicit development-only selection; never detected from the environment."""

    omp_executable: Path
    gateway_url: str
    token_env: str


@dataclass(frozen=True)
class ContainerPaths:
    repository: PurePosixPath
    full_diff: PurePosixPath
    system_instruction: PurePosixPath
    cwd: PurePosixPath
    agent_dir: PurePosixPath


@dataclass(frozen=True)
class RuntimeConfig:
    workshop_executable: Path
    project_template: Path
    harness_argv: tuple[str, ...]
    paths: ContainerPaths
    default_model: str
    allowed_models: tuple[str, ...]
    review_timeout_seconds: float
    authentication_interface: str
    workshop_version: str | None = None
    sdk_revisions: dict[str, str] = field(default_factory=dict)
    allowed_interfaces: tuple[str, ...] = ()
    mount_interfaces: tuple[str, ...] = ()
    profile_directory: Path | None = None
    test_shim: TestShimConfig | None = None
    configuration_path: Path | None = None
    workshop_socket: Path = Path("/var/lib/workshop/workshop.socket")

    def resolve_model(self, requested: str | None = None) -> str:
        """Apply exact operator policy, never a fuzzy selector or fallback.

        The ACP adapter must also verify support and the resolved model identity
        against the agent's advertised configuration before submitting a task.
        """
        selected = self.default_model if requested is None else requested
        if selected not in self.allowed_models:
            raise RunnerConfigurationError(
                "Requested model is not allowed by runtime policy"
            )
        return selected

    def resolve_timeout(self, requested: float | None = None) -> float:
        """Resolve a total startup/turn deadline override in seconds."""
        if requested is None:
            return self.review_timeout_seconds
        return _timeout(requested)


def load_runtime_config(path: Path) -> RuntimeConfig:
    """Read one explicit file; resolve host paths relative to that file.

    Host executables/templates must exist. Container paths are parsed as POSIX
    paths without accessing the host filesystem. Unknown keys are rejected so a
    typo cannot silently drop a security or model setting. No environment file,
    parent-directory search, shell expansion, or runtime fallback is involved.
    """
    try:
        config_path = path.resolve(strict=True)
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, RuntimeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RunnerConfigurationError(
            f"Cannot read runtime configuration: {exc}"
        ) from exc

    optional = {"interfaces", "test_shim", "artifacts"}
    _keys(
        {key: value for key, value in data.items() if key not in optional},
        {
            "workshop",
            "harness",
            "paths",
            "model",
            "authentication",
            "review_timeout_seconds",
        },
        "runtime configuration",
    )
    workshop = _table(
        data, "workshop", {"executable", "project_template"}, optional=("socket",)
    )
    socket = Path(
        _text(
            workshop.get("socket", "/var/lib/workshop/workshop.socket"),
            "workshop.socket",
        )
    )
    if not socket.is_absolute():
        raise RunnerConfigurationError("workshop.socket must be an absolute host path")
    harness = _table(data, "harness", {"argv"})
    paths = _table(
        data,
        "paths",
        {"repository", "full_diff", "system_instruction", "cwd", "agent_dir"},
    )
    model = _table(data, "model", {"default", "allowed"})
    authentication = _table(data, "authentication", {"interface"})

    container_paths = {
        name: _container_path(value, f"paths.{name}") for name, value in paths.items()
    }
    for name, current in container_paths.items():
        for other_name, other in container_paths.items():
            if name != other_name and current.is_relative_to(other):
                raise RunnerConfigurationError(
                    f"paths.{name} overlaps paths.{other_name}"
                )

    argv = harness["argv"]
    if not isinstance(argv, list) or not argv:
        raise RunnerConfigurationError(
            "harness.argv must be a nonempty array of strings"
        )
    if any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise RunnerConfigurationError(
            "harness.argv must contain only strings without NUL"
        )
    _text(argv[0], "harness.argv[0]")

    allowed = model["allowed"]
    if not isinstance(allowed, list) or not allowed:
        raise RunnerConfigurationError(
            "model.allowed must be a nonempty array of exact model identities"
        )
    allowed_models = tuple(_text(value, "model.allowed entry") for value in allowed)
    default_model = _text(model["default"], "model.default")
    if default_model not in allowed_models:
        raise RunnerConfigurationError("model.default must appear in model.allowed")

    interface = _text(authentication["interface"], "authentication.interface")
    sdk, separator, plug = interface.partition(":")
    if (
        not separator
        or not sdk
        or not plug
        or ":" in plug
        or any(char.isspace() for char in interface)
    ):
        raise RunnerConfigurationError("authentication.interface must name an SDK:plug")

    executable = _host_path(
        workshop["executable"],
        config_path.parent,
        "workshop.executable",
        directory=False,
    )
    if not os.access(executable, os.X_OK):
        raise RunnerConfigurationError("workshop.executable is not executable")
    project = _host_path(
        workshop["project_template"],
        config_path.parent,
        "workshop.project_template",
        directory=True,
    )
    if not (project / "workshop.yaml").is_file():
        raise RunnerConfigurationError(
            "workshop.project_template must contain workshop.yaml"
        )

    artifacts = data.get("artifacts", {})
    if not isinstance(artifacts, dict):
        raise RunnerConfigurationError("artifacts must be a TOML table")
    if artifacts:
        _keys(
            artifacts,
            {"workshop_version", "sdk_revisions", "profile_directory"},
            "artifacts",
        )
    revisions = artifacts.get("sdk_revisions", {})
    if not isinstance(revisions, dict):
        raise RunnerConfigurationError("artifacts.sdk_revisions must be a table")
    revisions = {
        _text(key, "SDK name"): _text(value, "SDK revision")
        for key, value in revisions.items()
    }
    interfaces = data.get("interfaces", {"allowed": [], "mounts": []})
    if not isinstance(interfaces, dict):
        raise RunnerConfigurationError("interfaces must be a TOML table")
    _keys(interfaces, {"allowed", "mounts"}, "interfaces")
    for name, entries in interfaces.items():
        if not isinstance(entries, list):
            raise RunnerConfigurationError(f"interfaces.{name} must be an array")
        for entry in entries:
            _text(entry, f"interfaces.{name}")
    shim = None
    if "test_shim" in data:
        settings = _table(
            data, "test_shim", {"omp_executable", "gateway_url", "token_env"}
        )
        token_env = _text(settings["token_env"], "test_shim.token_env")
        if not token_env.isidentifier() or not token_env.isascii():
            raise RunnerConfigurationError(
                "test_shim.token_env must be an environment variable name"
            )
        gateway_url = _text(settings["gateway_url"], "test_shim.gateway_url")
        try:
            url = urlsplit(gateway_url)
            _ = url.port
            valid_url = (
                url.scheme in ("http", "https")
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise RunnerConfigurationError(
                "Test gateway URL must be HTTP(S) without credentials, "
                "query, or fragment"
            )
        shim = TestShimConfig(
            omp_executable=_host_path(
                settings["omp_executable"],
                config_path.parent,
                "test_shim.omp_executable",
                directory=False,
            ),
            gateway_url=gateway_url,
            token_env=token_env,
        )
    profile = artifacts.get("profile_directory")

    return RuntimeConfig(
        workshop_executable=executable,
        project_template=project,
        harness_argv=tuple(argv),
        paths=ContainerPaths(**container_paths),
        default_model=default_model,
        allowed_models=allowed_models,
        review_timeout_seconds=_timeout(data["review_timeout_seconds"]),
        authentication_interface=interface,
        workshop_version=(
            _text(artifacts["workshop_version"], "artifacts.workshop_version")
            if artifacts
            else None
        ),
        sdk_revisions=revisions,
        allowed_interfaces=tuple(interfaces["allowed"]),
        mount_interfaces=tuple(interfaces["mounts"]),
        profile_directory=(
            _host_path(
                profile,
                config_path.parent,
                "artifacts.profile_directory",
                directory=True,
            )
            if profile is not None
            else None
        ),
        test_shim=shim,
        configuration_path=config_path,
        workshop_socket=socket,
    )


def _keys(data: dict, expected: set[str], name: str) -> None:
    missing = expected - data.keys()
    unknown = data.keys() - expected
    if missing:
        raise RunnerConfigurationError(
            f"{name} is missing: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise RunnerConfigurationError(
            f"{name} has unknown keys: {', '.join(sorted(unknown))}"
        )


def _table(
    data: dict,
    name: str,
    expected: set[str],
    *,
    optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    value = data[name]
    if not isinstance(value, dict):
        raise RunnerConfigurationError(f"{name} must be a TOML table")
    _keys(
        {key: item for key, item in value.items() if key not in optional},
        expected,
        name,
    )
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise RunnerConfigurationError(f"{name} must be a nonempty string without NUL")
    return value


def _container_path(value: Any, name: str) -> PurePosixPath:
    text = _text(value, name)
    path = PurePosixPath(text)
    if (
        not path.is_absolute()
        or path == PurePosixPath("/")
        or text.startswith("//")
        or ".." in path.parts
        or path.as_posix() != text
    ):
        raise RunnerConfigurationError(
            f"{name} must be a normalized absolute POSIX path other than /"
        )
    return path


def _host_path(value: Any, base: Path, name: str, *, directory: bool) -> Path:
    text = _text(value, name)
    selected = (base / text).absolute()
    try:
        path = selected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RunnerConfigurationError(f"Cannot resolve {name}: {exc}") from exc
    if not (path.is_dir() if directory else path.is_file()):
        kind = "directory" if directory else "file"
        raise RunnerConfigurationError(f"{name} must be a {kind}")
    # Multicall dispatchers and virtual environments depend on the selected
    # executable name. Validate its physical target without changing argv[0].
    return path if directory else selected


def _timeout(value: Any) -> float:
    message = "Review timeout must be a positive finite number of seconds"
    if type(value) not in (int, float):
        raise RunnerConfigurationError(message)
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise RunnerConfigurationError(message) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise RunnerConfigurationError(message)
    return seconds

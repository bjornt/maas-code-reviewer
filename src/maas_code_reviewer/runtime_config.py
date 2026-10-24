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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from maas_code_reviewer.agent_runner import RunnerConfigurationError


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

    _keys(
        data,
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
    workshop = _table(data, "workshop", {"executable", "project_template"})
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

    return RuntimeConfig(
        workshop_executable=executable,
        project_template=project,
        harness_argv=tuple(argv),
        paths=ContainerPaths(**container_paths),
        default_model=default_model,
        allowed_models=allowed_models,
        review_timeout_seconds=_timeout(data["review_timeout_seconds"]),
        authentication_interface=interface,
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


def _table(data: dict, name: str, expected: set[str]) -> dict[str, Any]:
    value = data[name]
    if not isinstance(value, dict):
        raise RunnerConfigurationError(f"{name} must be a TOML table")
    _keys(value, expected, name)
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
    try:
        path = (base / text).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RunnerConfigurationError(f"Cannot resolve {name}: {exc}") from exc
    if not (path.is_dir() if directory else path.is_file()):
        kind = "directory" if directory else "file"
        raise RunnerConfigurationError(f"{name} must be a {kind}")
    return path


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

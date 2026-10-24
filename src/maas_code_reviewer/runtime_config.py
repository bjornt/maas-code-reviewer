# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Built-in review runtime policy, optionally overridden by one explicit TOML.

Every setting has a trusted default owned by this package, so an ordinary review
needs no operator file. An explicitly selected TOML overrides individual values;
policy is never discovered in a reviewed checkout, a parent directory, the
environment, or the working directory.

Validation here is preflight, not a sandbox: the Workshop runner must separately
verify the staged definition, mounts, connected interfaces, harness/model
support, and effective policy. No command or authentication interface is invoked
while loading configuration.
"""

import math
import os
import shutil
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from maas_code_reviewer.agent_runner import RunnerConfigurationError

HARNESS_SDK = "omp"
DEFAULT_BASE = "ubuntu@26.04"
DEFAULT_INFERENCE_PLUG = "review-inference"
DEFAULT_INFERENCE_ENDPOINT = "localhost:4000"
DEFAULT_MODEL = "openai-codex/gpt-6.1-sol"
DEFAULT_REVIEW_TIMEOUT_SECONDS = 300.0
DEFAULT_WORKSHOP_SOCKET = Path("/var/lib/workshop/workshop.socket")
DEFAULT_HARNESS_ARGV = (
    "/var/lib/workshop/sdk/omp/bin/omp",
    "acp",
    "--model={model}",
    "--append-system-prompt={system_instruction_text}",
    "--tools=read,grep,glob,web_search",
    "--no-extensions",
    "--no-skills",
    "--no-rules",
    "--no-lsp",
    "--no-pty",
    "--no-title",
    "--approval-mode=always-ask",
    "--config={profile_dir}/policy.yml",
    "--max-time={timeout_seconds}s",
)


@dataclass(frozen=True)
class TestShimConfig:
    """Explicit development-only selection; never detected from the environment."""

    omp_executable: Path
    gateway_url: str
    token_env: str


@dataclass(frozen=True)
class ContainerPaths:
    repository: PurePosixPath = PurePosixPath("/review/repository")
    full_diff: PurePosixPath = PurePosixPath("/review/inputs/full.diff")
    system_instruction: PurePosixPath = PurePosixPath("/review/control/system.txt")
    cwd: PurePosixPath = PurePosixPath("/review/run")
    agent_dir: PurePosixPath = PurePosixPath("/review/state")


def _packaged_profile() -> Path:
    return Path(__file__).parent / "runtime" / "profile"


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved trusted policy for one review; the agent never supplies any of it."""

    workshop_executable: Path
    harness_argv: tuple[str, ...] = DEFAULT_HARNESS_ARGV
    paths: ContainerPaths = field(default_factory=ContainerPaths)
    default_model: str = DEFAULT_MODEL
    # Empty means the operator accepted any identity the CLI selects explicitly.
    allowed_models: tuple[str, ...] = ()
    review_timeout_seconds: float = DEFAULT_REVIEW_TIMEOUT_SECONDS
    base: str = DEFAULT_BASE
    # None keeps Workshop's own default channel rather than inventing a name.
    omp_channel: str | None = None
    inference_plug: str = DEFAULT_INFERENCE_PLUG
    inference_endpoint: str = DEFAULT_INFERENCE_ENDPOINT
    profile_directory: Path = field(default_factory=_packaged_profile)
    test_shim: TestShimConfig | None = None
    configuration_path: Path | None = None
    workshop_socket: Path = DEFAULT_WORKSHOP_SOCKET

    @property
    def authentication_interface(self) -> str:
        """The single tunnel this application's own definition declares."""
        return f"{HARNESS_SDK}:{self.inference_plug}"

    def workshop_definition(self, name: str) -> dict[str, Any]:
        """Build the whole job definition; never read one from any other source."""
        tunnel = {"interface": "tunnel", "endpoint": self.inference_endpoint}
        harness: dict[str, Any] = {"name": HARNESS_SDK}
        if self.omp_channel is not None:
            harness["channel"] = self.omp_channel
        harness["plugs"] = {self.inference_plug: dict(tunnel)}
        return {
            "name": name,
            "base": self.base,
            "sdks": [
                harness,
                {"name": "system", "slots": {self.inference_plug: dict(tunnel)}},
            ],
        }

    def resolve_model(self, requested: str | None = None) -> str:
        """Apply exact operator policy, never a fuzzy selector or fallback.

        The ACP adapter must also verify support and the resolved model identity
        against the agent's advertised configuration before submitting a task.
        """
        selected = self.default_model if requested is None else requested
        if self.allowed_models and selected not in self.allowed_models:
            raise RunnerConfigurationError(
                "Requested model is not allowed by runtime policy"
            )
        return selected

    def resolve_timeout(self, requested: float | None = None) -> float:
        """Resolve a total startup/turn deadline override in seconds."""
        if requested is None:
            return self.review_timeout_seconds
        return _timeout(requested)


def load_runtime_config(path: Path | None = None) -> RuntimeConfig:
    """Apply built-in defaults, then one explicitly selected override file.

    Relative host resources resolve against the resolved override file's own
    directory. Host executables/directories must exist. Container paths are
    parsed as POSIX paths without accessing the host filesystem. Unknown keys are
    rejected so a typo cannot silently drop a security or model setting. No
    environment file, parent-directory search, shell expansion, or runtime
    fallback is involved.
    """
    if path is None:
        return RuntimeConfig(workshop_executable=_installed_workshop())
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
            "review_timeout_seconds",
            "workshop",
            "omp",
            "harness",
            "paths",
            "model",
            "authentication",
            "profile",
            "test_shim",
        },
        "runtime configuration",
    )
    base = config_path.parent
    workshop = _table(data, "workshop", {"executable", "socket", "base"})
    omp = _table(data, "omp", {"channel"})
    harness = _table(data, "harness", {"argv"})
    paths = _table(data, "paths", set(ContainerPaths.__dataclass_fields__))
    model = _table(data, "model", {"default", "allowed"})
    authentication = _table(data, "authentication", {"plug", "endpoint"})
    profile = _table(data, "profile", {"directory"})

    config = RuntimeConfig(
        workshop_executable=(
            _executable(workshop["executable"], base, "workshop.executable")
            if "executable" in workshop
            else _installed_workshop()
        ),
        configuration_path=config_path,
    )
    changes: dict[str, Any] = {}
    if "review_timeout_seconds" in data:
        changes["review_timeout_seconds"] = _timeout(data["review_timeout_seconds"])
    if "socket" in workshop:
        socket = Path(_text(workshop["socket"], "workshop.socket"))
        if not socket.is_absolute():
            raise RunnerConfigurationError(
                "workshop.socket must be an absolute host path"
            )
        changes["workshop_socket"] = socket
    if "base" in workshop:
        changes["base"] = _text(workshop["base"], "workshop.base")
    if "channel" in omp:
        changes["omp_channel"] = _name(omp["channel"], "omp.channel", extra="/.")
    if "argv" in harness:
        changes["harness_argv"] = _argv(harness["argv"])
    if paths:
        changes["paths"] = _container_paths(paths, config.paths)
    if "allowed" in model:
        changes["allowed_models"] = _allowed_models(model["allowed"])
    if "default" in model:
        changes["default_model"] = _text(model["default"], "model.default")
    allowed = changes.get("allowed_models", config.allowed_models)
    if allowed and changes.get("default_model", config.default_model) not in allowed:
        raise RunnerConfigurationError("model.default must appear in model.allowed")
    if "plug" in authentication:
        changes["inference_plug"] = _name(authentication["plug"], "authentication.plug")
    if "endpoint" in authentication:
        changes["inference_endpoint"] = _endpoint(authentication["endpoint"])
    if "directory" in profile:
        changes["profile_directory"] = _host_path(
            profile["directory"], base, "profile.directory", directory=True
        )
    if "test_shim" in data:
        changes["test_shim"] = _test_shim(
            _table(data, "test_shim", {"omp_executable", "gateway_url", "token_env"}),
            base,
        )
    return replace(config, **changes)


def _installed_workshop() -> Path:
    found = shutil.which("workshop")
    if found is None:
        raise RunnerConfigurationError(
            "No workshop executable on PATH; install Workshop or set "
            "workshop.executable in an explicit --runtime-config file"
        )
    return Path(found)


def _keys(data: dict, expected: set[str], name: str) -> None:
    unknown = data.keys() - expected
    if unknown:
        raise RunnerConfigurationError(
            f"{name} has unknown keys: {', '.join(sorted(unknown))}"
        )


def _table(data: dict, name: str, expected: set[str]) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise RunnerConfigurationError(f"{name} must be a TOML table")
    _keys(value, expected, name)
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise RunnerConfigurationError(f"{name} must be a nonempty string without NUL")
    return value


def _name(value: Any, name: str, *, extra: str = "") -> str:
    """Accept lowercase Workshop-style names, never whitespace or a reference."""
    text = _text(value, name)
    parts = text
    for separator in "-" + extra:
        parts = parts.replace(separator, " ")
    if (
        not text.isascii()
        or any(map(str.isspace, text))
        or not all(
            part.isalnum() and part == part.lower() for part in parts.split(" ")
        )
    ):
        raise RunnerConfigurationError(
            f"{name} must be lowercase alphanumeric groups separated by "
            f"{', '.join(repr(character) for character in '-' + extra)}"
        )
    return text


def _endpoint(value: Any) -> str:
    text = _text(value, "authentication.endpoint")
    host, separator, port = text.rpartition(":")
    if not separator or not host or not port.isdigit() or any(map(str.isspace, text)):
        raise RunnerConfigurationError(
            "authentication.endpoint must be a HOST:PORT tunnel target"
        )
    return text


def _argv(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RunnerConfigurationError(
            "harness.argv must be a nonempty array of strings"
        )
    if any(not isinstance(argument, str) or "\0" in argument for argument in value):
        raise RunnerConfigurationError(
            "harness.argv must contain only strings without NUL"
        )
    _text(value[0], "harness.argv[0]")
    return tuple(value)


def _allowed_models(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RunnerConfigurationError(
            "model.allowed must be a nonempty array of exact model identities"
        )
    return tuple(_text(entry, "model.allowed entry") for entry in value)


def _container_paths(paths: dict[str, Any], current: ContainerPaths) -> ContainerPaths:
    selected = replace(
        current,
        **{
            name: _container_path(value, f"paths.{name}")
            for name, value in paths.items()
        },
    )
    resolved = {
        name: getattr(selected, name) for name in ContainerPaths.__dataclass_fields__
    }
    for name, path in resolved.items():
        for other_name, other in resolved.items():
            if name != other_name and path.is_relative_to(other):
                raise RunnerConfigurationError(
                    f"paths.{name} overlaps paths.{other_name}"
                )
    return selected


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


def _test_shim(settings: dict[str, Any], base: Path) -> TestShimConfig:
    missing = {"omp_executable", "gateway_url", "token_env"} - settings.keys()
    if missing:
        raise RunnerConfigurationError(
            f"test_shim is missing: {', '.join(sorted(missing))}"
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
            "Test gateway URL must be HTTP(S) without credentials, query, or fragment"
        )
    return TestShimConfig(
        omp_executable=_executable(
            settings["omp_executable"], base, "test_shim.omp_executable"
        ),
        gateway_url=gateway_url,
        token_env=token_env,
    )


def _executable(value: Any, base: Path, name: str) -> Path:
    path = _host_path(value, base, name, directory=False)
    if not os.access(path, os.X_OK):
        raise RunnerConfigurationError(f"{name} is not executable")
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

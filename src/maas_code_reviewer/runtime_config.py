# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Built-in review runtime policy, overridden only by one explicit TOML.

Every setting has a trusted default owned by this package, so an ordinary
review needs no operator file. An explicitly selected TOML overrides
individual values; policy is never discovered in a reviewed checkout, a
parent directory, the environment, or the working directory. Validation is
preflight only: loading starts no process.
"""

import math
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from maas_code_reviewer.agent_runner import RunnerConfigurationError

DEFAULT_MODEL = "openai-codex/gpt-6.1-sol"
DEFAULT_ALLOWED_MODELS = (DEFAULT_MODEL,)
DEFAULT_INFERENCE_ENDPOINT = "http://127.0.0.1:4000"
DEFAULT_REVIEW_TIMEOUT_SECONDS = 300.0
# An argv template, never a shell string: the review handler substitutes the
# trusted placeholders per argument and spawns the result directly.
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


def _packaged_profile() -> Path:
    return Path(__file__).parent / "runtime" / "profile"


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved trusted policy for one review; the agent never supplies any."""

    harness_argv: tuple[str, ...] = DEFAULT_HARNESS_ARGV
    default_model: str = DEFAULT_MODEL
    allowed_models: tuple[str, ...] = DEFAULT_ALLOWED_MODELS
    inference_endpoint: str = DEFAULT_INFERENCE_ENDPOINT
    review_timeout_seconds: float = DEFAULT_REVIEW_TIMEOUT_SECONDS
    profile_directory: Path = field(default_factory=_packaged_profile)
    configuration_path: Path | None = None

    def resolve_model(self, requested: str | None = None) -> str:
        """Apply the exact operator allowlist; never fuzzy matching or fallback."""
        selected = self.default_model if requested is None else requested
        if selected not in self.allowed_models:
            raise RunnerConfigurationError(
                "Requested model is not allowed by runtime policy"
            )
        return selected

    def resolve_timeout(self, requested: float | None = None) -> float:
        """Resolve an optional total startup/turn deadline override in seconds."""
        if requested is None:
            return self.review_timeout_seconds
        return _timeout(requested)


def load_runtime_config(path: Path | None = None) -> RuntimeConfig:
    """Apply built-in defaults, then one explicitly selected override file.

    Relative host resources resolve against the resolved override file's own
    directory, never the reviewed checkout or the working directory. Unknown
    keys are rejected so a typo cannot silently drop a security or model
    setting. No environment file, parent-directory search, shell expansion,
    or process is involved.
    """
    if path is None:
        config = RuntimeConfig()
        _check_profile(config.profile_directory)
        return config
    try:
        config_path = path.resolve(strict=True)
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    except (
        OSError,
        RuntimeError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        raise RunnerConfigurationError(
            f"Cannot read runtime configuration: {exc}"
        ) from exc
    _keys(
        data,
        {"review_timeout_seconds", "model", "harness", "inference", "profile"},
        "runtime configuration",
    )
    base = config_path.parent
    model = _table(data, "model", {"default", "allowed"})
    harness = _table(data, "harness", {"argv"})
    inference = _table(data, "inference", {"endpoint"})
    profile = _table(data, "profile", {"directory"})
    config = RuntimeConfig(configuration_path=config_path)
    changes: dict[str, Any] = {}
    if "review_timeout_seconds" in data:
        changes["review_timeout_seconds"] = _timeout(data["review_timeout_seconds"])
    if "argv" in harness:
        changes["harness_argv"] = _argv(harness["argv"])
    if "allowed" in model:
        changes["allowed_models"] = _allowed_models(model["allowed"])
    if "default" in model:
        changes["default_model"] = _model_identity(model["default"], "model.default")
    allowed = changes.get("allowed_models", config.allowed_models)
    if changes.get("default_model", config.default_model) not in allowed:
        raise RunnerConfigurationError("model.default must appear in model.allowed")
    if "endpoint" in inference:
        changes["inference_endpoint"] = _endpoint(inference["endpoint"])
    if "directory" in profile:
        changes["profile_directory"] = _profile_directory(profile["directory"], base)
    merged = replace(config, **changes)
    _check_profile(merged.profile_directory)
    return merged


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


def _model_identity(value: Any, name: str) -> str:
    text = _text(value, name)
    provider, separator, model = text.partition("/")
    if not separator or not provider or not model:
        raise RunnerConfigurationError(
            f"{name} must be an exact provider/model identity"
        )
    return text


def _allowed_models(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RunnerConfigurationError(
            "model.allowed must be a nonempty array of exact model identities"
        )
    return tuple(_model_identity(entry, "model.allowed entry") for entry in value)


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
    argv = tuple(value)
    if not any("{model}" in argument for argument in argv):
        raise RunnerConfigurationError("harness.argv must select the exact {model}")
    if not any("{system_instruction" in argument for argument in argv):
        raise RunnerConfigurationError(
            "harness.argv must install the trusted system instruction"
        )
    return argv


def _endpoint(value: Any) -> str:
    text = _text(value, "inference.endpoint")
    try:
        url = urlsplit(text)
        _ = url.port
        valid = (
            url.scheme in ("http", "https")
            and bool(url.hostname)
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise RunnerConfigurationError(
            "inference.endpoint must be an HTTP(S) URL without credentials, "
            "query, or fragment"
        )
    return text


def _profile_directory(value: Any, base: Path) -> Path:
    text = _text(value, "profile.directory")
    try:
        return (base / text).absolute().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RunnerConfigurationError(
            f"Cannot resolve profile.directory: {exc}"
        ) from exc


def _check_profile(path: Path) -> None:
    if not path.is_dir():
        raise RunnerConfigurationError(
            "profile.directory must be an existing directory"
        )
    if not (path / "policy.yml").is_file():
        raise RunnerConfigurationError(
            "profile.directory must contain the harness policy.yml"
        )


def _timeout(value: Any) -> float:
    message = "Review timeout must be a positive finite number of seconds"
    if type(value) not in (int, float):
        raise RunnerConfigurationError(message)
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise RunnerConfigurationError(message)
    return seconds

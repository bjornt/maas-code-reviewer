# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Disposable Workshop ownership, protected policy, and synchronous ACP boundary.

The trusted project is mounted only during provisioning. Before the harness is
started a root bootstrap copies inputs into the container and unmounts /project.
No agent-controlled file is subsequently used as host executable configuration.
The explicitly selected local CLI shim exercises lifecycle, not isolation.
"""

import asyncio
import http.client
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import yaml

from maas_code_reviewer import acp_client, runtime_guard, runtime_launch
from maas_code_reviewer.agent_runner import (
    AgentRunResult,
    ReviewRequest,
    RunnerConfigurationError,
    RunnerStartupError,
    RunnerTimeoutError,
)
from maas_code_reviewer.runtime_config import RuntimeConfig, load_runtime_config

_CLEAN_PATH = "/usr/local/bin:/usr/bin:/bin:/snap/bin"
_CLEANUP_SECONDS = 10.0


class ConfiguredAgentRunner:
    """Delay all policy loading and authentication until a review actually runs."""

    def __init__(
        self,
        config_path: Path | None,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.config_path = config_path
        self.model = model
        self.timeout_seconds = timeout_seconds

    def run(self, request: ReviewRequest) -> AgentRunResult:
        if self.config_path is None:
            raise RunnerConfigurationError(
                "A nonempty review requires --runtime-config"
            )
        _require_external_policy(self.config_path, request.repository_path)
        config = load_runtime_config(self.config_path)
        request = replace(
            request,
            model=config.resolve_model(
                self.model if self.model is not None else request.model
            ),
            timeout_seconds=config.resolve_timeout(self.timeout_seconds),
        )
        return WorkshopRunner(config).run(request)


class WorkshopRunner:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    def run(self, request: ReviewRequest) -> AgentRunResult:
        resources = [
            self.config.project_template,
            self.config.project_template / "workshop.yaml",
            self.config.workshop_executable,
        ]
        if self.config.configuration_path is not None:
            resources.append(self.config.configuration_path)
        resources.append(
            self.config.profile_directory
            or Path(__file__).parent / "runtime" / "profile"
        )
        if self.config.test_shim is not None:
            resources.append(self.config.test_shim.omp_executable)
        else:
            resources.append(self.config.workshop_socket)
        for resource in resources:
            _require_external_policy(resource, request.repository_path)
        request = replace(
            request,
            model=self.config.resolve_model(request.model),
            timeout_seconds=self.config.resolve_timeout(request.timeout_seconds),
        )
        deadline = time.monotonic() + request.timeout_seconds
        try:
            root = Path(tempfile.mkdtemp(prefix="maas-review-"))
        except OSError as exc:
            raise RunnerStartupError("Cannot allocate disposable review state") from exc
        job = _Job(self.config, root, deadline)
        job.start_guard()
        returned = False
        try:
            job.prepare(request)
            job.start()
            process, task = job.agent_process(request)
            result = asyncio.run(acp_client.run_acp(process, task, deadline=deadline))
            returned = True
            return result
        except OSError as exc:
            raise RunnerStartupError(
                "Cannot prepare or execute isolated review"
            ) from exc
        finally:
            primary = None if returned else sys.exception()
            errors = job.cleanup()
            if errors:
                message = "Review cleanup failed: " + "; ".join(errors)
                if primary is not None:
                    primary.add_note(message)
                else:
                    raise RunnerStartupError(message)


class _Job:
    def __init__(self, config: RuntimeConfig, root: Path, deadline: float) -> None:
        self.config, self.root, self.deadline = config, root, deadline
        self.project = root / "project"
        self.host_cwd = root / "launcher"
        # Preserve the unique suffix without tempfile's forbidden separators.
        suffix = root.name.removeprefix("maas-review-").encode().hex()
        self.name = "review-" + suffix
        self.attempted_launch = False
        self.environment = {
            "PATH": _CLEAN_PATH,
            "HOME": str(root / "host-home"),
            "XDG_CONFIG_HOME": str(root / "host-config"),
            "XDG_DATA_HOME": str(root / "host-data"),
            "XDG_CACHE_HOME": str(root / "host-cache"),
            "XDG_STATE_HOME": str(root / "host-state"),
            "SHELL": "/bin/sh",
            "TERM": "dumb",
            "USER": "review",
            "LOGNAME": "review",
        }
        self.paths: dict[str, str] = {}
        self.lease: int | None = None
        self.guardian: subprocess.Popen | None = None

    def lifecycle_environment(self) -> dict[str, str]:
        return {
            key: value
            for key, value in self.environment.items()
            if key not in ("MAAS_REVIEW_SHIM_TOKEN", "MAAS_REVIEW_SHIM_OMP")
        } | {"WORKSHOP_SOCKET": str(self.config.workshop_socket)}

    def start_guard(self) -> None:
        try:
            (self.root / runtime_guard.CONTROL).write_text(
                json.dumps(
                    {
                        "name": self.name,
                        "project": str(self.project),
                        "launch_attempted": False,
                        "launch_change": None,
                        "launch_settled": False,
                        "launch_status": None,
                        "launch_failed": False,
                        "removed": False,
                        "shim": self.config.test_shim is not None,
                        "daemon_socket": str(self.config.workshop_socket),
                    }
                )
            )
            reader, writer = os.pipe()
        except OSError as exc:
            shutil.rmtree(self.root)
            raise RunnerStartupError(
                "Cannot create independent review cleanup lease"
            ) from exc
        try:
            self.guardian = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(runtime_guard.__file__)),
                    str(reader),
                    str(self.root),
                    str(self.config.workshop_executable),
                    self.name,
                ],
                pass_fds=(reader,),
                env=self.lifecycle_environment(),
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(writer)
            shutil.rmtree(self.root)
            raise RunnerStartupError(
                "Cannot start independent review cleanup guardian"
            ) from exc
        finally:
            os.close(reader)
        self.lease = writer

    def prepare(self, request: ReviewRequest) -> None:
        for directory in (self.project, self.host_cwd, Path(self.environment["HOME"])):
            directory.mkdir()
        config = self.config
        try:
            definition = yaml.safe_load(
                (config.project_template / "workshop.yaml").read_text()
            )
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise RunnerConfigurationError(
                "Invalid trusted Workshop definition"
            ) from exc
        if not isinstance(definition, dict):
            raise RunnerConfigurationError(
                "Trusted Workshop definition must be a mapping"
            )
        definition["name"] = self.name
        sdks = definition.get("sdks", [])
        if not isinstance(sdks, list) or any(
            not isinstance(sdk, dict)
            or not isinstance(sdk.get("name"), str)
            or sdk["name"].startswith(("project-", "try-"))
            for sdk in sdks
        ):
            raise RunnerConfigurationError(
                "Use pinned Store SDKs, not project or try SDKs"
            )
        (self.project / "workshop.yaml").write_text(yaml.safe_dump(definition))
        payload = self.project / "payload"
        payload.mkdir()
        _snapshot(request.repository_path, payload / "repository", self.deadline)
        (payload / "full.diff").write_text(request.full_diff, encoding="utf-8")
        (payload / "system.txt").write_text(
            request.system_instruction, encoding="utf-8"
        )
        profile = payload / "profile"
        profile_source = (
            config.profile_directory or Path(__file__).parent / "runtime" / "profile"
        )
        _snapshot(profile_source, profile, self.deadline)
        for name in (
            "repository",
            "full_diff",
            "system_instruction",
            "cwd",
            "agent_dir",
        ):
            path = str(getattr(config.paths, name))
            if path == "/project" or path.startswith("/project/"):
                raise RunnerConfigurationError("Runtime paths must be outside /project")
            self.paths[name] = (
                str(self.root / "sandbox" / path.lstrip("/"))
                if config.test_shim
                else path
            )
        self.paths["profile_dir"] = self.paths["agent_dir"] + "/omp"
        self.paths["launch_spec"] = self.paths["agent_dir"] + "/launch.json"
        self.paths["launcher"] = self.paths["agent_dir"] + "/launch.py"
        if not any("{system_instruction" in value for value in config.harness_argv):
            raise RunnerConfigurationError(
                "harness.argv must install the trusted system instruction"
            )
        if not any("{model}" in value for value in config.harness_argv):
            raise RunnerConfigurationError("harness.argv must select the exact {model}")
        substitutions = {
            **self.paths,
            "model": request.model or "",
            "system_instruction_text": request.system_instruction,
            "timeout_seconds": str(_remaining(self.deadline)),
        }
        self.substitutions = substitutions
        try:
            argv = [value.format_map(substitutions) for value in config.harness_argv]
        except (KeyError, ValueError) as exc:
            raise RunnerConfigurationError(
                "Unknown or invalid harness argv placeholder"
            ) from exc
        agent_dir = self.paths["agent_dir"]
        env = {
            "PATH": _CLEAN_PATH,
            "HOME": agent_dir + "/home",
            "PI_CODING_AGENT_DIR": self.paths["profile_dir"],
            "OMP_PROFILE": "",
            "SHELL": "/bin/sh",
            "TERM": "dumb",
            "USER": "workshop",
            "LOGNAME": "workshop",
            "TMPDIR": agent_dir + "/tmp",
            **{
                f"XDG_{name.upper()}_HOME": agent_dir + "/" + name
                for name in ("config", "data", "state", "cache")
            },
            "XDG_RUNTIME_DIR": agent_dir + "/run",
        }
        inherit: list[str] = []
        if config.test_shim is not None:
            shim = config.test_shim
            token = os.environ.get(shim.token_env)
            if not token:
                raise RunnerConfigurationError(
                    "Explicit test gateway token environment variable is empty"
                )
            self.environment["MAAS_REVIEW_SHIM_TOKEN"] = token
            inherit.append("MAAS_REVIEW_SHIM_TOKEN")
            provider, separator, model = (request.model or "").partition("/")
            if not separator or not provider or not model:
                raise RunnerConfigurationError(
                    "Test shim requires an exact provider/model identity"
                )
            (profile / "models.yml").write_text(
                json.dumps(
                    {
                        "providers": {
                            provider: {
                                "baseUrl": shim.gateway_url,
                                "transport": "pi-native",
                                "apiKey": "MAAS_REVIEW_SHIM_TOKEN",
                            }
                        }
                    }
                )
            )
            self.environment["MAAS_REVIEW_SHIM_OMP"] = str(shim.omp_executable)
        (payload / "launch.json").write_text(
            json.dumps(
                {
                    "argv": argv,
                    "environment": env,
                    "inherit": inherit,
                    "cwd": self.paths["cwd"],
                }
            )
        )
        shutil.copyfile(Path(runtime_launch.__file__), payload / "launch.py")
        (self.project / "layout.json").write_text(json.dumps(self.paths))
        self.bootstrap = self._bootstrap(env)

    def start(self) -> None:
        config = self.config
        if config.test_shim is None and (
            not config.workshop_version or not config.sdk_revisions
        ):
            raise RunnerConfigurationError(
                "Production profile requires exact Workshop and SDK artifact pins"
            )
        if config.workshop_version:
            version = self.command("--version").strip()
            if version != config.workshop_version:
                raise RunnerConfigurationError(
                    "Workshop executable version does not match the trusted pin"
                )
        self.attempted_launch = True
        runtime_guard.update_control(self.root, launch_attempted=True)
        output = self.command("launch", "--no-wait", self.name)
        try:
            change = runtime_guard.record_launch(self.root, output)
            details = runtime_guard.change_request(
                self.root,
                str(config.workshop_executable),
                "wait",
                change,
                env=self.lifecycle_environment(),
                timeout=_remaining(self.deadline),
            )
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            raise RunnerTimeoutError("Workshop launch exceeded its deadline") from exc
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise RunnerStartupError("Cannot settle Workshop launch change") from exc
        if not details["ready"]:
            raise RunnerStartupError("Workshop launch change is not ready")
        runtime_guard.update_control(
            self.root,
            launch_settled=True,
            launch_status=details["status"],
            launch_failed=bool(details.get("err")),
        )
        if details["status"] != "Done" or details.get("err"):
            raise RunnerStartupError("Workshop launch change failed")
        info = self.command("info", self.name)
        try:
            details = yaml.safe_load(info)
        except yaml.YAMLError as exc:
            raise RunnerStartupError(
                "Malformed Workshop readiness information"
            ) from exc
        if not isinstance(details, dict) or details.get("status") != "ready":
            raise RunnerStartupError(
                "Workshop did not reach the documented ready state"
            )
        installed = details.get("sdks", {})
        if not isinstance(installed, dict):
            raise RunnerStartupError("Workshop did not report installed SDKs")
        if set(installed) != set(config.sdk_revisions):
            raise RunnerStartupError(
                "Installed SDK set differs from the explicit artifact pins"
            )
        for name, revision in config.sdk_revisions.items():
            sdk = installed.get(name)
            if not isinstance(sdk, dict) or not str(sdk.get("installed", "")).endswith(
                f"({revision})"
            ):
                raise RunnerStartupError(
                    "Workshop SDK artifact revision does not match trusted pin"
                )
        self.command("connect", f"{self.name}/{config.authentication_interface}")
        connections = self.command("connections", "--no-headers", self.name)
        connected: set[str] = set()
        mounts: set[str] = set()
        for line in connections.splitlines():
            parts = line.split()
            if len(parts) != 4:
                raise RunnerStartupError("Malformed Workshop interface report")
            interface, plug, slot, _ = parts
            if plug == "-" or slot == "-":
                continue
            prefix = self.name + "/"
            if not plug.startswith(prefix) or not slot.startswith(prefix):
                raise RunnerStartupError("Cross-workshop interfaces are forbidden")
            plug = plug.removeprefix(prefix)
            if interface == "mount":
                if slot != prefix + "system:mount":
                    raise RunnerStartupError(
                        "Only explicitly remapped host mount interfaces are supported"
                    )
                mounts.add(plug)
            elif interface == "tunnel":
                connected.add(plug)
            else:
                raise RunnerStartupError(
                    "Credential, device, and non-tunnel interfaces are forbidden"
                )
        if (
            connected != set(config.allowed_interfaces)
            or config.authentication_interface not in connected
        ):
            raise RunnerStartupError(
                "Connected interfaces differ from the explicit least-authority policy"
            )
        if mounts != set(config.mount_interfaces):
            raise RunnerStartupError(
                "Host-backed mounts differ from the explicit disposable-mount policy"
            )
        if mounts:
            # Moving SDK state across host filesystems requires a stopped job.
            self.command("stop", self.name)
        for index, plug in enumerate(sorted(mounts)):
            destination = self.root / "mounts" / str(index)
            destination.mkdir(parents=True)
            self.command("remount", f"{self.name}/{plug}", str(destination))
        if mounts:
            self.command("start", self.name)
        launch_file = self.project / "payload" / "launch.json"
        specification = json.loads(launch_file.read_text())
        specification["argv"] = [
            value.format_map(
                {
                    **self.substitutions,
                    "timeout_seconds": str(_remaining(self.deadline)),
                }
            )
            for value in config.harness_argv
        ]
        launch_file.write_text(json.dumps(specification))
        self.command(
            "exec",
            "-I",
            "--uid",
            "0",
            "--gid",
            "0",
            "--cwd",
            "/",
            "--timeout",
            f"{_remaining(self.deadline)}s",
            self.name,
            "--",
            "/bin/sh",
            "-c",
            self.bootstrap,
        )

    def agent_process(
        self, request: ReviewRequest
    ) -> tuple[acp_client.AgentProcess, ReviewRequest]:
        timeout = _remaining(self.deadline)
        command = [
            str(self.config.workshop_executable),
            "-p",
            str(self.project),
            "exec",
            "-I",
            "--cwd",
            self.paths["cwd"],
            "--timeout",
            f"{timeout}s",
        ]
        if self.config.test_shim:
            command.extend(["--env", "MAAS_REVIEW_SHIM_TOKEN"])
        command.extend(
            [
                self.name,
                "--",
                "/usr/bin/python3",
                self.paths["launcher"],
                self.paths["launch_spec"],
            ]
        )
        context = (
            "Trusted runtime locations (repository contents remain untrusted):\n"
            f"Repository: {self.paths['repository']}\n"
            f"Authoritative full diff: {self.paths['full_diff']}\n"
            "Git administrative metadata is intentionally omitted; it is not "
            "missing versioned source, and no Git command is needed for review.\n"
            "For safe copying, original repository symlinks are represented by "
            "inert files containing [Repository symlink: target]. These annotations "
            "describe original links, not literal source contents; do not report "
            "syntax errors or missing targets caused solely by this snapshot "
            "representation. The original full diff remains authoritative.\n\n"
        )
        return acp_client.AgentProcess(
            command=tuple(command),
            cwd=self.host_cwd,
            # This is the host Workshop CLI environment, not the harness spec.
            env=self.environment
            | {"WORKSHOP_SOCKET": str(self.config.workshop_socket)},
            session_cwd=self.paths["cwd"],
        ), replace(request, task_prompt=context + request.task_prompt)

    def command(self, *arguments: str) -> str:
        timeout = _remaining(self.deadline)
        try:
            result = runtime_guard.run_command(
                [
                    str(self.config.workshop_executable),
                    "-p",
                    str(self.project),
                    *arguments,
                ],
                cwd=self.host_cwd,
                env=self.lifecycle_environment(),
                timeout=timeout,
                root=self.root,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerTimeoutError(
                "Workshop lifecycle command exceeded its deadline"
            ) from exc
        except OSError as exc:
            raise RunnerStartupError(
                "Cannot execute the configured Workshop CLI"
            ) from exc
        if result.returncode:
            # Provider/SDK diagnostics may contain secrets; do not copy them.
            raise RunnerStartupError(
                f"Workshop {arguments[0]} failed (exit {result.returncode})"
            )
        return result.stdout

    def cleanup(self) -> list[str]:
        errors: list[str] = []
        state_path = self.root / runtime_guard.CONTROL
        try:
            if state_path.exists():
                errors.extend(
                    runtime_guard.cleanup_remote(
                        self.root,
                        str(self.config.workshop_executable),
                        self.name,
                        self.lifecycle_environment(),
                    )
                )
            state = runtime_guard.read_control(self.root) if state_path.exists() else {}
            safe = not state.get(
                "launch_attempted", self.attempted_launch
            ) or state.get("removed", False)
            if safe:
                try:
                    shutil.rmtree(self.root)
                except OSError:
                    errors.append("disposable host state could not be deleted")
            else:
                errors.append(
                    f"recovery state retained for {self.name} at {state_path}"
                )
        except (OSError, ValueError) as exc:
            errors.append(
                f"cleanup recovery failed for {self.name} at {state_path}: "
                f"{type(exc).__name__}"
            )
        if self.lease is not None:
            try:
                if not errors:
                    os.write(self.lease, b"D")
            except BrokenPipeError:
                errors.append("independent cleanup guardian exited unexpectedly")
            finally:
                os.close(self.lease)
                self.lease = None
        if self.guardian is not None:
            try:
                status = self.guardian.wait(timeout=5 * _CLEANUP_SECONDS + 5)
                if status:
                    errors.append(
                        "independent cleanup guardian failed; recovery control: "
                        f"{state_path}"
                    )
            except subprocess.TimeoutExpired:
                self.guardian.kill()
                try:
                    self.guardian.wait(timeout=_CLEANUP_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                errors.append("independent cleanup guardian did not settle")
        return errors

    def _bootstrap(self, env: dict[str, str]) -> str:
        targets = {
            "repository": "repository",
            "full.diff": "full_diff",
            "system.txt": "system_instruction",
            "profile": "profile_dir",
            "launch.json": "launch_spec",
            "launch.py": "launcher",
        }
        lines = ["#!/bin/sh", "set -eu", "cd /"]
        for source, key in targets.items():
            target = self.paths[key]
            lines.append("mkdir -p -- " + shlex.quote(str(Path(target).parent)))
            lines.append(
                "cp -a -- "
                + shlex.quote("/project/payload/" + source)
                + " "
                + shlex.quote(target)
            )
        directories = [
            self.paths["cwd"],
            *[
                value
                for key, value in env.items()
                if key == "HOME" or key == "TMPDIR" or key.startswith("XDG_")
            ],
        ]
        lines.append("mkdir -p -- " + shlex.join(directories))
        lines.append(
            "chown -R 1000:1000 -- "
            + shlex.join([self.paths["agent_dir"], self.paths["cwd"]])
        )
        lines.extend(
            [
                "umount -- /project",
                "if mountpoint -q -- /project; then exit 70; fi",
                "test ! -e /project/workshop.yaml",
                "test ! -e /project/layout.json",
            ]
        )
        # These binds protect disposable input contents, not the host boundary;
        # host control was removed from the namespace above.
        for key in ("repository", "full_diff", "system_instruction"):
            target = shlex.quote(self.paths[key])
            lines.append(f"mount --bind -- {target} {target}")
            lines.append(f"mount -o remount,bind,ro -- {target}")
        return "\n".join(lines) + "\n"


def _require_external_policy(resource: Path, repository: Path) -> None:
    try:
        selected = resource.resolve(strict=True)
        checkout = repository.resolve()
    except (OSError, RuntimeError) as exc:
        raise RunnerConfigurationError(
            "Cannot resolve trusted policy resource"
        ) from exc
    if selected.is_relative_to(checkout) or resource.absolute().is_relative_to(
        repository.absolute()
    ):
        raise RunnerConfigurationError(
            "Runtime policy resources must be outside the reviewed checkout"
        )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RunnerTimeoutError("Review deadline expired")
    return remaining


def _snapshot(source: Path, destination: Path, deadline: float) -> None:
    """Copy a tree using directory descriptors and O_NOFOLLOW, never host Git."""
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        destination.mkdir()
        _copy_directory(source_fd, destination, deadline)
    finally:
        os.close(source_fd)


def _copy_directory(source_fd: int, destination: Path, deadline: float) -> None:
    for name in os.listdir(source_fd):
        # Agent inspection never needs Git's hooks, credential headers, or
        # worktree pointers into another host repository.
        if name.casefold() == ".git":
            continue
        _remaining(deadline)
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        target = destination / name
        if stat.S_ISLNK(metadata.st_mode):
            # Keep link text as review data without creating a traversable host link.
            target.write_text(
                "[Repository symlink: " + os.readlink(name, dir_fd=source_fd) + "]\n"
            )
            continue
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise RunnerConfigurationError(
                "Repository contains an unsupported special file"
            )
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(metadata.st_mode):
            flags |= os.O_DIRECTORY
        descriptor = os.open(name, flags, dir_fd=source_fd)
        try:
            current = os.fstat(descriptor)
            if stat.S_ISDIR(current.st_mode):
                target.mkdir()
                _copy_directory(descriptor, target, deadline)
            elif stat.S_ISREG(current.st_mode):
                with (
                    os.fdopen(os.dup(descriptor), "rb") as reader,
                    target.open("wb") as writer,
                ):
                    while chunk := reader.read(1024 * 1024):
                        _remaining(deadline)
                        writer.write(chunk)
            else:
                raise RunnerConfigurationError(
                    "Repository file changed during snapshot"
                )
        finally:
            os.close(descriptor)

#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Explicit Workshop CLI test double, backed by installed omp 18.12.2.

NOT a sandbox: no mount, filesystem, service, network, or credential isolation
is provided. This implements lifecycle commands, never ACP framing or replies.
All process/state paths belong to the caller's unique disposable job directory.
"""

import argparse
import json
import math
import os
import select
import shutil
import signal
import subprocess
import sys
from pathlib import Path

VERSION = "workshop-shim/1"
OMP_VERSION = "omp/18.12.2"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", type=Path, required=True)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--review-change", choices=("wait", "abort"))
    parser.add_argument("--timeout")
    parser.add_argument("command", nargs="?")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    project = args.p.resolve(strict=True)
    try:
        if args.review_change:
            if args.command is None or args.arguments or args.timeout is None:
                parser.error("change control requires an ID and bounded timeout")
            _change(project, args.review_change, args.command, args.timeout)
            return 0
        match args.command:
            case "launch":
                _launch(project, args.arguments)
            case "info":
                status = "ready" if (project / "shim-ready").exists() else "stopped"
                if _pending(project):
                    status = "pending"
                print(f"status: {status}\nsdks: {{}}")
            case "connections":
                name = args.arguments[-1]
                print(f"tunnel {name}/omp:review-inference {name}/system:tunnel -")
            case "exec":
                return _exec(project, args.arguments)
            case "stop" | "remove":
                if _pending(project):
                    raise RuntimeError("Pending launch must be aborted and settled")
                _stop(project)
                (project / "shim-ready").unlink(missing_ok=True)
            case _:
                parser.error("Unsupported Workshop test lifecycle command")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        # Do not format diagnostics that could contain provider/environment data.
        print("Workshop test shim lifecycle failure", file=sys.stderr)
        return 1


def _kill_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _stop(project: Path) -> None:
    pid_file = project / "shim-process.pid"
    if not pid_file.exists():
        return
    state = json.loads(pid_file.read_text())
    try:
        descriptor = os.pidfd_open(state["pid"])
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return
    try:
        try:
            identity = _process_identity(state["pid"])
        except FileNotFoundError:
            return
        if identity != state["identity"]:
            return
        # Signal the stable process handle, not a potentially recycled group ID.
        # The still-running shim owns/reaps the group and disposes descendants.
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        if not select.select([descriptor], [], [], 2)[0]:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            if not select.select([descriptor], [], [], 2)[0]:
                raise RuntimeError("Test job did not terminate within cleanup grace")
    except ProcessLookupError:
        pass
    finally:
        os.close(descriptor)
        pid_file.unlink(missing_ok=True)


def _process_identity(pid: int) -> str:
    # Linux stat field 22; comm (field 2) can itself contain spaces/parentheses.
    return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]


def _bootstrap(project: Path) -> None:
    layout = json.loads((project / "layout.json").read_text())
    for source, key in {
        "repository": "repository",
        "full.diff": "full_diff",
        "system.txt": "system_instruction",
        "profile": "profile_dir",
        "launch.json": "launch_spec",
        "launch.py": "launcher",
    }.items():
        target = Path(layout[key])
        target.parent.mkdir(parents=True, exist_ok=True)
        source_path = project / "payload" / source
        if source_path.is_dir():
            shutil.copytree(source_path, target)
        else:
            shutil.copyfile(source_path, target)
    specification = json.loads(Path(layout["launch_spec"]).read_text())
    for key, value in specification["environment"].items():
        if key in ("HOME", "TMPDIR") or key.startswith("XDG_"):
            Path(value).mkdir(parents=True, exist_ok=True)
    Path(layout["cwd"]).mkdir(parents=True)


def _exec(project: Path, arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-I", action="store_true", required=True)
    parser.add_argument("--uid")
    parser.add_argument("--gid")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--timeout", required=True)
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("name")
    try:
        separator = arguments.index("--")
    except ValueError:
        parser.error("exec requires an explicit command separator")
    args = parser.parse_args(arguments[:separator])
    command = arguments[separator + 1 :]
    if not command:
        parser.error("exec requires a command after the separator")
    if command == ["/bin/sh", "/project/bootstrap.sh"]:
        _bootstrap(project)
        return 0
    if not (project / "shim-ready").exists():
        raise RuntimeError("Workshop test job is not ready")
    timeout = float(args.timeout.removesuffix("s"))
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "MAAS_REVIEW_SHIM_TOKEN"
    }
    for name in args.env:
        if name != "MAAS_REVIEW_SHIM_TOKEN":
            raise RuntimeError(
                "Test shim only accepts its explicitly scoped test bearer"
            )
        environment[name] = os.environ[name]
    omp = os.environ.get("MAAS_REVIEW_SHIM_OMP")
    if not omp:
        raise RuntimeError("Explicit test shim omp executable is missing")
    specification = json.loads(Path(command[-1]).read_text())
    if Path(specification["argv"][0]).resolve() != Path(omp).resolve():
        raise RuntimeError("Test shim harness differs from the explicitly selected omp")
    version = subprocess.run(
        [omp, "--version"],
        cwd=args.cwd,
        env=specification["environment"],
        capture_output=True,
        text=True,
        timeout=min(timeout, 10),
        check=True,
    )
    if version.stdout.strip() != OMP_VERSION:
        raise RuntimeError("Test shim requires installed omp 18.12.2")
    process = subprocess.Popen(
        command, cwd=args.cwd, env=environment, start_new_session=True
    )
    pid_file = project / "shim-process.pid"

    def interrupted(signum, frame):
        _kill_group(process.pid, signal.SIGTERM)
        raise InterruptedError

    previous = {}
    try:
        state_file = pid_file.with_suffix(".tmp")
        state_file.write_text(
            json.dumps({"pid": process.pid, "identity": _process_identity(process.pid)})
        )
        state_file.replace(pid_file)
        previous = {
            sig: signal.signal(sig, interrupted)
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        }
        return process.wait(timeout=timeout)
    except InterruptedError:
        return 130
    except subprocess.TimeoutExpired:
        print("Workshop test job exceeded its explicit deadline", file=sys.stderr)
        return 124
    finally:
        _kill_group(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_group(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        _kill_group(process.pid, signal.SIGKILL)
        pid_file.unlink(missing_ok=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _launch(project: Path, arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("name")
    args = parser.parse_args(arguments)
    if (project / "shim-ready").exists() or _pending(project):
        raise RuntimeError("A test job already exists in this project")
    changes = project / "shim-changes"
    changes.mkdir(exist_ok=True)
    identifier = str(len(list(changes.glob("*.json"))) + 1)
    change = {
        "id": identifier,
        "ready": not args.no_wait,
        "status": "Doing" if args.no_wait else "Done",
    }
    (changes / (identifier + ".json")).write_text(json.dumps(change))
    if args.no_wait:
        print(identifier)
    else:
        (project / "shim-ready").touch(exist_ok=False)
    print("LOCAL WORKSHOP TEST SHIM: not a sandbox", file=sys.stderr)


def _pending(project: Path) -> bool:
    changes = project / "shim-changes"
    return changes.exists() and any(
        json.loads(path.read_text())["ready"] is not True
        for path in changes.glob("*.json")
    )


def _change(project: Path, operation: str, identifier: str, timeout: str) -> None:
    seconds = float(timeout.removesuffix("s"))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Change timeout must be positive and finite")
    if not identifier.isascii() or not identifier.isdecimal() or int(identifier) <= 0:
        raise ValueError("Invalid test change identity")
    path = project / "shim-changes" / (identifier + ".json")
    change = json.loads(path.read_text())
    if operation == "abort":
        if change["ready"]:
            raise RuntimeError("Cannot abort a settled change")
        change.update(ready=True, status="Undone")
        (project / "shim-ready").unlink(missing_ok=True)
    elif not change["ready"]:
        change.update(ready=True, status="Done")
        (project / "shim-ready").touch(exist_ok=True)
    path.write_text(json.dumps(change))
    print(json.dumps(change))


if __name__ == "__main__":
    sys.exit(main())

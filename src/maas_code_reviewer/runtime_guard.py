# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Independent, bounded lifecycle ownership and protected recovery metadata."""

import fcntl
import http.client
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

CONTROL = "control.json"
CLEANUP_SECONDS = 10.0
# Workshop's LXD backend permits 60 seconds for a normal stop, plus bookkeeping.
STOP_SECONDS = 75.0
GUARD_WAIT_SECONDS = STOP_SECONDS + 10 * CLEANUP_SECONDS + 5


def read_control(root: Path) -> dict:
    return json.loads((root / CONTROL).read_text())


def update_control(root: Path, **changes) -> dict:
    state = read_control(root)
    state.update(changes)
    temporary = root / "control.new"
    temporary.write_text(json.dumps(state))
    temporary.replace(root / CONTROL)
    return state


def record_launch(root: Path, output: str) -> str:
    change = output.strip()
    if not change.isascii() or not change.isdecimal():
        raise ValueError("Workshop did not return a launch change ID")
    update_control(root, launch_change=change)
    return change


def change_request(
    root: Path,
    executable: str,
    operation: str,
    change: str,
    *,
    env: dict[str, str],
    timeout: float,
) -> dict:
    """Use pinned client/changes.go APIs; Workshop has no abort/wait commands."""
    state = read_control(root)
    if state["shim"]:
        result = run_command(
            [
                executable,
                "-p",
                str(root / "project"),
                "--review-change",
                operation,
                "--timeout",
                f"{timeout}s",
                change,
            ],
            cwd=root,
            env=env,
            timeout=timeout,
            root=root,
        )
        if result.returncode:
            raise OSError("Workshop shim change operation failed")
        details = json.loads(result.stdout)
    else:
        path = "/v1/changes/" + change
        if operation == "abort":
            details = _daemon_request(
                state["daemon_socket"], "POST", path, timeout, '{"action":"abort"}'
            )
        else:
            details = _daemon_request(
                state["daemon_socket"],
                "GET",
                path + f"/wait?timeout={timeout}s",
                timeout,
            )
    if (
        not isinstance(details, dict)
        or details.get("id") != change
        or not isinstance(details.get("ready"), bool)
        or not isinstance(details.get("status"), str)
    ):
        raise ValueError("Malformed Workshop change information")
    return details


def cleanup_remote(
    root: Path, executable: str, name: str, env: dict[str, str]
) -> list[str]:
    """Only confirmed removal or confirmed absence permits deleting host state."""
    state = read_control(root)
    if not state["launch_attempted"] or state["removed"]:
        return []
    change = state["launch_change"]
    if change is None:
        return ["launch outcome is uncertain (no launch change ID was received)"]
    if not state["launch_settled"]:
        try:
            # Abort may race a completed launch. A subsequent ready response is
            # authoritative even if the abort was rejected as already complete.
            try:
                change_request(
                    root, executable, "abort", change, env=env, timeout=CLEANUP_SECONDS
                )
            except (
                OSError,
                ValueError,
                http.client.HTTPException,
                subprocess.TimeoutExpired,
            ):
                pass
            details = change_request(
                root, executable, "wait", change, env=env, timeout=CLEANUP_SECONDS
            )
            if not details["ready"]:
                return ["Workshop launch change did not settle"]
            state = update_control(
                root,
                launch_settled=True,
                launch_status=details["status"],
                launch_failed=bool(details.get("err")),
            )
        except (
            OSError,
            ValueError,
            http.client.HTTPException,
            subprocess.TimeoutExpired,
        ):
            return ["Workshop launch abort/settlement did not complete"]
    errors: list[str] = []
    base = [executable, "-p", str(root / "project")]
    if state["launch_status"] == "Undone" and not state.get("launch_failed", False):
        try:
            result = run_command(
                base + ["info", name],
                cwd=root,
                env=env,
                timeout=CLEANUP_SECONDS,
                root=root,
            )
            if result.returncode == 0 and any(
                line.startswith("status:")
                and line.removeprefix("status:").strip() == "off"
                for line in result.stdout.splitlines()
            ):
                update_control(root, removed=True)
                return []
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        _settle_exec_changes(root, executable, env)
    except (OSError, ValueError, http.client.HTTPException, subprocess.TimeoutExpired):
        errors.append("Workshop exec changes did not settle")
    for action in ("stop", "remove"):
        try:
            result = run_command(
                base + [action, name],
                cwd=root,
                env=env,
                timeout=STOP_SECONDS if action == "stop" else CLEANUP_SECONDS,
                root=root,
            )
            if result.returncode:
                errors.append(f"Workshop {action} did not complete")
            elif action == "remove":
                update_control(root, removed=True)
        except (OSError, subprocess.TimeoutExpired):
            errors.append(f"Workshop {action} did not complete")
    return errors


def guard(descriptor: int, root: Path, executable: str, name: str) -> int:
    try:
        disarmed = os.read(descriptor, 1) == b"D"
    finally:
        os.close(descriptor)
    if disarmed:
        return 0
    try:
        # The independently bounded supervisor inherits this lock before it can
        # launch a CLI. EOF cannot race a late launch or an unrecorded response.
        with (root / "command.lock").open("a") as lock:
            deadline = time.monotonic() + CLEANUP_SECONDS
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        print(
                            f"Review guardian retained recovery state for {name} at "
                            f"{root / CONTROL}: lifecycle supervisor did not settle",
                            file=sys.stderr,
                        )
                        return 1
                    time.sleep(0.05)
        errors = cleanup_remote(root, executable, name, dict(os.environ))
        state = read_control(root)
        if state["launch_attempted"] and not state["removed"]:
            message = "; ".join(errors)
            print(
                f"Review guardian retained recovery state for {name} at "
                f"{root / CONTROL}: {message}",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(root)
        if errors:
            print(
                "Review guardian cleanup failed: " + "; ".join(errors), file=sys.stderr
            )
            return 1
    except FileNotFoundError:
        if root.exists():
            print(
                f"Review guardian retained uncertain recovery state for {name} "
                f"at {root}",
                file=sys.stderr,
            )
            return 1
    except (OSError, ValueError):
        print(
            f"Review guardian could not delete or recover host state for {name} "
            f"at {root}",
            file=sys.stderr,
        )
        return 1
    return 0


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    root: Path | None = None,
) -> subprocess.CompletedProcess:
    """The deadline belongs to a separate supervisor, not the coordinator."""
    lock = (root / "command.lock").open("a") if root is not None else None
    try:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        descriptor = lock.fileno() if lock is not None else -1
        owner_reader, owner_writer = os.pipe()
        try:
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__)),
                        "--command",
                        str(timeout),
                        str(descriptor),
                        str(root) if root is not None else "",
                        str(owner_reader),
                        json.dumps(command),
                    ],
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    start_new_session=True,
                    pass_fds=(owner_reader, descriptor)
                    if descriptor >= 0
                    else (owner_reader,),
                )
            finally:
                os.close(owner_reader)
            output, _ = process.communicate()
        finally:
            os.close(owner_writer)
    finally:
        if lock is not None:
            lock.close()
    try:
        result = json.loads(output)
    except ValueError as exc:
        raise OSError("Lifecycle supervisor did not return a result") from exc
    if result.get("timeout"):
        raise subprocess.TimeoutExpired(command, timeout)
    if result.get("error"):
        raise OSError("Cannot execute Workshop lifecycle command")
    return subprocess.CompletedProcess(
        command, result["returncode"], result["stdout"], ""
    )


def supervise(
    command: list[str],
    timeout: float,
    descriptor: int,
    root: Path | None,
    owner: int = -1,
) -> dict:
    try:
        result = _execute_command(
            command, cwd=Path.cwd(), env=dict(os.environ), timeout=timeout, owner=owner
        )
        if root is not None and command[3] == "launch" and result.returncode == 0:
            record_launch(root, result.stdout)
        return {"returncode": result.returncode, "stdout": result.stdout}
    except subprocess.TimeoutExpired as expired:
        if root is not None and command[3] == "launch" and expired.output:
            try:
                record_launch(root, expired.output.decode("utf-8", "replace"))
            except ValueError:
                pass
        return {"timeout": True}
    except (OSError, ValueError):
        return {"error": True}
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if owner >= 0:
            os.close(owner)


def _daemon_request(
    socket_path: str, method: str, path: str, timeout: float, body: str | None = None
):
    connection = http.client.HTTPConnection("localhost", timeout=timeout)
    transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        transport.settimeout(timeout)
        transport.connect(socket_path)
        connection.sock = transport
        connection.request(method, path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        envelope = json.loads(response.read())
        if response.status == 504:
            raise subprocess.TimeoutExpired(["Workshop", method, path], timeout)
        if (
            response.status != 200
            or not isinstance(envelope, dict)
            or envelope.get("type") != "sync"
        ):
            raise OSError("Workshop daemon change operation failed")
        return envelope.get("result")
    finally:
        connection.close()
        transport.close()


def _settle_exec_changes(root: Path, executable: str, env: dict[str, str]) -> None:
    state = read_control(root)
    if state["shim"] or state["launch_status"] != "Done":
        return
    launch = change_request(
        root,
        executable,
        "wait",
        state["launch_change"],
        env=env,
        timeout=CLEANUP_SECONDS,
    )
    project = launch.get("project-id")
    if not isinstance(project, str) or not project:
        raise ValueError("Workshop launch did not identify its project")
    query = urlencode({"select": "in-progress", "project-id": project})
    pending = _daemon_request(
        state["daemon_socket"], "GET", "/v1/changes?" + query, CLEANUP_SECONDS
    )
    if not isinstance(pending, list):
        raise ValueError("Malformed Workshop pending-change report")
    # The job runs one ACP process, never overlapping Workshop exec commands.
    if len(pending) > 1:
        raise ValueError("Unexpected overlapping Workshop exec operations")
    for change in pending:
        if (
            not isinstance(change, dict)
            or change.get("project-id") != project
            or change.get("kind") != "exec"
            or not isinstance(change.get("id"), str)
            or not change["id"].isascii()
            or not change["id"].isdecimal()
        ):
            raise ValueError("Unexpected pending Workshop operation")
        identity = change["id"]
        try:
            change_request(
                root, executable, "abort", identity, env=env, timeout=CLEANUP_SECONDS
            )
        except (
            OSError,
            ValueError,
            http.client.HTTPException,
            subprocess.TimeoutExpired,
        ):
            # Completion can race abort; readiness from wait remains authoritative.
            pass
        settled = change_request(
            root, executable, "wait", identity, env=env, timeout=CLEANUP_SECONDS
        )
        if not settled["ready"]:
            raise OSError("Workshop exec change did not settle")


def _execute_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    owner: int = -1,
) -> subprocess.CompletedProcess:
    """Bound the CLI and all host children, including inherited pipe owners."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(remaining, 0.1) if owner >= 0 else remaining,
                    )
                    break
                except subprocess.TimeoutExpired:
                    if (
                        remaining > 0
                        and owner >= 0
                        and not select.select([owner], [], [], 0)[0]
                    ):
                        continue
                    raise
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate(timeout=2)
            raise subprocess.TimeoutExpired(
                command, timeout, output=stdout.encode(), stderr=stderr.encode()
            ) from None
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


if __name__ == "__main__":
    if sys.argv[1] == "--command":
        print(
            json.dumps(
                supervise(
                    json.loads(sys.argv[6]),
                    float(sys.argv[2]),
                    int(sys.argv[3]),
                    Path(sys.argv[4]) if sys.argv[4] else None,
                    int(sys.argv[5]),
                )
            )
        )
    else:
        sys.exit(guard(int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]))

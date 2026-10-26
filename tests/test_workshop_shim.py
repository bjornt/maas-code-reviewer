# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from maas_code_reviewer import runtime_launch
from scripts import workshop_shim


@pytest.fixture
def local_job(tmp_path: Path):
    project = tmp_path / "job" / "project"
    payload = project / "payload"
    payload.mkdir(parents=True)
    sandbox = tmp_path / "job" / "sandbox"
    paths = {
        key: str(sandbox / name)
        for key, name in {
            "repository": "repository",
            "full_diff": "full.diff",
            "system_instruction": "system.txt",
            "profile_dir": "state/omp",
            "agent_dir": "state",
            "launch_spec": "state/launch.json",
            "launcher": "state/launch.py",
            "cwd": "cwd",
        }.items()
    }
    (payload / "repository").mkdir()
    (payload / "repository" / "source").write_text("snapshot")
    (payload / "profile").mkdir()
    (payload / "full.diff").write_text("authoritative")
    (payload / "system.txt").write_text("trusted")
    (payload / "launch.py").write_text(Path(runtime_launch.__file__).read_text())
    observation = tmp_path / "observed.json"
    omp = tmp_path / "fake-installed-omp"
    omp.write_text("""#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("omp/18.12.2")
else:
    Path(sys.argv[1]).write_text(json.dumps({
        "cwd": os.getcwd(), "environment": dict(os.environ), "pid": os.getpid(),
    }))
    if os.environ.get("MODE") == "wait":
        time.sleep(120)
    else:
        sys.stdout.write(sys.stdin.read())
""")
    omp.chmod(0o700)
    environment = {
        "HOME": str(sandbox / "home"),
        "PI_CODING_AGENT_DIR": paths["profile_dir"],
        "PATH": "/usr/bin:/bin",
    }
    specification = {
        "argv": [str(omp), str(observation)],
        "environment": environment,
        "inherit": ["MAAS_REVIEW_SHIM_TOKEN"],
        "cwd": paths["cwd"],
    }
    (payload / "launch.json").write_text(json.dumps(specification))
    (project / "layout.json").write_text(json.dumps(paths))
    return project, paths, omp, observation


def test_lifecycle_shim_transports_real_process_stdio_without_protocol_translation(
    local_job,
) -> None:
    project, paths, omp, observation = local_job
    environment = {
        "PATH": "/usr/bin:/bin",
        "MAAS_REVIEW_SHIM_OMP": str(omp),
        "MAAS_REVIEW_SHIM_TOKEN": "test-bearer",
        "GITHUB_TOKEN": "must-not-reach-agent",
    }
    assert workshop_shim.main(["-p", str(project), "launch", "review"]) == 0
    assert (
        workshop_shim.main(
            [
                "-p",
                str(project),
                "exec",
                "-I",
                "--uid",
                "0",
                "--gid",
                "0",
                "--cwd",
                "/",
                "--timeout",
                "10s",
                "review",
                "--",
                "/bin/sh",
                "-c",
                "set -eu\ncp -a -- /project/payload/. /review/",
            ]
        )
        == 0
    )
    result = subprocess.run(
        _exec_command(project, paths),
        input="opaque ACP transport bytes\n",
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout == "opaque ACP transport bytes\n"
    observed = json.loads(observation.read_text())
    assert observed["cwd"] == paths["cwd"]
    assert observed["environment"]["HOME"] != str(Path.home())
    assert observed["environment"]["MAAS_REVIEW_SHIM_TOKEN"] == "test-bearer"
    assert "GITHUB_TOKEN" not in observed["environment"]
    assert not (project / "shim-process.pid").exists()
    assert workshop_shim.main(["-p", str(project), "stop", "review"]) == 0
    assert workshop_shim.main(["-p", str(project), "remove", "review"]) == 0
    assert not (project / "shim-ready").exists()


def test_timeout_kills_the_owned_process_and_removes_pid_state(local_job) -> None:
    project, paths, omp, observation = local_job
    workshop_shim.main(["-p", str(project), "launch", "review"])
    spec_file = project / "payload" / "launch.json"
    specification = json.loads(spec_file.read_text())
    specification["environment"]["MODE"] = "wait"
    spec_file.write_text(json.dumps(specification))
    workshop_shim._bootstrap(project)
    environment = {
        "PATH": "/usr/bin:/bin",
        "MAAS_REVIEW_SHIM_OMP": str(omp),
        "MAAS_REVIEW_SHIM_TOKEN": "test",
    }
    result = subprocess.run(
        _exec_command(project, paths, "0.3s"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )
    assert result.returncode == 124
    pid = json.loads(observation.read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not (project / "shim-process.pid").exists()
    assert "test" not in result.stdout


def test_signal_teardown_reaps_agent_and_preserves_clean_protocol_output(
    local_job,
) -> None:
    project, paths, omp, observation = local_job
    workshop_shim.main(["-p", str(project), "launch", "review"])
    spec_file = project / "payload" / "launch.json"
    specification = json.loads(spec_file.read_text())
    specification["environment"]["MODE"] = "wait"
    spec_file.write_text(json.dumps(specification))
    workshop_shim._bootstrap(project)
    environment = {
        "PATH": "/usr/bin:/bin",
        "MAAS_REVIEW_SHIM_OMP": str(omp),
        "MAAS_REVIEW_SHIM_TOKEN": "test",
    }
    process = subprocess.Popen(
        _exec_command(project, paths),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    deadline = time.monotonic() + 10
    while not observation.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert observation.exists()
    pid = json.loads(observation.read_text())["pid"]
    descriptor = os.pidfd_open(pid)
    try:
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=10)
        assert process.returncode == 130
        assert stdout == ""
        assert select.select([descriptor], [], [], 1)[0] == [descriptor]
    finally:
        os.close(descriptor)
    assert not (project / "shim-process.pid").exists()


def test_shim_does_not_discover_missing_explicit_harness(
    local_job, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project, paths, omp, observation = local_job
    workshop_shim.main(["-p", str(project), "launch", "review"])
    workshop_shim._bootstrap(project)
    monkeypatch.delenv("MAAS_REVIEW_SHIM_OMP", raising=False)
    status = workshop_shim.main(
        [
            "-p",
            str(project),
            "exec",
            "-I",
            "--cwd",
            paths["cwd"],
            "--timeout",
            "1s",
            "review",
            "--",
            "/usr/bin/python3",
            paths["launcher"],
            paths["launch_spec"],
        ]
    )
    assert status == 1
    assert not observation.exists()
    assert "lifecycle failure" in capsys.readouterr().err


def test_shim_reports_only_explicit_test_identity(local_job, capsys) -> None:
    project, paths, omp, observation = local_job
    assert workshop_shim.main(["-p", str(project), "info", "review"]) == 0
    assert "stopped" in capsys.readouterr().out
    assert (
        workshop_shim.main(
            ["-p", str(project), "connections", "--no-headers", "review"]
        )
        == 0
    )
    assert "omp:review-inference" in capsys.readouterr().out


def test_inference_tunnel_requires_explicit_connection(local_job, capsys) -> None:
    project, paths, omp, observation = local_job
    workshop_shim.main(["-p", str(project), "launch", "review"])
    capsys.readouterr()

    def inference_slot() -> str:
        workshop_shim.main(
            ["-p", str(project), "connections", "--no-headers", "review"]
        )
        return capsys.readouterr().out.split()[2]

    assert inference_slot() == "-"
    workshop_shim.main(["-p", str(project), "connect", "review/omp:review-inference"])
    assert inference_slot() == "review/system:review-inference"
    workshop_shim.main(["-p", str(project), "stop", "review"])
    assert inference_slot() == "-"


def test_stop_uses_stable_process_identity_and_reaps_active_work(local_job) -> None:
    project, paths, omp, observation = local_job
    workshop_shim.main(["-p", str(project), "launch", "review"])
    spec_file = project / "payload" / "launch.json"
    specification = json.loads(spec_file.read_text())
    specification["environment"]["MODE"] = "wait"
    spec_file.write_text(json.dumps(specification))
    workshop_shim._bootstrap(project)
    environment = {
        "PATH": "/usr/bin:/bin",
        "MAAS_REVIEW_SHIM_OMP": str(omp),
        "MAAS_REVIEW_SHIM_TOKEN": "test",
    }
    process = subprocess.Popen(
        _exec_command(project, paths),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    deadline = time.monotonic() + 10
    while not observation.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert observation.exists()
    assert workshop_shim.main(["-p", str(project), "stop", "review"]) == 0
    stdout, _ = process.communicate(timeout=10)
    assert process.returncode != 0
    assert stdout == ""
    assert not (project / "shim-process.pid").exists()


def test_stale_pid_file_cannot_signal_a_reused_process(local_job) -> None:
    project, paths, omp, observation = local_job
    pid_file = project / "shim-process.pid"
    pid_file.write_text(
        json.dumps({"pid": os.getpid(), "identity": "not-this-process"})
    )
    workshop_shim._stop(project)
    assert not pid_file.exists()


@pytest.mark.parametrize("ending", [[], ["--"]])
def test_exec_rejects_missing_explicit_command(local_job, ending: list[str]) -> None:
    project, paths, omp, observation = local_job
    with pytest.raises(SystemExit) as exc:
        workshop_shim.main(
            [
                "-p",
                str(project),
                "exec",
                "-I",
                "--cwd",
                paths["cwd"],
                "--timeout",
                "1s",
                "review",
                *ending,
            ]
        )
    assert exc.value.code == 2
    assert not observation.exists()


def test_pending_launch_requires_settlement_before_removal(local_job, capsys) -> None:
    project, paths, omp, observation = local_job
    assert (
        workshop_shim.main(["-p", str(project), "launch", "--no-wait", "review"]) == 0
    )
    identifier = capsys.readouterr().out.strip()
    assert workshop_shim.main(["-p", str(project), "remove", "review"]) == 1
    capsys.readouterr()
    assert (
        workshop_shim.main(
            [
                "-p",
                str(project),
                "--review-change",
                "wait",
                "--timeout",
                "1s",
                identifier,
            ]
        )
        == 0
    )
    settled = json.loads(capsys.readouterr().out)
    assert settled["ready"] is True and settled["status"] == "Done"
    assert workshop_shim.main(["-p", str(project), "remove", "review"]) == 0
    assert not (project / "shim-ready").exists()


def test_pending_launch_can_be_aborted_without_becoming_ready(
    local_job, capsys
) -> None:
    project, paths, omp, observation = local_job
    workshop_shim.main(["-p", str(project), "launch", "--no-wait", "review"])
    identifier = capsys.readouterr().out.strip()
    assert (
        workshop_shim.main(
            [
                "-p",
                str(project),
                "--review-change",
                "abort",
                "--timeout",
                "1s",
                identifier,
            ]
        )
        == 0
    )
    aborted = json.loads(capsys.readouterr().out)
    assert aborted["ready"] is True and aborted["status"] == "Undone"
    assert not (project / "shim-ready").exists()
    assert not observation.exists()
    assert workshop_shim.main(["-p", str(project), "remove", "review"]) == 0


def _command(project: Path, *arguments: str) -> list[str]:
    return [
        sys.executable,
        str(Path(workshop_shim.__file__)),
        "-p",
        str(project),
        *arguments,
    ]


def _exec_command(
    project: Path, paths: dict[str, str], timeout: str = "10s"
) -> list[str]:
    return _command(
        project,
        "exec",
        "-I",
        "--cwd",
        paths["cwd"],
        "--timeout",
        timeout,
        "--env",
        "MAAS_REVIEW_SHIM_TOKEN",
        "review",
        "--",
        "/usr/bin/python3",
        paths["launcher"],
        paths["launch_spec"],
    )

# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import fcntl
import http.client
import http.server
import json
import os
import runpy
import shlex
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
import yaml
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from maas_code_reviewer import runtime_guard, runtime_launch, workshop_runner
from maas_code_reviewer.agent_runner import (
    AgentRunResult,
    CompletionStatus,
    ReviewRequest,
    RunnerConfigurationError,
    RunnerStartupError,
    RunnerTimeoutError,
    RunnerTransportError,
)
from maas_code_reviewer.runtime_config import ContainerPaths, RuntimeConfig
from maas_code_reviewer.runtime_config import TestShimConfig as ShimConfig
from maas_code_reviewer.workshop_runner import ConfiguredAgentRunner, WorkshopRunner
from scripts import workshop_shim


def control(root: Path, **changes) -> dict:
    state = {
        "name": "job",
        "project": str(root / "project"),
        "launch_attempted": False,
        "launch_change": None,
        "launch_settled": False,
        "launch_status": None,
        "removed": False,
        "shim": True,
        "daemon_socket": str(root / "daemon.socket"),
    } | changes
    (root / runtime_guard.CONTROL).write_text(json.dumps(state))
    return state


@pytest.fixture
def remote_service(tmp_path: Path) -> tuple[Path, Path]:
    """A daemon-like service persists launches independently of waiting clients."""
    record = tmp_path / "remote.json"
    record.write_text(json.dumps({"pending": False, "running": False, "actions": []}))
    executable = tmp_path / "remote-workshop"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
import sys
import time
from pathlib import Path
"""
        + f"record = Path({str(record)!r})\n"
        + """
state = json.loads(record.read_text())
action = sys.argv[3]
if action == "--review-change":
    action = sys.argv[4]
state["actions"].append(action)
def save():
    temporary = record.with_suffix(".new")
    temporary.write_text(json.dumps(state))
    temporary.replace(record)
if action == "launch":
    state.update(pending=True, running=True, cli_pid=os.getpid())
    save()
    time.sleep(state.get("response_delay", 0))
    print(state.get("launch_reply", "1"), flush=True)
    time.sleep(state.get("post_response_delay", 0))
elif action == "abort":
    state.update(pending=False, running=False, aborted=True)
    save()
    print(json.dumps({"id":"1", "ready":True, "status":"Undone"}))
elif action == "wait":
    state["wait_pid"] = os.getpid()
    save()
    if state["pending"] and state.get("stall"):
        time.sleep(120)
    state["pending"] = False
    save()
    print(json.dumps({"id":"1", "ready":True,
                      "status":"Undone" if state.get("aborted") else "Done"}))
elif action == "info":
    save()
    print("status: off" if state.get("aborted") else "status: ready")
elif action in ("stop", "remove"):
    if action == "stop":
        time.sleep(state.get("stop_delay", 0))
    if state["pending"] or (
        action == "remove" and (
            state.get("fail_remove") or state.get("pending_exec")
            or (state.get("require_stopped") and state["running"])
        )
    ):
        save()
        sys.exit(12)
    state["running"] = False
    if action == "remove":
        state["removed"] = True
    save()
else:
    sys.exit(13)
"""
    )
    executable.chmod(0o700)
    return executable, record


@pytest.fixture
def unix_daemon():
    state = {
        "status": 200,
        "reply": {
            "type": "sync",
            "status-code": 200,
            "result": {
                "id": "1",
                "ready": True,
                "status": "Done",
                "project-id": "owned",
            },
        },
        "requests": [],
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["requests"].append((self.command, self.path, body))
            reply = state["reply"]
            status = state["status"]
            if self.path.startswith("/v1/changes?"):
                pending = []
                if state.get("exec_record"):
                    remote = json.loads(state["exec_record"].read_text())
                    if remote.get("pending_exec"):
                        pending = [{"id": "2", "kind": "exec", "project-id": "owned"}]
                reply = dict(reply, result=state.get("pending_changes", pending))
            elif self.path.startswith("/v1/changes/2") and state.get("exec_record"):
                remote = json.loads(state["exec_record"].read_text())
                if self.command == "POST":
                    if not state.get("unsettled"):
                        remote["pending_exec"] = False
                        state["exec_record"].write_text(json.dumps(remote))
                    if state.get("abort_race"):
                        status = 400
                reply = dict(
                    reply,
                    result={
                        "id": "2",
                        "ready": not remote["pending_exec"],
                        "status": "Doing" if remote["pending_exec"] else "Done",
                        "project-id": "owned",
                    },
                )
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = reply
        do_POST = reply

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="review-daemon-") as directory:
        path = Path(directory) / "socket"
        with socketserver.UnixStreamServer(str(path), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                yield path, state
            finally:
                server.shutdown()
                thread.join()


@pytest.fixture
def review_request(tmp_path: Path) -> ReviewRequest:
    repository = tmp_path / "checkout"
    repository.mkdir()
    (repository / "source.py").write_text("original\n")
    (repository / "workshop.yaml").write_text("actions: malicious\n")
    (repository / ".git").mkdir()
    (repository / ".git" / "config").write_text(
        "[core]\nhooksPath=/malicious\n"
        "[http]\nextraHeader=AUTHORIZATION: fake-publication-credential\n"
    )
    (repository / ".omp").mkdir()
    (repository / ".omp" / "config.yml").write_text("extensions: [malicious]\n")
    return ReviewRequest(
        repository,
        "authoritative full diff\n",
        "review task",
        "trusted policy",
        30,
        "provider/model",
    )


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeConfig:
    executable = tmp_path / "workshop"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    daemon_socket = tmp_path / "daemon.socket"
    daemon_socket.touch()
    monkeypatch.setenv("TEST_REVIEW_BEARER", "test-secret-not-for-files")
    return RuntimeConfig(
        workshop_executable=executable,
        harness_argv=(
            "/opt/omp",
            "acp",
            "--model={model}",
            "--append-system-prompt={system_instruction_text}",
            "--max-time={timeout_seconds}s",
        ),
        paths=ContainerPaths(
            *map(
                PurePosixPath,
                (
                    "/review/repository",
                    "/review/full.diff",
                    "/review/system.txt",
                    "/review/cwd",
                    "/review/state",
                ),
            )
        ),
        default_model="provider/model",
        allowed_models=("provider/model", "provider/other"),
        review_timeout_seconds=30,
        test_shim=ShimConfig(executable, "http://localhost:4000", "TEST_REVIEW_BEARER"),
        workshop_socket=daemon_socket,
    )


class WorkshopService:
    """Stateful lifecycle service: it provisions inputs and owns per-job state."""

    def __init__(self) -> None:
        self.jobs: dict[Path, dict] = {}
        self.actions: list[tuple[Path, str]] = []
        self.info: str | None = None
        self.connection: str | None = None
        self.fail: set[str] = set()
        self.timeout: str | None = None
        self.pending_launch = False
        self.change_override: object | None = None

    def run(self, command, **kwargs):
        assert "MAAS_REVIEW_SHIM_TOKEN" not in kwargs["env"]
        project, action = Path(command[2]), command[3]
        if action == "--review-change":
            action = command[4]
        self.actions.append((project, action))
        if action == self.timeout and (
            action != "wait" or self.jobs[project]["pending"]
        ):
            raise subprocess.TimeoutExpired(command, 1)
        if action in self.fail:
            return subprocess.CompletedProcess(command, 12, "", "secret diagnostic")
        if action == "--version":
            text = "workshop-shim/1\n"
        elif action == "launch":
            definition = yaml.safe_load((project / "workshop.yaml").read_text())
            name = definition["name"]
            if not (
                len(name) <= 40
                and name[:1].islower()
                and all(
                    part and part.isascii() and part.isalnum() and part == part.lower()
                    for part in name.split("-")
                )
            ):
                return subprocess.CompletedProcess(command, 12, "", "Invalid name")
            self.jobs[project] = {
                "name": definition["name"],
                "running": True,
                "pending": self.pending_launch,
                "aborted": False,
                "tunnels": definition["sdks"][0]["plugs"],
                "connected": set(),
                "mounts": {},
            }
            text = "1\n"
        elif action == "info":
            text = (
                self.info
                if self.info is not None
                else (
                    "status: off\nsdks: {}\n"
                    if self.jobs[project]["aborted"]
                    else "status: ready\nsdks: {}\n"
                )
            )
        elif action == "connect":
            job = self.jobs[project]
            plug = command[4].partition(":")[2]
            if plug not in job["tunnels"]:
                return subprocess.CompletedProcess(command, 12, "", "Unknown plug")
            job["connected"].add(plug)
            text = ""
        elif action == "connections":
            job = self.jobs[project]
            name = job["name"]
            text = (
                self.connection
                if self.connection is not None
                else "".join(
                    f"tunnel {name}/omp:{plug} "
                    + (
                        f"{name}/system:{plug} manual\n"
                        if plug in job["connected"]
                        else "- -\n"
                    )
                    for plug in job["tunnels"]
                )
            )
            text = text.replace("JOB", name)
        elif action == "exec":
            if not self.jobs[project]["running"]:
                return subprocess.CompletedProcess(command, 12, "", "Job is stopped")
            workshop_shim._bootstrap(project)
            text = ""
        elif action in ("wait", "abort"):
            job = self.jobs[project]
            if action == "abort" and job["pending"]:
                job.update(pending=False, running=False, aborted=True)
            elif action == "wait":
                job["pending"] = False
            text = json.dumps(
                self.change_override
                if self.change_override is not None
                else {
                    "id": "1",
                    "ready": not job["pending"],
                    "status": "Undone" if job["aborted"] else "Done",
                }
            )
        elif action == "start":
            self.jobs[project]["running"] = True
            text = ""
        elif action in ("stop", "remove"):
            if project in self.jobs:
                assert not self.jobs[project]["pending"], (
                    "active launch cannot be removed"
                )
                self.jobs[project]["running"] = False
            text = ""
        elif action == "remount":
            job = self.jobs[project]
            if job["running"]:
                return subprocess.CompletedProcess(
                    command, 12, "", "Cross-filesystem remount requires a stopped job"
                )
            destination = Path(command[-1])
            if not destination.is_dir():
                return subprocess.CompletedProcess(
                    command, 12, "", "Replacement mount source does not exist"
                )
            job["mounts"][command[4].split("/", 1)[1]] = destination
            text = ""
        else:
            raise AssertionError(action)
        return subprocess.CompletedProcess(command, 0, text, "")


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Iterator[WorkshopService]:
    fake = WorkshopService()
    monkeypatch.setattr(workshop_runner.runtime_guard, "run_command", fake.run)
    remove_tree = shutil.rmtree
    base = Path(tempfile.gettempdir()).resolve()
    try:
        yield fake
    finally:
        # Recovery retention is production behavior, not a license for fixtures
        # to leave their own dummy jobs in the shared host temporary directory.
        for root in {project.parent for project, _ in fake.actions}:
            if root.name.startswith("maas-review-") and root.parent.resolve() == base:
                remove_tree(root, ignore_errors=True)


def completed() -> AgentRunResult:
    return AgentRunResult(
        '{"general_comment": "ok", "inline_comments": []}',
        CompletionStatus.COMPLETED,
        "test-agent",
    )


def test_snapshot_policy_inputs_and_credentials_are_isolated(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "credential").write_text("outside-sensitive-value")
    (review_request.repository_path / "outside").symlink_to(
        outside, target_is_directory=True
    )
    (review_request.repository_path / "broken").symlink_to("missing")
    monkeypatch.setenv("GITHUB_TOKEN", "publication-secret")
    observed: list[Path] = []

    async def agent(process, task, *, deadline):
        root = process.cwd.parent
        observed.append(root)
        layout = json.loads((root / "project" / "layout.json").read_text())
        repository = Path(layout["repository"])
        assert repository != review_request.repository_path
        assert not (repository / "outside").is_symlink()
        assert "Repository symlink" in (repository / "outside").read_text()
        assert not (repository / "outside" / "credential").exists()
        assert not (repository / ".git").exists()
        assert Path(layout["full_diff"]).read_text() == review_request.full_diff
        assert (
            Path(layout["system_instruction"]).read_text()
            == review_request.system_instruction
        )
        assert str(repository) in task.task_prompt
        assert task.full_diff == review_request.full_diff
        assert Path(process.session_cwd).is_dir()
        assert process.cwd != repository
        assert deadline > time.monotonic()
        assert "GITHUB_TOKEN" not in process.env
        spec = json.loads(Path(layout["launch_spec"]).read_text())
        assert "GITHUB_TOKEN" not in spec["environment"]
        assert "test-secret-not-for-files" not in json.dumps(spec)
        models = Path(layout["profile_dir"]) / "models.yml"
        assert "test-secret-not-for-files" not in models.read_text()
        assert "MAAS_REVIEW_SHIM_TOKEN" in models.read_text()
        policy = yaml.safe_load(
            (Path(layout["profile_dir"]) / "policy.yml").read_text()
        )
        assert policy["tools"]["xdev"] is False
        assert policy["autolearn"]["enabled"] is False
        assert policy["modelRoles"]["web"] == "web/duckduckgo"
        (repository / "source.py").write_text("agent mutation")
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    assert WorkshopRunner(config).run(review_request) == completed()
    assert (review_request.repository_path / "source.py").read_text() == "original\n"
    assert not observed[0].exists()
    assert all(not state["running"] for state in service.jobs.values())
    assert [action for _, action in service.actions][-2:] == ["stop", "remove"]


def test_run_logs_timing_for_each_workshop_phase(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    async def agent(process, task, *, deadline):
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    assert WorkshopRunner(config).run(review_request) == completed()
    err = capsys.readouterr().err
    for phase in (
        "timing: start cleanup guardian",
        "timing: stage review payload",
        "timing: create workshop",
        "timing: agent review session",
        "timing: teardown workshop",
    ):
        assert phase in err


@pytest.mark.parametrize("suffix", ["8n5agii_", "_8n5agii", "8n__5agi", "________"])
def test_temporary_directory_separators_do_not_prevent_workshop_startup(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    tmp_path: Path,
    suffix: str,
) -> None:
    root = tmp_path / ("maas-review-" + suffix)
    root.mkdir()
    job = workshop_runner._Job(config, root, time.monotonic() + 30)
    job.start_guard()
    try:
        job.prepare(review_request)
        job.start()
        assert service.jobs[job.project]["running"]
    finally:
        assert not job.cleanup()
    assert not root.exists()


def test_bootstrap_shell_does_not_hold_the_host_project_open(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class BootstrapService(WorkshopService):
        def run(self, command, **kwargs):
            if command[3] != "exec":
                return super().run(command, **kwargs)
            project = Path(command[2])
            arguments = command[command.index("--") + 1 :]
            if len(arguments) == 2 and arguments[1].startswith("/project/"):
                arguments[1] = str(project / arguments[1].removeprefix("/project/"))
            return subprocess.run(
                arguments,
                cwd=kwargs["cwd"],
                env=kwargs["env"],
                timeout=kwargs["timeout"],
                capture_output=True,
                text=True,
            )

    service = BootstrapService()
    monkeypatch.setattr(workshop_runner.runtime_guard, "run_command", service.run)
    probe = tmp_path / "probe.py"
    probe.write_text("""
import json
import os
import sys
from pathlib import Path

project = Path(sys.argv[1])
open_paths = []
for descriptor in Path(f"/proc/{os.getppid()}/fd").iterdir():
    try:
        target = descriptor.resolve(strict=True)
    except FileNotFoundError:
        continue
    if target.is_relative_to(project):
        open_paths.append(str(target))
Path(sys.argv[2]).write_text(json.dumps(open_paths))
sys.exit(32 if open_paths else 0)
""")
    observation = tmp_path / "open-project-descriptors.json"
    root = tmp_path / "job"
    root.mkdir()
    job = workshop_runner._Job(config, root, time.monotonic() + 30)
    job.start_guard()
    try:
        job.prepare(review_request)
        job.bootstrap = (
            shlex.join([sys.executable, str(probe), str(job.project), str(observation)])
            + '\nstatus=$?\nexit "$status"\n'
        )
        (job.project / "bootstrap.sh").write_text(job.bootstrap)
        job.start()
        assert json.loads(observation.read_text()) == []
    finally:
        assert not job.cleanup()


def test_concurrent_reviews_have_independent_lifecycle_and_state(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    roots: list[Path] = []

    async def agent(process, task, *, deadline):
        roots.append(process.cwd.parent)
        barrier.wait(timeout=10)
        assert all(root.exists() for root in roots)
        barrier.wait(timeout=10)
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(WorkshopRunner(config).run, [review_request, review_request])
        )
    assert results == [completed(), completed()]
    assert len(set(roots)) == 2
    assert len({job["name"] for job in service.jobs.values()}) == 2
    assert all(not root.exists() for root in roots)


@pytest.mark.parametrize(
    "failure", [RunnerTransportError("EOF"), RunnerTimeoutError("expired")]
)
def test_agent_failures_always_stop_remove_and_delete_state(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    async def agent(process, task, *, deadline):
        raise failure

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    with pytest.raises(type(failure), match=str(failure)):
        WorkshopRunner(config).run(review_request)
    assert [action for _, action in service.actions][-2:] == ["stop", "remove"]
    assert all(not project.parent.exists() for project in service.jobs)


@pytest.mark.parametrize("action", ["launch", "info", "connect", "exec"])
def test_startup_failures_cleanup_even_when_launch_did_not_complete(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    action: str,
) -> None:
    service.fail.add(action)
    with pytest.raises(RunnerStartupError, match=action) as caught:
        WorkshopRunner(config).run(review_request)
    if action == "launch":
        assert any("uncertain" in note for note in caught.value.__notes__)
        root = service.actions[-1][0].parent
        assert runtime_guard.read_control(root)["launch_change"] is None
        assert root.exists()
    else:
        assert [item for _, item in service.actions][-2:] == ["stop", "remove"]
        assert all(not job["running"] for job in service.jobs.values())
        assert all(not project.parent.exists() for project in service.jobs)
        if action == "connect":
            assert "exec" not in [item for _, item in service.actions]


@pytest.mark.parametrize("action", ["wait", "connect"])
def test_startup_timeout_is_one_total_deadline_and_still_disposes(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    action: str,
) -> None:
    service.pending_launch = True
    service.timeout = action
    with pytest.raises(RunnerTimeoutError):
        WorkshopRunner(config).run(review_request)
    assert "exec" not in [item for _, item in service.actions]
    assert all(not job["running"] for job in service.jobs.values())
    assert all(not project.parent.exists() for project in service.jobs)


@pytest.mark.parametrize(
    "info",
    [
        "[malformed",
        "[]",
        "status: waiting",
    ],
)
def test_nonready_jobs_never_start_agent(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    info: str,
) -> None:
    service.info = info
    with pytest.raises(RunnerStartupError):
        WorkshopRunner(config).run(review_request)
    assert "exec" not in [item for _, item in service.actions]


@pytest.mark.parametrize(
    "connection",
    [
        "bad report",
        "tunnel OTHER/omp:review-inference OTHER/system:tunnel -",
        "ssh-agent JOB/omp:review-inference JOB/system:ssh-agent -",
        "tunnel JOB/omp:extra JOB/system:tunnel -",
        "mount JOB/omp:state JOB/system:mount -",
        "mount JOB/omp:state JOB/other:mount -",
        "tunnel JOB/omp:review-inference - -",
    ],
)
def test_unexpected_authority_is_rejected_before_agent(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    connection: str,
) -> None:
    service.connection = connection
    with pytest.raises(RunnerStartupError):
        WorkshopRunner(config).run(review_request)
    assert "exec" not in [item for _, item in service.actions]


def test_mounts_are_owned_remapped_and_deleted(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.connection = (
        "tunnel JOB/omp:review-inference JOB/system:tunnel -\n"
        "mount JOB/omp:home JOB/system:mount -\n"
        "mount JOB/omp:undeclared JOB/system:mount -"
    )

    async def agent(process, task, *, deadline):
        job = service.jobs[process.cwd.parent / "project"]
        assert job["running"]
        assert set(job["mounts"]) == {"omp:home", "omp:undeclared"}
        for destination in job["mounts"].values():
            assert destination.is_dir()
            assert destination.is_relative_to(process.cwd.parent)
            (destination / "session.json").write_text("disposable session")
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    WorkshopRunner(config).run(review_request)
    assert all(not job["running"] for job in service.jobs.values())
    assert all(not project.parent.exists() for project in service.jobs)


@pytest.mark.parametrize("action", ["stop", "remount", "start"])
@pytest.mark.parametrize("timeout", [False, True])
def test_mount_transition_failure_blocks_agent_and_disposes_job(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    action: str,
    timeout: bool,
) -> None:
    service.connection = (
        "tunnel JOB/omp:review-inference JOB/system:review-inference manual\n"
        "mount JOB/omp:home JOB/system:mount -"
    )
    if timeout:
        service.timeout = action
    else:
        service.fail.add(action)

    expected = RunnerTimeoutError if timeout else RunnerStartupError
    with pytest.raises(expected):
        WorkshopRunner(config).run(review_request)

    assert "exec" not in [item for _, item in service.actions]
    assert all(not job["running"] for job in service.jobs.values())
    assert all(not project.parent.exists() for project in service.jobs)


def test_staged_definition_edits_never_reach_workshop(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = workshop_runner._Job.prepare

    def tampered(self, request):
        original(self, request)
        (self.project / "workshop.yaml").write_text("name: attacker\nsdks: []\n")

    monkeypatch.setattr(workshop_runner._Job, "prepare", tampered)
    with pytest.raises(RunnerConfigurationError, match="definition changed"):
        WorkshopRunner(config).run(review_request)
    assert not service.jobs


@pytest.mark.parametrize(
    "argv",
    [
        ("/agent", "{model}"),
        ("/agent", "{system_instruction}"),
        ("/agent", "{model}", "{system_instruction}", "{unknown}"),
    ],
)
def test_harness_policy_cannot_omit_model_or_system_instruction(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(RunnerConfigurationError):
        WorkshopRunner(replace(config, harness_argv=argv)).run(review_request)
    assert not service.jobs


def test_missing_shim_bearer_blocks_nonempty_review(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_REVIEW_BEARER")
    with pytest.raises(RunnerConfigurationError, match="empty"):
        WorkshopRunner(config).run(review_request)


def test_unsupported_special_files_and_expired_snapshot_are_rejected(
    review_request: ReviewRequest,
    tmp_path: Path,
) -> None:
    os.mkfifo(review_request.repository_path / "pipe")
    with pytest.raises(RunnerConfigurationError, match="special"):
        workshop_runner._snapshot(
            review_request.repository_path, tmp_path / "copy", time.monotonic() + 20
        )
    with pytest.raises(RunnerTimeoutError):
        workshop_runner._remaining(time.monotonic() - 1)


def test_runtime_launch_uses_only_explicit_environment_and_secret_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "clean"
    cwd.mkdir()
    spec = tmp_path / "launch.json"
    spec.write_text(
        json.dumps(
            {
                "environment": {"HOME": str(cwd)},
                "inherit": ["INFERENCE"],
                "argv": ["/agent"],
                "cwd": str(cwd),
            }
        )
    )
    monkeypatch.setenv("INFERENCE", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "publication")
    executed = []
    monkeypatch.setattr(
        runtime_launch.os,
        "execve",
        lambda executable, argv, env: executed.append((Path.cwd(), env)),
    )
    monkeypatch.chdir(tmp_path)
    runtime_launch.launch(spec)
    assert executed == [(cwd, {"HOME": str(cwd), "INFERENCE": "secret"})]
    monkeypatch.delenv("INFERENCE")
    with pytest.raises(RuntimeError, match="empty"):
        runtime_launch.launch(spec)


def test_guardian_pipe_eof_disposes_after_owner_death(
    tmp_path: Path,
    remote_service: tuple[Path, Path],
) -> None:
    root = tmp_path / "job"
    (root / "project").mkdir(parents=True)
    control(
        root,
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Done",
    )
    executable, record = remote_service
    state = json.loads(record.read_text())
    state["running"] = True
    record.write_text(json.dumps(state))
    reader, writer = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            runtime_guard.__file__,
            str(reader),
            str(root),
            str(executable),
            "job",
        ],
        pass_fds=(reader,),
    )
    os.close(reader)
    os.close(writer)
    assert process.wait(timeout=10) == 0
    state = json.loads(record.read_text())
    assert state["actions"] == ["stop", "remove"]
    assert state["removed"] and not state["running"]
    assert not root.exists()


def test_guardian_disarm_does_not_delete_live_job(tmp_path: Path) -> None:
    reader, writer = os.pipe()
    os.write(writer, b"D")
    os.close(writer)
    runtime_guard.guard(reader, tmp_path, "/not/executed", "job")
    assert tmp_path.exists()


@pytest.mark.parametrize("primary_failure", [True, False])
def test_teardown_failure_preserves_primary_and_never_returns_success(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
    primary_failure: bool,
) -> None:
    service.fail.update(("stop", "remove"))
    failure = RunnerTransportError("primary EOF")

    async def agent(process, task, *, deadline):
        if primary_failure:
            raise failure
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    with pytest.raises(
        RunnerTransportError if primary_failure else RunnerStartupError
    ) as caught:
        WorkshopRunner(config).run(review_request)
    if primary_failure:
        assert caught.value is failure
        assert any("cleanup failed" in note for note in caught.value.__notes__)
    else:
        assert "cleanup failed" in str(caught.value)
    for project in service.jobs:
        assert project.parent.exists()
        state = runtime_guard.read_control(project.parent)
        assert state["name"] == service.jobs[project]["name"]
        assert state["project"] == str(project)
        assert state["launch_change"] == "1"
        assert state["removed"] is False


def test_generated_definition_ignores_any_repository_definition(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged: list[dict] = []

    async def agent(process, task, *, deadline):
        project = process.cwd.parent / "project"
        staged.append(yaml.safe_load((project / "workshop.yaml").read_text()))
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    WorkshopRunner(replace(config, omp_channel="18/stable")).run(review_request)

    definition = staged[0]
    assert definition["name"].startswith("review-")
    assert definition["base"] == "ubuntu@26.04"
    assert "actions" not in definition
    assert definition["sdks"] == [
        {
            "name": "omp",
            "channel": "18/stable",
            "plugs": {
                "review-inference": {
                    "interface": "tunnel",
                    "endpoint": "localhost:4000",
                }
            },
        },
        {
            "name": "system",
            "slots": {
                "review-inference": {
                    "interface": "tunnel",
                    "endpoint": "localhost:4000",
                }
            },
        },
    ]


def test_container_path_cannot_overlap_mounted_host_control(
    config: RuntimeConfig,
    review_request: ReviewRequest,
) -> None:
    config = replace(
        config, paths=replace(config.paths, repository=PurePosixPath("/project/source"))
    )
    with pytest.raises(RunnerConfigurationError, match="outside /project"):
        WorkshopRunner(config).run(review_request)


def test_shim_rejects_providerless_model_even_when_allowed(
    config: RuntimeConfig,
    review_request: ReviewRequest,
) -> None:
    config = replace(config, allowed_models=("model",), default_model="model")
    with pytest.raises(RunnerConfigurationError, match="provider/model"):
        WorkshopRunner(config).run(replace(review_request, model="model"))


def test_snapshot_does_not_follow_root_symlink(
    review_request: ReviewRequest, tmp_path: Path
) -> None:
    link = tmp_path / "linked-repository"
    link.symlink_to(review_request.repository_path, target_is_directory=True)
    with pytest.raises(OSError):
        workshop_runner._snapshot(link, tmp_path / "copy", time.monotonic() + 10)


def test_host_io_failure_is_typed_and_removes_disposable_state(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
) -> None:
    with pytest.raises(RunnerStartupError, match="prepare or execute"):
        WorkshopRunner(config).run(
            replace(review_request, repository_path=Path("/missing/source"))
        )
    assert not service.jobs


@pytest.mark.parametrize(
    "resource",
    ["configuration_path", "profile_directory", "workshop_executable"],
)
def test_reviewed_checkout_cannot_supply_executable_runtime_policy(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    resource: str,
) -> None:
    selected = (
        review_request.repository_path
        if resource.endswith("directory")
        else review_request.repository_path / "workshop.yaml"
    )
    with pytest.raises(RunnerConfigurationError, match="outside the reviewed"):
        WorkshopRunner(replace(config, **{resource: selected})).run(review_request)
    assert not service.jobs


def test_explicit_config_and_symlink_targets_inside_review_are_rejected_before_loading(
    review_request: ReviewRequest,
    tmp_path: Path,
) -> None:
    malicious = review_request.repository_path / "runtime.toml"
    malicious.write_text("invalid PR-controlled configuration")
    link = tmp_path / "operator.toml"
    link.symlink_to(malicious)
    with pytest.raises(RunnerConfigurationError, match="outside the reviewed"):
        ConfiguredAgentRunner(link).run(review_request)


def test_policy_symlink_loop_is_a_configuration_error(
    review_request: ReviewRequest, tmp_path: Path
) -> None:
    link = tmp_path / "loop"
    link.symlink_to(link.name)
    with pytest.raises(RunnerConfigurationError, match="resolve trusted"):
        ConfiguredAgentRunner(link).run(review_request)


def test_lifecycle_command_timeout_kills_its_owned_host_process_group(
    tmp_path: Path,
) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(120)"]
    with pytest.raises(subprocess.TimeoutExpired):
        runtime_guard.run_command(
            command, cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, timeout=0.1
        )
    result = runtime_guard.run_command(
        [sys.executable, "-c", "print('ready')"], cwd=tmp_path, env={}, timeout=10
    )
    assert result.returncode == 0
    assert result.stdout == "ready\n"


def test_guardian_reports_failed_cleanup_and_retains_state(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "job"
    root.mkdir()
    control(
        root,
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Done",
    )
    reader, writer = os.pipe()
    os.close(writer)
    assert runtime_guard.guard(reader, root, "/missing/workshop", "job") == 1
    assert root.exists()
    assert "retained recovery state for job" in capsys.readouterr().err


def test_pinned_production_profile_does_not_inherit_test_or_publication_bearers(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unix_daemon,
) -> None:
    container = tmp_path / "private-container"
    paths = ContainerPaths(
        *map(
            PurePosixPath,
            (
                str(container / "repository"),
                str(container / "full.diff"),
                str(container / "system.txt"),
                str(container / "cwd"),
                str(container / "state"),
            ),
        )
    )
    config = replace(
        config,
        paths=paths,
        test_shim=None,
        workshop_socket=unix_daemon[0],
    )

    async def agent(process, task, *, deadline):
        layout = json.loads(
            (process.cwd.parent / "project" / "layout.json").read_text()
        )
        spec = json.loads(Path(layout["launch_spec"]).read_text())
        assert spec["inherit"] == []
        assert "MAAS_REVIEW_SHIM_TOKEN" not in process.env
        assert process.env["WORKSHOP_SOCKET"] == str(unix_daemon[0])
        assert "WORKSHOP_SOCKET" not in spec["environment"]
        assert not (Path(layout["profile_dir"]) / "models.yml").exists()
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    assert WorkshopRunner(config).run(review_request) == completed()


def test_cli_exec_spawn_failure_is_typed_and_retains_recovery_state(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(command, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(runtime_guard, "run_command", unavailable)
    with pytest.raises(RunnerStartupError, match="configured Workshop CLI") as failure:
        WorkshopRunner(config).run(review_request)

    # Launch is the first CLI call, so its outcome is unknown and host recovery
    # control must survive rather than be deleted as if disposal were confirmed.
    retained = {
        Path(part.rpartition(" at ")[2])
        for note in failure.value.__notes__
        for part in note.split("; ")
        if "recovery state retained" in part
    }
    assert len(retained) == 1
    control = retained.pop()
    assert control.is_file()
    shutil.rmtree(control.parent)


def test_packaged_launcher_and_guardian_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specification = tmp_path / "launch.json"
    specification.write_text(
        json.dumps(
            {
                "environment": {"HOME": str(tmp_path)},
                "inherit": [],
                "argv": ["/agent"],
                "cwd": str(tmp_path),
            }
        )
    )
    executed = []
    monkeypatch.setattr(
        os, "execve", lambda executable, argv, env: executed.append(env)
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [runtime_launch.__file__, str(specification)])
    runpy.run_path(runtime_launch.__file__, run_name="__main__")
    assert executed == [{"HOME": str(tmp_path)}]
    reader, writer = os.pipe()
    os.write(writer, b"D")
    os.close(writer)
    monkeypatch.setattr(
        sys,
        "argv",
        [runtime_guard.__file__, str(reader), str(tmp_path), "/not/executed", "job"],
    )
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(runtime_guard.__file__, run_name="__main__")
    assert caught.value.code == 0
    assert tmp_path.exists()


def test_pr_owned_symlink_cannot_select_an_external_runtime_file(
    review_request: ReviewRequest,
    tmp_path: Path,
) -> None:
    target = tmp_path / "trusted.toml"
    target.write_text("operator policy")
    selected = review_request.repository_path / "runtime.toml"
    selected.symlink_to(target)
    with pytest.raises(RunnerConfigurationError, match="outside the reviewed"):
        ConfiguredAgentRunner(selected).run(review_request)


def test_allocation_failure_is_a_startup_error(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def full_disk(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(workshop_runner.tempfile, "mkdtemp", full_disk)
    with pytest.raises(RunnerStartupError, match="allocate"):
        WorkshopRunner(config).run(review_request)


@pytest.mark.parametrize("failure", ["pipe", "spawn"])
def test_guardian_setup_failure_deletes_its_owned_root(
    config: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    root = tmp_path / "owned-state"
    root.mkdir()
    job = workshop_runner._Job(config, root, time.monotonic() + 10)

    def unavailable(*args, **kwargs):
        raise OSError("unavailable")

    if failure == "pipe":
        monkeypatch.setattr(workshop_runner.os, "pipe", unavailable)
    else:
        monkeypatch.setattr(workshop_runner.subprocess, "Popen", unavailable)
    with pytest.raises(RunnerStartupError, match="independent review cleanup"):
        job.start_guard()
    assert not root.exists()


def test_unexpected_guardian_eof_is_not_a_clean_success(
    config: RuntimeConfig, tmp_path: Path
) -> None:
    root = tmp_path / "owned-state"
    root.mkdir()
    job = workshop_runner._Job(config, root, time.monotonic() + 10)
    reader, writer = os.pipe()
    os.close(reader)
    job.lease = writer
    assert "independent cleanup guardian exited unexpectedly" in job.cleanup()
    assert not root.exists()


@pytest.mark.parametrize("unkillable", [False, True])
def test_guardian_teardown_has_a_bounded_escalation_and_reports_failure(
    config: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unkillable: bool,
) -> None:
    class StalledGuardian:
        def __init__(self):
            self.killed = False

        def wait(self, timeout):
            if not self.killed or unkillable:
                raise subprocess.TimeoutExpired("guard", timeout)
            return 0

        def kill(self):
            self.killed = True

    root = tmp_path / "owned-state"
    root.mkdir()
    job = workshop_runner._Job(config, root, time.monotonic() + 10)
    guardian = StalledGuardian()
    monkeypatch.setattr(job, "guardian", guardian)
    assert "independent cleanup guardian did not settle" in job.cleanup()
    assert guardian.killed


def test_host_state_delete_failure_activates_guardian_and_prevents_success(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_rmtree = workshop_runner.shutil.rmtree

    def unavailable(path, *args, **kwargs):
        raise PermissionError("state deletion denied")

    async def agent(process, task, *, deadline):
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    monkeypatch.setattr(workshop_runner.shutil, "rmtree", unavailable)
    try:
        with pytest.raises(RunnerStartupError, match="host state"):
            WorkshopRunner(config).run(review_request)
        assert all(not project.parent.exists() for project in service.jobs)
    finally:
        monkeypatch.setattr(workshop_runner.shutil, "rmtree", actual_rmtree)


def test_guardian_reports_nonzero_remote_cleanup_and_handles_already_absent_state(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "owned-state"
    root.mkdir()
    control(
        root,
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Done",
    )
    reader, writer = os.pipe()
    os.close(writer)
    assert runtime_guard.guard(reader, root, "/bin/false", "job") == 1
    assert "Workshop remove did not complete" in capsys.readouterr().err
    assert root.exists()
    reader, writer = os.pipe()
    os.close(writer)
    runtime_guard.guard(reader, root, "/bin/true", "job")
    assert not root.exists()


def test_guardian_reports_host_state_deletion_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    root = tmp_path / "owned-state"
    root.mkdir()
    control(root)

    def denied(path):
        raise PermissionError("denied")

    monkeypatch.setattr(runtime_guard.shutil, "rmtree", denied)
    reader, writer = os.pipe()
    os.close(writer)
    runtime_guard.guard(reader, root, "/bin/true", "job")
    assert root.exists()
    assert "could not delete or recover host state" in capsys.readouterr().err


def test_file_type_change_during_copy_is_contained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "regular").write_text("source")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    monkeypatch.setattr(workshop_runner.os, "fstat", lambda descriptor: fifo.stat())
    with pytest.raises(RunnerConfigurationError, match="changed during snapshot"):
        workshop_runner._snapshot(source, tmp_path / "snapshot", time.monotonic() + 10)


def test_process_exit_race_does_not_mask_lifecycle_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_group = runtime_guard.os.killpg

    def exited(pid, signum):
        kill_group(pid, signum)
        raise ProcessLookupError

    monkeypatch.setattr(runtime_guard.os, "killpg", exited)
    with pytest.raises(subprocess.TimeoutExpired):
        runtime_guard._execute_command(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=tmp_path,
            env={},
            timeout=0.1,
        )


def test_source_shaped_unused_slots_grant_no_authority(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.connection = (
        "tunnel JOB/omp:review-inference JOB/system:tunnel -\n"
        "ssh-agent - JOB/unused:ssh-agent -\n"
        "mount - JOB/unused:mount -\n"
        "tunnel JOB/unused:tunnel - -\n"
    )

    async def agent(process, task, *, deadline):
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    assert WorkshopRunner(config).run(review_request) == completed()


def test_container_stop_can_outlast_the_short_change_request_grace(
    tmp_path: Path, remote_service: tuple[Path, Path]
) -> None:
    # Keep production grace periods: subprocess startup alone can exceed a
    # 100 ms test-only removal deadline on a busy host.
    executable, record = remote_service
    record.write_text(
        json.dumps(
            {
                "pending": False,
                "running": True,
                "actions": [],
                "stop_delay": runtime_guard.CLEANUP_SECONDS + 0.3,
                "require_stopped": True,
            }
        )
    )
    root = tmp_path / "slow-stop"
    root.mkdir()
    control(
        root,
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Done",
    )
    assert runtime_guard.cleanup_remote(root, str(executable), "job", {}) == []
    remote = json.loads(record.read_text())
    assert remote["removed"] and not remote["running"]
    assert runtime_guard.read_control(root)["removed"]


@pytest.mark.parametrize(
    "scenario",
    [
        "pending",
        "none",
        "abort-race",
        "bad-list",
        "overlapping",
        "foreign",
        "other-kind",
        "bad-id",
        "missing-project",
        "unsettled",
    ],
)
def test_remote_disposal_settles_only_its_owned_exec_changes(
    tmp_path: Path, remote_service: tuple[Path, Path], unix_daemon, scenario: str
) -> None:
    executable, record = remote_service
    record.write_text(
        json.dumps(
            {
                "pending": False,
                "running": True,
                "pending_exec": scenario != "none",
                "actions": [],
            }
        )
    )
    socket_path, daemon = unix_daemon
    daemon["exec_record"] = record
    if scenario == "bad-list":
        daemon["pending_changes"] = {}
    elif scenario == "overlapping":
        daemon["pending_changes"] = [
            {"id": identity, "kind": "exec", "project-id": "owned"}
            for identity in ("2", "3")
        ]
    elif scenario in ("foreign", "other-kind", "bad-id"):
        pending = {"id": "2", "kind": "exec", "project-id": "owned"}
        pending[
            {"foreign": "project-id", "other-kind": "kind", "bad-id": "id"}[scenario]
        ] = "unexpected"
        daemon["pending_changes"] = [pending]
    elif scenario == "missing-project":
        daemon["reply"]["result"].pop("project-id")
    elif scenario == "unsettled":
        daemon["unsettled"] = True
    elif scenario == "abort-race":
        daemon["abort_race"] = True
    root = tmp_path / "owned-job"
    root.mkdir()
    control(
        root,
        shim=False,
        daemon_socket=str(socket_path),
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Done",
    )
    errors = runtime_guard.cleanup_remote(root, str(executable), "job", {})
    remote = json.loads(record.read_text())
    if scenario in ("pending", "none", "abort-race"):
        assert errors == []
        assert remote["removed"] and not remote["pending_exec"]
        assert runtime_guard.read_control(root)["removed"]
    else:
        assert errors
        assert not remote.get("removed", False)
        assert not runtime_guard.read_control(root)["removed"]
        assert root.exists()


@pytest.mark.parametrize("reply", ["1", "not-a-change-id"])
def test_timed_out_launch_retains_only_an_observed_change_id(
    tmp_path: Path,
    remote_service: tuple[Path, Path],
    reply: str,
) -> None:
    executable, record = remote_service
    state = json.loads(record.read_text()) | {
        "launch_reply": reply,
        "post_response_delay": 30,
    }
    record.write_text(json.dumps(state))
    root = tmp_path / "timed-out-launch"
    root.mkdir()
    control(root, launch_attempted=True)
    runtime_guard.supervise(
        [str(executable), "-p", str(root / "project"), "launch", "--no-wait"],
        0.5,
        -1,
        root,
    )
    if reply == "1":
        assert runtime_guard.read_control(root)["launch_change"] == "1"
        assert runtime_guard.cleanup_remote(root, str(executable), "job", {}) == []
        state = json.loads(record.read_text())
        assert state["aborted"] and not state["running"]
        assert runtime_guard.read_control(root)["removed"]
    else:
        assert runtime_guard.read_control(root)["launch_change"] is None
        assert runtime_guard.cleanup_remote(root, str(executable), "job", {})
        assert root.exists()


def test_pending_launch_timeout_aborts_before_remote_disposal(
    tmp_path: Path,
    remote_service: tuple[Path, Path],
) -> None:
    executable, record = remote_service
    root = tmp_path / "pending"
    root.mkdir()
    control(root, launch_attempted=True)
    runtime_guard.run_command(
        [str(executable), "-p", str(root / "project"), "launch", "--no-wait", "job"],
        cwd=root,
        env={},
        timeout=2,
        root=root,
    )
    assert runtime_guard.read_control(root)["launch_change"] == "1"
    state = json.loads(record.read_text()) | {"stall": True}
    record.write_text(json.dumps(state))
    with pytest.raises(subprocess.TimeoutExpired):
        runtime_guard.change_request(
            root, str(executable), "wait", "1", env={}, timeout=0.1
        )
    assert json.loads(record.read_text())["pending"]
    assert runtime_guard.cleanup_remote(root, str(executable), "job", {}) == []
    state = json.loads(record.read_text())
    assert state["aborted"] and not state["running"]
    assert state["actions"] == ["launch", "wait", "abort", "wait", "info"]
    assert runtime_guard.read_control(root)["removed"]


@pytest.mark.parametrize("response_delay", [0.0, 30.0])
def test_killed_owner_pending_launch_settles_within_cleanup_grace(
    tmp_path: Path,
    remote_service: tuple[Path, Path],
    response_delay: float,
) -> None:
    executable, record = remote_service
    state = json.loads(record.read_text()) | {
        "response_delay": response_delay,
        "stall": True,
    }
    record.write_text(json.dumps(state))
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    control(root, launch_attempted=True)
    report = tmp_path / "guardian-status"
    guard_source = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(runtime_guard.__file__).parents[1])!r})
from maas_code_reviewer.runtime_guard import guard
status = guard(int(sys.argv[1]), Path({str(root)!r}), {str(executable)!r}, "job")
Path({str(report)!r}).write_text(str(status))
sys.exit(status)
"""
    owner_source = f"""
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, {str(Path(runtime_guard.__file__).parents[1])!r})
from maas_code_reviewer import runtime_guard
root = Path({str(root)!r})
reader, writer = os.pipe()
guard = subprocess.Popen([sys.executable, "-c", {guard_source!r}, str(reader)],
                         pass_fds=(reader,), env={{}})
os.close(reader)
print(guard.pid, flush=True)
runtime_guard.run_command([{str(executable)!r}, "-p", str(root / "project"),
                           "launch", "--no-wait", "job"],
                          cwd=root, env={{}}, timeout=300, root=root)
runtime_guard.change_request(root, {str(executable)!r}, "wait", "1",
                             env={{}}, timeout=300)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_source],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert owner.stdout is not None
    guardian = int(owner.stdout.readline())
    end = time.monotonic() + 5
    while True:
        state = json.loads(record.read_text())
        if state["pending"] and (response_delay or "wait_pid" in state):
            break
        assert time.monotonic() < end
        time.sleep(0.01)
    cli_pid = state["cli_pid"] if response_delay else state["wait_pid"]
    started = time.monotonic()
    owner.kill()
    assert owner.wait(timeout=5) != 0
    owner.stdout.close()
    while not report.exists():
        assert time.monotonic() - started < 5, (
            f"guardian {guardian} exceeded cleanup grace"
        )
        time.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(cli_pid, 0)
    state = json.loads(record.read_text())
    if response_delay:
        assert report.read_text() == "1"
        assert (
            root.exists() and runtime_guard.read_control(root)["launch_change"] is None
        )
        assert state["pending"] and "abort" not in state["actions"]
    else:
        assert report.read_text() == "0" and not root.exists()
        assert state["aborted"] and not state["pending"] and not state["running"]
        assert "abort" in state["actions"]


def test_lost_launch_response_preserves_recovery_without_claiming_absence(
    tmp_path: Path,
    remote_service: tuple[Path, Path],
    capsys,
) -> None:
    executable, record = remote_service
    state = json.loads(record.read_text()) | {"response_delay": 1}
    record.write_text(json.dumps(state))
    root = tmp_path / "uncertain"
    root.mkdir(mode=0o700)
    control(root, launch_attempted=True)
    with pytest.raises(subprocess.TimeoutExpired):
        runtime_guard.run_command(
            [
                str(executable),
                "-p",
                str(root / "project"),
                "launch",
                "--no-wait",
                "job",
            ],
            cwd=root,
            env={},
            timeout=0.1,
            root=root,
        )
    reader, writer = os.pipe()
    os.close(writer)
    assert runtime_guard.guard(reader, root, str(executable), "job") == 1
    assert root.exists()
    assert runtime_guard.read_control(root)["launch_change"] is None
    assert json.loads(record.read_text())["pending"]
    diagnostic = capsys.readouterr().err
    assert "uncertain" in diagnostic and str(root / runtime_guard.CONTROL) in diagnostic


def test_real_daemon_change_wait_and_abort_use_supported_management_api(
    tmp_path: Path,
    unix_daemon,
) -> None:
    path, state = unix_daemon
    control(tmp_path, shim=False, daemon_socket=str(path))
    ready = runtime_guard.change_request(
        tmp_path, "/not-used", "wait", "1", env={}, timeout=2
    )
    assert ready["ready"] and ready["status"] == "Done"
    state["reply"]["result"] = {"id": "1", "ready": False, "status": "Undoing"}
    aborting = runtime_guard.change_request(
        tmp_path, "/not-used", "abort", "1", env={}, timeout=2
    )
    assert not aborting["ready"]
    assert state["requests"] == [
        ("GET", "/v1/changes/1/wait?timeout=2s", b""),
        ("POST", "/v1/changes/1", b'{"action":"abort"}'),
    ]


@pytest.mark.parametrize(
    "status,reply,error",
    [
        (
            504,
            {"type": "error", "result": {"message": "secret"}},
            subprocess.TimeoutExpired,
        ),
        (400, {"type": "error", "result": {"message": "secret"}}, OSError),
        (200, [], OSError),
        (200, {"type": "sync", "result": []}, ValueError),
        (
            200,
            {
                "type": "sync",
                "result": {"id": "other", "ready": True, "status": "Done"},
            },
            ValueError,
        ),
        (
            200,
            {"type": "sync", "result": {"id": "1", "ready": "yes", "status": "Done"}},
            ValueError,
        ),
        (
            200,
            {"type": "sync", "result": {"id": "1", "ready": True, "status": 1}},
            ValueError,
        ),
    ],
)
def test_management_api_errors_are_typed_without_copying_daemon_diagnostics(
    tmp_path: Path,
    unix_daemon,
    status: int,
    reply,
    error,
) -> None:
    path, state = unix_daemon
    state.update(status=status, reply=reply)
    control(tmp_path, shim=False, daemon_socket=str(path))
    with pytest.raises(error) as caught:
        runtime_guard.change_request(
            tmp_path, "/not-used", "wait", "1", env={}, timeout=2
        )
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "details",
    [
        {"id": "1", "ready": False, "status": "Doing"},
        {"id": "1", "ready": True, "status": "Undone"},
        {"id": "1", "ready": True, "status": "Done", "err": "provider secret"},
        [],
    ],
)
def test_unsettled_aborted_or_failed_launch_never_starts_an_agent(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    details,
) -> None:
    service.change_override = details
    with pytest.raises(RunnerStartupError) as caught:
        WorkshopRunner(config).run(review_request)
    assert "provider secret" not in str(caught.value)
    assert "exec" not in [action for _, action in service.actions]


def test_configured_override_changes_actual_harness_model_and_deadline(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = tmp_path / "operator.toml"
    policy.write_text(f"""
review_timeout_seconds = 30
[workshop]
executable = {json.dumps(str(config.workshop_executable))}
[harness]
argv = {json.dumps(list(config.harness_argv))}
[paths]
repository = "/review/repository"
full_diff = "/review/full.diff"
system_instruction = "/review/system.txt"
cwd = "/review/cwd"
agent_dir = "/review/state"
[model]
default = "provider/model"
allowed = ["provider/model", "provider/other"]
[test_shim]
omp_executable = {json.dumps(str(config.workshop_executable))}
gateway_url = "http://localhost:4000"
token_env = "TEST_REVIEW_BEARER"
""")

    async def agent(process, task, *, deadline):
        layout = json.loads(
            (process.cwd.parent / "project" / "layout.json").read_text()
        )
        specification = json.loads(Path(layout["launch_spec"]).read_text())
        assert "--model=provider/other" in specification["argv"]
        assert task.model == "provider/other"
        assert 0 < deadline - time.monotonic() <= 4
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    assert (
        ConfiguredAgentRunner(policy, model="provider/other", timeout_seconds=4).run(
            review_request,
        )
        == completed()
    )


def test_supervisor_entrypoint_and_owned_lock_persist_launch_identity(
    tmp_path: Path,
    remote_service: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    executable, record = remote_service
    root = tmp_path / "entrypoint"
    root.mkdir()
    control(root, launch_attempted=True)
    command = [
        str(executable),
        "-p",
        str(root / "project"),
        "launch",
        "--no-wait",
        "job",
    ]
    descriptor = os.open(root / "command.lock", os.O_CREAT | os.O_RDWR, 0o600)
    reader, writer = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            runtime_guard.__file__,
            "--command",
            "2",
            str(descriptor),
            str(root),
            str(reader),
            json.dumps(command),
        ],
    )
    try:
        runpy.run_path(runtime_guard.__file__, run_name="__main__")
    finally:
        os.close(writer)
    result = json.loads(capsys.readouterr().out)
    assert result == {"returncode": 0, "stdout": "1\n"}
    assert runtime_guard.read_control(root)["launch_change"] == "1"
    assert json.loads(record.read_text())["pending"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            runtime_guard.__file__,
            "--command",
            "2",
            "-1",
            "",
            "-1",
            json.dumps([sys.executable, "-c", "print('ready')"]),
        ],
    )
    runpy.run_path(runtime_guard.__file__, run_name="__main__")
    assert json.loads(capsys.readouterr().out)["stdout"] == "ready\n"


@pytest.mark.parametrize("kind", ["timeout", "spawn", "invalid-id"])
def test_independent_supervisor_reports_failures_without_discarding_ownership(
    tmp_path: Path,
    kind: str,
) -> None:
    control(tmp_path, launch_attempted=True)
    if kind == "timeout":
        command = [sys.executable, "-c", "import time; time.sleep(120)"]
        expected = {"timeout": True}
    elif kind == "spawn":
        command = ["/missing/workshop"]
        expected = {"error": True}
    else:
        executable = tmp_path / "malformed"
        executable.write_text(f"#!{sys.executable}\nprint('not-a-change')\n")
        executable.chmod(0o700)
        command = [str(executable), "-p", str(tmp_path), "launch"]
        expected = {"error": True}
    descriptor = os.open(tmp_path / "command.lock", os.O_CREAT | os.O_RDWR, 0o600)
    assert (
        runtime_guard.supervise(
            command, 0.1, descriptor, tmp_path if kind == "invalid-id" else None
        )
        == expected
    )
    assert runtime_guard.read_control(tmp_path)["launch_change"] is None


def test_remote_removal_failure_keeps_guardian_recovery_control(
    tmp_path: Path,
    remote_service: tuple[Path, Path],
    capsys,
) -> None:
    executable, record = remote_service
    root = tmp_path / "failed-removal"
    root.mkdir(mode=0o700)
    control(
        root,
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Done",
    )
    state = json.loads(record.read_text()) | {"running": True, "fail_remove": True}
    record.write_text(json.dumps(state))
    reader, writer = os.pipe()
    os.close(writer)
    assert runtime_guard.guard(reader, root, str(executable), "job") == 1
    assert root.exists() and runtime_guard.read_control(root)["removed"] is False
    assert not json.loads(record.read_text())["running"]
    assert str(root / runtime_guard.CONTROL) in capsys.readouterr().err


def test_missing_or_corrupt_recovery_metadata_is_not_proof_of_remote_removal(
    tmp_path: Path,
    capsys,
    config: RuntimeConfig,
) -> None:
    root = tmp_path / "unknown"
    root.mkdir()
    reader, writer = os.pipe()
    os.close(writer)
    assert runtime_guard.guard(reader, root, "/not-used", "job") == 1
    assert root.exists()
    assert "uncertain recovery state" in capsys.readouterr().err
    (root / runtime_guard.CONTROL).write_text("not-json")
    job = workshop_runner._Job(config, root, time.monotonic() + 10)
    assert any("cleanup recovery failed" in error for error in job.cleanup())
    assert root.exists()
    reader, writer = os.pipe()
    os.close(writer)
    assert runtime_guard.guard(reader, root, "/not-used", "job") == 1
    assert "could not delete or recover" in capsys.readouterr().err
    root = tmp_path / "already-absent"
    reader, writer = os.pipe()
    os.close(writer)
    assert runtime_guard.guard(reader, root, "/not-used", "job") == 0


def test_lost_launch_response_keeps_normal_cleanup_uncertain(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
) -> None:
    service.timeout = "launch"
    with pytest.raises(RunnerTimeoutError) as caught:
        WorkshopRunner(config).run(review_request)
    assert any("uncertain" in note for note in caught.value.__notes__)
    root = service.actions[-1][0].parent
    assert root.exists() and runtime_guard.read_control(root)["launch_change"] is None


def test_failed_change_wait_preserves_remote_job_and_recovery_control(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
) -> None:
    service.pending_launch = True
    service.fail.add("wait")
    with pytest.raises(RunnerStartupError, match="settle"):
        WorkshopRunner(config).run(review_request)
    root = next(iter(service.jobs)).parent
    assert root.exists()
    assert runtime_guard.read_control(root)["removed"] is False
    assert "remove" not in [action for _, action in service.actions]


def test_abort_rejection_does_not_hide_authoritative_settled_launch(
    tmp_path: Path,
    service: WorkshopService,
) -> None:
    root = tmp_path / "settled"
    root.mkdir()
    control(root, launch_attempted=True, launch_change="1")
    service.jobs[root / "project"] = {
        "pending": False,
        "running": True,
        "aborted": False,
    }
    service.fail.add("abort")
    assert runtime_guard.cleanup_remote(root, "/not-used", "job", {}) == []
    assert runtime_guard.read_control(root)["removed"]
    assert [action for _, action in service.actions] == [
        "abort",
        "wait",
        "stop",
        "remove",
    ]


def test_failed_launch_absence_probe_failure_still_requires_remote_removal(
    tmp_path: Path,
    service: WorkshopService,
) -> None:
    root = tmp_path / "probe"
    root.mkdir()
    control(
        root,
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Undone",
    )
    service.jobs[root / "project"] = {
        "pending": False,
        "running": True,
        "aborted": False,
    }
    service.timeout = "info"
    assert runtime_guard.cleanup_remote(root, "/not-used", "job", {}) == []
    assert runtime_guard.read_control(root)["removed"]
    assert [action for _, action in service.actions] == ["info", "stop", "remove"]


def test_guardian_stop_failure_is_reported_even_after_confirmed_removal(
    tmp_path: Path,
    service: WorkshopService,
    capsys,
) -> None:
    root = tmp_path / "stop-failure"
    root.mkdir()
    control(
        root,
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Done",
    )
    service.jobs[root / "project"] = {
        "pending": False,
        "running": True,
        "aborted": False,
    }
    service.fail.add("stop")
    reader, writer = os.pipe()
    os.close(writer)
    assert runtime_guard.guard(reader, root, "/not-used", "job") == 1
    assert not root.exists()
    assert "Workshop stop did not complete" in capsys.readouterr().err


def test_supervisor_crash_fails_closed_instead_of_using_partial_output(
    tmp_path: Path,
) -> None:
    with pytest.raises(OSError, match="supervisor did not return"):
        runtime_guard.run_command(
            [
                sys.executable,
                "-c",
                "import os, signal; os.kill(os.getppid(), signal.SIGKILL)",
            ],
            cwd=tmp_path,
            env={},
            timeout=2,
        )


def test_real_management_socket_cannot_be_selected_by_reviewed_checkout(
    config: RuntimeConfig,
    review_request: ReviewRequest,
) -> None:
    socket = review_request.repository_path / "management.socket"
    socket.touch()
    with pytest.raises(RunnerConfigurationError, match="outside the reviewed"):
        WorkshopRunner(replace(config, test_shim=None, workshop_socket=socket)).run(
            review_request
        )


def test_exec_uses_selected_daemon_without_forwarding_socket(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    tmp_path: Path,
    unix_daemon,
) -> None:
    path, state = unix_daemon
    executable = tmp_path / "socket-aware-workshop"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import http.client
import os
import socket
import sys
if sys.argv[3] != "exec":
    sys.exit(2)
transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
transport.settimeout(2)
transport.connect(os.environ["WORKSHOP_SOCKET"])
connection = http.client.HTTPConnection("localhost", timeout=2)
connection.sock = transport
connection.request("GET", "/host-exec-transport")
response = connection.getresponse()
print(response.read().decode())
connection.close()
"""
    )
    executable.chmod(0o700)
    config = replace(
        config, test_shim=None, workshop_executable=executable, workshop_socket=path
    )
    root = tmp_path / "host-transport"
    root.mkdir()
    job = workshop_runner._Job(config, root, time.monotonic() + 10)
    job.prepare(review_request)
    process, _ = job.agent_process(review_request)
    result = subprocess.run(
        process.command,
        cwd=process.cwd,
        env=process.env,
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    assert json.loads(result.stdout)["result"]["ready"]
    assert state["requests"] == [("GET", "/host-exec-transport", b"")]
    specification = json.loads((job.project / "payload" / "launch.json").read_text())
    assert "WORKSHOP_SOCKET" not in specification["environment"]
    assert "WORKSHOP_SOCKET" not in specification["inherit"]


def test_failed_rollback_does_not_treat_off_as_confirmed_cleanup(
    tmp_path: Path,
    service: WorkshopService,
) -> None:
    root = tmp_path / "failed-rollback"
    root.mkdir()
    control(
        root,
        launch_attempted=True,
        launch_change="1",
        launch_settled=True,
        launch_status="Undone",
        launch_failed=True,
    )
    service.jobs[root / "project"] = {
        "pending": False,
        "running": False,
        "aborted": True,
    }
    service.fail.update(("stop", "remove"))
    assert runtime_guard.cleanup_remote(root, "/not-used", "job", {})
    assert root.exists() and not runtime_guard.read_control(root)["removed"]
    assert [action for _, action in service.actions] == ["stop", "remove"]


def test_supervisor_owner_eof_reaps_cli_without_waiting_for_review_deadline(
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    pid_file = tmp_path / "cli-pid"
    command = [
        sys.executable,
        "-c",
        (
            "import os, time; from pathlib import Path; "
            f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(300)"
        ),
    ]

    def owner_dies():
        end = time.monotonic() + 2
        while not pid_file.exists() and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.2)
        os.close(writer)

    owner = threading.Thread(target=owner_dies)
    owner.start()
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            runtime_guard._execute_command(
                command, cwd=tmp_path, env={}, timeout=300, owner=reader
            )
    finally:
        os.close(reader)
        owner.join(timeout=2)
    assert time.monotonic() - started < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("release", [True, False])
def test_guardian_lock_settlement_is_bounded_and_never_deletes_unresolved_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    release: bool,
) -> None:
    root = tmp_path / "locked"
    root.mkdir()
    control(root, launch_attempted=True)
    lock = (root / "command.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    monkeypatch.setattr(runtime_guard, "CLEANUP_SECONDS", 0.15)
    releaser = threading.Timer(0.05, lock.close) if release else None
    if releaser is not None:
        releaser.start()
    reader, writer = os.pipe()
    os.close(writer)
    started = time.monotonic()
    try:
        assert runtime_guard.guard(reader, root, "/not-used", "job") == 1
    finally:
        lock.close()
        if releaser is not None:
            releaser.join()
    assert time.monotonic() - started < 1
    assert root.exists() and runtime_guard.read_control(root)["launch_change"] is None
    diagnostic = capsys.readouterr().err
    assert str(root / runtime_guard.CONTROL) in diagnostic
    assert (
        "no launch change ID" if release else "supervisor did not settle"
    ) in diagnostic


def test_supervisor_spawn_failure_releases_command_lock_and_owner_lease(
    tmp_path: Path,
) -> None:
    control(tmp_path)
    with pytest.raises(FileNotFoundError):
        runtime_guard.run_command(
            ["/bin/true"], cwd=tmp_path / "missing", env={}, timeout=2, root=tmp_path
        )
    with (tmp_path / "command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert runtime_guard.read_control(tmp_path)["launch_attempted"] is False


def test_cleanup_failure_cannot_escape_an_unrelated_exception_handler(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.fail.update(("stop", "remove"))

    async def agent(process, task, *, deadline):
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    try:
        raise ValueError("an unrelated caller error")
    except ValueError:
        with pytest.raises(RunnerStartupError):
            WorkshopRunner(config).run(review_request)


def test_default_packaged_policy_cannot_belong_to_reviewed_tree(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
) -> None:
    reviewed_resources = Path(workshop_runner.__file__).parent / "runtime"
    with pytest.raises(RunnerConfigurationError):
        WorkshopRunner(config).run(
            replace(
                review_request,
                repository_path=reviewed_resources,
            )
        )
    assert not service.jobs


@pytest.mark.parametrize("fail_review", [False, True])
def test_per_review_telemetry_outlives_acp_and_preserves_failure_diagnostics(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fail_review: bool,
) -> None:
    output = tmp_path / "otel"
    endpoints: list[str] = []

    async def agent(process, task, *, deadline):
        layout = json.loads(
            (process.cwd.parent / "project" / "layout.json").read_text()
        )
        spec = json.loads(Path(layout["launch_spec"]).read_text())
        environment = spec["environment"]
        endpoint = environment["OTEL_EXPORTER_OTLP_ENDPOINT"].removeprefix("http://")
        endpoints.append(endpoint)
        packet = ExportMetricsServiceRequest()
        metric = packet.resource_metrics.add().scope_metrics.add().metrics.add()
        metric.name = "omp.agent.tool.calls"
        point = metric.sum.data_points.add()
        point.as_int = 2
        point.attributes.add(key="gen_ai.tool.name").value.string_value = "read"
        connection = http.client.HTTPConnection(endpoint, timeout=2)
        connection.request(
            "POST",
            "/v1/metrics",
            packet.SerializeToString(),
            {"Content-Type": "application/x-protobuf"},
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.close()
        if fail_review:
            raise RunnerTransportError("lost review transport")
        return completed()

    monkeypatch.setattr(workshop_runner.acp_client, "run_acp", agent)
    runner = WorkshopRunner(config, otel_output=output)
    if fail_review:
        with pytest.raises(RunnerTransportError, match="lost review transport"):
            runner.run(review_request)
    else:
        assert runner.run(review_request) == completed()
    batch = json.loads((output / "metrics.jsonl").read_text())
    metric = batch["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    assert metric["name"] == "omp.agent.tool.calls"
    assert metric["sum"]["dataPoints"][0]["asInt"] == "2"
    assert all(not state["running"] for state in service.jobs.values())
    assert all(not project.parent.exists() for project in service.jobs)
    connection = http.client.HTTPConnection(endpoints[0], timeout=2)
    try:
        with pytest.raises(ConnectionRefusedError):
            connection.connect()
    finally:
        connection.close()


def test_missing_declared_telemetry_tunnel_blocks_agent_start(
    config: RuntimeConfig,
    review_request: ReviewRequest,
    service: WorkshopService,
    tmp_path: Path,
) -> None:
    service.connection = (
        "tunnel JOB/omp:review-inference JOB/system:review-inference manual\n"
    )
    with pytest.raises(RunnerStartupError, match="Connected interfaces differ"):
        WorkshopRunner(config, otel_output=tmp_path / "otel").run(review_request)
    assert "exec" not in [action for _, action in service.actions]
    assert all(not state["running"] for state in service.jobs.values())

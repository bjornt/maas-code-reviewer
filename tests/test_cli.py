# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError

import pytest
from github import GithubException

from maas_code_reviewer import cli
from maas_code_reviewer.agent_runner import (
    AgentRunResult,
    AgentUsage,
    CompletionStatus,
    RunnerCancelledError,
    RunnerConfigurationError,
    RunnerLimitError,
    RunnerRefusedError,
    RunnerResultError,
    RunnerStartupError,
    RunnerTimeoutError,
    RunnerTransportError,
)
from maas_code_reviewer.models import Comment
from maas_code_reviewer.reviewer import EMPTY_DIFF_GENERAL_COMMENT, REVIEW_MARKER
from tests.factory import make_mp
from tests.fake_agent_runner import FakeAgentRunner
from tests.fake_git import FakeGitClient
from tests.fake_github import FakeGitHubClient
from tests.fake_launchpad import FakeLaunchpadClient

_DIFF = (
    "diff --git a/file.txt b/file.txt\n"
    "--- a/file.txt\n"
    "+++ b/file.txt\n"
    "@@ -1 +1 @@\n"
    "-original\n"
    "+changed\n"
)
_PR_URL = "https://github.com/owner/repo/pull/7"


def make_outcome(
    *,
    terminal_text: str | None = None,
    status: CompletionStatus = CompletionStatus.COMPLETED,
) -> AgentRunResult:
    return AgentRunResult(
        terminal_text=(
            json.dumps(
                {
                    "general_comment": "Review completed.",
                    "inline_comments": {"file.txt": {"1": "[P1] Preserve the value."}},
                }
            )
            if terminal_text is None
            else terminal_text
        ),
        status=status,
        agent_name="test-agent",
        agent_version="1.2.3",
        model="provider/resolved-model",
        usage=AgentUsage(
            input_tokens=13,
            output_tokens=5,
            cache_read_tokens=8,
            total_tokens=26,
            cost=0.004,
            cost_currency="USD",
        ),
    )


def test_explicit_runtime_configuration_selects_the_review_runner(
    diff_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected = tmp_path / "operator.toml"
    configured = FakeAgentRunner(make_outcome())
    unconfigured = FakeAgentRunner(RunnerConfigurationError("no selected runtime"))

    def select_runner(
        config_path: Path | None,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
        otel_output: Path | None = None,
    ) -> FakeAgentRunner:
        return configured if config_path == selected else unconfigured

    monkeypatch.setattr(cli, "ConfiguredAgentRunner", select_runner)
    cli.main(["review-diff", str(diff_file), "--runtime-config", str(selected)])
    assert "Review completed." in capsys.readouterr().out
    assert len(configured.requests) == 1
    assert unconfigured.requests == []


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeAgentRunner:
    fake = FakeAgentRunner(make_outcome())
    monkeypatch.setattr(cli, "ConfiguredAgentRunner", lambda *args, **kwargs: fake)
    return fake


@pytest.fixture
def diff_file(tmp_path: Path) -> Path:
    path = tmp_path / "change.diff"
    path.write_text(_DIFF)
    return path


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHubClient:
    fake = FakeGitHubClient()
    fake.add_pull_request("owner", "repo", 7, _DIFF, "PR description")
    monkeypatch.setattr(cli, "GitHubClient", lambda token: fake)
    monkeypatch.setenv("GITHUB_TOKEN", "publication-token")
    return fake


@pytest.fixture
def launchpad(monkeypatch: pytest.MonkeyPatch) -> FakeLaunchpadClient:
    fake = FakeLaunchpadClient()
    monkeypatch.setattr(cli, "LaunchpadClient", lambda credentials_file=None: fake)
    return fake


@pytest.fixture
def mp_url(tmp_path: Path, launchpad: FakeLaunchpadClient) -> str:
    git = FakeGitClient()
    target = tmp_path / "target"
    git.create_repo(target)
    git.add_commit(target, {"file.txt": "original\n", "AGENTS.md": "Merged guidance"})
    source = tmp_path / "source"
    git.clone(str(target), source, "main")
    git.create_branch(source, "feature")
    git.checkout(source, "feature")
    git.add_commit(source, {"file.txt": "changed\n"})
    mp = make_mp(
        target_git_repository=str(target),
        source_git_repository=str(source),
        description="MP description",
    )
    launchpad.add_merge_proposal(mp)
    return mp.url


@pytest.mark.parametrize("command", ["review-mp", "review-diff", "review-pr"])
@pytest.mark.parametrize(
    "legacy",
    [["-g", "secret"], ["--gemini-api-key-file", "secret"], ["--max-tool-calls", "5"]],
)
def test_legacy_runtime_options_are_rejected(command: str, legacy: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args([command, *legacy, "input"])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["review-mp", "review-diff", "review-pr"])
@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf", "not-a-number"])
def test_review_deadline_must_be_positive_and_finite(
    command: str, timeout: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args(
            [command, f"--review-timeout={timeout}", "input"]
        )
    assert exc.value.code == 2


def test_no_command_prints_help_and_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 1
    assert "usage:" in capsys.readouterr().out


def test_list_command_works_without_runtime_and_prints_review_state(
    launchpad: FakeLaunchpadClient, capsys: pytest.CaptureFixture[str]
) -> None:
    mp = make_mp()
    launchpad.add_merge_proposal(mp)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    launchpad.add_comment(
        mp.api_url, Comment(author="review-bot", body=REVIEW_MARKER, date=when)
    )
    cli.main(["list-lp-mps", "myproject", "--launchpad-credentials", "trusted-creds"])
    captured = capsys.readouterr()
    assert captured.out == f"{mp.url} Needs review {when.isoformat()}\n"
    assert captured.err == ""


def test_list_command_empty_has_no_output(
    launchpad: FakeLaunchpadClient, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["list-lp-mps", "myproject", "--status", "Merged"])
    assert capsys.readouterr().out == ""


def test_file_review_prints_every_validated_finding_with_repo_guidance(
    runner: FakeAgentRunner,
    diff_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "AGENTS.md").write_text("Project review guidance")
    cli.main(
        [
            "review-diff",
            str(diff_file),
            "--repo-dir",
            str(tmp_path),
            "--runtime-config",
            str(tmp_path / "operator.toml"),
            "--model",
            "provider/requested-model",
            "--review-timeout",
            "17.5",
            "--max-diff-chars",
            "1000",
        ]
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(REVIEW_MARKER)
    assert "Review completed." in captured.out
    assert "file.txt" in captured.out and "Preserve the value" in captured.out
    request = runner.requests[0]
    assert request.repository_path == tmp_path.resolve()
    assert request.full_diff == _DIFF
    assert "Project review guidance" in request.task_prompt
    assert request.model == "provider/requested-model"
    assert request.timeout_seconds == 17.5


@pytest.mark.parametrize("structured", [False, True])
def test_local_diff_prefix_option_resolves_ambiguous_paths_before_output(
    runner: FakeAgentRunner,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    structured: bool,
) -> None:
    (tmp_path / "file.txt").write_text("changed\n")
    (tmp_path / "same").mkdir()
    (tmp_path / "same" / "file.txt").write_text("other\n")
    diff = tmp_path / "changes.diff"
    diff.write_text(
        "--- same/file.txt\n+++ same/file.txt\n@@ -1 +1 @@\n-original\n+changed\n"
    )
    output = tmp_path / "review.json"
    args = ["review-diff", str(diff), "--repo-dir", str(tmp_path)]
    if structured:
        args.extend(["--json-output", str(output)])
    with pytest.raises(SystemExit):
        cli.main(args)
    assert runner.requests == []
    assert not output.exists()
    assert capsys.readouterr().out == ""

    cli.main([*args, "--diff-prefix", "same/"])
    captured = capsys.readouterr()
    if structured:
        assert json.loads(output.read_text())["inline_comments"] == {
            "file.txt": {"1": "[P1] Preserve the value."}
        }
        assert captured.out == ""
    else:
        assert "file.txt:1" in captured.out
        assert "Preserve the value" in captured.out


def test_stdin_review_uses_current_checkout(
    runner: FakeAgentRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(_DIFF))
    cli.main(["review-diff", "-"])
    assert "Review completed." in capsys.readouterr().out
    assert runner.requests[0].repository_path == tmp_path
    assert runner.requests[0].full_diff == _DIFF


def test_structured_diff_output_has_no_review_stdout_and_portable_metrics(
    runner: FakeAgentRunner,
    diff_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "review.json"
    metrics = tmp_path / "metrics.json"
    cli.main(
        [
            "review-diff",
            str(diff_file),
            "--repo-dir",
            str(tmp_path),
            "--json-output",
            str(output),
            "--metrics",
            str(metrics),
        ]
    )
    assert capsys.readouterr().out == ""
    review = json.loads(output.read_text())
    assert review["general_comment"].startswith(REVIEW_MARKER)
    assert review["inline_comments"] == {"file.txt": {"1": "[P1] Preserve the value."}}
    usage = json.loads(metrics.read_text())
    assert usage["agent_name"] == "test-agent"
    assert usage["agent_version"] == "1.2.3"
    assert usage["model_name"] == "provider/resolved-model"
    assert usage["tokens_input"] == 13
    assert usage["tokens_output"] == 5
    assert usage["tokens_cache_read"] == 8
    assert usage["tokens_cache_write"] is None
    assert usage["tokens_thinking"] is None
    assert usage["tokens_total"] == 26
    assert usage["cost"] == 0.004
    assert usage["cost_currency"] == "USD"
    assert usage["diff_size_bytes"] == len(_DIFF.encode())
    assert usage["agents_md_read"] is False
    assert "files_read" not in usage
    assert "max_tool_calls" not in usage


@pytest.mark.parametrize("structured", [False, True])
def test_empty_diff_needs_no_config_model_credentials_or_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    structured: bool,
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n \t"))
    (tmp_path / "AGENTS.md").write_bytes(b"\xff")
    output = tmp_path / "review.json"
    metrics = tmp_path / "metrics.json"
    args = ["review-diff", "-", "--repo-dir", str(tmp_path), "--metrics", str(metrics)]
    if structured:
        args.extend(["--json-output", str(output)])
    cli.main(args)
    captured = capsys.readouterr()
    assert captured.err == ""
    if structured:
        assert captured.out == ""
        assert json.loads(output.read_text()) == {
            "general_comment": EMPTY_DIFF_GENERAL_COMMENT,
            "inline_comments": {},
        }
    else:
        assert captured.out.startswith(REVIEW_MARKER)
        assert EMPTY_DIFF_GENERAL_COMMENT in captured.out
    usage = json.loads(metrics.read_text())
    assert usage["tokens_input"] == usage["tokens_output"] == 0
    assert usage["tokens_total"] == 0


def test_nonempty_diff_without_explicit_config_fails_without_output(
    diff_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "review.json"
    metrics = tmp_path / "metrics.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "review-diff",
                str(diff_file),
                "--json-output",
                str(output),
                "--metrics",
                str(metrics),
            ]
        )
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runtime" in captured.err.lower() and "config" in captured.err.lower()
    assert not output.exists()
    assert not metrics.exists()


@pytest.mark.parametrize(
    "failure",
    [
        RunnerConfigurationError("unsupported model"),
        RunnerStartupError("cannot launch runtime"),
        RunnerTransportError("agent connection closed"),
        RunnerTimeoutError("review deadline exceeded"),
        RunnerCancelledError("review cancelled"),
        RunnerRefusedError("review refused"),
        RunnerLimitError("agent budget exhausted"),
        RunnerResultError("invalid review output"),
    ],
)
def test_runtime_failure_is_stderr_only_without_review_artifacts(
    runner: FakeAgentRunner,
    diff_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: RunnerConfigurationError,
) -> None:
    runner.outcome = failure
    output = tmp_path / "review.json"
    metrics = tmp_path / "metrics.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "review-diff",
                str(diff_file),
                "--json-output",
                str(output),
                "--metrics",
                str(metrics),
            ]
        )
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(failure) in captured.err
    assert not output.exists() and not metrics.exists()


@pytest.mark.parametrize(
    "terminal_text",
    [
        "",
        "provider rejected this request",
        '{"general_comment":"unsafe","inline_comments":{"outside.txt":{"1":"bad"}}}',
        '{"general_comment":"unsafe","inline_comments":{"file.txt":{"99":"bad"}}}',
    ],
)
def test_invalid_terminal_review_never_reaches_text_or_json_output(
    runner: FakeAgentRunner,
    diff_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    terminal_text: str,
) -> None:
    runner.outcome = make_outcome(terminal_text=terminal_text)
    output = tmp_path / "review.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-diff", str(diff_file), "--json-output", str(output)])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Error:")
    assert not output.exists()


@pytest.mark.parametrize("missing_input", ["diff", "description"])
def test_diff_acquisition_failure_is_useful_stderr(
    runner: FakeAgentRunner,
    diff_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing_input: str,
) -> None:
    output = tmp_path / "review.json"
    missing = tmp_path / f"missing-{missing_input}"
    arguments = ["review-diff", "--json-output", str(output)]
    if missing_input == "description":
        arguments.extend([str(diff_file), "--description-file", str(missing)])
    else:
        arguments.append(str(missing))
    with pytest.raises(SystemExit) as exc:
        cli.main(arguments)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert missing.name in captured.err
    assert runner.requests == []
    assert not output.exists()


def test_mp_publishes_host_validated_text_and_usage_after_review(
    runner: FakeAgentRunner,
    launchpad: FakeLaunchpadClient,
    mp_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metrics = tmp_path / "metrics.json"
    cli.main(["review-mp", mp_url, "--metrics", str(metrics)])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    comments = launchpad.get_comments_for(launchpad.get_merge_proposal(mp_url).api_url)
    assert len(comments) == 1
    assert comments[0].body.startswith(REVIEW_MARKER)
    assert "Preserve the value" in comments[0].body
    assert not runner.requests[0].repository_path.exists()
    usage = json.loads(metrics.read_text())
    assert usage["agents_md_read"] is True
    assert usage["tokens_input"] == 13


def test_mp_dry_run_keeps_publication_on_host_and_does_not_post(
    runner: FakeAgentRunner,
    launchpad: FakeLaunchpadClient,
    mp_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(["review-mp", mp_url, "--dry-run", "--review-timeout", "23.25"])
    assert "Preserve the value" in capsys.readouterr().out
    assert (
        launchpad.get_comments_for(launchpad.get_merge_proposal(mp_url).api_url) == []
    )


def test_duplicate_mp_needs_no_config_and_produces_no_metrics(
    launchpad: FakeLaunchpadClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mp = make_mp(target_git_repository="/does-not-exist")
    launchpad.add_merge_proposal(mp)
    launchpad.add_comment(
        mp.api_url,
        Comment(author="review-bot", body=REVIEW_MARKER, date=datetime.now(UTC)),
    )
    metrics = tmp_path / "metrics.json"
    cli.main(
        [
            "review-mp",
            mp.url,
            "--runtime-config",
            str(tmp_path / "missing.toml"),
            "--metrics",
            str(metrics),
        ]
    )
    assert capsys.readouterr().out == "Already reviewed, skipping.\n"
    assert not metrics.exists()
    assert len(launchpad.get_comments_for(mp.api_url)) == 1


@pytest.mark.parametrize("invalid_output", [False, True])
def test_failed_mp_never_publishes_or_prints_review(
    runner: FakeAgentRunner,
    launchpad: FakeLaunchpadClient,
    mp_url: str,
    capsys: pytest.CaptureFixture[str],
    invalid_output: bool,
) -> None:
    runner.outcome = (
        make_outcome(terminal_text="unvalidated provider error")
        if invalid_output
        else RunnerTimeoutError("timed out")
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-mp", mp_url])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Error:")
    assert (
        launchpad.get_comments_for(launchpad.get_merge_proposal(mp_url).api_url) == []
    )
    assert not runner.requests[0].repository_path.exists()


def test_mp_git_acquisition_failure_is_reported_without_publication(
    launchpad: FakeLaunchpadClient, capsys: pytest.CaptureFixture[str]
) -> None:
    mp = make_mp(target_git_repository="/does-not-exist")
    launchpad.add_merge_proposal(mp)
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-mp", mp.url])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "git" in captured.err
    assert launchpad.get_comments_for(mp.api_url) == []


def test_pr_publishes_validated_general_and_inline_comments(
    runner: FakeAgentRunner,
    github: FakeGitHubClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metrics = tmp_path / "metrics.json"
    (tmp_path / "AGENTS.md").write_text("PR checkout guidance")
    cli.main(
        [
            "review-pr",
            _PR_URL,
            "--repo-dir",
            str(tmp_path),
            "--model",
            "provider/requested-model",
            "--review-timeout",
            "9.5",
            "--metrics",
            str(metrics),
        ]
    )
    assert capsys.readouterr().out == ""
    reviews = github.get_posted_reviews("owner", "repo", 7)
    assert len(reviews) == 1
    assert reviews[0]["body"].startswith(REVIEW_MARKER)
    assert reviews[0]["comments"] == [
        {"path": "file.txt", "line": 1, "body": "[P1] Preserve the value."}
    ]
    request = runner.requests[0]
    assert "PR description" in request.task_prompt
    assert "PR checkout guidance" in request.task_prompt
    assert request.repository_path == tmp_path
    assert request.model == "provider/requested-model"
    assert request.timeout_seconds == 9.5
    assert json.loads(metrics.read_text())["agents_md_read"] is True


def test_pr_dry_run_prints_json_without_publishing(
    runner: FakeAgentRunner,
    github: FakeGitHubClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    cli.main(["review-pr", _PR_URL, "--dry-run"])
    review = json.loads(capsys.readouterr().out)
    assert review["general_comment"].startswith(REVIEW_MARKER)
    assert review["inline_comments"]["file.txt"]["1"] == "[P1] Preserve the value."
    assert github.get_posted_reviews("owner", "repo", 7) == []
    assert runner.requests[0].repository_path == tmp_path


@pytest.mark.parametrize("explicit_token", [False, True])
def test_pr_token_selection_authenticates_publication_client(
    runner: FakeAgentRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    explicit_token: bool,
) -> None:
    fake = FakeGitHubClient()
    fake.add_pull_request("owner", "repo", 7, _DIFF)
    monkeypatch.setenv("GITHUB_TOKEN", "environment-token")
    accepted = "explicit-token" if explicit_token else "environment-token"

    def authenticated_client(token: str) -> FakeGitHubClient:
        if token != accepted:
            raise ValueError("publication authentication failed")
        return fake

    monkeypatch.setattr(cli, "GitHubClient", authenticated_client)
    args = ["review-pr", _PR_URL, "--repo-dir", str(tmp_path)]
    if explicit_token:
        args.extend(["--github-token", "explicit-token"])
    cli.main(args)
    assert len(fake.get_posted_reviews("owner", "repo", 7)) == 1
    assert "explicit-token" not in runner.requests[0].task_prompt
    assert "environment-token" not in runner.requests[0].task_prompt


def test_pr_missing_token_fails_before_runtime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-pr", _PR_URL])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--github-token" in captured.err
    assert "GITHUB_TOKEN" in captured.err


def test_invalid_pr_url_is_useful_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-pr", "https://example.com/not-a-pr"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Invalid GitHub PR URL" in captured.err


def test_empty_pr_needs_no_runtime_config_and_can_dry_run(
    github: FakeGitHubClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github.add_pull_request("owner", "repo", 7, "")
    cli.main(["review-pr", _PR_URL, "--repo-dir", str(tmp_path), "--dry-run"])
    assert json.loads(capsys.readouterr().out) == {
        "general_comment": EMPTY_DIFF_GENERAL_COMMENT,
        "inline_comments": {},
    }
    assert github.get_posted_reviews("owner", "repo", 7) == []


@pytest.mark.parametrize(
    "outcome",
    [
        RunnerTransportError("connection closed"),
        make_outcome(terminal_text="provider error"),
        make_outcome(status=CompletionStatus.CANCELLED),
        make_outcome(
            terminal_text=(
                '{"general_comment":"bad","inline_comments":{"no.txt":{"1":"bad"}}}'
            )
        ),
    ],
)
def test_pr_runtime_and_validation_failures_never_publish(
    runner: FakeAgentRunner,
    github: FakeGitHubClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    outcome: AgentRunResult | RunnerTransportError,
) -> None:
    runner.outcome = outcome
    metrics = tmp_path / "metrics.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-pr", _PR_URL, "--metrics", str(metrics)])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Error:")
    assert github.get_posted_reviews("owner", "repo", 7) == []
    assert not metrics.exists()


def test_empty_mp_dry_run_needs_no_runtime_and_reports_known_zero_usage(
    launchpad: FakeLaunchpadClient,
    mp_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mp = launchpad.get_merge_proposal(mp_url)
    FakeGitClient.add_commit(Path(mp.source_git_repository), {"file.txt": "original\n"})
    metrics = tmp_path / "metrics.json"
    cli.main(["review-mp", mp_url, "--dry-run", "--metrics", str(metrics)])
    captured = capsys.readouterr()
    assert captured.out.startswith(REVIEW_MARKER)
    assert EMPTY_DIFF_GENERAL_COMMENT in captured.out
    assert captured.err == ""
    assert launchpad.get_comments_for(mp.api_url) == []
    usage = json.loads(metrics.read_text())
    assert usage["tokens_input"] == usage["tokens_output"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        URLError("network unavailable"),
        GithubException(403, {"message": "forbidden"}),
        subprocess.CalledProcessError(1, ["git", "fetch"]),
    ],
)
def test_acquisition_failures_are_reported_without_publication(
    monkeypatch: pytest.MonkeyPatch,
    github: FakeGitHubClient,
    capsys: pytest.CaptureFixture[str],
    failure: URLError | GithubException | subprocess.CalledProcessError,
) -> None:
    class UnavailableGitHub(FakeGitHubClient):
        def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
            raise failure

    monkeypatch.setattr(cli, "GitHubClient", lambda token: UnavailableGitHub())
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-pr", _PR_URL])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Error:")
    assert github.get_posted_reviews("owner", "repo", 7) == []


def test_primary_failure_keeps_cleanup_diagnostics_visible(
    runner: FakeAgentRunner,
    diff_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = RunnerTransportError("primary transport failure")
    failure.add_note("Review cleanup failed: container still busy")
    runner.outcome = failure
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-diff", str(diff_file)])
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "primary transport failure" in output.err
    assert "container still busy" in output.err


def test_keyboard_cancellation_is_nonzero_without_review_or_traceback(
    diff_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class InterruptedRunner:
        def run(self, request):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        cli, "ConfiguredAgentRunner", lambda *args, **kwargs: InterruptedRunner()
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-diff", str(diff_file)])
    assert exc.value.code == 130
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err and "Traceback" not in output.err

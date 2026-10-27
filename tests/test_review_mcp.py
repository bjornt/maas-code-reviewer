# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from maas_code_reviewer import cli, review_mcp
from maas_code_reviewer.agent_runner import AgentRunResult, CompletionStatus
from maas_code_reviewer.metrics import ReviewMetrics
from maas_code_reviewer.reviewer import review_diff_structured
from tests.fake_agent_runner import FakeAgentRunner
from tests.test_reviewer import DIFF, JSON_REVIEW

DIFF_FILE = (
    "diff --git a/src/code.py b/src/code.py\n"
    "--- a/src/code.py\n+++ b/src/code.py\n"
    "@@ -1 +1,2 @@\n old\n+new\n"
)


def rpc(method: str, *, id: int = 1, params: dict | None = None) -> str:
    message: dict = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def call_tool(arguments: dict, *, id: int = 3) -> str:
    return rpc(
        "tools/call",
        id=id,
        params={"name": review_mcp.TOOL_NAME, "arguments": arguments},
    )


def serve_lines(*lines: str) -> list:
    writer = io.StringIO()
    review_mcp.serve(
        DIFF_FILE,
        path_prefix=None,
        repository_files=frozenset({"src/code.py"}),
        reader=io.StringIO("\n".join(lines) + "\n"),
        writer=writer,
    )
    return [json.loads(line) for line in writer.getvalue().splitlines()]


def test_serves_tool_list_and_initializes():
    responses = serve_lines(
        rpc("initialize", id=1, params={"protocolVersion": "2024-11-05"}),
        rpc("tools/list", id=2),
    )
    assert responses[0]["result"]["serverInfo"]["name"] == (review_mcp.SERVER_NAME)
    tools = responses[1]["result"]["tools"]
    assert [tool["name"] for tool in tools] == [review_mcp.TOOL_NAME]
    assert tools[0]["inputSchema"]["required"] == ["review"]


def test_valid_document_passes_validation():
    review = json.dumps(
        {
            "general_comment": "Looks good.",
            "inline_comments": {"src/code.py": {"2": "Check the new line."}},
        }
    )
    responses = serve_lines(call_tool({"review": review}))
    content = responses[0]["result"]["content"]
    assert content[0]["text"] == review_mcp.VALID_DOCUMENT_MESSAGE


def test_invalid_document_reports_every_reason():
    review = json.dumps(
        {
            "general_comment": "ok",
            "inline_comments": {
                "unknown.py": {"1": "x"},
                "src/code.py": {"9": "y"},
            },
        }
    )
    responses = serve_lines(call_tool({"review": review}))
    text = responses[0]["result"]["content"][0]["text"]
    assert "Invalid review document" in text
    assert "does not appear in the diff" in text
    assert "unknown.py" in text


def test_malformed_json_and_missing_argument_are_reported():
    responses = serve_lines(call_tool({"review": "{not json"}))
    assert "Invalid review JSON" in responses[0]["result"]["content"][0]["text"]
    responses = serve_lines(call_tool({"wrong": "argument"}))
    assert (
        "the 'review' argument is required"
        in responses[0]["result"]["content"][0]["text"]
    )


def test_cli_serves_validation_over_stdio(tmp_path: Path) -> None:
    valid_review = json.dumps(
        {
            "general_comment": "Looks good.",
            "inline_comments": {"src/code.py": {"2": "Check the new line."}},
        }
    )
    invalid_review = json.dumps(
        {"general_comment": "no", "inline_comments": {"unknown.py": {"1": "x"}}}
    )
    requests = "\n".join(
        [
            rpc("initialize", id=1, params={"protocolVersion": "2024-11-05"}),
            rpc("tools/list", id=2),
            call_tool({"review": valid_review}),
            call_tool({"review": invalid_review}),
        ]
    )
    diff_file = tmp_path / "review.diff"
    diff_file.write_text(DIFF_FILE)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "code.py").write_text("old\nnew\n")
    script = (
        "import sys; from maas_code_reviewer.cli import main;"
        " main(['review-mcp', '--repo-dir', sys.argv[1],"
        " '--diff-file', sys.argv[2]])"
    )
    process = subprocess.run(
        [sys.executable, "-c", script, str(repo), str(diff_file)],
        input=requests + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert responses[1]["result"]["tools"][0]["name"] == review_mcp.TOOL_NAME
    assert responses[2]["result"]["content"][0]["text"] == (
        review_mcp.VALID_DOCUMENT_MESSAGE
    )
    # JSON_REVIEW references src/first.py and src/second.py, absent from this repo.
    assert "does not appear in the diff" in responses[3]["result"]["content"][0]["text"]


def test_review_stages_validation_server_and_cleans_up(tmp_path: Path) -> None:
    runner = FakeAgentRunner(
        AgentRunResult(
            terminal_text=JSON_REVIEW,
            status=CompletionStatus.COMPLETED,
            agent_name="review-agent",
        )
    )
    metrics = ReviewMetrics()
    review_diff_structured(runner, DIFF, None, tmp_path, metrics=metrics)
    request = runner.requests[0]
    (server,) = request.mcp_servers
    assert server["name"] == "review-validation"
    assert server["command"] == "maas-code-reviewer"
    assert server["args"][:3] == [
        "review-mcp",
        "--diff-file",
        ".maas-code-reviewer/review-input.diff",
    ]
    # The staged review diff is removed with its directory after the review.
    assert not (tmp_path / ".maas-code-reviewer").exists()


def test_review_passes_diff_prefix_to_the_validation_server(
    tmp_path: Path,
) -> None:
    runner = FakeAgentRunner(
        AgentRunResult(
            terminal_text=JSON_REVIEW,
            status=CompletionStatus.COMPLETED,
            agent_name="review-agent",
        )
    )
    review_diff_structured(runner, DIFF, None, tmp_path, diff_prefix="b/")
    (server,) = runner.requests[0].mcp_servers
    assert "--diff-prefix" in server["args"]
    assert "b/" in server["args"]
    assert not (tmp_path / ".maas-code-reviewer").exists()


def test_blank_lines_and_notifications_are_ignored():
    responses = serve_lines("", json.dumps({"jsonrpc": "2.0", "method": "x"}))
    assert responses == []


def test_unknown_method_receives_an_error_response():
    responses = serve_lines(rpc("resources/list", id=7))
    assert responses[0]["error"] == {
        "code": -32601,
        "message": "Unknown method: 'resources/list'",
    }


def test_main_dispatches_review_mcp_over_stdio(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    diff_file = tmp_path / "review.diff"
    diff_file.write_text(DIFF_FILE)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(call_tool({"review": "{not json"}) + "\n")
    )
    cli.main(["review-mcp", "--repo-dir", str(tmp_path), "--diff-file", str(diff_file)])
    response = json.loads(capsys.readouterr().out.splitlines()[0])
    assert "Invalid review JSON" in response["result"]["content"][0]["text"]

# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from maas_code_reviewer import review_mcp

DIFF_FILE = (
    "diff --git a/src/code.py b/src/code.py\n"
    "--- a/src/code.py\n+++ b/src/code.py\n"
    "@@ -1 +1,2 @@\n old\n+new\n"
)
VALID_REVIEW = json.dumps(
    {
        "general_comment": "Looks good.",
        "inline_comments": {"src/code.py": {"2": "Check the new line."}},
    }
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


def serve_lines(
    *lines: str,
    diff: str = DIFF_FILE,
    path_prefix: str | None = None,
    repository_files: frozenset[str] = frozenset({"src/code.py"}),
) -> list[dict]:
    writer = io.StringIO()
    review_mcp.serve(
        diff,
        path_prefix=path_prefix,
        repository_files=repository_files,
        reader=io.StringIO("\n".join(lines) + "\n"),
        writer=writer,
    )
    return [json.loads(line) for line in writer.getvalue().splitlines()]


def test_initialize_reply_names_the_server() -> None:
    responses = serve_lines(rpc("initialize", params={"protocolVersion": "1"}))
    assert len(responses) == 1
    result = responses[0]["result"]
    assert result["protocolVersion"] == review_mcp.PROTOCOL_VERSION
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"] == {
        "name": review_mcp.SERVER_NAME,
        "version": review_mcp.SERVER_VERSION,
    }


def test_tools_list_exposes_the_single_tool() -> None:
    responses = serve_lines(rpc("tools/list"))
    tools = responses[0]["result"]["tools"]
    assert ["validate_review"] == [tool["name"] for tool in tools]
    assert tools[0]["inputSchema"]["required"] == ["review"]
    assert tools[0]["inputSchema"]["properties"]["review"]["type"] == "string"
    assert tools[0]["description"] == review_mcp.TOOL_DESCRIPTION


def test_ping_replies_with_an_empty_result() -> None:
    responses = serve_lines(rpc("ping"))
    assert responses[0]["result"] == {}


def test_valid_document_passes_validation() -> None:
    responses = serve_lines(call_tool({"review": VALID_REVIEW}))
    content = responses[0]["result"]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"] == review_mcp.VALID_DOCUMENT_MESSAGE


@pytest.mark.parametrize(
    "wrapper",
    [
        lambda text: text,
        lambda text: "```\n" + text + "\n```",
        lambda text: "```json\n" + text + "\n```",
    ],
)
def test_fenced_documents_are_accepted(wrapper) -> None:
    responses = serve_lines(call_tool({"review": wrapper(VALID_REVIEW)}))
    text = responses[0]["result"]["content"][0]["text"]
    assert text == review_mcp.VALID_DOCUMENT_MESSAGE


def test_invalid_document_reports_every_reason() -> None:
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
    assert "unknown.py" in text
    assert "does not appear in the diff" in text


@pytest.mark.parametrize(
    "fenced",
    [
        "```python\n" + VALID_REVIEW + "\n```",
        "```\n" + VALID_REVIEW + "\n```\nTrailing prose.",
    ],
)
def test_malformed_fence_is_rejected(fenced: str) -> None:
    responses = serve_lines(call_tool({"review": fenced}))
    text = responses[0]["result"]["content"][0]["text"]
    assert text == "Review must contain one JSON object or exact JSON fence."


def test_prose_wrapped_document_is_rejected() -> None:
    responses = serve_lines(call_tool({"review": "Here is my review: " + VALID_REVIEW}))
    text = responses[0]["result"]["content"][0]["text"]
    assert text.startswith("Invalid review JSON")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_constants_are_rejected(constant: str) -> None:
    review = '{"general_comment": ' + constant + ', "inline_comments": {}}'
    responses = serve_lines(call_tool({"review": review}))
    text = responses[0]["result"]["content"][0]["text"]
    assert "Invalid review JSON" in text
    assert "Non-JSON numeric constant" in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("true", "Review must be a JSON object (dict), got bool."),
        ("[1, 2]", "Review must be a JSON object (dict), got list."),
        ("42", "Review must be a JSON object (dict), got int."),
    ],
)
def test_non_object_documents_are_rejected(text: str, expected: str) -> None:
    responses = serve_lines(call_tool({"review": text}))
    text = responses[0]["result"]["content"][0]["text"]
    assert text == f"Invalid review document: {expected}"


def test_missing_review_argument_is_reported_in_a_result() -> None:
    for arguments in [{"wrong": "argument"}, {"review": 42}]:
        responses = serve_lines(call_tool(arguments))
        text = responses[0]["result"]["content"][0]["text"]
        assert "the 'review' argument is required" in text


def test_unknown_tool_name_is_a_protocol_error() -> None:
    frame = rpc("tools/call", params={"name": "other", "arguments": {"review": "x"}})
    responses = serve_lines(frame)
    assert responses[0]["error"] == {
        "code": -32602,
        "message": "Unknown tool: 'other'",
    }


def test_unknown_method_receives_method_not_found() -> None:
    responses = serve_lines(rpc("resources/list", id=7))
    assert responses[0]["error"] == {
        "code": -32601,
        "message": "Unknown method: 'resources/list'",
    }


def test_blank_lines_and_notifications_are_ignored() -> None:
    responses = serve_lines("", json.dumps({"jsonrpc": "2.0", "method": "x"}))
    assert responses == []


def test_prefix_and_listing_thread_into_verdicts() -> None:
    prefixed = DIFF_FILE.replace("a/src/", "a/updated/tree/src/").replace(
        "b/src/", "b/updated/tree/src/"
    )
    responses = serve_lines(
        call_tool({"review": VALID_REVIEW}),
        diff=prefixed,
        path_prefix="b/updated/tree/",
    )
    text = responses[0]["result"]["content"][0]["text"]
    assert text == review_mcp.VALID_DOCUMENT_MESSAGE

    # Without the prefix the prefixed diff paths resolve to prefixed
    # repository coordinates the document never uses.
    responses = serve_lines(call_tool({"review": VALID_REVIEW}), diff=prefixed)
    text = responses[0]["result"]["content"][0]["text"]
    assert "Invalid review document" in text
    assert "'src/code.py'" in text


def test_cli_serves_validation_over_stdio(tmp_path: Path) -> None:
    invalid_review = json.dumps(
        {"general_comment": "no", "inline_comments": {"unknown.py": {"1": "x"}}}
    )
    requests = "\n".join(
        [
            rpc("initialize", params={"protocolVersion": "2024-11-05"}),
            rpc("tools/list"),
            call_tool({"review": VALID_REVIEW}),
            call_tool({"review": invalid_review}),
        ]
    )
    diff_file = tmp_path / "review.diff"
    diff_file.write_text(DIFF_FILE)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
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
    assert len(responses) == 4
    assert responses[1]["result"]["tools"][0]["name"] == review_mcp.TOOL_NAME
    assert (
        responses[2]["result"]["content"][0]["text"]
        == review_mcp.VALID_DOCUMENT_MESSAGE
    )
    assert "does not appear in the diff" in responses[3]["result"]["content"][0]["text"]


def test_review_mcp_help_lists_exactly_three_flags() -> None:
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from maas_code_reviewer.cli import main; main(['review-mcp', '--help'])",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert "--diff-file" in process.stdout
    assert "--diff-prefix" in process.stdout
    assert "--repo-dir" in process.stdout

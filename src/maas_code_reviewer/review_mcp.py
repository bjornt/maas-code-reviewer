# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""MCP stdio server exposing in-review validation to the reviewing agent.

The agent reviews the repository; this server gives it the same schema
validation the terminal review must pass, so it can verify and correct a
draft review document before finishing instead of failing host-side and
needing a repair round. The server is bound at startup to the staged diff
text and the repository file listing; it grants no host access, and its
verdict is advisory -- publish-side host validation remains the authority.
"""

import json
from collections.abc import Set
from typing import IO, Any

from maas_code_reviewer.review_schema import parse_review_document

TOOL_NAME = "validate_review"
TOOL_DESCRIPTION = (
    "Validate a draft review JSON document against the reviewed diff. "
    "Pass the complete review document (the same JSON you will emit as your "
    "final answer, with general_comment and inline_comments) as the 'review' "
    "string. Returns the validation verdict, or every reason the document "
    "is invalid so you can correct it before finishing."
)
TOOL_INPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["review"],
    "properties": {
        "review": {
            "type": "string",
            "description": "The complete review JSON document to validate.",
        }
    },
}
SERVER_NAME = "maas-code-reviewer-review-validation"
SERVER_VERSION = "1"
PROTOCOL_VERSION = "2024-11-05"
VALID_DOCUMENT_MESSAGE = "Review document is valid."


def serve(
    diff_text: str,
    *,
    path_prefix: str | None,
    repository_files: Set[str],
    reader: IO[str],
    writer: IO[str],
) -> None:
    """Serve the validate_review tool over newline-delimited MCP stdio."""
    for line in reader:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "id" not in message:
            # Notifications and other id-less frames need no reply.
            continue
        request_id = message["id"]
        method = message.get("method")
        if method == "initialize":
            _respond(
                writer,
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            )
        elif method == "ping":
            _respond(writer, request_id, {})
        elif method == "tools/list":
            _respond(
                writer,
                request_id,
                {
                    "tools": [
                        {
                            "name": TOOL_NAME,
                            "description": TOOL_DESCRIPTION,
                            "inputSchema": TOOL_INPUT_SCHEMA,
                        }
                    ]
                },
            )
        elif method == "tools/call":
            params = message.get("params", {})
            if params.get("name") != TOOL_NAME:
                _respond(
                    writer,
                    request_id,
                    error={
                        "code": -32602,
                        "message": f"Unknown tool: {params.get('name')!r}",
                    },
                )
                continue
            arguments = params.get("arguments", {})
            review = arguments.get("review")
            if not isinstance(review, str):
                _respond(
                    writer,
                    request_id,
                    {"content": [{"type": "text", "text": _missing_review_error()}]},
                )
                continue
            _respond(
                writer,
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": _validation_verdict(
                                review,
                                diff_text=diff_text,
                                path_prefix=path_prefix,
                                repository_files=repository_files,
                            ),
                        }
                    ]
                },
            )
        else:
            _respond(
                writer,
                request_id,
                error={"code": -32601, "message": f"Unknown method: {method!r}"},
            )


def _validation_verdict(
    review: str,
    *,
    diff_text: str,
    path_prefix: str | None,
    repository_files: Set[str],
) -> str:
    """Return the tool text for one draft review document."""
    _, failure = parse_review_document(
        review,
        diff_text=diff_text,
        path_prefix=path_prefix,
        repository_files=repository_files,
    )
    if failure is None:
        return VALID_DOCUMENT_MESSAGE
    return failure


def _missing_review_error() -> str:
    return (
        "Invalid tool call: the 'review' argument is required and must be a "
        "string containing the complete review JSON document."
    )


def _respond(
    writer: IO[str],
    request_id: Any,
    result: dict | None = None,
    *,
    error: dict | None = None,
) -> None:
    response: dict = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    writer.write(json.dumps(response) + "\n")
    writer.flush()

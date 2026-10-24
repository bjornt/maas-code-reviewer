# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""A deliberately wire-level adversarial ACP subprocess, never a runtime backend."""

import json
import os
import select
import sys
import time
from pathlib import Path
from typing import Any


class Peer:
    def __init__(self, path: Path) -> None:
        self.script = json.loads(path.read_text())
        self.trace = Path(self.script["trace"])
        self.events: list[dict[str, Any]] = []
        self.prompt_id: int | None = None
        self.cancelled = False

    def send(self, message: dict[str, Any]) -> None:
        encoded = (json.dumps({"jsonrpc": "2.0"} | message) + "\n").encode()
        split = self.script.get("split_bytes", len(encoded))
        for start in range(0, len(encoded), split):
            sys.stdout.buffer.write(encoded[start : start + split])
            sys.stdout.buffer.flush()

    def receive(self) -> dict[str, Any] | None:
        line = sys.stdin.readline()
        if not line:
            return None
        message = json.loads(line)
        self.events.append(message)
        self.trace.write_text(json.dumps(self.events))
        return message

    def reply(self, request: dict[str, Any], result: Any) -> None:
        self.send({"id": request["id"], "result": result})

    def finish(self, stop: str | None = None) -> None:
        self.send(
            {
                "id": self.prompt_id,
                "result": {"stopReason": stop or self.script.get("stop", "end_turn")}
                | ({"usage": self.script["usage"]} if "usage" in self.script else {}),
            }
        )

    def prompt(self) -> None:
        for index, action in enumerate(self.script.get("actions", [])):
            if "update" in action:
                self.send(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": action.get("session", "fresh-session"),
                            "update": action["update"],
                        },
                    }
                )
            elif "request" in action:
                request_id = "peer-" + str(index)
                self.send({"id": request_id} | action["request"])
                while (message := self.receive()) is not None:
                    if message.get("method") == "session/cancel":
                        self.cancelled = True
                    if message.get("id") == request_id:
                        break
                if self.cancelled:
                    self.finish("cancelled")
                    return
            elif "notification" in action:
                self.send(action["notification"])
            elif "sleep" in action:
                time.sleep(action["sleep"])
            elif "stderr" in action:
                os.write(2, b"secret-diagnostic\n" * action["stderr"])
            elif "malformed" in action:
                sys.stdout.write(action["malformed"] + "\n")
                sys.stdout.flush()
            elif "exit" in action:
                sys.exit(action["exit"])
            elif "wait_cancel" in action:
                while (message := self.receive()) is not None:
                    if message.get("method") == "session/cancel":
                        self.finish("cancelled")
                        return
                return
            elif "hang" in action:
                while self.receive() is not None:
                    pass
                return
        if "prompt_error" in self.script:
            self.send({"id": self.prompt_id, "error": self.script["prompt_error"]})
        else:
            self.finish()

    def run(self) -> None:
        while (message := self.receive()) is not None:
            method = message.get("method")
            if method == "initialize":
                if self.script.get("startup_exit"):
                    return
                if "startup_error" in self.script:
                    self.send(
                        {"id": message["id"], "error": self.script["startup_error"]}
                    )
                else:
                    self.reply(
                        message,
                        self.script.get(
                            "initialize",
                            {
                                "protocolVersion": 1,
                                "agentInfo": {
                                    "name": "isolated-peer",
                                    "version": "1.2.3",
                                },
                                "agentCapabilities": {},
                            },
                        ),
                    )
            elif method == "session/new":
                self.reply(
                    message,
                    self.script.get(
                        "session",
                        {
                            "sessionId": "fresh-session",
                            "models": {
                                "currentModelId": "provider/exact-model",
                                "availableModels": [
                                    {"modelId": "provider/exact-model", "name": "Exact"}
                                ],
                            },
                        },
                    ),
                )
                if "backpressure_marker" in self.script:
                    # Observe queued prompt bytes without consuming them. The
                    # large prompt then blocks the SDK sender in writer.drain.
                    if not select.select([sys.stdin], [], [], 5)[0]:
                        raise RuntimeError("Coordinator never sent prompt bytes")
                    self.send(
                        {
                            "method": "session/update",
                            "params": {
                                "sessionId": "fresh-session",
                                "update": {
                                    "sessionUpdate": "usage_update",
                                    "used": 1,
                                    "size": 1000,
                                    "cost": {"amount": 0.25, "currency": "USD"},
                                },
                            },
                        }
                    )
                    Path(self.script["backpressure_marker"]).write_text(
                        str(os.getpid())
                    )
                    # A finite fail-safe keeps a regressed test suite runnable;
                    # the coordinator must kill us well before this expires.
                    time.sleep(6)
                    return
            elif method == "session/prompt":
                self.prompt_id = message["id"]
                self.prompt()
            elif method == "session/cancel":
                pass
            else:
                self.send(
                    {
                        "id": message.get("id"),
                        "error": {
                            "code": -32601,
                            "message": "Unexpected coordinator request",
                        },
                    }
                )
        if "shutdown_marker" in self.script:
            Path(self.script["shutdown_marker"]).touch()
            time.sleep(self.script.get("shutdown_delay", 0))


if __name__ == "__main__":
    Peer(Path(sys.argv[1])).run()

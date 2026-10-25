#!/usr/bin/python3
# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Loopback-only native inference; keep the upstream bearer on the host."""

import asyncio
import json
import os
from pathlib import Path

LISTEN = ("127.0.0.1", 4401)
UPSTREAM = ("127.0.0.1", 4000)
ROUTES = {("GET", "/v1/models"), ("POST", "/v1/pi/stream")}
HOSTS = {"127.0.0.1:4401", "localhost:4401"}
MAX_BODY = 64 * 1024 * 1024
OPTION_KEYS = frozenset(
    """
temperature topP topK minP presencePenalty frequencyPenalty repetitionPenalty
stopSequences maxTokens maxHttpAttempts cacheRetention cachedContent userProfileId
initiatorOverride maxRetryDelayMs metadata sessionId promptCacheKey promptCache
statefulResponses storeResponses streamFirstEventTimeoutMs streamIdleTimeoutMs
reasoning disableReasoning forceReasoningOff hideThinkingSummary thinkingBudgets
toolChoice serviceTier guardrailIdentifier guardrailVersion guardrailTrace
requestMetadata kimiApiFormat syntheticApiFormat preferWebsockets openrouterVariant
loopGuard acceptEmptyResponse anthropicCompaction
""".split()
)
TOKEN = (
    (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "gateway-token").read_text().strip()
)
if not TOKEN or any(ord(c) < 33 or ord(c) > 126 for c in TOKEN):
    raise RuntimeError("Invalid upstream credential file")


async def reject(writer, status, message):
    body = ('{"error":"' + message + '"}\n').encode()
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()


async def handle(reader, writer):
    upstream = None
    tasks = set()
    response_started = False
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
        lines = raw[:-4].decode("ascii").split("\r\n")
        method, path, version = lines[0].split(" ")
        if (method, path) not in ROUTES or version not in {"HTTP/1.0", "HTTP/1.1"}:
            await reject(writer, "403 Forbidden", "inference route denied")
            return
        headers = {}
        for line in lines[1:]:
            name, value = line.split(":", 1)
            if not name or any(
                c
                not in (
                    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyz"
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                )
                for c in name
            ):
                raise ValueError("invalid header")
            name = name.lower()
            if name in headers:
                raise ValueError("duplicate header")
            value = value.strip(" \t")
            if any(ord(c) < 32 and c != "\t" for c in value):
                raise ValueError("invalid header value")
            headers[name] = value
        if (
            headers.get("host") not in HOSTS
            or "transfer-encoding" in headers
            or "expect" in headers
        ):
            await reject(writer, "403 Forbidden", "forwarding authority denied")
            return
        length_text = headers.get("content-length", "0")
        if not length_text.isascii() or not length_text.isdecimal():
            raise ValueError("invalid content length")
        length = int(length_text)
        if length > MAX_BODY or (method == "GET" and length):
            await reject(writer, "413 Content Too Large", "request body refused")
            return
        body = await asyncio.wait_for(reader.readexactly(length), 60)
        if method == "POST":
            request = json.loads(body)
            if not isinstance(request, dict):
                raise ValueError("invalid native request")
            model = request.get("modelId", request.get("model"))
            if isinstance(model, dict):
                model = model.get("id")
            context = request.get("context")
            options = request.get("options", {})
            if (
                not isinstance(model, str)
                or not model
                or not isinstance(context, dict)
                or not isinstance(options, dict)
            ):
                raise ValueError("invalid native request")
            # A registry identity never grants caller-selected provider URLs,
            # credentials, transports, or provider HTTP headers.
            native = {
                "modelId": model,
                "context": context,
                "options": {
                    key: value for key, value in options.items() if key in OPTION_KEYS
                },
            }
            if "stream" in request:
                native["stream"] = request["stream"]
            body = json.dumps(native, separators=(",", ":")).encode()
            length = len(body)
        upstream_reader, upstream = await asyncio.wait_for(
            asyncio.open_connection(*UPSTREAM), 15
        )
        # Client Host, Authorization and proxy/identity headers grant no authority.
        upstream.write(
            f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:4000\r\n"
            f"Authorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
            "Accept: application/json, text/event-stream\r\n"
            f"Content-Length: {length}\r\n"
            "Connection: close\r\n\r\n".encode()
            + body
        )
        await upstream.drain()

        async def relay():
            nonlocal response_started
            while data := await upstream_reader.read(65536):
                response_started = True
                writer.write(data)
                await writer.drain()

        # EOF or attempted pipelining immediately closes the upstream inference.
        tasks = {asyncio.create_task(relay()), asyncio.create_task(reader.read(1))}
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (
        ValueError,
        UnicodeError,
        asyncio.LimitOverrunError,
        asyncio.IncompleteReadError,
        TimeoutError,
    ):
        if not response_started:
            await reject(writer, "400 Bad Request", "invalid inference request")
    except (ConnectionError, OSError):
        if not response_started:
            try:
                await reject(
                    writer, "502 Bad Gateway", "inference upstream unavailable"
                )
            except (ConnectionError, OSError):
                pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if upstream is not None:
            upstream.close()
        writer.close()


async def main():
    server = await asyncio.start_server(handle, *LISTEN, limit=65536)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

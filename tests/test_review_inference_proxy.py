# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    (tmp_path / "gateway-token").write_text("test-upstream-credential")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "review_inference_proxy_test",
        Path(__file__).parents[1] / "scripts/review_inference_proxy.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InferencePeer:
    def __init__(self, streaming=False):
        self.streaming = streaming
        self.requests = 0
        self.cancelled = asyncio.Event()
        self.active = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            method, path, _ = lines[0].split()
            headers = {
                name.lower(): value.strip()
                for line in lines[1:]
                if line
                for name, _, value in [line.partition(":")]
            }
            self.requests += 1
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            authorized = (
                headers.get("authorization") == "Bearer test-upstream-credential"
            )
            if method == "POST":
                native = json.loads(body)
                authorized = authorized and (
                    set(native) <= {"modelId", "context", "options", "stream"}
                    and native.get("modelId") in {"provider/alpha", "provider/beta"}
                    and set(native.get("options", {})) <= {"temperature"}
                )
            if not authorized:
                content = b'{"error":"untrusted provider authority"}'
                status = "403 Forbidden"
            elif method == "GET" and path == "/v1/models":
                content = json.dumps(
                    {"data": [{"id": "provider/alpha"}, {"id": "provider/beta"}]}
                ).encode()
                status = "200 OK"
            else:
                content = b'data: {"type":"done","reason":"stop"}\n\n'
                status = "200 OK"
            if self.streaming:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\ndata: start\n\n"
                )
                await writer.drain()
                assert await reader.read() == b""
                self.cancelled.set()
            else:
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Length: {len(content)}\r\n"
                    "Connection: close\r\n\r\n".encode()
                    + content
                )
                await writer.drain()
        finally:
            writer.close()
            self.active.discard(task)


@asynccontextmanager
async def gateway(proxy, streaming=False):
    peer = InferencePeer(streaming)
    backend = await asyncio.start_server(peer.handle, "127.0.0.1", 0)
    proxy.UPSTREAM = backend.sockets[0].getsockname()[:2]
    active = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        active.add(task)
        try:
            await proxy.handle(reader, writer)
        finally:
            active.discard(task)

    frontend = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield frontend.sockets[0].getsockname()[1], peer
    finally:
        frontend.close()
        backend.close()
        await frontend.wait_closed()
        await backend.wait_closed()
        pending = active | peer.active
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def exchange(port, path, payload=None, host="127.0.0.1:4401", method=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps(payload).encode() if payload is not None else b""
    method = method or ("POST" if payload is not None else "GET")
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
        "Authorization: Bearer untrusted-client-credential\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    try:
        response = await asyncio.wait_for(reader.read(), 3)
    finally:
        writer.close()
        await writer.wait_closed()
    headers, _, content = response.partition(b"\r\n\r\n")
    return int(headers.split()[1]), content


def test_only_inference_authority_is_granted_while_all_models_remain_available(proxy):
    async def scenario():
        async with gateway(proxy) as (port, peer):
            for path in [
                "/admin",
                "/credentials",
                "/v1/models?endpoint=foreign",
                "http://foreign.invalid/v1/models",
            ]:
                status, _ = await exchange(port, path)
                assert status == 403
            status, _ = await exchange(port, "/v1/models", host="foreign.invalid")
            assert status == 403
            status, _ = await exchange(port, "/v1/models", method="CONNECT")
            assert status == 403
            assert peer.requests == 0
            status, body = await exchange(port, "/v1/models")
            assert status == 200
            assert {row["id"] for row in json.loads(body)["data"]} == {
                "provider/alpha",
                "provider/beta",
            }
            status, body = await exchange(
                port,
                "/v1/pi/stream",
                {
                    "model": {
                        "id": "provider/beta",
                        "baseUrl": "http://foreign.invalid",
                    },
                    "context": {"messages": []},
                    "baseUrl": "http://foreign.invalid",
                    "options": {
                        "temperature": 0.2,
                        "headers": {"Authorization": "foreign"},
                        "apiKey": "foreign",
                        "baseUrl": "http://foreign.invalid",
                    },
                },
            )
            assert status == 200
            assert b'"type":"done"' in body

    asyncio.run(scenario())


def test_downstream_cancellation_closes_upstream_generation(proxy):
    async def scenario():
        async with gateway(proxy, streaming=True) as (port, peer):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps(
                {"modelId": "provider/alpha", "context": {"messages": []}}
            ).encode()
            writer.write(
                f"POST /v1/pi/stream HTTP/1.1\r\nHost: 127.0.0.1:4401\r\n"
                f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            assert (
                await asyncio.wait_for(reader.readuntil(b"\n\n"), 3)
                == b"data: start\n\n"
            )
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(peer.cancelled.wait(), 3)

    asyncio.run(scenario())

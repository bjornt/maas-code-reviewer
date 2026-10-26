# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""A review-scoped, loopback-only receiver for untrusted OTLP diagnostics."""

import json
import sys
from base64 import b64decode
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import TracebackType
from typing import Any, TextIO, cast

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

_MAX_BODY_BYTES = 16 * 1024 * 1024
_REQUEST_TIMEOUT = 5.0
_SIGNALS = {
    "/v1/traces": ("traces", ExportTraceServiceRequest),
    "/v1/metrics": ("metrics", ExportMetricsServiceRequest),
}


def _hex_ids(value: Any) -> None:
    """OTLP JSON uses hex IDs, unlike protobuf JSON's base64 byte strings."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"traceId", "spanId", "parentSpanId"}:
                value[key] = b64decode(child).hex()
            else:
                _hex_ids(child)
    elif isinstance(value, list):
        for child in value:
            _hex_ids(child)


class TelemetryCollector:
    """Collect real protobuf exports until the owning review has shut down.

    Files contain one OTLP JSON export request per line, not synthesized
    usage summaries. Existing batches are retained when a directory is reused.
    """

    def __init__(self, output_directory: Path) -> None:
        self.output_directory = output_directory
        self._files: dict[str, TextIO] = {}
        self._errors: list[Exception] = []
        self._server: _TelemetryServer | None = None
        self._thread: Thread | None = None

    @property
    def endpoint(self) -> str:
        if self._server is None:
            raise RuntimeError("OTLP telemetry collector is not running")
        return f"localhost:{self._server.server_port}"

    @property
    def environment(self) -> dict[str, str]:
        port = self.endpoint.removeprefix("localhost:")
        return {
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "none",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
            "OTEL_SERVICE_NAME": "maas-code-reviewer",
        }

    def __enter__(self) -> "TelemetryCollector":
        if self._server is not None:
            raise RuntimeError("OTLP telemetry collector is already running")
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self._files.clear()
        self._errors.clear()
        started = False
        try:
            for signal in ("traces", "metrics"):
                self._files[signal] = (self.output_directory / f"{signal}.jsonl").open(
                    "a", encoding="utf-8"
                )
            self._server = _TelemetryServer(self)
            self._thread = Thread(target=self._serve, name="review-otlp", daemon=True)
            self._thread.start()
            started = True
        except (OSError, RuntimeError):
            raise OSError("Could not start OTLP telemetry collector") from None
        finally:
            if not started:
                self.__exit__(*sys.exc_info())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        cleanup: list[Callable[[], None]] = []
        if self._server is not None:
            if self._thread is not None and self._thread.ident is not None:
                if self._thread.is_alive():
                    cleanup.append(self._server.shutdown)
                cleanup.append(self._thread.join)
            cleanup.append(self._server.server_close)
        cleanup.extend(file.close for file in self._files.values())
        for close in cleanup:
            try:
                close()
            except (OSError, RuntimeError) as error:
                self._errors.append(error)
        self._server = None
        self._thread = None
        if self._errors:
            # Neither exception strings nor protobuf values belong in review logs.
            message = (
                "OTLP telemetry collection failed; diagnostic files may be partial"
            )
            if exc_value is not None:
                exc_value.add_note(message)
            else:
                raise OSError(message) from None

    def _serve(self) -> None:
        try:
            assert self._server is not None
            self._server.serve_forever(poll_interval=0.05)
        except OSError as error:
            self._errors.append(error)


class _TelemetryServer(HTTPServer):
    def __init__(self, collector: TelemetryCollector) -> None:
        self.collector = collector
        super().__init__(("127.0.0.1", 0), _OTLPHandler)

    def handle_error(self, request: object, client_address: object) -> None:
        # socketserver's default handler prints a traceback to stderr.
        self.collector._errors.append(cast(Exception, sys.exception()))


class _OTLPHandler(BaseHTTPRequestHandler):
    timeout = _REQUEST_TIMEOUT

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        signal = _SIGNALS.get(self.path)
        if signal is None:
            self._respond(404)
            return
        if (
            self.headers.get("Content-Encoding", "identity").lower() != "identity"
            or self.headers.get("Content-Type", "").split(";", 1)[0].lower()
            != "application/x-protobuf"
        ):
            self._respond(415)
            return
        try:
            body = self._read_body()
        except TimeoutError:
            self._respond(408)
            return
        except ValueError:
            self._respond(400)
            return
        if body is None:
            return
        name, request_type = signal
        message = request_type()
        try:
            message.ParseFromString(body)
        except DecodeError:
            self._respond(400)
            return
        collector = cast(_TelemetryServer, self.server).collector
        try:
            document = MessageToDict(message, use_integers_for_enums=True)
            _hex_ids(document)
            collector._files[name].write(
                json.dumps(document, separators=(",", ":")) + "\n"
            )
            collector._files[name].flush()
        except (OSError, ValueError) as error:
            collector._errors.append(error)
            self._respond(500)
            return
        # Empty Export*ServiceResponse messages have an empty wire encoding.
        self._respond(200)

    def _respond(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _read_body(self) -> bytes | None:
        transfer = self.headers.get("Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if transfer is not None:
            if lengths:
                raise ValueError("Ambiguous request framing")
            if transfer.lower() != "chunked":
                self._respond(415)
                return None
            return self._read_chunked()
        if not lengths:
            self._respond(411)
            return None
        if len(lengths) != 1:
            raise ValueError("Ambiguous content length")
        length = int(lengths[0])
        if length < 0:
            raise ValueError("Negative content length")
        if length > _MAX_BODY_BYTES:
            self._respond(413)
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Incomplete export")
        return body

    def _read_chunked(self) -> bytes | None:
        body = bytearray()
        while True:
            line = self.rfile.readline(8192)
            if not line.endswith(b"\r\n"):
                raise ValueError("Invalid chunk header")
            size = int(line.partition(b";")[0], 16)
            if size < 0:
                raise ValueError("Negative chunk size")
            if size > _MAX_BODY_BYTES - len(body):
                self._respond(413)
                return None
            if size == 0:
                trailer_size = 0
                while (line := self.rfile.readline(8192)) != b"\r\n":
                    trailer_size += len(line)
                    if not line.endswith(b"\r\n") or trailer_size > 8192:
                        raise ValueError("Invalid chunk trailers")
                return bytes(body)
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                raise ValueError("Incomplete export chunk")
            body.extend(chunk)

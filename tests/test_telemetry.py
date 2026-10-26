# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
import socket
from http.client import HTTPConnection, RemoteDisconnected
from pathlib import Path
from typing import TextIO

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from maas_code_reviewer import telemetry
from maas_code_reviewer.telemetry import TelemetryCollector


def post(collector, path, body=b"", headers=None, *, chunked=False):
    port = int(collector.endpoint.split(":")[1])
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            "POST",
            path,
            body=[body[: len(body) // 2], body[len(body) // 2 :]] if chunked else body,
            headers=headers or {"Content-Type": "application/x-protobuf"},
            encode_chunked=chunked,
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def raw_exchange(collector, headers, body=b"", *, shutdown_write=False):
    port = int(collector.endpoint.split(":")[1])
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(
            b"POST /v1/metrics HTTP/1.0\r\n"
            b"Content-Type: application/x-protobuf\r\n" + headers + b"\r\n" + body
        )
        if shutdown_write:
            connection.shutdown(socket.SHUT_WR)
        response = connection.makefile("rb")
        try:
            return int(response.readline().split()[1])
        finally:
            response.close()


def trace_request():
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    resource.resource.attributes.add(
        key="service.name"
    ).value.string_value = "maas-code-reviewer"
    scope = resource.scope_spans.add()
    scope.scope.name = "omp.tools"
    span = scope.spans.add(
        name="execute_tool read",
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        parent_span_id=b"\x03" * 8,
        kind=1,
        start_time_unix_nano=1700000000000000001,
        end_time_unix_nano=1700000000000000123,
    )
    span.attributes.add(key="gen_ai.tool.name").value.string_value = "read"
    span.attributes.add(key="gen_ai.tool.call.id").value.string_value = "call-42"
    span.attributes.add(key="tool.result.bytes").value.int_value = 513
    return request


def metrics_request():
    request = ExportMetricsServiceRequest()
    resource = request.resource_metrics.add()
    metric = resource.scope_metrics.add().metrics.add(
        name="gen_ai.client.token.usage", unit="{token}"
    )
    metric.sum.aggregation_temporality = 2
    metric.sum.is_monotonic = True
    point = metric.sum.data_points.add(
        as_int=9007199254740993, time_unix_nano=1700000000000000001
    )
    point.exemplars.add(
        as_int=7,
        time_unix_nano=1700000000000000001,
        trace_id=b"\x04" * 16,
        span_id=b"\x05" * 8,
    )
    point.attributes.add(key="gen_ai.token.type").value.string_value = "input"
    point.attributes.add(key="gen_ai.request.model").value.string_value = "test/model"
    return request


@pytest.mark.parametrize("chunked", [False, True])
def test_real_protobuf_batches_preserve_tool_spans_and_token_metrics(
    tmp_path, capsys, chunked
):
    traces = trace_request()
    metrics = metrics_request()
    directory = tmp_path / "artifacts" / "otel"
    with TelemetryCollector(directory) as collector:
        assert collector._server.server_address[0] == "127.0.0.1"
        for path, request, response_type in (
            ("/v1/traces", traces, ExportTraceServiceResponse),
            ("/v1/metrics", metrics, ExportMetricsServiceResponse),
        ):
            status, headers, body = post(
                collector, path, request.SerializeToString(), chunked=chunked
            )
            assert status == 200
            assert headers["Content-Type"] == "application/x-protobuf"
            assert headers["Content-Length"] == "0"
            assert response_type.FromString(body) == response_type()
        trace_lines = (directory / "traces.jsonl").read_text().splitlines()
        metric_lines = (directory / "metrics.jsonl").read_text().splitlines()
    assert len(trace_lines) == len(metric_lines) == 1
    trace_json = json.loads(trace_lines[0])
    metric_json = json.loads(metric_lines[0])
    span = trace_json["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["name"] == "execute_tool read"
    assert span["attributes"] == [
        {"key": "gen_ai.tool.name", "value": {"stringValue": "read"}},
        {"key": "gen_ai.tool.call.id", "value": {"stringValue": "call-42"}},
        {"key": "tool.result.bytes", "value": {"intValue": "513"}},
    ]
    assert span["traceId"] == "01" * 16
    assert span["spanId"] == "02" * 8
    assert span["parentSpanId"] == "03" * 8
    assert span["kind"] == 1
    assert span["startTimeUnixNano"] == "1700000000000000001"
    metric = metric_json["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    assert metric["name"] == "gen_ai.client.token.usage"
    assert metric["sum"]["dataPoints"][0]["asInt"] == "9007199254740993"
    assert metric["sum"]["aggregationTemporality"] == 2
    assert metric["sum"]["dataPoints"][0]["exemplars"][0]["traceId"] == "04" * 16
    assert metric["sum"]["dataPoints"][0]["exemplars"][0]["spanId"] == "05" * 8
    assert metric["sum"]["dataPoints"][0]["attributes"][0] == {
        "key": "gen_ai.token.type",
        "value": {"stringValue": "input"},
    }
    assert "resourceMetrics" not in trace_json
    assert "resourceSpans" not in metric_json
    assert capsys.readouterr() == ("", "")


def test_environment_lifecycle_and_reuse(tmp_path):
    collector = TelemetryCollector(tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        _ = collector.endpoint
    for iteration in range(2):
        with collector:
            port = int(collector.endpoint.removeprefix("localhost:"))
            with pytest.raises(RuntimeError, match="already running"):
                collector.__enter__()
            files = list(collector._files.values())
            worker = collector._thread
            assert worker.is_alive()
            assert post(collector, "/v1/metrics")[0] == 200
        assert not worker.is_alive()
        assert all(file.closed for file in files)
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        with pytest.raises(RuntimeError, match="not running"):
            _ = collector.environment
        assert (tmp_path / "metrics.jsonl").read_text() == "{}\n" * (iteration + 1)
        assert (tmp_path / "traces.jsonl").read_text() == ""


@pytest.mark.parametrize(
    ("path", "body", "headers", "status"),
    [
        ("/other", b"", None, 404),
        ("/v1/logs", b"", None, 404),
        ("/v1/traces?other=1", b"", None, 404),
        ("/v1/metrics", b"\xff", None, 400),
        ("/v1/traces", b"\xff", None, 400),
        ("/v1/metrics", b"{}", {"Content-Type": "application/json"}, 415),
        ("/v1/metrics", b"", {"Content-Encoding": "gzip"}, 415),
        ("/v1/metrics", b"", {"Transfer-Encoding": "gzip"}, 415),
        ("/v1/metrics", b"", {"Content-Length": "bad"}, 400),
        ("/v1/metrics", b"", {"Content-Length": "-1"}, 400),
        (
            "/v1/metrics",
            b"",
            {"Content-Length": str(telemetry._MAX_BODY_BYTES + 1)},
            413,
        ),
    ],
)
def test_invalid_exports_do_not_invent_metrics(tmp_path, path, body, headers, status):
    headers = {"Content-Type": "application/x-protobuf"} | (headers or {})
    with TelemetryCollector(tmp_path) as collector:
        assert post(collector, path, body, headers)[0] == status
    assert (tmp_path / "metrics.jsonl").read_text() == ""
    assert (tmp_path / "traces.jsonl").read_text() == ""


@pytest.mark.parametrize(
    ("headers", "body", "shutdown_write", "status"),
    [
        (b"", b"", False, 411),
        (b"Content-Length: 0\r\nContent-Length: 0\r\n", b"", False, 400),
        (b"Content-Length: 2\r\n", b"\x01", True, 400),
        (b"Content-Length: 2\r\n", b"\x01", False, 408),
        (
            b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n",
            b"0\r\n\r\n",
            False,
            400,
        ),
        (b"Transfer-Encoding: chunked\r\n", b"invalid\r\n", True, 400),
        (b"Transfer-Encoding: chunked\r\n", b"-1\r\n", True, 400),
        (b"Transfer-Encoding: chunked\r\n", b"2", True, 400),
        (b"Transfer-Encoding: chunked\r\n", b"2\r\nx", True, 400),
        (b"Transfer-Encoding: chunked\r\n", b"1\r\nx!!", True, 400),
        (b"Transfer-Encoding: chunked\r\n", b"0\r\ntruncated", True, 400),
        (
            b"Transfer-Encoding: chunked\r\n",
            f"{telemetry._MAX_BODY_BYTES + 1:x}\r\n".encode(),
            False,
            413,
        ),
    ],
)
def test_bounded_body_reads(
    tmp_path, monkeypatch, headers, body, shutdown_write, status
):
    monkeypatch.setattr(telemetry._OTLPHandler, "timeout", 0.05)
    with TelemetryCollector(tmp_path) as collector:
        actual = raw_exchange(collector, headers, body, shutdown_write=shutdown_write)
        assert actual == status
    assert (tmp_path / "metrics.jsonl").read_text() == ""


class FailingFile:
    def __init__(self, file: TextIO, operation: str) -> None:
        self.file = file
        self.operation = operation

    def write(self, value: str) -> int:
        if self.operation == "write":
            raise OSError("untrusted diagnostic contents")
        return self.file.write(value)

    def flush(self) -> None:
        if self.operation == "flush":
            raise OSError("untrusted diagnostic contents")
        self.file.flush()

    def close(self) -> None:
        self.file.close()
        if self.operation == "close":
            raise OSError("untrusted diagnostic contents")


@pytest.mark.parametrize("operation", ["write", "flush", "close"])
def test_file_errors_are_observable_and_resources_close(tmp_path, operation, capsys):
    collector = TelemetryCollector(tmp_path)
    with pytest.raises(OSError, match="OTLP telemetry collection failed") as caught:
        with collector:
            files = list(collector._files.values())
            collector._files["metrics"] = FailingFile(
                collector._files["metrics"], operation
            )
            status = post(collector, "/v1/metrics")[0]
            assert status == (200 if operation == "close" else 500)
    assert all(file.closed for file in files)
    assert "untrusted" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert capsys.readouterr() == ("", "")


def test_collector_error_adds_note_without_masking_review_exception(tmp_path):
    collector = TelemetryCollector(tmp_path)
    primary = ValueError("review failed")
    with pytest.raises(ValueError) as caught:
        with collector:
            files = list(collector._files.values())
            collector._files["traces"] = FailingFile(
                collector._files["traces"], "write"
            )
            assert post(collector, "/v1/traces")[0] == 500
            raise primary
    assert caught.value is primary
    assert caught.value.__notes__ == [
        "OTLP telemetry collection failed; diagnostic files may be partial"
    ]
    assert all(file.closed for file in files)


def test_failed_review_retains_valid_artifacts(tmp_path):
    with pytest.raises(ValueError, match="review failed"):
        with TelemetryCollector(tmp_path) as collector:
            request = trace_request().SerializeToString()
            assert post(collector, "/v1/traces", request)[0] == 200
            raise ValueError("review failed")
    document = json.loads((tmp_path / "traces.jsonl").read_text())
    span = document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["name"] == "execute_tool read"
    assert span["traceId"] == "01" * 16


def test_partial_startup_failure_closes_previously_opened_files(tmp_path, monkeypatch):
    original_open = Path.open
    opened = []

    def failing_open(path, *args, **kwargs):
        if path.name == "metrics.jsonl":
            raise OSError("cannot create metrics")
        file = original_open(path, *args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(Path, "open", failing_open)
    collector = TelemetryCollector(tmp_path)
    with pytest.raises(OSError, match="Could not start OTLP telemetry collector"):
        collector.__enter__()
    assert len(opened) == 1
    assert opened[0].closed


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_thread_start_failure_closes_server_and_files(
    tmp_path, monkeypatch, failure_type
):
    def failing_start(thread):
        raise failure_type("cannot start receiver")

    monkeypatch.setattr(telemetry.Thread, "start", failing_start)
    collector = TelemetryCollector(tmp_path)
    expected = OSError if failure_type is RuntimeError else KeyboardInterrupt
    with pytest.raises(expected):
        collector.__enter__()
    assert collector._server is None
    assert all(file.closed for file in collector._files.values())


def test_worker_errors_surface_on_exit(tmp_path, monkeypatch):
    def failing_serve(server, **kwargs):
        raise OSError("untrusted diagnostic contents")

    monkeypatch.setattr(telemetry._TelemetryServer, "serve_forever", failing_serve)
    collector = TelemetryCollector(tmp_path)
    with pytest.raises(OSError, match="OTLP telemetry collection failed"):
        with collector:
            collector._thread.join(1)
            assert not collector._thread.is_alive()
    assert all(file.closed for file in collector._files.values())


def test_unexpected_handler_errors_do_not_print_diagnostic_data(
    tmp_path, monkeypatch, capsys
):
    def failing_post(handler):
        raise OSError("untrusted diagnostic contents")

    monkeypatch.setattr(telemetry._OTLPHandler, "do_POST", failing_post)
    with pytest.raises(OSError, match="OTLP telemetry collection failed"):
        with TelemetryCollector(tmp_path) as collector:
            with pytest.raises(RemoteDisconnected):
                post(collector, "/v1/metrics")
    assert capsys.readouterr() == ("", "")


def test_chunk_extensions_and_trailers_preserve_metric_values(tmp_path):
    packet = metrics_request().SerializeToString()
    body = (
        f"{len(packet):x};source=omp\r\n".encode()
        + packet
        + b"\r\n0\r\nx-exporter: omp\r\n\r\n"
    )
    with TelemetryCollector(tmp_path) as collector:
        assert raw_exchange(collector, b"Transfer-Encoding: chunked\r\n", body) == 200
    document = json.loads((tmp_path / "metrics.jsonl").read_text())
    point = document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"][
        "dataPoints"
    ][0]
    assert point["asInt"] == "9007199254740993"
    assert point["exemplars"][0]["spanId"] == "05" * 8

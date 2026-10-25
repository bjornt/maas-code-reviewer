# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import copy
import json
import socket
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from maas_code_reviewer.otel_export import TraceExporter, annotate_traces

LABELS = {
    "run_id": "comparison / one",
    "case_id": "case-42",
    "reviewed_commit": "a" * 40,
    "landed_commit": "b" * 40,
    "repository": "team/project",
    "model": "provider/model",
    "attempt_id": "c" * 32,
}


@contextmanager
def receiver(
    *,
    status=200,
    body=b"",
    location=None,
    disconnect=False,
    delay=0,
    content_type="application/x-protobuf",
):
    """An actual HTTP endpoint decoding requests and serving OTLP responses."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            request = ExportTraceServiceRequest.FromString(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            received.append((self.path, self.headers, request))
            if disconnect:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if location:
                self.send_header("Location", location)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def batch():
    root = {
        "traceId": "a1" * 16,
        "spanId": "b2" * 8,
        "name": "review",
        "kind": 1,
        "startTimeUnixNano": "1700000000000000001",
        "endTimeUnixNano": "1700000000000000999",
        "attributes": [
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1234"}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "42"}},
            {
                "key": "gen_ai.usage.cache_read.input_tokens",
                "value": {"intValue": "900"},
            },
            {"key": "gen_ai.usage.cost", "value": {"doubleValue": 0.0123}},
            {"key": "opaque.bytes", "value": {"bytesValue": "AP8="}},
            {"key": "session.id", "value": {"stringValue": "untrusted"}},
            {"key": "session.id", "value": {"stringValue": "duplicate"}},
            {"key": "langfuse.session.id", "value": {"stringValue": "untrusted"}},
            {"key": "langfuse.trace.tags", "value": {"stringValue": "untrusted"}},
            {
                "key": "langfuse.trace.metadata.run_id",
                "value": {"stringValue": "untrusted"},
            },
        ],
        "events": [{"timeUnixNano": "1700000000000000111", "name": "tool"}],
        "links": [{"traceId": "d4" * 16, "spanId": "e5" * 8}],
        "status": {"code": 1},
    }
    child = {
        "traceId": root["traceId"],
        "spanId": "c3" * 8,
        "parentSpanId": root["spanId"],
        "name": "model",
        "startTimeUnixNano": "1700000000000000100",
        "endTimeUnixNano": "1700000000000000800",
    }
    other = {
        "traceId": "f6" * 16,
        "spanId": "07" * 8,
        "parentSpanId": "",
        "name": "tool",
    }
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "omp"}}
                    ]
                },
                "scopeSpans": [{"scope": {"name": "review"}, "spans": [root, child]}],
            },
            {"scopeSpans": [{"spans": [other]}]},
        ]
    }


def spans(document):
    return [
        span
        for resource in document["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


def trace_file(tmp_path, document=None):
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps(batch() if document is None else document) + "\n")
    return path


def test_annotation_replaces_trusted_labels_without_changing_source(tmp_path):
    document = batch()
    source = trace_file(tmp_path, document)
    original = source.read_bytes()
    derived = tmp_path / "derived" / "langfuse-traces.jsonl"
    assert annotate_traces(source, derived, LABELS) == 3
    assert source.read_bytes() == original
    annotated = json.loads(derived.read_text())
    expected = copy.deepcopy(document)
    for original_span, enriched in zip(spans(expected), spans(annotated), strict=True):
        trusted = {
            "session.id",
            "langfuse.session.id",
            "langfuse.trace.tags",
            *(f"langfuse.trace.metadata.{key}" for key in LABELS),
        }
        original_attributes = original_span.pop("attributes", [])
        enriched_attributes = enriched.pop("attributes")
        assert enriched == original_span
        keys = [attribute["key"] for attribute in enriched_attributes]
        assert len(keys) == len(set(keys))
        attributes = {
            attribute["key"]: attribute["value"] for attribute in enriched_attributes
        }
        assert [
            attribute
            for attribute in enriched_attributes
            if attribute["key"] not in trusted
        ] == [
            attribute
            for attribute in original_attributes
            if attribute["key"] not in trusted
        ]
        for key, value in LABELS.items():
            assert attributes[f"langfuse.trace.metadata.{key}"] == {
                "stringValue": value
            }
        assert "session.id" not in attributes
        assert attributes["langfuse.session.id"] == {"stringValue": LABELS["run_id"]}
        assert attributes["langfuse.trace.tags"] == {
            "arrayValue": {"values": [{"stringValue": "model-review-comparison"}]}
        }
    second = tmp_path / "second.jsonl"
    assert annotate_traces(derived, second, LABELS) == 3
    assert second.read_bytes() == derived.read_bytes()


@pytest.mark.parametrize("estimate", [0.0, 0.05])
def test_omp_cost_mapping_excludes_agent_totals_and_preserves_explicit_cost(
    tmp_path, estimate
):
    document = batch()
    agent, model, other = spans(document)
    agent["attributes"] = [
        {
            "key": "omp.gen_ai.agent.cost.estimated_usd.total",
            "value": {"doubleValue": 9.0},
        }
    ]
    model["attributes"] = [
        {"key": "omp.gen_ai.cost.estimated_usd", "value": {"doubleValue": estimate}},
        {
            "key": "langfuse.observation.metadata.cost_source",
            "value": {"stringValue": "untrusted"},
        },
    ]
    other["attributes"] = [
        {"key": "gen_ai.usage.cost", "value": {"doubleValue": 0.7}},
        {"key": "omp.gen_ai.cost.estimated_usd", "value": {"doubleValue": 0.1}},
    ]
    source = trace_file(tmp_path, document)
    original = source.read_bytes()
    derived = tmp_path / "annotated.jsonl"
    annotate_traces(source, derived, LABELS)
    mapped = [
        {attribute["key"]: attribute["value"] for attribute in span["attributes"]}
        for span in spans(json.loads(derived.read_text()))
    ]
    assert "gen_ai.usage.cost" not in mapped[0]
    assert mapped[1]["gen_ai.usage.cost"] == {"doubleValue": estimate}
    assert mapped[1]["langfuse.observation.metadata.cost_source"] == {
        "stringValue": "omp_estimate"
    }
    assert mapped[2]["gen_ai.usage.cost"] == {"doubleValue": 0.7}
    assert source.read_bytes() == original
    again = tmp_path / "annotated-again.jsonl"
    annotate_traces(derived, again, LABELS)
    assert again.read_bytes() == derived.read_bytes()


@pytest.mark.parametrize("contents", [None, "", "\n  \n", "{}\n"])
def test_missing_and_empty_trace_sources_do_not_fabricate_spans(tmp_path, contents):
    source = tmp_path / "absent.jsonl"
    if contents is not None:
        source.write_text(contents)
    destination = tmp_path / "nested" / "derived.jsonl"
    assert annotate_traces(source, destination, LABELS) == 0
    assert destination.read_text() == ("{}\n" if contents == "{}\n" else "")
    assert source.exists() == (contents is not None)


@pytest.mark.parametrize("alias", ["same", "hardlink", "symlink"])
def test_annotation_refuses_to_overwrite_raw_source(tmp_path, alias):
    source = trace_file(tmp_path)
    original = source.read_bytes()
    destination = source if alias == "same" else tmp_path / "alias.jsonl"
    if alias == "hardlink":
        destination.hardlink_to(source)
    elif alias == "symlink":
        destination.symlink_to(source)
    with pytest.raises(ValueError, match="separate destination"):
        annotate_traces(source, destination, LABELS)
    assert source.read_bytes() == original


def test_annotation_requires_run_metadata_and_reports_write_errors(tmp_path):
    source = trace_file(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        annotate_traces(source, tmp_path / "derived.jsonl", {})
    destination = tmp_path / "directory"
    destination.mkdir()
    with pytest.raises(OSError, match="annotated OTLP"):
        annotate_traces(source, destination, LABELS)


@pytest.mark.parametrize(
    "contents",
    [
        "synthetic-private-invalid-json",
        "null",
        "[]",
        '{"resourceSpans":42}',
        '{"unknown":"synthetic-private-value"}',
        '{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"invalid"}]}]}]}',
        '{"resourceSpans":[{"scopeSpans":[{"spans":[{"spanId":42}]}]}]}',
    ],
)
def test_invalid_batches_fail_safely_for_annotation_and_export(tmp_path, contents):
    source = tmp_path / "invalid.jsonl"
    source.write_text(contents)
    exporter = TraceExporter.from_environment(
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost"}
    )
    for operation in (
        lambda: annotate_traces(source, tmp_path / "derived.jsonl", LABELS),
        lambda: exporter.export_file(source),
    ):
        with pytest.raises(ValueError, match="Invalid OTLP trace JSON batch") as error:
            operation()
        assert "synthetic-private" not in str(error.value)
        assert "synthetic-private" not in "".join(
            traceback.format_exception(error.value)
        )


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"OTEL_TRACES_EXPORTER": "unsupported"},
        {"OTEL_EXPORTER_OTLP_ENDPOINT": ""},
        {"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://localhost/v1/metrics"},
        {"OTEL_TRACES_EXPORTER": " none ", "OTEL_EXPORTER_OTLP_ENDPOINT": "invalid"},
        {"OTEL_SDK_DISABLED": " TrUe ", "OTEL_EXPORTER_OTLP_ENDPOINT": "invalid"},
    ],
)
def test_disabled_or_unconfigured_export_does_nothing(environment):
    assert TraceExporter.from_environment(environment) is None


def test_environment_defaults_and_specific_endpoint_precedence(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost/collector///")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    assert (
        TraceExporter.from_environment().endpoint
        == "http://localhost/collector/v1/traces"
    )
    exporter = TraceExporter.from_environment(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "invalid generic endpoint",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://localhost:443/custom/path/",
        }
    )
    assert exporter.endpoint == "https://localhost:443/custom/path/"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("OTEL_TRACES_EXPORTER", "console", "otlp trace exporter"),
        ("OTEL_TRACES_EXPORTER", "otlp,console", "otlp trace exporter"),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc", "http/protobuf"),
        ("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/json", "http/protobuf"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "Authorization", "headers"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "bad%20name=value", "headers"),
        (
            "OTEL_EXPORTER_OTLP_HEADERS",
            "Authorization=synthetic%0D%0Ainjection",
            "headers",
        ),
        ("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=synthetic%00value", "headers"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=synthetic%7Fvalue", "headers"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=%E2%82%AC", "headers"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=value,", "headers"),
    ],
)
def test_invalid_export_configuration_is_eager_and_credential_safe(
    name, value, message
):
    with pytest.raises(ValueError, match=message) as error:
        TraceExporter.from_environment(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost", name: value}
        )
    assert value not in str(error.value)
    assert "synthetic" not in repr(error.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "not-a-url",
        "ftp://localhost/traces",
        "http:///missing-host",
        "http://synthetic-user:synthetic-private@localhost/traces",
        "http://localhost/traces?token=synthetic-private",
        "http://localhost/traces?",
        "http://localhost/traces#synthetic-private",
        "http://localhost/traces#",
        "http://localhost:invalid/traces",
        "http://localhost:65536/traces",
        "http://[invalid/traces",
        "http://localhost/ synthetic-private",
        "http://localhost/\nsynthetic-private",
        "http://localhost/\u00e9",
    ],
)
def test_unsafe_or_invalid_endpoints_fail_without_disclosing_values(endpoint):
    with pytest.raises(ValueError, match="Invalid OTLP trace endpoint") as error:
        TraceExporter.from_environment({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint})
    assert endpoint not in str(error.value)
    assert "synthetic-private" not in repr(error.value)
    assert "synthetic-private" not in "".join(traceback.format_exception(error.value))


def test_real_export_preserves_ids_times_usage_cost_and_all_span_labels(
    tmp_path, capsys
):
    source = trace_file(tmp_path)
    derived = tmp_path / "derived.jsonl"
    annotate_traces(source, derived, LABELS)
    # Empty batches are not uploads; independent batches retain their own IDs.
    derived.write_text("\n{}\n" + derived.read_text() * 2)
    with receiver() as (endpoint, received):
        exporter = TraceExporter.from_environment(
            {
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": (
                    endpoint + "/api/public/otel/v1/traces"
                ),
                "OTEL_EXPORTER_OTLP_ENDPOINT": "invalid",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
                "OTEL_EXPORTER_OTLP_HEADERS": "invalid-header",
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS": (
                    "Authorization=Basic%20synthetic-placeholder==,"
                    "ingestion-version=4,x-once=a%2520b,x%2Dkey=hello%2Cworld%3Dvalue"
                ),
            }
        )
        assert "synthetic-placeholder" not in repr(exporter)
        assert "Authorization" not in repr(exporter)
        assert exporter.export_file(derived) == 6
    assert len(received) == 2
    for path, headers, request in received:
        assert path == "/api/public/otel/v1/traces"
        assert headers["Authorization"] == "Basic synthetic-placeholder=="
        assert headers["Ingestion-Version"] == "4"
        assert headers["X-Once"] == "a%20b"
        assert headers["X-Key"] == "hello,world=value"
        assert headers["Content-Type"] == "application/x-protobuf"
        root, child = request.resource_spans[0].scope_spans[0].spans
        assert root.trace_id == bytes.fromhex("a1" * 16)
        assert root.span_id == bytes.fromhex("b2" * 8)
        assert child.trace_id == root.trace_id
        assert child.parent_span_id == root.span_id
        assert root.start_time_unix_nano == 1700000000000000001
        assert root.end_time_unix_nano == 1700000000000000999
        assert root.events[0].time_unix_nano == 1700000000000000111
        assert root.links[0].trace_id == bytes.fromhex("d4" * 16)
        assert root.links[0].span_id == bytes.fromhex("e5" * 8)
        attributes = {attribute.key: attribute.value for attribute in root.attributes}
        assert attributes["gen_ai.usage.input_tokens"].int_value == 1234
        assert attributes["gen_ai.usage.output_tokens"].int_value == 42
        assert attributes["gen_ai.usage.cache_read.input_tokens"].int_value == 900
        assert attributes["gen_ai.usage.cost"].double_value == 0.0123
        assert attributes["opaque.bytes"].bytes_value == b"\x00\xff"
        for resource in request.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    values = {
                        attribute.key: attribute.value for attribute in span.attributes
                    }
                    for key, value in LABELS.items():
                        attribute = values[f"langfuse.trace.metadata.{key}"]
                        assert attribute.string_value == value
                    assert (
                        values["langfuse.session.id"].string_value == LABELS["run_id"]
                    )
                    assert "session.id" not in values
        child_keys = {attribute.key for attribute in child.attributes}
        assert not any(key.startswith("gen_ai.") for key in child_keys)
    assert capsys.readouterr() == ("", "")


def test_generic_headers_and_explicit_empty_specific_headers(tmp_path):
    source = trace_file(tmp_path)
    with receiver() as (endpoint, received):
        environment = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint + "/collector/",
            "OTEL_EXPORTER_OTLP_HEADERS": (
                "Authorization=Basic%20synthetic-placeholder=="
            ),
        }
        assert TraceExporter.from_environment(environment).export_file(source) == 3
        environment["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = ""
        assert TraceExporter.from_environment(environment).export_file(source) == 3
    assert received[0][0] == "/collector/v1/traces"
    assert received[0][1]["Authorization"] == "Basic synthetic-placeholder=="
    assert "Authorization" not in received[1][1]


def test_empty_files_have_no_network_side_effects_and_missing_files_fail(tmp_path):
    with receiver() as (endpoint, received):
        exporter = TraceExporter.from_environment(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}
        )
        source = tmp_path / "empty.jsonl"
        source.write_text("\n{}\n")
        assert exporter.export_file(source) == 0
        with pytest.raises(OSError, match="Could not read OTLP trace batches"):
            exporter.export_file(tmp_path / "missing.jsonl")
        assert received == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_never_forward_credentials_or_report_success(tmp_path, status):
    source = trace_file(tmp_path)
    with receiver() as (destination, destination_requests):
        with receiver(
            status=status,
            body=b"synthetic-private-server-response",
            location=destination + "/stolen?token=synthetic-private",
        ) as (endpoint, received):
            exporter = TraceExporter.from_environment(
                {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
                    "OTEL_EXPORTER_OTLP_HEADERS": (
                        "Authorization=Basic%20synthetic-placeholder=="
                    ),
                }
            )
            with pytest.raises(OSError, match=f"HTTP status {status}") as error:
                exporter.export_file(source)
            assert "synthetic" not in str(error.value)
            assert "stolen" not in repr(error.value)
        assert len(received) == 1
        assert destination_requests == []


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_http_failures_are_safe_and_not_retried(tmp_path, status):
    source = trace_file(tmp_path)
    with receiver(status=status, body=b"synthetic-private-server-response") as (
        endpoint,
        received,
    ):
        exporter = TraceExporter.from_environment(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}
        )
        with pytest.raises(OSError, match=f"HTTP status {status}") as error:
            exporter.export_file(source)
    assert len(received) == 1
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize("response_kind", ["partial", "malformed", "oversized"])
def test_invalid_or_rejected_protobuf_response_fails(tmp_path, response_kind):
    response = ExportTraceServiceResponse()
    response.partial_success.rejected_spans = 1
    response.partial_success.error_message = "synthetic-private-server-message"
    bodies = {
        "partial": response.SerializeToString(),
        "malformed": b"\xffsynthetic-private-invalid-protobuf",
        "oversized": b"x" * (1024 * 1024 + 1),
    }
    with receiver(body=bodies[response_kind]) as (endpoint, received):
        exporter = TraceExporter.from_environment(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}
        )
        with pytest.raises(OSError) as error:
            exporter.export_file(trace_file(tmp_path))
    assert len(received) == 1
    assert "synthetic" not in str(error.value)
    assert "server-message" not in repr(error.value)


@pytest.mark.parametrize(
    "acknowledgement",
    [{}, {"id": "queued-job", "data": {}, "attemptsMade": 0}],
)
def test_json_acknowledgements_accept_queued_ingestion(tmp_path, acknowledgement):
    with receiver(
        body=json.dumps(acknowledgement).encode(),
        content_type="application/json; charset=utf-8",
    ) as (endpoint, received):
        exporter = TraceExporter.from_environment(
            {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint}
        )
        assert exporter.export_file(trace_file(tmp_path)) == 3
    assert len(received) == 1


@pytest.mark.parametrize(
    "body",
    [
        b'{"partialSuccess":{"rejectedSpans":"1","errorMessage":"synthetic-private"}}',
        b"synthetic-invalid-json",
        b"[]",
    ],
)
def test_invalid_or_rejected_json_acknowledgements_fail_safely(tmp_path, body):
    with receiver(body=body, content_type="application/json") as (endpoint, received):
        exporter = TraceExporter.from_environment(
            {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint}
        )
        with pytest.raises(OSError) as error:
            exporter.export_file(trace_file(tmp_path))
    assert len(received) == 1
    assert "synthetic" not in str(error.value)


def test_zero_rejections_can_return_a_protobuf_warning_without_leaking_it(
    tmp_path, capsys
):
    response = ExportTraceServiceResponse()
    response.partial_success.error_message = "synthetic-private-warning"
    with receiver(body=response.SerializeToString()) as (endpoint, received):
        exporter = TraceExporter.from_environment(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}
        )
        assert exporter.export_file(trace_file(tmp_path)) == 3
    assert len(received) == 1
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", ["disconnect", "timeout", "tls"])
def test_real_transport_errors_are_safe_and_not_retried(tmp_path, failure):
    with receiver(
        disconnect=failure == "disconnect", delay=0.1 if failure == "timeout" else 0
    ) as (endpoint, received):
        if failure == "tls":
            endpoint = endpoint.replace("http://", "https://")
        exporter = TraceExporter(
            endpoint, {"authorization": "synthetic-private-value"}, timeout=0.02
        )
        with pytest.raises(
            OSError, match="OTLP trace export transport failed"
        ) as error:
            exporter.export_file(trace_file(tmp_path))
    assert len(received) == (0 if failure == "tls" else 1)
    assert "synthetic" not in repr(error.value)


def test_connection_refusal_does_not_expose_endpoint_or_headers(tmp_path):
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{unused.getsockname()[1]}/private-path"
        exporter = TraceExporter(endpoint, {"authorization": "synthetic-private-value"})
        with pytest.raises(OSError) as error:
            exporter.export_file(trace_file(tmp_path))
    assert "private-path" not in str(error.value)
    assert "synthetic" not in str(error.value)

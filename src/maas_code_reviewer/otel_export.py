# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Enrich retained trace batches and export them from the trusted host only."""

import json
import os
from base64 import b64encode
from collections.abc import Iterator, Mapping
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from string import ascii_letters, digits, hexdigits
from typing import Any
from urllib.parse import unquote, urlsplit

from google.protobuf.json_format import ParseDict, ParseError
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

_MAX_RESPONSE_BYTES = 1024 * 1024
_HEADER_CHARACTERS = frozenset("!#$%&'*+.^_`|~-" + ascii_letters + digits)
_ID_LENGTHS = {"traceId": 32, "spanId": 16, "parentSpanId": 16}


def annotate_traces(source: Path, destination: Path, metadata: dict[str, str]) -> int:
    """Write derived batches with trusted comparison labels on every span.

    The source is never rewritten, including when no spans were collected.
    Existing values of the trusted keys are replaced, rather than duplicated.
    """
    if source.resolve() == destination.resolve() or (
        source.exists() and destination.exists() and source.samefile(destination)
    ):
        raise ValueError("OTLP trace annotation requires a separate destination")
    if "run_id" not in metadata:
        raise ValueError("OTLP trace annotation requires run_id metadata")
    attributes: dict[str, dict[str, Any]] = {
        f"langfuse.trace.metadata.{key}": {"stringValue": value}
        for key, value in sorted(metadata.items())
    }
    attributes["langfuse.session.id"] = {"stringValue": metadata["run_id"]}
    attributes["langfuse.trace.tags"] = {
        "arrayValue": {"values": [{"stringValue": "model-review-comparison"}]}
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        input_file = source.open(encoding="utf-8")
    except FileNotFoundError:
        destination.write_text("", encoding="utf-8")
        return 0
    count = 0
    try:
        with input_file, destination.open("w", encoding="utf-8") as output:
            for line in input_file:
                if not line.strip():
                    continue
                document = json.loads(line)
                # Validate the whole OTLP batch, including its hierarchy and IDs.
                _request(document)
                for span in _spans(document):
                    span_attributes = span.get("attributes", [])
                    estimate = next(
                        (
                            attribute["value"]
                            for attribute in span_attributes
                            if attribute["key"] == "omp.gen_ai.cost.estimated_usd"
                        ),
                        None,
                    )
                    if estimate is not None and not any(
                        attribute["key"] == "gen_ai.usage.cost"
                        for attribute in span_attributes
                    ):
                        # Only per-call estimates, never the agent aggregate:
                        # Langfuse sums generation costs across a trace.
                        span_attributes = [
                            attribute
                            for attribute in span_attributes
                            if attribute["key"]
                            != "langfuse.observation.metadata.cost_source"
                        ]
                        span_attributes.extend(
                            [
                                {"key": "gen_ai.usage.cost", "value": estimate},
                                {
                                    "key": "langfuse.observation.metadata.cost_source",
                                    "value": {"stringValue": "omp_estimate"},
                                },
                            ]
                        )
                    span["attributes"] = [
                        attribute
                        for attribute in span_attributes
                        if attribute["key"] not in attributes
                        and attribute["key"] != "session.id"
                    ] + [
                        {"key": key, "value": value}
                        for key, value in attributes.items()
                    ]
                    count += 1
                output.write(json.dumps(document, separators=(",", ":")) + "\n")
    except (ValueError, TypeError, AttributeError, KeyError, ParseError):
        raise ValueError("Invalid OTLP trace JSON batch") from None
    except OSError:
        raise OSError("Could not write annotated OTLP trace batches") from None
    return count


class TraceExporter:
    """A credential-private HTTP/protobuf trace exporter with no redirects.

    Authentication lives only in host memory. The public endpoint cannot contain
    credentials, a query, or a fragment, and the object representation is opaque.
    """

    def __init__(
        self, endpoint: str, headers: Mapping[str, str], timeout: float = 10.0
    ) -> None:
        self.endpoint = endpoint
        self._headers = dict(headers)
        self._timeout = timeout

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "TraceExporter | None":
        environment = os.environ if environment is None else environment
        exporter = environment.get("OTEL_TRACES_EXPORTER", "otlp").strip().lower()
        if (
            environment.get("OTEL_SDK_DISABLED", "false").strip().lower() == "true"
            or exporter == "none"
        ):
            return None
        specific = environment.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        generic = environment.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        endpoint = specific
        if not endpoint:
            if not generic:
                return None
            endpoint = generic.rstrip("/") + "/v1/traces"
        if exporter != "otlp":
            raise ValueError("Only the otlp trace exporter is supported")
        protocol = environment.get(
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
            environment.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
        )
        if protocol != "http/protobuf":
            raise ValueError("OTLP trace export requires http/protobuf")
        try:
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or "?" in endpoint
                or "#" in endpoint
                or any(
                    ord(character) <= 32 or ord(character) >= 127
                    for character in endpoint
                )
            ):
                raise ValueError
            # Accessing port validates malformed or out-of-range port values.
            _ = parsed.port
        except ValueError:
            raise ValueError("Invalid OTLP trace endpoint") from None
        headers = _headers(
            environment.get(
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
                environment.get("OTEL_EXPORTER_OTLP_HEADERS", ""),
            )
        )
        return cls(endpoint, headers)

    def export_file(self, path: Path) -> int:
        """Export each nonempty batch, failing rather than counting rejected spans."""
        count = 0
        for request in _requests(path):
            spans = sum(
                len(scope.spans)
                for resource in request.resource_spans
                for scope in resource.scope_spans
            )
            if spans:
                self._send(request.SerializeToString())
                count += spans
        return count

    def _send(self, body: bytes) -> None:
        parsed = urlsplit(self.endpoint)
        hostname = parsed.hostname
        assert hostname is not None
        connection_type = (
            HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        )
        connection = connection_type(hostname, parsed.port, timeout=self._timeout)
        headers = {**self._headers, "content-type": "application/x-protobuf"}
        try:
            connection.request("POST", parsed.path or "/", body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type", "").split(";", 1)[0]
            data = response.read(_MAX_RESPONSE_BYTES + 1) if status == 200 else b""
        except (OSError, HTTPException, ValueError):
            raise OSError("OTLP trace export transport failed") from None
        finally:
            connection.close()
        if status != 200:
            raise OSError(f"OTLP trace export received HTTP status {status}")
        if len(data) > _MAX_RESPONSE_BYTES:
            raise OSError("OTLP trace export response exceeds size limit")
        try:
            if content_type.strip().lower() == "application/json":
                document = json.loads(data)
                if not isinstance(document, dict):
                    raise ValueError("Invalid OTLP response object")
                # Some OTLP backends acknowledge queued ingestion with extra
                # JSON fields. Preserve known partial-rejection semantics.
                response_message = ParseDict(
                    document, ExportTraceServiceResponse(), ignore_unknown_fields=True
                )
            else:
                response_message = ExportTraceServiceResponse.FromString(data)
        except (DecodeError, ParseError, ValueError, TypeError):
            raise OSError("Invalid OTLP trace export response") from None
        if response_message.partial_success.rejected_spans:
            raise OSError("OTLP trace export rejected spans")


def _headers(value: str) -> dict[str, str]:
    headers = {}
    if not value:
        return headers
    for item in value.split(","):
        key, separator, encoded = item.strip().partition("=")
        key, decoded = unquote(key), unquote(encoded)
        if (
            not separator
            or not key
            or any(character not in _HEADER_CHARACTERS for character in key)
            or any(ord(character) < 32 or ord(character) > 255 for character in decoded)
            or "\x7f" in decoded
        ):
            raise ValueError("Invalid OTLP trace headers")
        headers[key.lower()] = decoded
    return headers


def _spans(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for resource in document.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []):
            yield from scope.get("spans", [])


def _protobuf_json(value: Any) -> Any:
    """Convert OTLP JSON's hex IDs, not timestamps or attribute byte values."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key in _ID_LENGTHS:
                if not isinstance(child, str) or (
                    child
                    and (
                        len(child) != _ID_LENGTHS[key]
                        or any(character not in hexdigits for character in child)
                    )
                ):
                    raise ValueError("Invalid OTLP trace identifier")
                result[key] = b64encode(bytes.fromhex(child)).decode("ascii")
            else:
                result[key] = _protobuf_json(child)
        return result
    if isinstance(value, list):
        return [_protobuf_json(child) for child in value]
    return value


def _request(document: dict[str, Any]) -> ExportTraceServiceRequest:
    if not isinstance(document, dict):
        raise ValueError("Invalid OTLP trace JSON batch")
    return ParseDict(_protobuf_json(document), ExportTraceServiceRequest())


def _requests(path: Path) -> Iterator[ExportTraceServiceRequest]:
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    yield _request(json.loads(line))
    except (ValueError, TypeError, AttributeError, ParseError):
        raise ValueError("Invalid OTLP trace JSON batch") from None
    except OSError:
        raise OSError("Could not read OTLP trace batches") from None

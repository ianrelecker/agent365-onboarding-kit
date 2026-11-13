"""Agent 365 spans for a third-party agent.

Direct OTLP/HTTP+JSON is the documented fallback for a stack that does not use
the Microsoft OpenTelemetry Distro. Agent 365 drops spans that omit the
mandatory attributes, and a 200 response can still report every span as
rejected. This module emits the mandatory attributes and refuses to treat a
rejected result as success.

Demo mode prints spans locally. It does not send them anywhere.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode, Tracer

from identity import AgentIdentity, IdentityError

OBSERVABILITY_SCOPE = "9b975845-388f-4429-889e-eab1ef63949c/.default"
INGEST_HOST = "agent365.svc.cloud.microsoft"
_provider: TracerProvider | None = None
_exporter: "Agent365JsonExporter | None" = None


class TelemetryExportError(RuntimeError):
    """Agent 365 did not accept the spans, or the export call failed."""


def reset_telemetry() -> None:
    """Drop the process-local provider. Used by tests and by a reconfigure."""
    global _provider, _exporter
    if _provider is not None:
        _provider.shutdown()
    _provider = None
    _exporter = None


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def trace_content_enabled(environ: Mapping[str, str], identity: AgentIdentity) -> bool:
    raw = environ.get("AGENT365_TRACE_CONTENT", "").strip()
    if raw:
        return _truthy(raw)
    return identity.demo_mode


def redact_text(text: str, limit: int = 500) -> str:
    import re

    cleaned = re.sub(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*", "bearer [redacted]", text)
    cleaned = re.sub(
        r"eyJ[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}",
        "[redacted-jwt]",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\bsk-[A-Za-z0-9]{8,}\b", "[redacted]", cleaned)
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


def message_json(role: str, content: str, include_content: bool) -> str:
    text = redact_text(content) if include_content else "[redacted]"
    return json.dumps([{"role": role, "content": text}], separators=(",", ":"))


def arguments_json(arguments: Mapping, include_content: bool) -> str:
    if not include_content:
        return json.dumps({"redacted": True}, separators=(",", ":"))
    safe: dict[str, object] = {}
    for key, value in arguments.items():
        safe[str(key)] = redact_text(value) if isinstance(value, str) else value
    return json.dumps(safe, separators=(",", ":"))


def run_attributes(identity: AgentIdentity, environ: Mapping[str, str], conversation_id: str) -> dict[str, str]:
    """Attributes that must be copied onto every span in the run."""
    if identity.demo_mode:
        caller = identity.caller_object_id or "demo-caller"
        tenant = "demo"
        agent_id = "demo"
        blueprint_id = "demo"
    else:
        caller = identity.caller_object_id
        if not caller:
            raise TelemetryExportError(
                "AGENT365_CALLER_OBJECT_ID is required. Agent 365 drops spans that omit user.id. "
                "Use the sponsor's Entra object id for an autonomous run, or the signed-in user's object id."
            )
        tenant = identity.tenant_id
        agent_id = identity.agent_client_id
        blueprint_id = identity.blueprint_client_id
    server = environ.get("AGENT365_SERVER_ADDRESS", "").strip() or socket.gethostname() or "localhost"
    port = environ.get("AGENT365_SERVER_PORT", "").strip() or "443"
    if not port.isdigit():
        raise TelemetryExportError("AGENT365_SERVER_PORT must be a number")
    channel = environ.get("AGENT365_CHANNEL", "").strip() or "web"
    attributes = {
        "microsoft.tenant.id": tenant,
        "gen_ai.agent.id": agent_id,
        "gen_ai.agent.name": identity.agent_name,
        "microsoft.a365.agent.blueprint.id": blueprint_id,
        "user.id": caller,
        "client.address": environ.get("AGENT365_CLIENT_ADDRESS", "").strip() or "0.0.0.0",
        "gen_ai.conversation.id": conversation_id,
        "microsoft.channel.name": channel,
        "server.address": server,
        "server.port": port,
    }
    if identity.caller_upn:
        attributes["user.email"] = identity.caller_upn
    return attributes


def new_conversation_id() -> str:
    return str(uuid.uuid4())


def ingest_url(tenant_id: str, agent_client_id: str) -> str:
    return (
        f"https://{INGEST_HOST}/observabilityService/tenants/{tenant_id}"
        f"/otlp/agents/{agent_client_id}/traces?api-version=1"
    )


def spans_to_otlp_json(spans: Sequence) -> dict:
    encoded = []
    for span in spans:
        parent = ""
        if span.parent is not None and getattr(span.parent, "is_valid", True):
            parent = format(span.parent.span_id, "016x")
        status_code = int(getattr(span.status.status_code, "value", span.status.status_code))
        attributes = []
        for key, value in span.attributes.items():
            if value is None or value == "":
                continue
            attributes.append({"key": str(key), "value": {"stringValue": str(value)}})
        encoded.append(
            {
                "traceId": format(span.context.trace_id, "032x"),
                "spanId": format(span.context.span_id, "016x"),
                "parentSpanId": parent,
                "name": span.name,
                "kind": 1,
                "startTimeUnixNano": str(span.start_time),
                "endTimeUnixNano": str(span.end_time),
                "status": {"code": status_code},
                "attributes": attributes,
            }
        )
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "scope": {"name": "sample_agent", "version": "1.0.0"},
                        "spans": encoded,
                    }
                ]
            }
        ]
    }


class CompactConsoleExporter(SpanExporter):
    """One line per span for a local dry run. Not an Agent 365 export."""

    def export(self, spans: Sequence) -> SpanExportResult:
        for span in spans:
            status = getattr(getattr(span.status, "status_code", None), "name", "UNSET")
            details = []
            attributes = span.attributes or {}
            for key in ("gen_ai.tool.name", "error.type", "gen_ai.tool.call.result"):
                if key in attributes:
                    value = str(attributes[key])
                    if len(value) > 180:
                        value = value[:180] + "…"
                    details.append(f"{key}={value}")
            suffix = (" " + " ".join(details)) if details else ""
            print(f"[span] {span.name} status={status}{suffix}")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


class Agent365JsonExporter(SpanExporter):
    """POST OTLP/HTTP+JSON and fail when Agent 365 reports a span as rejected."""

    def __init__(self, url: str, token_provider: Callable[[], str], transport: Callable | None = None):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != INGEST_HOST:
            raise TelemetryExportError("refusing to send Agent 365 telemetry to an unexpected host")
        self._url = url
        self._token_provider = token_provider
        self._transport = transport or _post_json
        self.error: str | None = None

    def export(self, spans: Sequence) -> SpanExportResult:
        if not spans:
            return SpanExportResult.SUCCESS
        try:
            token = self._token_provider()
            body = spans_to_otlp_json(spans)
            status, payload = self._transport(self._url, body, token)
        except IdentityError as exc:
            self.error = str(exc)
            return SpanExportResult.FAILURE
        except TelemetryExportError as exc:
            self.error = str(exc)
            return SpanExportResult.FAILURE
        problem = _rejection_reason(status, payload)
        if problem:
            self.error = problem
            return SpanExportResult.FAILURE
        self.error = None
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def _rejection_reason(status: int, payload: object) -> str | None:
    """Return why this response is not ingestion, or None when a sink accepted spans.

    HTTP 200 is not success. Agent 365 can return 200 with ``rejectedSpans``
    left at 0 while every sink status is ``rejected`` (for example
    ``tenant_not_licensed``). ``not_routed`` is not acceptance either.
    """
    if status != 200:
        detail = _payload_detail(payload)
        if detail:
            return f"Agent 365 telemetry export returned HTTP {status}: {detail}"
        return f"Agent 365 telemetry export returned HTTP {status}"
    if not isinstance(payload, dict):
        return "Agent 365 telemetry export returned an unexpected body"

    partial = payload.get("partialSuccess")
    if isinstance(partial, dict):
        message = str(partial.get("errorMessage") or "").strip()
        if message:
            return "Agent 365 telemetry export reported an error: " + redact_text(message, limit=200)
        try:
            rejected_count = int(partial.get("rejectedSpans") or 0)
        except (TypeError, ValueError):
            rejected_count = 0
        if rejected_count:
            return (
                "Agent 365 accepted the HTTP request and rejected "
                f"{rejected_count} spans. A 200 response is not ingestion."
            )

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return (
            "Agent 365 returned HTTP 200 without per-span results. "
            "A 200 response is not ingestion."
        )
    reasons: list[str] = []
    sent = 0
    for result in results:
        if not isinstance(result, dict):
            reasons.append("result was not an object")
            continue
        sinks = result.get("sinks")
        if not isinstance(sinks, dict) or not sinks:
            reasons.append("result had no sinks")
            continue
        for name, sink in sinks.items():
            if not isinstance(sink, dict):
                reasons.append(f"{name}: unexpected sink")
                continue
            sink_status = sink.get("status")
            if sink_status == "sent":
                sent += 1
            elif sink_status == "rejected":
                reason = sink.get("reason") or "rejected"
                reasons.append(f"{name}: {reason}")
            elif sink_status == "not_routed":
                continue
            else:
                reasons.append(f"{name}: unexpected status {sink_status}")
    if reasons:
        return (
            "Agent 365 accepted the HTTP request and rejected the spans "
            f"({', '.join(reasons)}). A 200 response is not ingestion."
        )
    if sent == 0:
        return (
            "Agent 365 returned HTTP 200 and did not mark any span as sent. "
            "A 200 response is not ingestion."
        )
    return None


def _payload_detail(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        detail = str(error.get("message") or error.get("code") or "")
    elif error:
        detail = str(error)
    else:
        detail = str(payload.get("message") or "")
    return redact_text(detail, limit=200) if detail else ""


def _post_json(url: str, body: dict, token: str) -> tuple[int, dict]:
    if not token or token == "DEMO-TOKEN-NOT-A-REAL-TOKEN":
        raise TelemetryExportError("refusing to export telemetry without a real access token")
    raw = json.dumps(body).encode("utf-8")
    if len(raw) > 1024 * 1024:
        raise TelemetryExportError("telemetry request body exceeds 1 MiB")
    request = urllib.request.Request(url, data=raw, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    status, text, headers = _read_export(request)
    if status in {429, 503}:
        time.sleep(_retry_delay(headers))
        status, text, _headers = _read_export(request)
    try:
        payload = json.loads(text.decode("utf-8")) if text else {}
    except (UnicodeError, json.JSONDecodeError):
        payload = {}
    return status, payload


def _read_export(request: urllib.request.Request) -> tuple[int, bytes, object]:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(1024 * 1024), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1024 * 1024), exc.headers
    except urllib.error.URLError as exc:
        raise TelemetryExportError("Agent 365 telemetry endpoint was unreachable") from exc


def _retry_delay(headers: object) -> float:
    raw = ""
    if headers is not None:
        try:
            raw = headers.get("Retry-After") or ""
        except AttributeError:
            raw = ""
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = 1.0
    if delay < 0:
        delay = 0.0
    return min(delay, 2.0)


def configure_telemetry(
    identity: AgentIdentity,
    environ: Mapping[str, str],
    token_provider: Callable[[], str] | None = None,
    span_exporter: SpanExporter | None = None,
) -> Tracer:
    """Install the process-local tracer. Later calls replace it."""
    global _provider, _exporter
    reset_telemetry()
    target = environ.get("AGENT365_TELEMETRY_TARGET", "").strip().lower()
    if span_exporter is not None:
        exporter: SpanExporter = span_exporter
    elif identity.demo_mode and target in {"", "console"}:
        exporter = CompactConsoleExporter()
    elif target == "console":
        if not _truthy(environ.get("AGENT365_ALLOW_CONSOLE_TELEMETRY", "")):
            raise TelemetryExportError(
                "Refusing to drop production telemetry on the console. "
                "Unset AGENT365_TELEMETRY_TARGET or set it to agent365."
            )
        exporter = CompactConsoleExporter()
    elif target in {"", "agent365"}:
        if identity.demo_mode:
            raise TelemetryExportError("demo mode cannot export to Agent 365")
        _exporter = Agent365JsonExporter(
            ingest_url(identity.tenant_id, identity.agent_client_id),
            token_provider or (lambda: ""),
        )
        exporter = _exporter
    else:
        raise TelemetryExportError(
            "AGENT365_TELEMETRY_TARGET must be 'agent365' or, for a local dry run, 'console'"
        )
    if isinstance(exporter, Agent365JsonExporter):
        _exporter = exporter
    _provider = TracerProvider(resource=Resource.create({"service.name": identity.agent_name}))
    # One batch for Agent 365 so a run is a single request. Console and test
    # exporters emit as each span ends so a short process cannot lose them.
    if isinstance(exporter, Agent365JsonExporter):
        _provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=2048,
                schedule_delay_millis=600_000,
                max_export_batch_size=512,
                export_timeout_millis=30_000,
            )
        )
    else:
        _provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _provider.get_tracer("sample_agent")


def get_tracer() -> Tracer:
    if _provider is None:
        raise TelemetryExportError("telemetry is not configured")
    return _provider.get_tracer("sample_agent")


def flush_telemetry() -> None:
    if _provider is None:
        return
    flushed = _provider.force_flush(timeout_millis=30_000)
    if _exporter is not None and _exporter.error:
        raise TelemetryExportError(_exporter.error)
    if not flushed:
        raise TelemetryExportError("telemetry flush did not complete")


def mark_ok(span) -> None:
    span.set_status(Status(StatusCode.OK))


def mark_error(span, message: str) -> None:
    span.set_status(Status(StatusCode.ERROR, message))
    span.set_attribute("error.type", message[:200])

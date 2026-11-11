"""Telemetry for the sample agent, in the shape Agent 365 observability ingests.

OpenTelemetry is the documented fallback path for non-Microsoft agent stacks:
spans named for agent operations, carrying the attributes an operator (and the
Agent 365 observability views) need to answer "which agent did what, for whom,
using which tool, and how did it end":

    agent.id, agent.blueprint.id, agent.sponsor, tool.name, tool.outcome

Exporter selection: OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set (point it at
your collector / Agent 365 ingest path); console exporter otherwise, so demo
mode still shows every span.

REMINDER: telemetry is dropped when no Agent 365-licensed user is involved in
the flow. The sponsor attribute is on every span partly so you can prove a
licensed user is attached to the run when spans don't land.
"""
from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_configured = False


def configure_telemetry(service_name: str = "sample-agent") -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer() -> trace.Tracer:
    configure_telemetry()
    return trace.get_tracer("sample_agent")


def agent_attributes(identity, blueprint_id: str = "", sponsor: str = "") -> dict:
    """Attributes attached to every span so runs are attributable."""
    return {
        "agent.id": identity.label,
        "agent.blueprint.id": blueprint_id or os.environ.get("AGENT365_BLUEPRINT_ID", "demo-blueprint"),
        "agent.sponsor": sponsor or os.environ.get("AGENT365_SPONSOR_UPN", "demo-sponsor@example.com"),
        "agent.demo_mode": identity.demo_mode,
    }

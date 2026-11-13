import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import EXIT_DENIED, EXIT_ERROR, EXIT_OK, execute, plan
from identity import IdentityError
from telemetry import (
    Agent365JsonExporter,
    CompactConsoleExporter,
    TelemetryExportError,
    _rejection_reason,
    configure_telemetry,
    ingest_url,
    message_json,
    redact_text,
    spans_to_otlp_json,
)
from identity import load_identity

TENANT = "11111111-1111-1111-1111-111111111111"
BLUEPRINT = "22222222-2222-2222-2222-222222222222"
AGENT = "33333333-3333-3333-3333-333333333333"
CALLER = "55555555-5555-5555-5555-555555555555"
MANAGED = "44444444-4444-4444-4444-444444444444"


def _spans(prompt: str, environ=None):
    exporter = InMemorySpanExporter()
    code = execute(prompt, environ={} if environ is None else environ, span_exporter=exporter)
    return code, exporter.get_finished_spans()


def test_weather_run_emits_the_required_span_shape():
    code, spans = _spans("what's the weather in Reseda?")
    assert code == EXIT_OK
    by_name = {span.name: span for span in spans}
    assert set(by_name) == {"invoke_agent", "execute_tool", "output_messages"}
    conversation_ids = {span.attributes["gen_ai.conversation.id"] for span in spans}
    assert len(conversation_ids) == 1
    invoke = by_name["invoke_agent"]
    assert invoke.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert invoke.attributes["gen_ai.agent.id"] == "demo"
    assert invoke.attributes["microsoft.a365.agent.blueprint.id"] == "demo"
    assert invoke.attributes["microsoft.tenant.id"] == "demo"
    assert invoke.attributes["user.id"] == "demo-caller"
    assert "Reseda" in invoke.attributes["gen_ai.input.messages"]
    tool = by_name["execute_tool"]
    assert tool.attributes["gen_ai.tool.name"] == "get_weather"
    assert tool.attributes["gen_ai.tool.type"] == "function"
    assert tool.attributes["gen_ai.tool.call.id"]
    assert tool.parent.span_id == invoke.context.span_id
    encoded = spans_to_otlp_json(spans)
    body_spans = encoded["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert all(item["kind"] == 1 for item in body_spans)
    assert all(isinstance(item["startTimeUnixNano"], str) for item in body_spans)
    values = body_spans[0]["attributes"]
    assert all("stringValue" in item["value"] for item in values)


def test_mail_is_refused_and_research_is_not_mail():
    code, spans = _spans("send the forecast")
    assert code == EXIT_DENIED
    tool = next(span for span in spans if span.name == "execute_tool")
    assert json.loads(tool.attributes["gen_ai.tool.call.result"])["reason"] == "approval_required"
    assert "would be sent" not in tool.attributes["gen_ai.tool.call.result"]

    code, spans = _spans("research the weather in Reseda")
    assert code == EXIT_OK
    tool = next(span for span in spans if span.name == "execute_tool")
    assert tool.attributes["gen_ai.tool.name"] == "get_weather"
    assert plan("please resend the weather in Reseda")[0] == "get_weather"


def test_prompt_secrets_are_redacted_on_spans():
    secret = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    code, spans = _spans(f"what's the weather in Reseda? {secret}")
    assert code == EXIT_OK
    invoke = next(span for span in spans if span.name == "invoke_agent")
    assert secret not in invoke.attributes["gen_ai.input.messages"]
    assert "[redacted-jwt]" in invoke.attributes["gen_ai.input.messages"]
    assert "eyJ" not in redact_text(f"bearer {secret}")


def test_trace_content_can_be_turned_off():
    code, spans = _spans("what's the weather in Reseda?", {"AGENT365_TRACE_CONTENT": "false"})
    assert code == EXIT_OK
    invoke = next(span for span in spans if span.name == "invoke_agent")
    assert json.loads(invoke.attributes["gen_ai.input.messages"])[0]["content"] == "[redacted]"
    tool = next(span for span in spans if span.name == "execute_tool")
    assert json.loads(tool.attributes["gen_ai.tool.call.arguments"]) == {"redacted": True}


def test_production_mode_requires_a_caller_before_any_token_call(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("token endpoint should not be called")

    monkeypatch.setattr("agent.acquire_token", explode)
    code, spans = _spans(
        "what's the weather in Reseda?",
        {
            "AGENT365_TENANT_ID": TENANT,
            "AGENT365_BLUEPRINT_CLIENT_ID": BLUEPRINT,
            "AGENT365_AGENT_CLIENT_ID": AGENT,
            "AGENT365_MANAGED_IDENTITY_CLIENT_ID": MANAGED,
        },
    )
    assert code == EXIT_ERROR
    assert spans == ()


def test_production_console_export_is_refused():
    identity = load_identity(
        {
            "AGENT365_TENANT_ID": TENANT,
            "AGENT365_BLUEPRINT_CLIENT_ID": BLUEPRINT,
            "AGENT365_AGENT_CLIENT_ID": AGENT,
            "AGENT365_MANAGED_IDENTITY_CLIENT_ID": MANAGED,
            "AGENT365_CALLER_OBJECT_ID": CALLER,
        }
    )
    with pytest.raises(TelemetryExportError, match="console"):
        configure_telemetry(identity, {"AGENT365_TELEMETRY_TARGET": "console"})


def test_rejected_spans_fail_the_run(monkeypatch):
    monkeypatch.setattr("agent.acquire_token", lambda *_args, **_kwargs: "resource-token")
    calls = []

    def transport(url, body, token):
        calls.append((url, body, token))
        return 200, {"results": [{"sinks": {"flashpoint": {"status": "rejected", "reason": "tenant_not_licensed"}}}]}

    exporter = Agent365JsonExporter(ingest_url(TENANT, AGENT), lambda: "resource-token", transport=transport)
    code = execute(
        "what's the weather in Reseda?",
        environ={
            "AGENT365_TENANT_ID": TENANT,
            "AGENT365_BLUEPRINT_CLIENT_ID": BLUEPRINT,
            "AGENT365_AGENT_CLIENT_ID": AGENT,
            "AGENT365_MANAGED_IDENTITY_CLIENT_ID": MANAGED,
            "AGENT365_CALLER_OBJECT_ID": CALLER,
        },
        span_exporter=exporter,
    )
    assert code == EXIT_ERROR
    assert calls
    assert calls[0][2] == "resource-token"
    assert "Bearer" not in json.dumps(calls[0][1])
    posted = calls[0][1]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert {span["name"] for span in posted} == {"invoke_agent", "execute_tool", "output_messages"}
    assert all(span["attributes"] for span in posted)


def test_http_200_is_ingestion_only_when_a_sink_sent_the_span():
    sent = {"results": [{"sinks": {"flashpoint": {"status": "sent"}, "sentinel": {"status": "not_routed"}}}]}
    assert _rejection_reason(200, sent) is None
    empty = _rejection_reason(200, {"partialSuccess": {"rejectedSpans": 0, "errorMessage": ""}})
    assert empty and "not ingestion" in empty
    unrouted = _rejection_reason(
        200,
        {"results": [{"sinks": {"sentinel": {"status": "not_routed", "reason": "not_configured"}}}]},
    )
    assert unrouted and "not mark any span as sent" in unrouted
    mixed = _rejection_reason(
        200,
        {"results": [{"sinks": {"flashpoint": {"status": "sent"}, "esp": {"status": "rejected", "reason": "tenant_not_licensed"}}}]},
    )
    assert mixed and "tenant_not_licensed" in mixed
    reported = _rejection_reason(
        200,
        {"partialSuccess": {"rejectedSpans": 0, "errorMessage": "license missing"}, "results": sent["results"]},
    )
    assert reported and "license missing" in reported


def test_exporter_refuses_a_foreign_host():
    with pytest.raises(TelemetryExportError, match="unexpected host"):
        Agent365JsonExporter("https://collector.example/v1/traces", lambda: "token")


def test_compact_console_stays_one_line(capsys):
    status_code = type("Code", (), {"name": "ERROR"})()
    span = type("Span", (), {})()
    span.name = "execute_tool"
    span.status = type("Status", (), {"status_code": status_code})()
    span.attributes = {
        "gen_ai.tool.name": "send_external_email",
        "gen_ai.tool.call.result": "x" * 500,
    }
    assert CompactConsoleExporter().export([span]).name == "SUCCESS"
    line = capsys.readouterr().out
    assert line.count("\n") == 1
    assert "send_external_email" in line
    assert len(line) < 400


def test_message_json_shape():
    assert json.loads(message_json("user", "hi", True)) == [{"role": "user", "content": "hi"}]


def test_bad_identity_does_not_raise_a_stack_to_the_caller(capsys):
    code = execute("hi", environ={"AGENT365_TENANT_ID": "not-a-guid"})
    assert code == EXIT_ERROR
    assert "GUID" in capsys.readouterr().out
    with pytest.raises(IdentityError):
        load_identity({"AGENT365_TENANT_ID": "not-a-guid"})

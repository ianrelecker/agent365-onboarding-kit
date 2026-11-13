"""Sample third-party agent wired for Agent 365 governance.

The planner is a deterministic stub. Identity, telemetry, and the tool
allowlist are the parts that have to be right in production. Replace `plan`
with a model call; the harness around it stays.

    python agent.py "what's the weather in Reseda?"
    python agent.py "send the forecast"          # governance refuses

Exit codes: 0 the tool ran, 2 the allowlist refused it, 1 the run failed.
A refusal is not a successful exit. Demo mode does not contact a tenant.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Mapping, MutableMapping

from opentelemetry.trace import SpanKind

from identity import GRAPH_SCOPE, IdentityError, acquire_token, load_env_file, load_identity
from telemetry import (
    OBSERVABILITY_SCOPE,
    TelemetryExportError,
    arguments_json,
    configure_telemetry,
    flush_telemetry,
    get_tracer,
    mark_error,
    mark_ok,
    message_json,
    new_conversation_id,
    run_attributes,
    trace_content_enabled,
)
from tool_governance import PolicyError, ToolDenied, check_tool, load_policy

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DENIED = 2

_EMAIL_INTENT = re.compile(r"\b(e-mail|email|send|mail)\b", re.IGNORECASE)
_SKIP_TITLE = {"What", "Whats", "The", "In", "A", "An", "Please", "Weather"}


def get_weather(location: str) -> str:
    """Stub weather tool. The allowlist marks it allowed."""
    return f"(stub) Clear and 72 F in {location}."


def send_external_email(to: str, body: str) -> str:
    """Stub email tool. The allowlist requires approval, so this does not run."""
    return f"(stub) Email to {to} would be sent here."


TOOLS = {"get_weather": get_weather, "send_external_email": send_external_email}


def plan(prompt: str) -> tuple[str, dict]:
    """Deterministic stand-in for a model tool call.

    Replace this with a call that returns (tool_name, arguments). The harness
    checks the allowlist before the tool function runs.
    """
    if _EMAIL_INTENT.search(prompt):
        return "send_external_email", {"to": "someone@example.com", "body": prompt[:4000]}
    location = "Reseda"
    for word in re.findall(r"[A-Za-z][A-Za-z'-]*", prompt):
        if word.istitle() and word not in _SKIP_TITLE:
            location = word
    return "get_weather", {"location": location}


def _env_file() -> Path:
    cwd_file = Path.cwd() / ".env"
    if cwd_file.is_file():
        return cwd_file
    return Path(__file__).resolve().parent / ".env"


def _apply_attributes(span, attributes: Mapping[str, str]) -> None:
    for key, value in attributes.items():
        span.set_attribute(key, value)


def _telemetry_scope(environ: Mapping[str, str]) -> str:
    target = environ.get("AGENT365_TELEMETRY_TARGET", "").strip().lower()
    if target in {"", "agent365"}:
        return OBSERVABILITY_SCOPE
    explicit = environ.get("AGENT365_TOKEN_SCOPE", "").strip()
    return explicit or GRAPH_SCOPE


def execute(
    prompt: str,
    environ: MutableMapping[str, str] | None = None,
    span_exporter=None,
) -> int:
    if environ is None:
        load_env_file(_env_file(), os_environ())
        environ = os_environ()
    if len(prompt) > 8000:
        print("prompt is too long")
        return EXIT_ERROR
    try:
        identity = load_identity(environ)
        policy = load_policy(environ=environ)
    except (IdentityError, PolicyError) as exc:
        print(exc)
        return EXIT_ERROR

    code = EXIT_ERROR
    try:
        scope = _telemetry_scope(environ)
        configure_telemetry(
            identity,
            environ,
            token_provider=lambda: acquire_token(identity, scope=scope),
            span_exporter=span_exporter,
        )
        code = _invoke(prompt, identity, policy, environ, scope)
    except (IdentityError, TelemetryExportError, PolicyError) as exc:
        print(exc)
        code = EXIT_ERROR
    try:
        flush_telemetry()
    except TelemetryExportError as exc:
        print(exc)
        return EXIT_ERROR
    return code


def os_environ() -> MutableMapping[str, str]:
    import os

    return os.environ


def _invoke(prompt: str, identity, policy: dict, environ: Mapping[str, str], scope: str) -> int:
    tracer = get_tracer()
    conversation_id = new_conversation_id()
    attributes = run_attributes(identity, environ, conversation_id)
    include_content = trace_content_enabled(environ, identity)
    print(f"[identity] acting as {identity.label} (demo_mode={identity.demo_mode})")
    if not identity.demo_mode:
        acquire_token(identity, scope=scope)

    code = EXIT_OK
    output = ""
    with tracer.start_as_current_span("invoke_agent", kind=SpanKind.INTERNAL) as span:
        _apply_attributes(span, attributes)
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.input.messages", message_json("user", prompt, include_content))
        tool_name, tool_args = plan(prompt)
        with tracer.start_as_current_span("execute_tool", kind=SpanKind.INTERNAL) as tool_span:
            _apply_attributes(tool_span, attributes)
            tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
            tool_span.set_attribute("gen_ai.tool.name", tool_name)
            tool_span.set_attribute("gen_ai.tool.type", "function")
            tool_span.set_attribute("gen_ai.tool.call.id", str(uuid.uuid4()))
            tool_span.set_attribute("gen_ai.tool.call.arguments", arguments_json(tool_args, include_content))
            try:
                check_tool(policy, tool_name, tool_args)
                tool = TOOLS.get(tool_name)
                if tool is None:
                    raise ToolDenied(
                        tool_name,
                        "not_implemented",
                        "tool is allowed by policy but this runtime does not implement it",
                    )
                result = tool(**tool_args)
            except ToolDenied as exc:
                tool_span.set_attribute(
                    "gen_ai.tool.call.result",
                    json.dumps({"outcome": "denied", "reason": exc.reason}, separators=(",", ":")),
                )
                mark_error(tool_span, exc.reason)
                print(f"[governance] {exc}")
                output = "I can't run that tool under the current policy."
                print(f"[agent] {output}")
                code = EXIT_DENIED
            else:
                tool_span.set_attribute("gen_ai.tool.call.result", message_json("tool", result, include_content))
                mark_ok(tool_span)
                output = result
                print(f"[tool] {tool_name} -> {result}")
                print(f"[agent] {result}")
        span.set_attribute("gen_ai.output.messages", message_json("assistant", output, include_content))
        # A governed refusal is a completed run. The tool span records the denial.
        mark_ok(span)
        with tracer.start_as_current_span("output_messages", kind=SpanKind.INTERNAL) as out_span:
            _apply_attributes(out_span, attributes)
            out_span.set_attribute("gen_ai.operation.name", "output_messages")
            out_span.set_attribute("gen_ai.output.messages", message_json("assistant", output, include_content))
            mark_ok(out_span)
    return code


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    prompt = " ".join(args) or "what's the weather in Reseda?"
    return execute(prompt)


if __name__ == "__main__":
    raise SystemExit(main())

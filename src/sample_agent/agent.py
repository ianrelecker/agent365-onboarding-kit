"""Sample 'third-party agent' wired for Agent 365 governance.

Pretend this agent was built on a non-Microsoft stack and is now being
onboarded: it authenticates as an Entra agent identity (identity.py), emits
OpenTelemetry spans (telemetry.py), and checks every tool call against the
allowlist before executing (tool_governance.py).

The planner is a deterministic stub — the governance wiring is the subject of
this repo, not the intelligence. The LLM adapter point is marked below.

Run it:
    python agent.py "what's the weather in Reseda?"
    python agent.py "delete the production database"     # watch governance refuse

Demo mode (no env vars): stub token, console telemetry, real allowlist
enforcement.
"""
from __future__ import annotations

import sys

from identity import acquire_token, load_identity
from telemetry import agent_attributes, get_tracer
from tool_governance import ToolDenied, check_tool, load_policy


# --- Tools -----------------------------------------------------------------
# In a real agent these are MCP tools or API calls. The governance layer does
# not care what they do — only whether they're allowed to run.
def get_weather(location: str) -> str:
    """Stub weather tool (the allowlist's 'allowed' example)."""
    return f"(stub) Clear and 72°F in {location}."


def send_external_email(to: str, body: str) -> str:
    """Stub email tool (the allowlist's 'requires approval' example)."""
    return f"(stub) Email to {to} would be sent here — it never runs unattended."


TOOLS = {"get_weather": get_weather, "send_external_email": send_external_email}


# --- Planner ---------------------------------------------------------------
def plan(prompt: str) -> tuple[str, dict]:
    """Deterministic stub planner.

    LLM ADAPTER POINT: replace this function with a call to your model
    (OpenAI, Anthropic, …) that returns (tool_name, args). Nothing downstream
    changes — identity, telemetry, and governance wrap whatever planner you
    plug in. That is the whole point of the kit: governance is a property of
    the harness, not of the model vendor.
    """
    text = prompt.lower()
    if "email" in text or "send" in text:
        return "send_external_email", {"to": "someone@example.com", "body": prompt}
    location = "Reseda"
    for word in prompt.replace("?", "").split():
        if word.istitle():
            location = word
    return "get_weather", {"location": location}


# --- Main loop ---------------------------------------------------------------
def main() -> int:
    prompt = " ".join(sys.argv[1:]) or "what's the weather in Reseda?"
    identity = load_identity()
    policy = load_policy()
    tracer = get_tracer()
    attrs = agent_attributes(identity)

    with tracer.start_as_current_span("agent.invoke", attributes=attrs) as span:
        span.set_attribute("agent.prompt", prompt)
        token = acquire_token(identity)
        span.set_attribute("agent.token_acquired", bool(token) and token != "")
        print(f"[identity] acting as {identity.label} (demo_mode={identity.demo_mode})")

        tool_name, tool_args = plan(prompt)
        with tracer.start_as_current_span("tool.call", attributes=attrs) as tool_span:
            try:
                check_tool(policy, tool_name)  # governance gate — before execution, always
            except ToolDenied as exc:
                tool_span.set_attribute("tool.outcome", "denied")
                print(f"[governance] {exc}")
                print("[agent] I can't run that tool under the current policy.")
                return 0
            result = TOOLS[tool_name](**tool_args)
            tool_span.set_attribute("tool.outcome", "success")
            print(f"[tool] {tool_name} -> {result}")
            print(f"[agent] {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

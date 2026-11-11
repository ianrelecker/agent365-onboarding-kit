"""Tool governance: allowlist enforcement before any tool executes.

This is the difference between an agent that *has* tools and an agent whose
tool use is *governed*: every call is checked against
policies/mcp-allowlist.json first. Default is deny. A tool can be:

    "allowed": true            -> runs
    "requires_approval": true  -> refused in this sample (a real deployment
                                  routes to a human approval flow; refusing
                                  closed is the safe default)
    anything else / unlisted   -> denied

Denials raise ToolDenied and are recorded on the current span, so an attempted
policy violation is itself telemetry in the observability pipeline.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from opentelemetry import trace


class ToolDenied(Exception):
    """Raised when a tool call is not permitted by the allowlist."""


def _default_policy_path() -> Path:
    # policies/ lives at the repo root, two levels up from this file.
    return Path(__file__).resolve().parents[2] / "policies" / "mcp-allowlist.json"


def load_policy(path: str | None = None) -> dict:
    policy_path = Path(path or os.environ.get("AGENT365_TOOL_POLICY", _default_policy_path()))
    with open(policy_path, "r", encoding="utf-8") as f:
        return json.load(f)


def check_tool(policy: dict, tool_name: str) -> None:
    """Raise ToolDenied unless the allowlist permits this tool call."""
    span = trace.get_current_span()
    tools = policy.get("tools", {})
    entry = tools.get(tool_name)

    def deny(reason: str):
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.outcome", "denied")
        span.set_attribute("tool.deny_reason", reason)
        raise ToolDenied(f"Tool '{tool_name}' denied: {reason}")

    if policy.get("default", "deny") != "deny":
        # The kit ships default-deny; anything else is a misconfiguration
        # worth failing loudly over.
        deny("policy default is not 'deny' — refusing to evaluate a non-default-deny policy")
    if entry is None:
        deny("tool is not on the allowlist")
    if entry.get("requires_approval"):
        deny("tool requires human approval, which this runtime does not grant automatically")
    if not entry.get("allowed"):
        deny("tool is listed but not allowed")
    span.set_attribute("tool.name", tool_name)
    span.set_attribute("tool.outcome", "allowed")

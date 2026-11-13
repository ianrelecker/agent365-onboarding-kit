"""Local tool allowlist. Default deny, checked before a tool function runs.

Agent 365 does not read this file. Conditional Access and Entra permissions
govern tokens; this module governs tools the process is about to execute
itself. A tool runs only when the policy says it is allowed and, if it
requires approval, the caller supplies an approval decision. There is no
environment variable that grants approval.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Mapping

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_MAX_POLICY_BYTES = 1024 * 1024
_ARGUMENT_TYPES = {"string", "number", "boolean"}
_TOOL_KEYS = {"allowed", "requires_approval", "arguments", "comment"}
_POLICY_KEYS = {"version", "default", "tools", "comment"}


class PolicyError(ValueError):
    """The allowlist file is not safe to evaluate."""


class ToolDenied(Exception):
    def __init__(self, tool_name: str, reason: str, detail: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"Tool {tool_name!r} denied: {detail}")


def _default_policy_path() -> Path:
    return Path(__file__).resolve().parents[2] / "policies" / "mcp-allowlist.json"


def load_policy(path: str | None = None, environ: Mapping[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    policy_path = Path(path or env.get("AGENT365_TOOL_POLICY") or _default_policy_path())
    if not policy_path.is_file():
        raise PolicyError(f"tool policy file does not exist: {policy_path}")
    size = policy_path.stat().st_size
    if size > _MAX_POLICY_BYTES:
        raise PolicyError("tool policy file is larger than 1 MiB")
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PolicyError(f"tool policy is not valid JSON: line {exc.lineno}") from exc
    return validate_policy(raw)


def validate_policy(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise PolicyError("tool policy must be a JSON object")
    unknown = set(raw) - _POLICY_KEYS
    if unknown:
        raise PolicyError("tool policy has unknown fields: " + ", ".join(sorted(unknown)))
    if raw.get("version") != 1:
        raise PolicyError("tool policy version must be 1")
    if raw.get("default") != "deny":
        raise PolicyError("tool policy default must be 'deny'")
    tools = raw.get("tools")
    if not isinstance(tools, dict):
        raise PolicyError("tool policy tools must be an object")
    normalized: dict[str, dict] = {}
    for name, entry in tools.items():
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise PolicyError(f"tool name {name!r} is not allowed")
        normalized[name] = _validate_entry(name, entry)
    return {"version": 1, "default": "deny", "tools": normalized}


def _validate_entry(name: str, entry: object) -> dict:
    if not isinstance(entry, dict):
        raise PolicyError(f"tool {name} must be an object")
    unknown = set(entry) - _TOOL_KEYS
    if unknown:
        raise PolicyError(f"tool {name} has unknown fields: " + ", ".join(sorted(unknown)))
    allowed = entry.get("allowed")
    approval = entry.get("requires_approval")
    if not isinstance(allowed, bool) or not isinstance(approval, bool):
        raise PolicyError(f"tool {name} allowed and requires_approval must be JSON booleans")
    arguments = entry.get("arguments", {})
    if not isinstance(arguments, dict):
        raise PolicyError(f"tool {name} arguments must be an object")
    normalized_args = {}
    for arg_name, spec in arguments.items():
        if not isinstance(arg_name, str) or not _TOOL_NAME.fullmatch(arg_name):
            raise PolicyError(f"tool {name} has an invalid argument name")
        normalized_args[arg_name] = _validate_argument(name, arg_name, spec)
    return {
        "allowed": allowed,
        "requires_approval": approval,
        "arguments": normalized_args,
    }


def _validate_argument(tool: str, arg_name: str, spec: object) -> dict:
    if not isinstance(spec, dict):
        raise PolicyError(f"tool {tool} argument {arg_name} must be an object")
    allowed_keys = {"type", "required", "max_length", "minimum", "maximum", "comment"}
    unknown = set(spec) - allowed_keys
    if unknown:
        raise PolicyError(f"tool {tool} argument {arg_name} has unknown fields")
    arg_type = spec.get("type")
    if arg_type not in _ARGUMENT_TYPES:
        raise PolicyError(f"tool {tool} argument {arg_name} has an unsupported type")
    required = spec.get("required", True)
    if not isinstance(required, bool):
        raise PolicyError(f"tool {tool} argument {arg_name} required must be a boolean")
    normalized = {"type": arg_type, "required": required}
    if arg_type == "string":
        max_length = spec.get("max_length")
        if not isinstance(max_length, int) or isinstance(max_length, bool) or not 1 <= max_length <= 8000:
            raise PolicyError(f"tool {tool} argument {arg_name} needs max_length between 1 and 8000")
        normalized["max_length"] = max_length
    if arg_type == "number":
        for bound in ("minimum", "maximum"):
            if bound not in spec:
                continue
            value = spec[bound]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PolicyError(f"tool {tool} argument {arg_name} {bound} must be a number")
            normalized[bound] = value
        if "minimum" in normalized and "maximum" in normalized and normalized["minimum"] > normalized["maximum"]:
            raise PolicyError(f"tool {tool} argument {arg_name} minimum is greater than maximum")
    return normalized


def check_tool(
    policy: dict,
    tool_name: str,
    tool_args: Mapping | None = None,
    approval_granted: Callable[[str, Mapping], bool] | None = None,
) -> None:
    """Raise ToolDenied unless this call may run.

    ``requires_approval`` is an extra gate. ``allowed: false`` without
    ``requires_approval`` cannot be overridden. Approval is a caller-supplied
    decision for a specific invocation, never a process-wide switch.
    """
    if not isinstance(policy, dict) or policy.get("default") != "deny" or policy.get("version") != 1:
        raise ToolDenied(tool_name or "", "policy_not_default_deny", "policy is not a validated default-deny policy")
    if not isinstance(tool_name, str) or not _TOOL_NAME.fullmatch(tool_name):
        raise ToolDenied(str(tool_name), "not_listed", "tool name is not on the allowlist")
    if tool_args is None:
        tool_args = {}
    if not isinstance(tool_args, Mapping) or isinstance(tool_args, (str, bytes)):
        raise ToolDenied(tool_name, "argument_type", "tool arguments must be an object")

    entry = policy.get("tools", {}).get(tool_name)
    if entry is None:
        raise ToolDenied(tool_name, "not_listed", "tool is not on the allowlist")
    if entry.get("requires_approval"):
        granted = False
        if approval_granted is not None:
            granted = bool(approval_granted(tool_name, tool_args))
        if not granted:
            raise ToolDenied(
                tool_name,
                "approval_required",
                "tool requires human approval, which this runtime does not grant automatically",
            )
    elif not entry.get("allowed"):
        raise ToolDenied(tool_name, "not_allowed", "tool is listed but not allowed")
    elif entry.get("allowed") is not True:
        raise ToolDenied(tool_name, "not_allowed", "tool is listed but not allowed")

    _check_arguments(tool_name, entry.get("arguments") or {}, tool_args)


def _check_arguments(tool_name: str, schema: Mapping, tool_args: Mapping) -> None:
    unexpected = sorted(set(tool_args) - set(schema))
    if unexpected:
        raise ToolDenied(tool_name, "unexpected_argument", "unexpected arguments: " + ", ".join(unexpected))
    for name, spec in schema.items():
        if name not in tool_args:
            if spec.get("required", True):
                raise ToolDenied(tool_name, "missing_argument", f"missing argument {name}")
            continue
        value = tool_args[name]
        arg_type = spec["type"]
        if arg_type == "string":
            if not isinstance(value, str):
                raise ToolDenied(tool_name, "argument_type", f"argument {name} must be a string")
            if len(value) > spec["max_length"]:
                raise ToolDenied(tool_name, "argument_too_long", f"argument {name} exceeds max_length")
        elif arg_type == "boolean":
            if not isinstance(value, bool):
                raise ToolDenied(tool_name, "argument_type", f"argument {name} must be a boolean")
        elif arg_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolDenied(tool_name, "argument_type", f"argument {name} must be a number")
            if "minimum" in spec and value < spec["minimum"]:
                raise ToolDenied(tool_name, "argument_type", f"argument {name} is below minimum")
            if "maximum" in spec and value > spec["maximum"]:
                raise ToolDenied(tool_name, "argument_type", f"argument {name} is above maximum")

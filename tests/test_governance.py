import json

import pytest

from tool_governance import PolicyError, ToolDenied, check_tool, load_policy, validate_policy


def _policy():
    return load_policy()


def test_shipped_policy_allows_weather_and_holds_mail():
    policy = _policy()
    check_tool(policy, "get_weather", {"location": "Reseda"})
    with pytest.raises(ToolDenied) as caught:
        check_tool(policy, "send_external_email", {"to": "a@contoso.com", "body": "hi"})
    assert caught.value.reason == "approval_required"


def test_approval_is_per_call_and_cannot_be_implied():
    policy = _policy()
    check_tool(
        policy,
        "send_external_email",
        {"to": "a@contoso.com", "body": "hi"},
        approval_granted=lambda name, args: name == "send_external_email" and args["to"] == "a@contoso.com",
    )
    with pytest.raises(ToolDenied):
        check_tool(
            policy,
            "send_external_email",
            {"to": "other@contoso.com", "body": "hi"},
            approval_granted=lambda name, args: args["to"] == "a@contoso.com",
        )


def test_unlisted_tool_and_extra_arguments_are_denied():
    policy = _policy()
    with pytest.raises(ToolDenied, match="not on the allowlist"):
        check_tool(policy, "delete_database", {})
    with pytest.raises(ToolDenied) as caught:
        check_tool(policy, "get_weather", {"location": "Reseda", "units": "C"})
    assert caught.value.reason == "unexpected_argument"


def test_string_booleans_are_not_accepted():
    raw = json.loads(json.dumps(_policy()))
    raw["tools"]["get_weather"]["allowed"] = "true"
    with pytest.raises(PolicyError, match="boolean"):
        validate_policy(raw)


def test_non_deny_default_is_rejected():
    raw = {"version": 1, "default": "allow", "tools": {}}
    with pytest.raises(PolicyError, match="deny"):
        validate_policy(raw)
    with pytest.raises(ToolDenied) as caught:
        check_tool({"version": 1, "default": "allow", "tools": {}}, "get_weather", {})
    assert caught.value.reason == "policy_not_default_deny"


def test_argument_limits_fail_closed():
    policy = _policy()
    with pytest.raises(ToolDenied) as caught:
        check_tool(policy, "get_weather", {"location": "x" * 81})
    assert caught.value.reason == "argument_too_long"
    with pytest.raises(ToolDenied) as missing:
        check_tool(policy, "get_weather", {})
    assert missing.value.reason == "missing_argument"


def test_hard_deny_cannot_be_approved(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "default": "deny",
                "tools": {
                    "drop_table": {
                        "allowed": False,
                        "requires_approval": False,
                        "arguments": {},
                    }
                },
            }
        )
    )
    policy = load_policy(str(path))
    with pytest.raises(ToolDenied) as caught:
        check_tool(policy, "drop_table", {}, approval_granted=lambda *_args: True)
    assert caught.value.reason == "not_allowed"

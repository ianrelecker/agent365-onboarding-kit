# Conditional Access for agents — policy template

**This is a template to adapt with your Conditional Access administrator, not a
policy to paste into production.** CA mistakes against agent identities break
business processes at machine speed. Deploy in **report-only mode**, observe a
full business cycle, then enforce.

## Policy intent

| Element | Setting |
|---|---|
| Target | Agent identities (the ones created via this kit's registration flow) — target the blueprint/identity group, never "all users" |
| Exclude | Break-glass accounts; the sponsor accounts needed to remediate a lockout |
| Grant — normal | Allow when sign-in risk is low/medium and the request comes from a compliant context where applicable |
| Grant — high risk | **Block** on high sign-in risk; agents should fail closed |
| Session | Short token lifetimes for agent identities; no persistent sessions |
| Mode | Report-only → on, per the runbook |

Notes on the tricky parts:

- **"Compliant device" doesn't translate literally to agents.** An agent
  identity running server-side has no device in the user sense. Apply device
  conditions to the *human sponsor's* interactive sessions, and use
  risk + location conditions for the agent's own token requests.
- **Location conditions are your sharpest tool.** An agent that runs in your
  Azure region has no business requesting tokens from another continent. Named
  locations turn that into a one-line block.
- **Every agent allow is an exception someone owns.** If a business process
  needs an agent exempted from a control, the exemption names the sponsor and
  an expiry — CA exclusions without owners become permanent holes.

## Graph JSON sketch (adapt — do not apply verbatim)

Field names follow the Conditional Access policy schema in Microsoft Graph;
agent-identity targeting uses the policy's conditions/applications sections
with the agent's client ID, and was a preview-adjacent area at GA — verify the
current schema and the agents-specific conditions against Microsoft Learn
before use.

```json
{
  "displayName": "Agents — baseline (report-only)",
  "state": "enabledForReportingButNotEnforced",
  "conditions": {
    "signInRiskLevels": ["high"],
    "applications": {
      "includeApplications": ["<agent application / client id>"]
    },
    "locations": {
      "includeLocations": ["All"],
      "excludeLocations": ["<your named agent egress location>"]
    }
  },
  "grantControls": {
    "operator": "OR",
    "builtInControls": ["block"]
  }
}
```

A companion (enforced) policy for low/medium risk typically pairs
`"builtInControls": ["mfa"]`-style requirements for the *sponsor's* interactive
access to agent management, rather than the agent's own token flow — agents
can't do MFA, and a policy that demands it from a workload identity is a
lockout with extra steps.

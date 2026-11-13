# Conditional Access for agents — policy template

This is a template to adapt with your Conditional Access administrator. It is
not a policy to paste unchanged into production. Deploy it in **report-only**
mode, watch a full business cycle, then enforce. A bad grant against an agent
identity breaks the business process at machine speed.

Conditional Access here is evaluated when the **agent identity** asks for a
resource token. It does not apply to the blueprint's intermediate exchange at
the AAD Token Exchange endpoint. Blocking that first hop is not how you stop
an agent; block the agent identity's resource token.

Entra ID P1 is required for Conditional Access.

## What to target

| Element | Setting |
|---|---|
| Who | Agent identities, via `clientApplications.includeAgentIdServicePrincipals`. Use the blueprint **principal object id** from `Register-Agent365.ps1` to cover every identity created from that blueprint, including ones created later. Use one agent identity's object id to cover only that instance. |
| Not this | `applications.includeApplications` is the **resource** the agent is calling (Graph, a custom API, or `All`). It is not the agent's client id. Putting the client id there does not select the agent. |
| Exclude | A named break-glass agent identity, with an owner and an expiry. Do not exclude "all users". User exclusions do not exclude agent identities. |
| Where | Block token requests from outside the agent's egress named location. An agent that runs in one region has no business requesting tokens from another. |
| Grant | `block` for the outside-location policy. Agents cannot satisfy MFA. A policy that demands MFA from an agent identity is a lockout. |
| Mode | `enabledForReportingButNotEnforced` until the report-only results are understood. |

`includeAgentIdServicePrincipals` accepts `All` or object ids. The field is
part of the beta Conditional Access policy resource. Confirm the current
property names in the Entra admin center by creating the policy in report-only
and exporting it, before anyone posts this JSON to Graph.

## Graph JSON sketch

```json
{
  "displayName": "Agents — block outside approved egress (report-only)",
  "state": "enabledForReportingButNotEnforced",
  "conditions": {
    "clientApplications": {
      "includeAgentIdServicePrincipals": ["<blueprint-principal-object-id>"],
      "excludeAgentIdServicePrincipals": []
    },
    "applications": {
      "includeApplications": ["All"],
      "excludeApplications": []
    },
    "locations": {
      "includeLocations": ["All"],
      "excludeLocations": ["<named-location-id-of-agent-egress>"]
    }
  },
  "grantControls": {
    "operator": "AND",
    "builtInControls": ["block"]
  }
}
```

POST this only after adapting it, to
`https://graph.microsoft.com/beta/identity/conditionalAccess/policies`.
Beta means the shape can change. The portal is the source of truth if this
sketch and the exported policy disagree.

## Notes that prevent lockouts

- **"Compliant device" does not describe a server-side agent.** Apply device
  conditions to the sponsor's interactive sessions. Use risk and location for
  the agent's own token requests.
- **High agent risk** is configured in the portal (agent risk levels). Copy
  that condition from a policy you created in report-only rather than inventing
  the JSON property.
- **Every exclusion has an owner and an expiry.** An exclusion without either
  becomes a permanent hole.
- **Test with one blueprint principal**, not `All`, the first time. `All`
  covers every agent identity in the tenant.

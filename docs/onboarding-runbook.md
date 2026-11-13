# Onboarding runbook — third-party agent → Agent 365 governance

Do these in order. An identity problem poisons telemetry, and telemetry that
returns HTTP 200 can still have been rejected.

## Step 0 — Prerequisites

- **Portal:** Microsoft 365 admin center. Confirm an Agent 365 or Microsoft 365
  E7 license is **assigned** to the sponsor, not merely purchased.
- **Script:** `scripts/Test-Prereqs.ps1` on PowerShell 7, with the Microsoft
  Graph PowerShell SDK installed.
- **Validate:** you can name the sponsor, and you know which credential the
  blueprint will use. Production default is the user-assigned managed identity
  from `infra/entra-agent-blueprint.bicep`.

Licensing reminders: Agent 365 is per user. Agents themselves need no license.
The people who sponsor, use, or manage them do. Enterprise standalone Agent 365
requires a Microsoft 365 E5 base. Confirm the current licensing FAQ before a
rollout.

## Step 1 — Deploy the credential boundary

- **Script:** deploy `infra/entra-agent-blueprint.bicep` to a resource group.
  It creates a user-assigned managed identity and a Key Vault with RBAC, purge
  protection, and public network access disabled.
- **Validate:** the deployment outputs `managedIdentityClientId` and
  `managedIdentityPrincipalId`. The vault is not reachable from a laptop until
  you add a private endpoint plus the `privatelink.vaultcore.azure.net` zone,
  or you explicitly set `publicNetworkAccess` to `Enabled` and pass
  `allowedIpAddresses`.

Purge protection cannot be turned off after this deployment.

## Step 2 — Create the blueprint, the principal, and the identity

An agent identity blueprint is the template and the credential holder. The
blueprint principal is what Conditional Access targets when you want every
identity minted from that blueprint. The agent identity is the account the
process runs as. It has no credential of its own.

- **Portal:** Entra admin center → Entra ID → Agents → Agent blueprints.
- **Script:**

```powershell
./scripts/Register-Agent365.ps1 `
  -DisplayName "Contoso Support Agent" `
  -SponsorUpn "jane@contoso.com" `
  -ManagedIdentityPrincipalId "<principal id from the deployment>" `
  -ManagedIdentityClientId "<client id from the deployment>"
```

`-WhatIf` stops before any write. Re-running reuses the same display name and
refuses to guess when two objects share it.

- **Validate:** the script prints `BlueprintClientId`, `AgentClientId`,
  `BlueprintPrincipalObjectId`, and `SponsorObjectId`. The sponsor is an
  active user and is listed on both the blueprint and the identity.

`-IssueDevelopmentSecret` prints a short-lived secret once. Do not use it for
a production agent, and do not combine it with a managed identity.

## Step 3 — Register the agent in Agent 365

Entra identity is not the Agent 365 registry. Use the Microsoft 365 Agents SDK
or `a365 setup all` so the instance is registered. An unregistered instance
that exports on the service-to-service telemetry route is rejected.

This repo does not call the beta Agent Registry. That surface has been in
migration, and a stale POST is worse than a manual step you can see.

- **Validate:** the agent appears in the Agent 365 admin experience with the
  sponsor and the blueprint you just created. If it appears from a cloud sync
  with no Entra identity, you have inventory, not governance.

## Step 4 — Prove the token exchange

The agent host needs:

| Variable | Value |
|---|---|
| `AGENT365_TENANT_ID` | Tenant id from the script |
| `AGENT365_BLUEPRINT_CLIENT_ID` | `BlueprintClientId` (blueprint appId) |
| `AGENT365_AGENT_CLIENT_ID` | `AgentClientId` (agent identity appId, also `fmi_path`) |
| `AGENT365_CALLER_OBJECT_ID` | `SponsorObjectId` for an autonomous run, otherwise the signed-in user's object id |
| `AGENT365_MANAGED_IDENTITY_CLIENT_ID` | The managed identity client id, on the Azure host that owns that identity |

Leave `AGENT365_CLIENT_SECRET` unset. The process refuses a secret unless
`AGENT365_ALLOW_CLIENT_SECRET=true`, and that flag is a development opt-in.

The exchange is two token requests: the blueprint presents its federated
credential with `fmi_path` set to the agent identity appId, then the agent
identity presents that token as a client assertion. Conditional Access applies
to the second request, not to the blueprint's exchange at the AAD Token
Exchange endpoint.

- **Validate:** the sample prints `demo_mode=False` and does not print the
  token. A failure names the token error without the secret or the assertion.

## Step 5 — Wire telemetry

Outside demo mode the sample posts OTLP/HTTP+JSON to:

```text
https://agent365.svc.cloud.microsoft/observabilityService/tenants/{tenantId}/otlp/agents/{agentId}/traces?api-version=1
```

`{agentId}` is the agent identity appId. Every span carries
`gen_ai.operation.name`, `gen_ai.agent.id`, `microsoft.a365.agent.blueprint.id`,
`microsoft.tenant.id`, `user.id`, `gen_ai.conversation.id`, and the other
mandatory run attributes. Prompt text is redacted unless
`AGENT365_TRACE_CONTENT=true`.

- **Validate:** the process exits `1` unless a sink status is `sent` and
  none are `rejected`. HTTP 200 alone is not ingestion. A 200 whose sinks
  are `not_routed`, or whose reason is `tenant_not_licensed`, is a routing
  or licensing problem, not a span-shape problem. Then find the run in the
  Agent 365 activity view.

Demo mode (`python agent.py` with no identity variables) prints spans locally
and does not call this endpoint.

## Step 6 — Enforce the local tool allowlist

`policies/mcp-allowlist.json` is checked before the tool function runs.
Default is deny. `requires_approval` does not run unless the caller passes an
approval decision for that invocation. There is no environment variable that
approves a tool. `allowed: false` without `requires_approval` cannot be
approved.

This file is not an Agent 365 policy. It binds the process you ship.

- **Validate:** `python agent.py "send the forecast"` exits `2`, does not
  claim the mail was sent, and the `execute_tool` span says
  `approval_required`.

## Step 7 — Conditional Access

Adapt `policies/conditional-access-for-agents.md`. Target
`BlueprintPrincipalObjectId` with `includeAgentIdServicePrincipals`. Leave the
policy in report-only until you have watched a business cycle. Do not put the
agent's client id in `includeApplications`; that field is the resource being
called.

## Rollback

Disable the Conditional Access policy, revoke the blueprint credential
(federated credential or secret), then disable the agent identity. Do not
delete it if you still need the audit trail. Deleting the blueprint also
deletes the identities created from it.

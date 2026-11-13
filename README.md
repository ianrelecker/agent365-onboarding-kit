# agent365-onboarding-kit

A reference implementation for onboarding **third-party AI agents** into
**Microsoft Agent 365** governance — the part registry-sync alone doesn't give you.

## The gap this closes

Agent 365 is a governance and control plane, not an agent runtime. Agents built on
Microsoft's own stack (Copilot Studio, M365 Copilot / Agent Builder, SharePoint agents,
Foundry agents) get the full treatment automatically: an Entra Agent ID, Conditional
Access for agents, Purview DLP and audit, Defender threat detection, observability,
and governed tool access.

Agents built anywhere else — OpenAI / ChatGPT Enterprise, Anthropic, LangChain,
custom code — do **not** get that by default. What they get without code-level
onboarding is:

- **Registry sync** from a handful of other platforms (Amazon Bedrock, Google Vertex
  AI, Salesforce Agentforce, Claude Managed Agents, Databricks Genie, Oracle GenAI
  Agents, Snowflake Cortex): an inventory record, some metadata, a subset of
  management actions, and partial observability.
- **Endpoint discovery** (via Defender for Endpoint) of agent processes running on
  managed devices: visibility into shadow agent activity.

Visible is not governed. Full governance for a third-party agent requires onboarding
it the way this kit demonstrates:

1. **Identity** — an Entra Agent ID: an *agent identity blueprint* plus an *agent
   identity* created under it, with a named human **sponsor** accountable for it.
2. **Telemetry** — the agent emits traces in the shape Agent 365 observability
   ingests (this kit uses OpenTelemetry, the documented fallback path).
3. **Governed tool access** — the agent's tool calls are checked against an
   allowlist before they execute, so tool use is a policy decision, not an
   accident of whatever the model decided.

```
                        ┌────────────────────────────────────────────┐
                        │            Microsoft 365 tenant            │
                        │                                            │
  Human sponsor ──────► │  Entra ID                                  │
  (accountable owner)   │   └─ Agent identity blueprint              │
                        │       └─ Agent identity ──► tokens, CA     │
                        │                                            │
  Third-party agent     │  Agent 365 control plane                   │
  (your code, any       │   ├─ Registry (agent instance record)      │
   stack) ──────────────┼──►├─ Observability (OTel traces ingested)  │
   │ identity.py        │   ├─ Purview (audit / DLP on agent data)   │
   │ telemetry.py       │   └─ Defender (threat detection, endpoint  │
   │ tool_governance.py │        discovery of local agents)          │
   └─ allowlist check   │                                            │
      before every      │  Conditional Access for agents             │
      tool call         │   (policies target agent identities)       │
                        └────────────────────────────────────────────┘
```

## Repo map

| Path | What it is |
|---|---|
| `docs/onboarding-runbook.md` | Step-by-step onboarding: prereqs → blueprint → sponsor → registration → telemetry → tool governance → validation → CA |
| `docs/limitations.md` | What this kit (and Agent 365) does *not* do — read before promising anyone anything |
| `infra/entra-agent-blueprint.bicep` | User-assigned managed identity plus a closed Key Vault. The blueprint itself is created by the Graph script |
| `scripts/Register-Agent365.ps1` | Idempotent Graph script: blueprint, blueprint principal, agent identity, sponsor, optional federated credential |
| `scripts/Test-Prereqs.ps1` | Prereq checker: Graph connection, roles, license/defender checklist |
| `src/sample_agent/` | A runnable Python "third-party agent" wired for identity, telemetry, and tool governance |
| `policies/mcp-allowlist.json` | Sample tool allowlist: one allowed tool, one approval-required tool, default deny |
| `policies/conditional-access-for-agents.md` | CA-for-agents policy template (adapt before use) |

## Quickstart (demo mode, no tenant required)

```bash
cd src/sample_agent
pip install -r requirements.txt
python agent.py "what's the weather in Reseda?"
```

With no `AGENT365_*` identity variables set, the sample runs in **demo mode**:
a stub token stands in for the agent identity, spans print to the console, and
the tool allowlist is still enforced. Ask it to send mail and it refuses.
Exit codes are `0` (the tool ran), `2` (the allowlist refused it), and `1`
(the run failed). A refusal is not a successful exit.

Copy `.env.example` to `.env` only when you are leaving demo mode. The process
loads that file for variables that are not already set. A partial identity
configuration is refused: demo mode is not a fallback for a missing tenant id,
client id, or credential. Production authentication is the two-step blueprint
exchange (`fmi_path`), using a managed identity or a certificate. A client
secret is a development-tenant opt-in, not the production credential.

The allowlist in `policies/mcp-allowlist.json` is enforced by this harness
before a tool function runs. Agent 365 does not read that file. Conditional
Access and Entra permissions govern tokens; the file governs the process.
Follow `docs/onboarding-runbook.md` before pointing this at a tenant.

## Prerequisites

**Licensing** (per Microsoft's Agent 365 licensing guidance at GA):

- Agent 365 is licensed **per user** — agents themselves need no license. The users
  who interact with, own/sponsor, or manage agents using premium capabilities do.
- Buy it **standalone at $15/user/month**, or get it included in
  **Microsoft 365 E7 ($99/user/month)**. It is *not* included in E3 or E5.
- Enterprise tenants: the standalone offer requires a **Microsoft 365 E5** base
  (SMB: Business Premium). Older "E3 + Defender Suite + Purview Suite" qualification
  paths were tightened — verify your tenant's eligibility against the current
  licensing FAQ before planning a rollout.
- No Microsoft 365 Copilot license is required for Agent 365, and there were no
  consumption charges for it at GA.
- **Endpoint discovery of locally running agents depends on Defender for Endpoint**
  being deployed to those devices.

**Roles** (for the person running the scripts):

- Agent ID Developer can create a blueprint and a federated credential. Agent ID
  Administrator (or Global Administrator) is required to add a client secret.
- Conditional Access Administrator for the CA-for-agents policies.
- The agent's **sponsor** must be a real, licensed, active user — pick someone
  who will notice if the agent starts behaving strangely. That person's object
  id is `user.id` on autonomous spans. Agent 365 drops spans that omit it.

## Honest status — GA vs preview vs verify-before-use

Agent 365 GA'd **May 1, 2026**, centered on the *agent acting on behalf of a user*
pattern. At GA:

- **GA:** registry and inventory, Entra Agent ID for Microsoft-native agents,
  Conditional Access for agents, Purview audit/DLP, Defender detection,
  observability for onboarded agents.
- **Frontier preview at GA:** fully **autonomous agents** and complete
  **agent-as-user** identities. The control plane leans on-behalf-of today.
- **Shallower for third parties:** registry-synced agents from other clouds get
  metadata and a subset of actions; some synced observability was still
  "coming soon" at GA. Deeper control requires the SDK-style onboarding this kit
  demonstrates — it is code work, not a toggle.
- **Caveats that bite:** agents published before GA may need to be rebuilt or
  republished to receive Entra Agent IDs; telemetry is dropped unless a licensed
  user is involved in the flow; the control plane is Microsoft-hosted only
  (no sovereign/self-hosted option at GA).

Where this kit touches a preview or fast-moving surface, the code isolates it in a
clearly marked adapter with a note about what to verify against Microsoft Learn.
Nothing in this repo invents SDK package names, cmdlets, or Graph endpoints and
presents them as fact.

## License

MIT — see `LICENSE`.

# Onboarding runbook — third-party agent → Agent 365 governance

Each step gives you three ways to know you're done: the **portal path** (where it
shows up), the **script path** (what automates it), and the **validation check**
(how you prove it worked). Do them in order; identity problems poison everything
downstream.

## Step 0 — Prerequisites

- **Portal:** Microsoft 365 admin center → verify Agent 365 licensing is assigned
  to the users who will sponsor/manage agents.
- **Script:** `scripts/Test-Prereqs.ps1` — checks your Graph connection and roles,
  and prints the checklist for the parts no script can verify (licenses present,
  Defender for Endpoint onboarded on devices where local agents run).
- **Validate:** the script's checklist is fully green, and you can name the human
  sponsor for the agent you're onboarding.

Licensing reminders (verify against the current Microsoft licensing FAQ before a
rollout): Agent 365 is per-user — $15/user/mo standalone or included in M365 E7
($99). Enterprise standalone requires an M365 E5 base. Agents themselves need no
license; the people who interact with, sponsor, or manage them do.

## Step 1 — Create the Entra agent identity blueprint

An agent identity *blueprint* is the template; agent *identities* are created
under it (think: app registration vs service principal, for agents).

- **Portal:** Microsoft Entra admin center → Identity → Agents (preview surfaces
  move — search "agent identity blueprint").
- **Script:** `scripts/Register-Agent365.ps1 -DisplayName "Contoso Support Agent"`
  creates or reuses the blueprint idempotently.
- **Validate:** the blueprint appears in the Entra admin center and the script
  prints its object ID.

## Step 2 — Assign the sponsor

The sponsor is the human accountable for the agent: they get its access reviews,
its alerts, and the blame.

- **Portal:** on the blueprint/agent identity → Sponsors → add the user.
- **Script:** `Register-Agent365.ps1 -SponsorUpn sponsor@contoso.com`.
- **Validate:** the sponsor is listed on the identity, and the sponsor is a
  licensed, active user (not a shared mailbox, not someone on leave).

## Step 3 — Create the agent identity and credentials

- **Script:** the same `Register-Agent365.ps1` run creates the agent identity
  under the blueprint and prints its **object ID** and **client ID**. Create a
  client secret (or, better, a certificate / federated credential) for it in the
  portal and store it in your secret manager — never in the repo.
- **Validate:** the client ID, copied into the agent's environment
  (`AGENT365_CLIENT_ID`), acquires a token (step 5's demo/real run proves it).

## Step 4 — Register the agent instance

Registration is what puts the agent in the Agent 365 registry with its metadata,
owner, and risk profile. The documented Graph surface for registry records is the
beta agentRegistry API (`GET/PATCH /beta/agentRegistry/agentInstances`) — treat
beta as beta: it can change, and fields should be verified against Microsoft
Learn at implementation time.

- **Portal:** Agent 365 admin experience → Agents → confirm your agent appears
  after registration/sync.
- **Validate:** the agent instance shows the right sponsor, blueprint, and
  environment. If it shows up via registry sync *without* an Entra identity, you
  have inventory, not governance — keep going.

## Step 5 — Wire telemetry

The sample agent (`src/sample_agent/telemetry.py`) emits OpenTelemetry spans —
`agent.invoke`, `tool.call` — with attributes Agent 365 observability cares
about: `agent.id`, `agent.blueprint.id`, `agent.sponsor`, `tool.name`,
`tool.outcome`.

- **Script/config:** set `OTEL_EXPORTER_OTLP_ENDPOINT` to your collector/ingest
  endpoint. Unset = console exporter (demo mode).
- **Validate:** run the agent once, then find the run in the Agent 365
  observability views. ⚠️ Telemetry is dropped if no licensed user is involved
  in the flow — if spans don't land, check licensing before debugging code.

## Step 6 — Enforce tool governance

Tool calls are checked against `policies/mcp-allowlist.json` **before**
execution (`src/sample_agent/tool_governance.py`): default deny, per-tool
`allowed` / `requires_approval` flags. Denied calls raise and are traced, so an
attempted policy violation is itself telemetry.

- **Validate:** ask the sample agent to use the approval-required tool in demo
  mode. It must refuse, and the refusal must appear in the trace output.

## Step 7 — Validate in the admin center

- Registry: agent present, correct identity/sponsor/environment.
- Observability: a full run's spans are visible end-to-end.
- Purview: the agent's activity appears in audit.
- Defender: no unexpected alerts from a known-good run (baseline your noise
  now, not during an incident).

## Step 8 — Conditional Access for agents

Apply the template in `policies/conditional-access-for-agents.md` in
**report-only mode first**, watch it for a full business cycle, then enforce.
CA mistakes against agent identities break business processes at machine speed —
report-only is not optional caution, it's the procedure.

## Rollback

Disable the CA policy → revoke the agent identity's credentials → disable (don't
delete) the agent identity. Deleting destroys the audit trail you may need.

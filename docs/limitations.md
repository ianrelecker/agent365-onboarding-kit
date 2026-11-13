# Limitations — what Agent 365 (and this kit) does not do

Read this before scoping a rollout. Agent 365 is a governance plane; most
disappointment with it comes from expecting it to be something else.

## Agent 365 itself

1. **It is not an agent builder or runtime.** It does not host, run, or improve
   agents. It inventories, identifies, observes, and constrains agents that run
   somewhere else.

2. **Registry sync from other clouds is shallow.** Agents synced from Amazon
   Bedrock, Google Vertex AI, Salesforce Agentforce, Claude Managed Agents,
   Databricks Genie, Oracle GenAI Agents, or Snowflake Cortex arrive as inventory
   records: metadata, some lifecycle actions where the source API supports them
   (e.g. deletion), and partial observability. They do **not** gain Entra
   identities, Conditional Access evaluation, or Purview coverage from sync alone.
   Sync is also a manual/point-in-time operation, not a live feed.

3. **Full governance of a third-party agent is code work.** The agent (or a shim
   around it) must be changed to: authenticate as its Entra agent identity, emit
   telemetry in the ingested shape, and route tool calls through governed access.
   There is no network-level or purely administrative way to retrofit this onto an
   arbitrary agent binary.

4. **Models ≠ surfaces.** Claude (or GPT) models running *inside* Microsoft
   surfaces — Foundry, M365 Copilot — are governable because the surface is
   Microsoft's. The same vendor's own surfaces (claude.ai, ChatGPT) are not
   governed by Agent 365 beyond whatever you register or discover. A ChatGPT
   Enterprise deployment's agents can be governed **if** they are registered
   through the SDK-style onboarding; unregistered use is shadow activity:
   visible, not governed.

5. **Telemetry has a licensing tripwire.** An Agent 365 or Microsoft 365 E7
   license has to be **assigned** to at least one user. A SKU that is only
   present in the tenant is not enough: ingestion can return HTTP 200 while
   `results` says every span was rejected. A 200 is not proof the run landed.
   Spans also have to carry the mandatory attributes (`gen_ai.operation.name`,
   `gen_ai.agent.id`, `microsoft.tenant.id`, `user.id`, and the rest of the
   attribute reference). Missing ones are dropped.

6. **Pre-GA agents may need republishing.** Agents created before GA don't all
   retroactively acquire Entra Agent IDs; expect to rebuild/republish some of
   them.

7. **Autonomous agents are the preview edge.** GA centered on agents acting
   on-behalf-of a user. Fully autonomous agents and complete agent-user
   identities were Frontier preview at GA — plan production governance around
   the on-behalf-of pattern and treat the rest as roadmap.

8. **Microsoft-hosted control plane only.** No self-hosted/sovereign option at
   GA. If your compliance posture requires the control plane in a specific
   boundary, that is a gating question, not a configuration.

9. **Endpoint discovery needs Defender for Endpoint.** Local agent discovery on
   devices depends on DfE being deployed there. Unmanaged/BYOD devices are a
   blind spot by definition.

## This kit

- `scripts/Register-Agent365.ps1` calls the v1.0 agent identity blueprint,
  blueprint principal, and agent identity APIs. It does not create a credential
  unless you pass a managed identity or explicitly ask for a development
  secret. Registry registration is still a separate step (the Microsoft 365
  Agents SDK or `a365 setup all`). This script does not call the beta Agent
  Registry, which Microsoft has been replacing.
- The sample does not use the Microsoft OpenTelemetry Distro. It posts
  OTLP/HTTP+JSON itself, which is the documented fallback, and it fails the
  process unless Agent 365 marks a sink `sent` and marks none `rejected`.
  A 200 with `rejectedSpans: 0` can still be a rejection. The recommended
  path for a new agent that can take the dependency is the Distro.
- Token acquisition follows the autonomous agent exchange: the blueprint
  credential plus `fmi_path` set to the agent identity appId, then a second
  call as the agent identity. A single client-credentials call as the agent
  identity does not work, because the identity has no credential.
- The sample agent's planner is a deterministic stub. Identity, telemetry, and
  the allowlist are the parts that are real.
- The allowlist is local. It is not an Agent 365 control. Turning it off in
  this process does not turn on a server-side replacement.
- The Bicep file creates the managed identity and a closed Key Vault. It
  cannot create the blueprint. Purge protection, once deployed, cannot be
  disabled. The firewall does not bypass Azure services. The template does
  not create the private DNS zone the vault needs when public access is
  disabled.
- The Conditional Access template targets agent identities through
  `includeAgentIdServicePrincipals`. It is report-only on purpose. Beta policy
  JSON can drift; export the portal policy and prefer that if they disagree.

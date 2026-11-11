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

5. **Telemetry has a licensing tripwire.** If no Agent 365-licensed user is
   involved in a flow, the SDK telemetry from that flow may be dropped. Design
   flows so a licensed user (at minimum the sponsor/owner) is in the loop, and
   verify ingestion in the admin center rather than assuming it.

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

- `scripts/Register-Agent365.ps1` targets the Microsoft Graph PowerShell SDK and
  isolates preview-specific calls behind clearly marked TODO/verify comments.
  Agent-identity Graph surfaces were still evolving at GA — expect to adjust the
  adapter section against current Microsoft Learn docs on first use in a tenant.
- The sample agent's LLM is a deterministic stub. The point is the governance
  wiring (identity, telemetry, allowlist), which is real; the "intelligence" is
  deliberately not.
- The Bicep file covers only what Bicep can express. Agent identity blueprints
  are a Graph-side construct; Bicep cannot create them directly, and the file
  says so rather than pretending otherwise.
- The Conditional Access template in `policies/` is a starting point to adapt
  with your CA administrator — CA for agents has failure modes (locking out a
  business process at 2 AM) that a template cannot foresee.

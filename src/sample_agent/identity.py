"""Agent identity for the sample third-party agent.

In production this agent authenticates as its Entra **agent identity** (created
by scripts/Register-Agent365.ps1) using MSAL client credentials. The client ID
represents the agent identity; the secret lives in the environment here only
because this is a sample — in a real deployment it comes from Key Vault or a
federated credential, never from a committed file.

Demo mode: with no AGENT365_* variables set, returns a clearly-labelled stub
token so the rest of the governance wiring (telemetry, allowlist) can be
exercised without a tenant.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class AgentIdentity:
    client_id: str
    tenant_id: str
    demo_mode: bool

    @property
    def label(self) -> str:
        return f"demo-agent" if self.demo_mode else self.client_id


def load_identity() -> AgentIdentity:
    client_id = os.environ.get("AGENT365_CLIENT_ID", "")
    tenant_id = os.environ.get("AGENT365_TENANT_ID", "")
    demo = not (client_id and tenant_id)
    return AgentIdentity(client_id=client_id or "demo", tenant_id=tenant_id or "demo", demo_mode=demo)


def acquire_token(identity: AgentIdentity, scope: str = "https://graph.microsoft.com/.default") -> str:
    """Return an access token for the agent identity, or a stub in demo mode."""
    if identity.demo_mode:
        return "DEMO-TOKEN-NOT-A-REAL-TOKEN"

    # Real path: MSAL confidential client (the agent identity is a
    # workload identity — no user is present, which is exactly why the
    # sponsor + CA-for-agents story matters around it).
    import msal  # imported lazily so demo mode needs no dependencies installed

    secret = os.environ.get("AGENT365_CLIENT_SECRET", "")
    if not secret:
        raise RuntimeError(
            "AGENT365_CLIENT_ID/TENANT_ID are set but AGENT365_CLIENT_SECRET is not. "
            "In production, source the secret from Key Vault instead of the environment."
        )
    app = msal.ConfidentialClientApplication(
        client_id=identity.client_id,
        client_credential=secret,
        authority=f"https://login.microsoftonline.com/{identity.tenant_id}",
    )
    result = app.acquire_token_for_client(scopes=[scope])
    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result.get('error_description', result)}")
    return result["access_token"]

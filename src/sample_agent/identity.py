"""Entra agent identity for a third-party agent.

An agent identity has no credentials of its own. The blueprint holds the
credential and exchanges it for a token the agent identity can use. That is
two calls to the tenant token endpoint:

1. Blueprint presents its credential with ``fmi_path`` set to the agent
   identity's client ID and scope ``api://AzureADTokenExchange/.default``.
2. The agent identity presents that exchange token as a client assertion and
   receives a resource token.

Documented in "Authenticate and acquire tokens for autonomous agents"
(Microsoft Learn). Client secrets are a local-development credential only.
Production blueprints should use a managed identity federated credential or a
certificate.

Demo mode runs only when no ``AGENT365_*`` identity variable is set.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, MutableMapping

TOKEN_HOST = "login.microsoftonline.com"
IMDS_HOST = "169.254.169.254"
EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"

# Any of these means the process is trying to leave demo mode.
_PRODUCTION_MARKERS = (
    "AGENT365_TENANT_ID",
    "AGENT365_BLUEPRINT_CLIENT_ID",
    "AGENT365_AGENT_CLIENT_ID",
    "AGENT365_CLIENT_ID",
    "AGENT365_CLIENT_SECRET",
    "AGENT365_CLIENT_CERT_PATH",
    "AGENT365_MANAGED_IDENTITY_CLIENT_ID",
    "AGENT365_ALLOW_CLIENT_SECRET",
)


class IdentityError(RuntimeError):
    """Configuration or token-endpoint failure safe to show to an operator."""


@dataclass
class ClientSecret:
    secret: str

    def __repr__(self) -> str:
        return "ClientSecret(secret='[redacted]')"


@dataclass
class CertificateCredential:
    private_key: object
    thumbprint: str

    def __repr__(self) -> str:
        return f"CertificateCredential(thumbprint='{self.thumbprint}')"

    def sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return self.private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())


@dataclass
class ManagedIdentityCredential:
    client_id: str


Credential = ClientSecret | CertificateCredential | ManagedIdentityCredential


@dataclass
class AgentIdentity:
    blueprint_client_id: str
    agent_client_id: str
    tenant_id: str
    demo_mode: bool
    credential: Credential | None = None
    agent_name: str = "sample-agent"
    caller_object_id: str = ""
    caller_upn: str = ""
    _cache: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        return "demo-agent" if self.demo_mode else self.agent_client_id


def load_env_file(path: Path, environ: MutableMapping[str, str]) -> None:
    """Load KEY=VALUE lines. Does not override variables already set.

    Malformed lines raise IdentityError with a line number and no line text,
    so a secret in a bad line is not echoed.
    """
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if len(text) > 100_000:
        raise IdentityError(f"{path} is unexpectedly large")
    import re

    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            raise IdentityError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not re.fullmatch(_ENV_NAME, key):
            raise IdentityError(f"{path}:{lineno}: invalid variable name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        environ.setdefault(key, value)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require_guid(value: str, name: str) -> str:
    import re

    if not re.fullmatch(_GUID, value or ""):
        raise IdentityError(f"{name} must be a GUID")
    return value


def load_identity(environ: Mapping[str, str] | None = None) -> AgentIdentity:
    env = os.environ if environ is None else environ
    markers = [name for name in _PRODUCTION_MARKERS if env.get(name, "").strip()]
    if not markers:
        return AgentIdentity(
            blueprint_client_id="demo",
            agent_client_id="demo",
            tenant_id="demo",
            demo_mode=True,
            agent_name=env.get("AGENT365_AGENT_NAME", "").strip() or "sample-agent",
            caller_upn=env.get("AGENT365_SPONSOR_UPN", "").strip(),
        )
    if "AGENT365_CLIENT_ID" in markers and "AGENT365_BLUEPRINT_CLIENT_ID" not in markers:
        raise IdentityError(
            "AGENT365_CLIENT_ID is not a complete identity. Set "
            "AGENT365_BLUEPRINT_CLIENT_ID (the blueprint appId) and "
            "AGENT365_AGENT_CLIENT_ID (the agent identity appId used as fmi_path)."
        )

    tenant_id = _require_guid(env.get("AGENT365_TENANT_ID", "").strip(), "AGENT365_TENANT_ID")
    blueprint_id = _require_guid(
        env.get("AGENT365_BLUEPRINT_CLIENT_ID", "").strip(),
        "AGENT365_BLUEPRINT_CLIENT_ID",
    )
    agent_id = _require_guid(
        env.get("AGENT365_AGENT_CLIENT_ID", "").strip(),
        "AGENT365_AGENT_CLIENT_ID",
    )
    if blueprint_id.lower() == agent_id.lower():
        raise IdentityError(
            "AGENT365_BLUEPRINT_CLIENT_ID and AGENT365_AGENT_CLIENT_ID are the same. "
            "fmi_path must be the agent identity appId, not the blueprint appId."
        )

    credential = _load_credential(env)
    caller = env.get("AGENT365_CALLER_OBJECT_ID", "").strip()
    if caller:
        _require_guid(caller, "AGENT365_CALLER_OBJECT_ID")
    return AgentIdentity(
        blueprint_client_id=blueprint_id,
        agent_client_id=agent_id,
        tenant_id=tenant_id,
        demo_mode=False,
        credential=credential,
        agent_name=env.get("AGENT365_AGENT_NAME", "").strip() or "sample-agent",
        caller_object_id=caller,
        caller_upn=env.get("AGENT365_SPONSOR_UPN", "").strip(),
    )


def _load_credential(env: Mapping[str, str]) -> Credential:
    secret = env.get("AGENT365_CLIENT_SECRET", "").strip()
    cert_path = env.get("AGENT365_CLIENT_CERT_PATH", "").strip()
    mi = env.get("AGENT365_MANAGED_IDENTITY_CLIENT_ID", "").strip()
    allow_secret = _truthy(env.get("AGENT365_ALLOW_CLIENT_SECRET", ""))
    chosen = [name for name, present in (
        ("client secret", bool(secret)),
        ("certificate", bool(cert_path)),
        ("managed identity", bool(mi)),
    ) if present]
    if len(chosen) > 1:
        raise IdentityError(
            "Set only one blueprint credential: managed identity, certificate, or "
            "a development client secret. Found: " + ", ".join(chosen) + "."
        )
    if secret:
        if not allow_secret:
            raise IdentityError(
                "AGENT365_CLIENT_SECRET is set, but client secrets are not a production "
                "credential for an agent identity blueprint. Use a managed identity "
                "federated credential or a certificate. For a local development tenant "
                "only, also set AGENT365_ALLOW_CLIENT_SECRET=true."
            )
        return ClientSecret(secret)
    if cert_path:
        return _load_certificate(cert_path)
    if mi:
        return ManagedIdentityCredential(_require_guid(mi, "AGENT365_MANAGED_IDENTITY_CLIENT_ID"))
    raise IdentityError(
        "Production identity is partially configured but no blueprint credential is set. "
        "Set AGENT365_MANAGED_IDENTITY_CLIENT_ID or AGENT365_CLIENT_CERT_PATH."
    )


def _load_certificate(path: str) -> CertificateCredential:
    file = Path(path)
    if not file.is_file():
        raise IdentityError(f"AGENT365_CLIENT_CERT_PATH does not exist: {path}")
    data = file.read_bytes()
    if len(data) > 64 * 1024:
        raise IdentityError("certificate file is unexpectedly large")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise IdentityError("certificate file must be PEM text") from exc
    key_pem = _pem_block(text, "PRIVATE KEY")
    cert_pem = _pem_block(text, "CERTIFICATE")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    try:
        key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
        cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
    except (ValueError, TypeError):
        raise IdentityError("could not read the PEM certificate and private key") from None
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_module

    if not isinstance(key, rsa_module.RSAPrivateKey):
        raise IdentityError("blueprint certificate must be an RSA private key")
    try:
        keys_match = key.public_key().public_numbers() == cert.public_key().public_numbers()
    except (AttributeError, TypeError, ValueError):
        keys_match = False
    if not keys_match:
        raise IdentityError("certificate does not match the private key")
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    if cert.not_valid_after_utc <= now:
        raise IdentityError("blueprint certificate is expired")
    if cert.not_valid_before_utc > now + timedelta(minutes=5):
        raise IdentityError("blueprint certificate is not valid yet")
    thumbprint = cert.fingerprint(hashes.SHA1()).hex()
    return CertificateCredential(private_key=key, thumbprint=thumbprint)


def _pem_block(text: str, label: str) -> str:
    start = f"-----BEGIN {label}-----"
    end = f"-----END {label}-----"
    # Also accept "BEGIN RSA PRIVATE KEY" via the PRIVATE KEY search below.
    if label == "PRIVATE KEY" and start not in text and "-----BEGIN RSA PRIVATE KEY-----" in text:
        start = "-----BEGIN RSA PRIVATE KEY-----"
        end = "-----END RSA PRIVATE KEY-----"
    at = text.find(start)
    stop = text.find(end)
    if at < 0 or stop < 0 or stop < at:
        raise IdentityError(f"PEM is missing a {label} block")
    return text[at : stop + len(end)] + "\n"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_client_assertion(
    credential: CertificateCredential,
    client_id: str,
    tenant_id: str,
    now: int | None = None,
) -> str:
    """Signed client assertion for the blueprint's certificate credential."""
    issued = int(time.time() if now is None else now)
    # Two minutes of clock skew, and a lifetime of exactly ten minutes.
    # Entra rejects a client assertion whose nbf/exp window is longer than that.
    not_before = issued - 120
    header = {
        "alg": "RS256",
        "typ": "JWT",
        "x5t": _b64url(bytes.fromhex(credential.thumbprint)),
    }
    payload = {
        "aud": f"https://{TOKEN_HOST}/{tenant_id}/oauth2/v2.0/token",
        "iss": client_id,
        "sub": client_id,
        "jti": str(uuid.uuid4()),
        "nbf": not_before,
        "exp": not_before + 600,
    }
    signing_input = ".".join(
        (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        )
    )
    signature = credential.sign(signing_input.encode("ascii"))
    return signing_input + "." + _b64url(signature)


def _token_url(tenant_id: str) -> str:
    _require_guid(tenant_id, "tenant id")
    return f"https://{TOKEN_HOST}/{tenant_id}/oauth2/v2.0/token"


def _secrets_for(identity: AgentIdentity) -> list[str]:
    secrets: list[str] = []
    if isinstance(identity.credential, ClientSecret):
        secrets.append(identity.credential.secret)
    return [item for item in secrets if item]


def _redact(text: str, secrets: list[str]) -> str:
    import re

    redacted = text
    for secret in secrets:
        redacted = redacted.replace(secret, "[redacted]")
    redacted = re.sub(
        r"eyJ[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}",
        "[redacted-jwt]",
        redacted,
    )
    redacted = re.sub(r"(?i)(client_secret|client_assertion)=[^&\s]+", r"\1=[redacted]", redacted)
    if len(redacted) > 500:
        redacted = redacted[:500] + "…"
    return redacted


class UrllibTransport:
    """Token and IMDS calls with the hosts fixed. Not a general HTTP client."""

    def post_form(self, url: str, form: Mapping[str, str], timeout: float) -> dict:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != TOKEN_HOST:
            raise IdentityError("refusing token request to an unexpected host")
        data = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return _read_json(request, timeout)

    def get_imds(self, url: str, timeout: float) -> dict:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "http" or parsed.hostname != IMDS_HOST or parsed.path != "/metadata/identity/oauth2/token":
            raise IdentityError("refusing metadata request to an unexpected host")
        request = urllib.request.Request(url, method="GET")
        request.add_header("Metadata", "true")
        request.add_header("Accept", "application/json")
        return _read_json(request, timeout)


def _read_json(request: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise IdentityError(f"HTTP {exc.code}") from _ErrorBody(raw, exc.code)
    except urllib.error.URLError as exc:
        raise IdentityError("token endpoint was unreachable") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityError("token endpoint returned a non-JSON body") from exc
    if not isinstance(payload, dict):
        raise IdentityError("token endpoint returned an unexpected payload")
    return payload


class _ErrorBody(Exception):
    def __init__(self, raw: bytes, status: int):
        self.raw = raw
        self.status = status
        super().__init__("http error body")


def _raise_token_failure(payload_or_body: dict | bytes, secrets: list[str], status: int | None = None) -> None:
    text = ""
    parsed: object
    if isinstance(payload_or_body, bytes):
        try:
            parsed = json.loads(payload_or_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            text = str(parsed.get("error_description") or parsed.get("error") or "")
        else:
            text = payload_or_body.decode("utf-8", errors="replace")
    else:
        text = str(
            payload_or_body.get("error_description")
            or payload_or_body.get("error")
            or "token request failed"
        )
    prefix = f"HTTP {status}: " if status else ""
    raise IdentityError("Token acquisition failed: " + prefix + _redact(text, secrets))


def acquire_token(
    identity: AgentIdentity,
    scope: str = GRAPH_SCOPE,
    transport: UrllibTransport | None = None,
    now: Callable[[], float] | None = None,
) -> str:
    """Return a resource token for the agent identity, or a stub in demo mode."""
    if identity.demo_mode:
        return "DEMO-TOKEN-NOT-A-REAL-TOKEN"
    if not scope or any(character.isspace() for character in scope):
        raise IdentityError("token scope is empty or contains whitespace")
    clock = time.monotonic if now is None else now
    cached = identity._cache.get(scope)
    if cached and cached[1] > clock():
        return cached[0]
    if identity.credential is None:
        raise IdentityError("production identity has no credential")
    client = transport or UrllibTransport()
    secrets = _secrets_for(identity)
    try:
        exchange = _blueprint_exchange_token(identity, client, secrets)
        resource = _agent_resource_token(identity, client, exchange, scope, secrets)
    except IdentityError as exc:
        cause = exc.__cause__
        if isinstance(cause, _ErrorBody):
            _raise_token_failure(cause.raw, secrets, status=cause.status)
        raise
    token = resource.get("access_token")
    if not isinstance(token, str) or not token:
        _raise_token_failure(resource, secrets)
    expires_in = resource.get("expires_in", 0)
    try:
        # Refresh a minute early, and never trust a multi-hour lifetime.
        ttl = min(3600, max(0, int(expires_in) - 60))
    except (TypeError, ValueError):
        ttl = 0
    if ttl:
        identity._cache[scope] = (token, clock() + ttl)
    return token


def _blueprint_exchange_token(identity: AgentIdentity, transport: UrllibTransport, secrets: list[str]) -> str:
    form: dict[str, str] = {
        "client_id": identity.blueprint_client_id,
        "scope": EXCHANGE_SCOPE,
        "grant_type": "client_credentials",
        "fmi_path": identity.agent_client_id,
    }
    credential = identity.credential
    if isinstance(credential, ClientSecret):
        form["client_secret"] = credential.secret
    elif isinstance(credential, CertificateCredential):
        form["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        form["client_assertion"] = build_client_assertion(
            credential, identity.blueprint_client_id, identity.tenant_id
        )
    elif isinstance(credential, ManagedIdentityCredential):
        assertion = _managed_identity_assertion(credential, transport, secrets)
        form["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        form["client_assertion"] = assertion
        secrets.append(assertion)
    else:
        raise IdentityError("unsupported credential type")
    payload = transport.post_form(_token_url(identity.tenant_id), form, timeout=20)
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        _raise_token_failure(payload, secrets)
    return token


def _managed_identity_assertion(
    credential: ManagedIdentityCredential,
    transport: UrllibTransport,
    secrets: list[str],
) -> str:
    query = urllib.parse.urlencode(
        {
            "api-version": "2018-02-01",
            "resource": "api://AzureADTokenExchange",
            "client_id": credential.client_id,
        }
    )
    url = f"http://{IMDS_HOST}/metadata/identity/oauth2/token?{query}"
    payload = transport.get_imds(url, timeout=5)
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        _raise_token_failure(payload, secrets)
    return token


def _agent_resource_token(
    identity: AgentIdentity,
    transport: UrllibTransport,
    exchange_token: str,
    scope: str,
    secrets: list[str],
) -> dict:
    secrets.append(exchange_token)
    form = {
        "client_id": identity.agent_client_id,
        "scope": scope,
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": exchange_token,
    }
    return transport.post_form(_token_url(identity.tenant_id), form, timeout=20)

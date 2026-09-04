# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2", "httpx", "regent-httpsig>=0.4.0"]
# ///
"""get4agent buyer MCP — lets YOUR agent buy real goods under YOUR mandate.

Runs as a local stdio MCP server (Claude Desktop / any MCP host). On first setup it
generates a signing key on THIS machine (the key never leaves it), self-registers the
agent at get4agent.com over an RFC 9421-signed request, and the OWNER approves it by
clicking the claim link e-mailed to them — authority is granted by a human, never by
a key. After approval every purchase is a signed request governed by the owner's
mandate, balance and kill switch.
"""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

MARKET = "https://get4agent.com"
DIRECTORY_ORIGIN = "https://regentprotocol.org"  # hosts the public-key directory
KEY_PATH = Path.home() / ".get4agent" / "agent.seed"
PASSPORT_PATH = Path.home() / ".get4agent" / "passport.jwt"

mcp = MCPServer("get4agent")


def _signer():
    from regent_httpsig.sign import EgressSigner
    if not KEY_PATH.exists():
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        seed = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        KEY_PATH.write_text(seed)
        KEY_PATH.chmod(0o600)
    return EgressSigner(seed=KEY_PATH.read_text().strip(),
                        signature_agent=DIRECTORY_ORIGIN)


def _bootstrap_token(signer) -> str:
    """Identity-only passport for this key — minted once, cached, auto-renewed.
    The private key NEVER leaves this machine; the platform only sees the public
    half and attests "this key is this key" (zero authority until the owner
    approves the claim e-mail)."""
    if PASSPORT_PATH.exists():
        return PASSPORT_PATH.read_text().strip()
    r = httpx.post(f"{MARKET}/v1/agents/bootstrap",
                   json={"jwk": signer.public_jwk}, timeout=30.0)
    r.raise_for_status()
    token = r.json()["agent_token"]
    PASSPORT_PATH.write_text(token)
    PASSPORT_PATH.chmod(0o600)
    return token


def _signed_headers(signer, method: str, url: str) -> dict:
    host = httpx.URL(url).host
    h = signer.sign(method, url, {"Host": host, "Content-Type": "application/json"})
    # Label must match the message signature's label (sig1) — the verifier pairs them.
    h["Signature-Key"] = f'sig1=jwt;jwt="{_bootstrap_token(signer)}"'
    return h


def _post_signed(path: str, body: dict) -> httpx.Response:
    signer = _signer()
    url = f"{MARKET}{path}"
    payload = json.dumps(body).encode()
    headers = _signed_headers(signer, "POST", url)
    return httpx.post(url, content=payload, headers=headers, timeout=60.0)


def _get_signed(path: str) -> httpx.Response:
    signer = _signer()
    url = f"{MARKET}{path}"
    return httpx.get(url, headers=_signed_headers(signer, "GET", url), timeout=30.0)


@mcp.tool()
def setup(contact_email: str, agent_name: str = "personal shopping agent") -> str:
    """One-time onboarding, fully automatic: generates this agent's own key (it
    never leaves this machine), gets an identity-only bootstrap passport, and
    self-registers at get4agent. The owner then approves via the claim e-mail —
    authority always comes from a human click, never from a key."""
    signer = _signer()
    r = _post_signed("/v1/agents/register", {
        "name": agent_name, "contact_email": contact_email,
        "intent": "Buy digital goods on get4agent under my owner's mandate.",
    })
    if r.status_code == 401:
        # Stale/expired bootstrap passport — mint a fresh one and retry once.
        PASSPORT_PATH.unlink(missing_ok=True)
        r = _post_signed("/v1/agents/register", {
            "name": agent_name, "contact_email": contact_email,
            "intent": "Buy digital goods on get4agent under my owner's mandate.",
        })
    r.raise_for_status()
    reg = r.json()
    return (f"Registered (status: {reg.get('status')}). A claim e-mail was sent to "
            f"{contact_email} — the OWNER must click it while signed in on get4agent.com. "
            "After approval, purchases run under the owner's mandate.")


@mcp.tool()
def status() -> str:
    """Registration + approval state of this agent."""
    r = _get_signed("/v1/agents/registration")
    if r.status_code == 401:
        return "Signature not accepted — run setup to refresh the passport."
    if r.status_code == 404:
        return "Not registered yet — run setup."
    return json.dumps(r.json(), ensure_ascii=False, indent=1)


@mcp.tool()
def search_catalog(query: str = "", category: str = "gaming") -> str:
    """Search get4agent's catalog of real goods (public, no auth)."""
    r = httpx.post(f"{MARKET}/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_listings",
                   "arguments": {"q": query, "category": category}},
    }, timeout=30.0)
    r.raise_for_status()
    return json.dumps(r.json().get("result", {}), ensure_ascii=False)[:4000]


@mcp.tool()
def buy(listing_id: str, game_user_id: str = "", game_server_id: str = "") -> str:
    """Buy a listing. The request is signed by this agent's key and must pass the
    owner's mandate (per-purchase / daily limits) before any money moves. Returns
    the delivered key/code and the receipt. game_user_id is only for direct
    game top-ups (e.g. PUBG Mobile)."""
    body: dict = {"agent_id": "mcp-buyer"}
    if game_user_id:
        body["gameUserId"] = game_user_id
    if game_server_id:
        body["gameServerId"] = game_server_id
    r = _post_signed(f"/v1/call/{listing_id}", body)
    try:
        data = r.json()
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:500]}"
    if r.status_code >= 400:
        det = data.get("detail") or data
        return (f"REFUSED ({det.get('code', r.status_code)}): {det.get('message', '')} "
                "— nothing was charged.")
    result = data.get("result", {})
    receipt = data.get("receipt", {})
    return json.dumps({
        "delivered": result.get("delivery"),
        "product": result.get("product"),
        "order_id": result.get("orderId"),
        "paid": f'{receipt.get("amount")} {receipt.get("currency")}',
        "balance_after": receipt.get("balance_after"),
        "decision_id": data.get("decision_id"),
    }, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    mcp.run()

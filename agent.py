# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2", "httpx", "regent-httpsig>=0.7.3"]
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
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer

MARKET = "https://get4agent.com"
# EgressSigner wants an origin for its Web Bot Auth mode. This connector does not
# sign that way (see _signed_headers): the value is never sent.
DIRECTORY_ORIGIN = "https://regentprotocol.org"
KEY_PATH = Path.home() / ".get4agent" / "agent.seed"
PASSPORT_PATH = Path.home() / ".get4agent" / "passport.jwt"

mcp = MCPServer("get4agent")


def _write_private(path: Path, text: str) -> bool:
    """Create ``path`` holding ``text``, readable by the owner only FROM THE START:
    the file is opened with mode 0600 and ``O_EXCL``, so it never exists with wider
    permissions (write-then-chmod leaves it readable under the umask for a moment)
    and an existing file is never overwritten. Returns False when the file was
    already there (another process created it first): the caller reads that one."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    return True


def _signer():
    from regent_httpsig.sign import EgressSigner
    if not KEY_PATH.exists():
        seed = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        _write_private(KEY_PATH, seed)
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
    # Useless without the private key (every request proves possession), but it is
    # this agent's identity token: same private-from-the-start rule as the seed.
    if not _write_private(PASSPORT_PATH, token):
        return PASSPORT_PATH.read_text().strip()
    return token


def _signed_headers(signer, method: str, url: str, body: bytes | None = None) -> dict:
    """Sign the way get4agent verifies: AAuth (draft-hardt-oauth-aauth-protocol-11).

    The agent token rides in ``Signature-Key`` under the ``jwt`` scheme and the
    signature covers ``@method @authority @path signature-key``; with a body also
    ``content-digest content-type``, and a ``Content-Digest`` header is sent. No
    ``keyid``, ``alg`` or ``tag`` parameter and no ``Signature-Agent`` header: the
    key is the token's ``cnf.jwk``. The body must be sent exactly as passed here.

    Until 27.11.2026 this signed the Web Bot Auth way and added ``Signature-Key``
    afterwards; the server has required the -11 form since 06.10.2026 and answered
    every such request 401 (``invalid_input: signature does not cover signature-key``)."""
    return signer.sign_aauth(method, url, token=_bootstrap_token(signer), body=body)


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                   re.IGNORECASE)


def _call_path(listing_id: str) -> str:
    """The path of a purchase. ``listing_id`` comes from the model, which may have
    read it in seller-written text: anything that is not a UUID is refused BEFORE a
    key is loaded or a byte is sent. Otherwise ``../budget/token`` would become a
    correctly signed ``POST /v1/budget/token``. The segment is percent-encoded as
    well, so the rule does not rest on the pattern alone."""
    if not isinstance(listing_id, str) or not _UUID.fullmatch(listing_id):
        raise ValueError("listing_id must be a listing UUID from search_catalog "
                         "(xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). Nothing was sent.")
    return "/v1/call/" + quote(listing_id, safe="")


def _signed_url(path: str) -> str:
    """``MARKET + path``, refused unless the HTTP client will request exactly
    ``path``: no dot segment, query or fragment may change what gets signed."""
    url = f"{MARKET}{path}"
    if (".." in path.split("/") or "." in path.split("/") or "?" in path or "#" in path
            or httpx.URL(url).raw_path.decode("ascii") != path):
        raise ValueError("refusing to sign a request whose path is not the intended one. "
                         "Nothing was sent.")
    return url


def _post_signed(path: str, body: dict) -> httpx.Response:
    url = _signed_url(path)
    signer = _signer()
    payload = json.dumps(body).encode()
    headers = _signed_headers(signer, "POST", url, payload)
    return httpx.post(url, content=payload, headers=headers, timeout=60.0)


def _get_signed(path: str) -> httpx.Response:
    url = _signed_url(path)
    signer = _signer()
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
def search_catalog(query: str = "", category: str = "") -> str:
    """Search get4agent's catalog of real goods (public, no auth)."""
    r = httpx.post(f"{MARKET}/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_listings",
                   "arguments": {"query": query, "category": category}},
    }, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"].get("message", "Marketplace search failed"))
    result = data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Invalid marketplace search response")
    if result.get("isError"):
        raise RuntimeError(json.dumps(result.get("content", []), ensure_ascii=False))
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def buy(listing_id: str, game_user_id: str = "", game_server_id: str = "") -> str:
    """Buy a listing. The request is signed by this agent's key and must pass the
    owner's mandate (per-purchase / daily limits) before any money moves. Returns
    the delivered key/code and the receipt. game_user_id is only for direct
    game top-ups (e.g. PUBG Mobile)."""
    path = _call_path(listing_id)  # refuses anything but a UUID, before any I/O
    body: dict = {"agent_id": "mcp-buyer"}
    if game_user_id:
        body["gameUserId"] = game_user_id
    if game_server_id:
        body["gameServerId"] = game_server_id
    r = _post_signed(path, body)
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

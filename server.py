"""GET4AGENT MARKET — local (stdio) MCP server.

Zero-dependency proxy: exposes the marketplace tools over the MCP stdio transport
(newline-delimited JSON-RPC) and forwards each `tools/call` to the hosted endpoint
(https://get4agent.com/mcp). Lets any stdio MCP client use GET4AGENT, and gives
directories (Glama etc.) a runnable server to introspect. Free tools need no auth.

Env: GET4AGENT_MCP_URL (default https://get4agent.com/mcp).
Stdlib only — nothing to pip install, so the build/introspection never breaks on deps.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

REMOTE = os.environ.get("GET4AGENT_MCP_URL", "https://get4agent.com/mcp")
PROTOCOL = "2024-11-05"

S = {"type": "string"}


def _tool(name, desc, props, required):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    _tool("search_listings", "Search the GET4AGENT MARKET catalog of KYA-verified API listings.",
          {"query": S, "category": S, "kind": S, "verified_only": {"type": "boolean"}}, []),
    _tool("get_listing", "Get full detail (price, seller KYA status, seller DID) for one listing.",
          {"listing_id": S}, ["listing_id"]),
    _tool("verify_agent", "Check an agent's KYA / identity status.",
          {"agent_id": S}, ["agent_id"]),
    _tool("track_parcel", "Track a Kazakhstan Post (Kazpost) parcel by barcode — status + route history.",
          {"track": S}, ["track"]),
    _tool("validate_iin", "Validate a Kazakhstan IIN / BIN (checksum) and parse it.",
          {"iin": S}, ["iin"]),
    _tool("validate_kz_iban", "Validate a Kazakhstan IBAN (ISO 7064 mod-97).",
          {"iban": S}, ["iban"]),
    _tool("lei_lookup", "Look up a global Legal Entity Identifier (GLEIF) by LEI or name.",
          {"lei": S, "q": S}, []),
    _tool("kzt_rates", "Current KZT (Kazakhstani tenge) exchange rates.",
          {"codes": S}, []),
    _tool("fx_rates", "ECB reference FX cross-rates (Frankfurter).",
          {"base": S, "symbols": S}, []),
    _tool("weather", "Current weather + short forecast for a latitude/longitude (Open-Meteo).",
          {"lat": {"type": "number"}, "lon": {"type": "number"}}, ["lat", "lon"]),
]


def _forward(name: str, arguments: dict) -> str:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}}).encode()
    req = urllib.request.Request(REMOTE, data=payload,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        return f"error contacting {REMOTE}: {exc}"
    if data.get("error"):
        return f"error: {data['error']}"
    result = data.get("result", data)
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return json.dumps(result, ensure_ascii=False)


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _handle(msg: dict):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "GET4AGENT MARKET", "version": "1.0.0"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        text = _forward(params.get("name", ""), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is not None:  # unknown method with an id → JSON-RPC error
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None  # notification → no response


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = _handle(msg)
        if reply is not None:
            _send(reply)


if __name__ == "__main__":
    main()

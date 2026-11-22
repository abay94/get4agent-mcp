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
import http.client
import os
import sys
import urllib.request

REMOTE = os.environ.get("GET4AGENT_MCP_URL", "https://get4agent.com/mcp")
PROTOCOL = "2024-11-05"

S = {"type": "string"}
B = {"type": "boolean"}
I = {"type": "integer"}


def _tool(name, desc, props, required):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


# Kept static on purpose: directories introspect this server without network access.
# It mirrors `tools/list` of the hosted endpoint; tests/test_discovery.py pins the names.
TOOLS = [
    _tool("list_categories", "List the catalog's categories with the number of listings in each.",
          {"verified_only": B}, []),
    _tool("search_listings", "Search the GET4AGENT MARKET catalog, newest first, in pages "
          "(has_more / next_offset). Omit category to search all categories.",
          {"query": S, "category": S, "kind": S, "verified_only": B, "limit": I, "offset": I}, []),
    _tool("get_listing", "Get full detail (price, seller, seller verification) for one listing.",
          {"listing_id": S}, ["listing_id"]),
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


def _forward(method: str, params: dict, request_id) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": request_id,
                          "method": method, "params": params}).encode()
    req = urllib.request.Request(REMOTE, data=payload,
                                 headers={"content-type": "application/json",
                                          "accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if not isinstance(data, dict):
            raise ValueError("Invalid upstream response")
        if isinstance(data.get("error"), dict):
            return {"jsonrpc": "2.0", "id": request_id, "error": data["error"]}
        if not isinstance(data.get("result"), dict):
            raise ValueError("Missing upstream result")
        return {"jsonrpc": "2.0", "id": request_id, "result": data["result"]}
    except (OSError, ValueError, http.client.HTTPException):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32603, "message": "Marketplace unavailable or invalid response"}}


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
        return _forward(method, msg.get("params") or {}, mid)
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

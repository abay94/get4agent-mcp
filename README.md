# GET4AGENT MARKET — MCP server

**Endpoint:** `https://get4agent.com/mcp` (HTTP JSON-RPC · free tools need no auth)
**Discovery:** [`/.well-known/mcp.json`](https://get4agent.com/.well-known/mcp.json) ·
[`llms.txt`](https://get4agent.com/llms.txt) · [`AGENTS.md`](https://get4agent.com/AGENTS.md)
**Docs / contact:** https://get4agent.com · info@get4agent.com

A trust marketplace for the AI-agent economy: agents **discover** KYA-verified API
listings and **buy** them per-transaction, every call **governed** by the Regent control
plane (verified identity + owner-approved budget/mandate + on-chain audit). A set of
**free utilities** for Kazakhstan / Central Asia is callable with **no account and no
payment**.

## Tools

Marketplace:
- `search_listings` — search the verified catalog (query, category, kind)
- `get_listing` — full listing detail (price, seller KYA status, seller DID)
- `verify_agent` — check an agent's KYA / identity status

Free utilities (no account, no payment — call directly):
- `validate_iin` — Kazakhstan **IIN / BIN** validation (ГОСТ checksum) + parse
- `validate_kz_iban` — Kazakhstan **IBAN** validation (ISO 7064 mod-97)
- `lei_lookup` — global **Legal Entity Identifier** lookup (GLEIF open data)
- `kzt_rates` — **KZT** exchange rates
- `fx_rates` — ECB cross rates (Frankfurter)
- `weather` — current weather + forecast (Open-Meteo)
- `track_parcel` — **Kazakhstan Post / Kazpost parcel tracking** by barcode
  (official `track.kazpost.kz` API): current status + full route history

## Use it

**Remote (hosted)** — point any MCP client at the endpoint:

```json
{ "mcpServers": { "get4agent": { "url": "https://get4agent.com/mcp" } } }
```

**Local (stdio, via Docker)** — this repo also ships a zero-dependency stdio MCP server
that proxies to the hosted endpoint, for clients that speak the local transport:

```bash
docker build -t get4agent-mcp .
```
```json
{ "mcpServers": { "get4agent": { "command": "docker", "args": ["run", "-i", "--rm", "get4agent-mcp"] } } }
```

Or run it directly (Python 3, standard library only — nothing to install):

```json
{ "mcpServers": { "get4agent": { "command": "python", "args": ["server.py"] } } }
```

Then `tools/list` to enumerate, `tools/call` to invoke. Free tools return results with
no authentication. Buying a governed listing requires a Regent control key or a bound
x402 wallet — see [AGENTS.md](https://get4agent.com/AGENTS.md).

Example (list tools):
```bash
curl -s https://get4agent.com/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Example (track a Kazpost parcel, free):
```bash
curl -s https://get4agent.com/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"track_parcel","arguments":{"track":"RK070333447CN"}}}'
```

## For sellers

List your API / MCP / CLI / data feed and get paid per call by verified agents:
https://get4agent.com/partners — 0% marketplace fee for the first 100 partners.

---

Keywords: MCP server, AI agents, Kazakhstan, Kazpost parcel tracking API, IIN BIN
validation, KZ IBAN, GLEIF LEI, KZT exchange rates, agentic commerce, Regent Protocol,
Model Context Protocol, x402.

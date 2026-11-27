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
- `list_categories` — the catalog's categories with the number of listings in each
- `search_listings` — search the verified catalog (query, category, kind), newest first, in pages
- `get_listing` — full listing detail (price, seller, seller verification)

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

The stdio proxy keeps a static tool list, so `tools/list` answers without contacting
the hosted endpoint and directories can introspect the server offline. The list mirrors
the hosted endpoint's `tools/list`. Each `tools/call` is forwarded to
`GET4AGENT_MCP_URL` (default `https://get4agent.com/mcp`) and the hosted answer is
returned as is, including `structuredContent` and `isError`. The proxy expects JSON
responses; it is not a general SSE client.

Catalog search uses `query`. Omit `category` to search all categories; results come in
pages of up to 50 (`limit`, `offset`, `has_more`, `next_offset`). Search needs no
account, creates no signing key and authorizes no purchase. The hosted catalog tools are
read-only; the buyer connectors (`agent.py`, the desktop extension in `mcpb/`) have their
own purchase flow.

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_listings","arguments":{"query":"courier","limit":20,"offset":0}}}
```

## Development checks

```bash
python3 -m unittest discover -s tests -v
node --test tests/*.test.cjs
```

Python buyer tests require `httpx`; the SDK registration boundary is stubbed, and
HTTP responses are local fixtures. Tests do not call live services or make purchases.

The signing tests (`tests/test_signing.py`, `tests/desktop-signing.test.cjs`) check that
both buyer connectors sign the way get4agent verifies: AAuth
(draft-hardt-oauth-aauth-protocol-11) — the agent token from `/v1/agents/bootstrap` in
`Signature-Key` under the `jwt` scheme, and a signature over
`@method @authority @path signature-key`, plus `content-digest content-type` and a
`Content-Digest` header when there is a body. `test_signing.py` hands the requests both
connectors produce to the `regent-httpsig` verifier (the one the server runs) and
compares the desktop connector's headers byte for byte with that library's signer; it
needs `regent-httpsig[aauth]` importable and `node` on `PATH`, and reports a skip
otherwise. These tests create throwaway keys in a temporary directory, never in
`~/.get4agent`.

`agent.py` depends on `mcp`, `httpx` and `regent-httpsig>=0.7.3` (declared in its
script header). `server.py` and the desktop extension have no third-party dependencies:
the extension signs with `node:crypto` alone.

`get4agent.mcpb` is the packaged desktop extension: a zip of `mcpb/manifest.json` and
`mcpb/server/index.js`. After changing either file, bump `version` in the manifest and
repack:

```bash
(cd mcpb && zip -X -r ../get4agent.mcpb manifest.json server/index.js)
```

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
https://get4agent.com/partners — no marketplace commission; listing is free during the pilot.

---

Keywords: MCP server, AI agents, Kazakhstan, Kazpost parcel tracking API, IIN BIN
validation, KZ IBAN, GLEIF LEI, KZT exchange rates, agentic commerce, Regent Protocol,
Model Context Protocol, x402.

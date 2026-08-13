# Local (stdio) MCP server for GET4AGENT MARKET — proxies to the hosted endpoint.
# Directories (Glama etc.) build this, run it, and introspect over stdio (tools/list).
# Stdlib-only server → nothing to install, so the build/check can't fail on deps.
FROM python:3.11-slim
WORKDIR /app
COPY server.py .
# MCP stdio server: reads JSON-RPC on stdin, writes on stdout.
CMD ["python", "server.py"]

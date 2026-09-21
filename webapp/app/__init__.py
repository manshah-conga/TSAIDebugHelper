"""
Package init, and the one place local configuration is loaded.

This runs before any `app.*` submodule can be imported, which is exactly why
the env-file load lives here rather than in main.py. Several modules read
their settings at IMPORT time -- `chat.MAX_TOOL_ROUNDS`, `llm.OPENROUTER_BASE`,
`mcp_http.MCP_PATH` -- so a load placed in a startup hook would run too late
for those and they would silently keep their defaults.

Putting it here means every entry point gets the same configuration with no
extra step: `uvicorn app.main:app`, the Windows launchers, the stdio MCP
server, and the test suites.

Silently, though. The stdio MCP server speaks JSON-RPC over stdout, so a
banner printed during import would corrupt the protocol and the client would
fail to connect with something unrelated-looking. The web app prints the
report itself, from its startup hook -- see `main.lifespan`.
"""
from . import env_file

env_file.load(verbose=False)

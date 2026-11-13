"""Spike: the in-browser search as an MCP App. A minimal server with one tool linked to a ui:// resource.

Run: python web/app_spike_server.py  (serves http://127.0.0.1:3001/mcp)
The resource is web/search-app.html with web/search_core.py inlined, so the frame needs no fetch to our own origin.
"""
import json, os, pathlib
from mcp.server.mcpserver import MCPServer

HERE = pathlib.Path(__file__).parent
URI = "ui://kepler/search"
CSP = {"connectDomains": ["https://stpubdata.s3.amazonaws.com", "https://cdn.jsdelivr.net"],
       "resourceDomains": ["https://cdn.jsdelivr.net"]}

mcp = MCPServer("kepler-app-spike")


@mcp.resource(URI, name="search", title="Kepler in-chat search", mime_type="text/html;profile=mcp-app",
              meta={"ui": {"csp": CSP}})
def search_app() -> str:
    html = (HERE / "search-app.html").read_text()
    return html.replace("__SEARCH_CORE__", json.dumps((HERE / "search_core.py").read_text()))


@mcp.tool(title="Search a star in the chat",
          description="Runs a transit search on one star inside the chat, on the user's own computer.",
          meta={"ui": {"resourceUri": URI}})
def search_in_chat() -> dict:
    return {"name": "pi Mensae (TIC 261136679)", "sector": 1, "cam": 4, "ccd": 2, "col": 1632.29, "row": 259.79, "size": 15}


if __name__ == "__main__":
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware
    app = CORSMiddleware(mcp.streamable_http_app(), allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                         expose_headers=["mcp-session-id"])   # the reference host connects from its own browser origin
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 3001)))

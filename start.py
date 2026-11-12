#!/usr/bin/env python3
"""Start the tess-hunt MCP server locally.

    python start.py              # http://127.0.0.1:8000/mcp
    python start.py --port 9000
    python start.py --stdio      # for a client that launches the server itself

Checks that the dependencies are importable, keeps the cache next to the code (./data), and prints how to connect.
"""
import argparse, importlib.util, os, socket, sys, warnings

ROOT = os.path.dirname(os.path.abspath(__file__))
NEEDED = {"mcp": "mcp", "lightkurve": "lightkurve", "astrocut": "astrocut", "astroquery": "astroquery",
          "tess_stars2px": "tess-point", "uvicorn": "uvicorn", "anyio": "anyio", "s3fs": "s3fs"}


def main():
    ap = argparse.ArgumentParser(description="Start the tess-hunt MCP server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    args = ap.parse_args()
    say = (lambda *a: print(*a, file=sys.stderr)) if args.stdio else print   # stdout belongs to the protocol in stdio mode

    if sys.version_info < (3, 11):
        sys.exit(f"Python 3.11 or newer is required; this is {sys.version.split()[0]}")
    missing = [pip for mod, pip in NEEDED.items() if importlib.util.find_spec(mod) is None]
    if missing:
        sys.exit("Missing packages: " + ", ".join(missing) + f"\nInstall them with:\n    {sys.executable} -m pip install -e \"{ROOT}\"")
    import mcp
    try:
        from mcp.server.mcpserver import MCPServer  # noqa: F401
    except Exception:
        sys.exit(f"The installed mcp package is too old (need 2.x).\n    {sys.executable} -m pip install -U 'mcp>=2,<3'")

    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    os.environ.setdefault("TESSHUNT_MCP_DATA", os.path.join(ROOT, "data", "mcp"))
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("PYTHONWARNINGS", "ignore")
    warnings.filterwarnings("ignore")
    os.makedirs(os.environ["TESSHUNT_MCP_DATA"], exist_ok=True)

    if not args.stdio:
        with socket.socket() as s:
            if s.connect_ex((args.host, args.port)) == 0:
                sys.exit(f"Port {args.port} is already in use. Stop the other server or run: python start.py --port {args.port + 1}")
        url = f"http://{args.host}:{args.port}/mcp"
        say(f"tess-hunt MCP server starting at {url}")
        say(f"  cache and logs: {os.environ['TESSHUNT_MCP_DATA']}")
        say(f"  connect Claude Code (once):  claude mcp add --transport http tess-hunt {url}")
        say('  then ask:  "Use tess-hunt on Pi Mensae. Run the tools yourself and show the numbers."')
        say("  stop with Ctrl-C\n")

    sys.argv = ["tesshunt-mcp"] + (["--stdio"] if args.stdio else ["--host", args.host, "--port", str(args.port)])
    from tesshunt_mcp.server import main as serve
    try:
        serve()
    except KeyboardInterrupt:
        say("\nstopped")


if __name__ == "__main__":
    main()

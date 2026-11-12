# tess-hunt-mcp

MCP server for finding and vetting transiting planet candidates in TESS full-frame images. General-purpose: it must work for any TESS star a user names and must not refer to any particular discovery, candidate list or manuscript.

## Layout
- tesshunt/          science library: pipeline.py (clean, flatten, search, vet, centroid), sweep_core.py (pixel fetch, candidate_bar), cubes.py (pixel cache), vetting.py (catalogues, neighbours, verdict), followup.py (multi-sector recovery), star.py (TIC lookup)
- tesshunt_mcp/      core.py (tools as plain functions), store.py (cache, traces, rate limits, jobs), server.py (MCP wiring), guide.md (vetting guide served to clients)
- tests/             offline tests; scripts/warm.py is the live smoke test
- deploy/            Docker + Caddy runbook

## Rules
- Output says "planet candidate", never "planet" or "discovered".
- Tools return JSON numbers plus an optional PNG, with a `caveats` list. An archive failure raises ToolError; it is never turned into a verdict.
- No tool takes a URL or a file path from the caller. Cutout size is chosen by the server from the star's brightness.
- Run `pytest` and `scripts/warm.py` against a local server before committing server changes.

## Pitfalls
- (symptom → cause → rule, one line each)
- Clients saw only "Error executing tool X" → the MCP SDK hides the message of any exception that is not its own ToolError → raise mcp.server.mcpserver.exceptions.ToolError with a sentence the client model can act on.
- `from mcp.server.fastmcp import FastMCP` fails → SDK 2.x renamed it to MCPServer → pin mcp>=2,<3.
- Neighbour check failed with "HTTPError: OK" and a page of HTML → the ESA Gaia archive was down for maintenance → fall back to TESS Input Catalog neighbours and say which source was used.
- A bright star (Tmag 5) returned a 20,000 ppm single-cadence "transit" → 15-pixel cutout cannot hold a saturated star's light, and one glitch cadence dominated the search → cutout 25 px and threshold 10 below Tmag 9, and isolated single-cadence 5-sigma dips are dropped.
- A client explained a star from the bundled background text without running any tool → the instructions told it to read the log first and the log named stars → the guide names no stars, and instructions say every number must come from a tool run in the conversation.
- zsh aborted a whole `rm` because one glob matched nothing → unmatched globs are errors in zsh → never mix optional globs into a destructive command.
- A confirmed planet (LHS 3844 b) and a clean candidate were labelled eclipsing binaries by the follow-up → the stitched any-phase scan takes the deepest of ~70 boxes and compares it with a white-noise error; one bad stretch of one sector is enough → a secondary must be 5 robust sigma deeper than the other boxes (pipeline.vet has_secondary) AND show up separately in at least half the sectors before the status says eclipsing_binary.
- A confirmed 260 ppm planet (Pi Mensae c) came back "does not repeat" though all 12 sectors showed the same depth → the rule counted only sectors whose own blind search reached SNR 7 → judge repetition at the stitched ephemeris: a sector agrees at 3 sigma and within a factor 2 of the depth, and only a sector with the sensitivity to see the dip can contradict it.
- A 0.5 h transit at 30-minute cadence failed "duration exceeds the physical maximum" → exposure time smears short transits → the bar allows 1.5x the physical maximum plus one cadence.
- Rules tuned on one population (faint M dwarfs, deep dips) mislabel others → before changing a threshold, run scripts/warm.py on known planets across brightness: Pi Mensae (bright, shallow), LHS 3844 (ultra-short period), TOI-700 (small, long period).


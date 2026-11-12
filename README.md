# tess-hunt-mcp

An MCP server that lets Claude, or any MCP client, look for **transiting planet candidates** around any star NASA's TESS mission observed, and then try to break what it finds.

Give it a star. It builds the light curve from the mission's public full-frame images, searches for periodic dips, and runs the tests that separate planet candidates from eclipsing binaries, spotted stars and neighbours' eclipses. It works one star at a time and reports candidates, never planets: confirmation needs other telescopes.

## Quick start
```bash
git clone https://github.com/khariha/tess-hunt-mcp.git && cd tess-hunt-mcp
pip install -e .
claude mcp add tess-hunt -- tesshunt-mcp --stdio
```
Or run it as an HTTP server and connect to that:
```bash
tesshunt-mcp --host 127.0.0.1 --port 8000
claude mcp add --transport http tess-hunt http://127.0.0.1:8000/mcp
```
Then ask:
- "Use tess-hunt on Pi Mensae. Run the tools and show the numbers."
- "Check TOI-700 with tess-hunt: which sectors observed it, and what does the earliest one show?"
- "Run the hunt prompt on TIC 410153553."

## Tools
| Tool | What it does | Typical time |
|---|---|---|
| `resolve_star(query)` | name, TIC id or coordinates to catalogue entry and observed sectors | 1 s |
| `catalog_check(tic)` | already a confirmed planet host, TOI or Community TOI? | 5 s |
| `light_curve(tic, sector)` | light curve from full-frame-image pixels: stats, binned series, plot | 5-50 s first time |
| `search_transits(tic, sector, ...)` | box least squares; best signal, shape tests, candidate bar with reasons. Period range and bar thresholds are arguments | 5-15 s |
| `vet_signal(tic, sector, period_d, t0, duration_h)` | odd/even depths, secondary eclipse at every phase, physical duration, centroid shift, difference image, neighbours; verdict with reasons | 5-15 s |
| `neighbors(tic, sector, depth_ppm?)` | stars within 3 pixels and the eclipse each would need to fake the dip | 5 s |
| `fold_plot(tic, sectors, period_d, t0)` | overlay up to 4 sectors folded at a period | 5 s when cached |
| `followup_start(...)`, `job_status(job_id)` | independent search of every observed sector (up to 12) and a stitched refinement | 1-5 min |
| `report_verdict(tic, verdict, reason)` | optional: record your final call | instant |
| `read_guide()` | how transit signals fool you, and this server's limits | instant |

Prompt `hunt(star)` walks the whole procedure. Resource `tess-hunt://guide` is the vetting guide ([tesshunt_mcp/guide.md](tesshunt_mcp/guide.md)).

Units: times are BTJD (BJD - 2457000), depths ppm, durations hours, periods days.

## How it adapts to the star
- Stars brighter than TESS magnitude 9 get a 25-pixel cutout and a stricter aperture; fainter stars a 15-pixel cutout.
- The candidate bar defaults to search SNR 9 with no depth floor, plus shape tests. Both thresholds are arguments to `search_transits`.
- Every result carries a `caveats` list (saturation, crowding, missing stellar radius, single sector).

## Limits
- Simple aperture photometry on full-frame images. Expect it to find hot, deep or frequent transits, not Earth analogues.
- One sector is never enough; only a signal that repeats across sectors with no secondary eclipse at any phase is called a candidate.
- An on-target verdict does not exclude a second star inside the same 21-arcsecond pixel.
- No survey-scale sweeps, no MCMC fits, no statistical validation, no submissions to any archive or journal.

## Data sources
Public archives only: TESS full-frame-image cubes and the TESS Input Catalog from MAST (STScI), the NASA Exoplanet Archive, ExoFOP-TESS, and Gaia DR3 (ESA). Please cite them, and the TESS mission, if you publish anything found with this tool.

## Running a public instance
[deploy/README.md](deploy/README.md) has a one-box Docker and Caddy setup. A public instance rate-limits per user (60 ordinary calls and 5 follow-ups an hour by default) and caches results.

**Logging.** By default the server appends tool name, arguments, numeric results, duration and an anonymous daily-rotating id to a local JSON-lines file. It stores no IP address, account, prompt or model reasoning. Set `TESSHUNT_MCP_TRACES=0` to turn it off.

## Development
```bash
pip install -e ".[dev]" && pytest          # offline tests
python scripts/warm.py http://127.0.0.1:8000/mcp   # live smoke test against a running server
```
MIT licence.

# Kepler

**Welcome to Kepler. Let's hunt for planets.**

Kepler is an MCP server that lets Claude, or any MCP client, search NASA's TESS space-telescope data for **transiting planet candidates** and then try to break what it finds. It guides a newcomer through the whole thing. (Named for Johannes Kepler; the data is from the TESS mission, not the Kepler space telescope.)

Three ways in:
- **Learn.** Find a planet astronomers already know about, starting from raw telescope pixels, with every step explained. About ten minutes.
- **Hunt.** Take a real lead: a star where a bulk search saw a repeating dip that nobody has checked. Try to break it. If it survives your tests and a second hunter's, it goes on the public board with your name. Most leads are impostors; expect roughly 1 in 10 to survive.
- **Bring your own star.** Name any star TESS observed and run the full check.

It reports candidates, never planets: confirmation needs other telescopes.

## Quick start
```bash
git clone https://github.com/khariha/kepler-mcp.git && cd kepler-mcp
pip install -e .
python start.py
claude mcp add --transport http kepler http://127.0.0.1:8000/mcp
```
Then open Claude and say: **"Open Kepler. Let's hunt for planets."**

`start.py` checks the dependencies, loads the leads queue, and prints the board address. For a client that launches the server itself: `claude mcp add kepler -- kepler-mcp --stdio`.

## Hunting leads
| Tool | What it does |
|---|---|
| `welcome()` | the greeting and the three paths |
| `lesson()` | the guided five-step lesson on a known planet |
| `join(handle)` | pick a hunter name; returns a `hunter_id` to keep for later sessions |
| `next_lead(hunter_id)` | one unchecked lead, with the exact values to test |
| `submit_vet(hunter_id, lead_id, verdict, reason, ...)` | your verdict, accepted only after the tests have been run |
| `board()`, `my_progress(hunter_id)` | standings and your tally |

The board is also a web page at `/board` (JSON at `/board.json`).

**How the board stays honest.** A verdict is refused until the vetting and the multi-sector follow-up have actually been run on the lead's exact values. The server keeps its own reading of every lead from those results. A lead is listed as surviving only when two different hunters call it a candidate **and** the server's reading agrees, so nobody can vote an impostor onto the board. Nothing is submitted to any archive.

Prompts: `start` (welcome), `hunt` (the guided loop over leads), `check_star(star)` (one star end to end).

## Analysis tools
| Tool | What it does | Typical time |
|---|---|---|
| `resolve_star(query)` | name, TIC id or coordinates to catalogue entry and observed sectors | 1 s |
| `catalog_check(tic)` | already a confirmed planet host, TOI or Community TOI? | 5 s |
| `light_curve(tic, sector)` | light curve from full-frame-image pixels: stats, binned series, plot | 5-50 s first time |
| `search_transits(tic, sector, ...)` | box least squares; best signal, shape tests, candidate bar with reasons. Period range and bar thresholds are arguments | 5-15 s |
| `vet_signal(tic, sector, period_d, t0, duration_h)` | odd/even depths, secondary eclipse at every phase, physical duration, centroid shift, difference image, neighbours; verdict with reasons | 5-15 s |
| `neighbors(tic, sector, depth_ppm?)` | stars within 3 pixels and the eclipse each would need to fake the dip | 5 s |
| `fold_plot(tic, sectors, period_d, t0)` | overlay up to 4 sectors folded at a period | 5 s when cached |
| `followup_start(...)`, `job_status(job_id)` | independent search of every observed sector (up to 12), a stitched refinement, and a sector-by-sector agreement check. `job_status` waits and streams live progress | 1-5 min |
| `report_verdict(tic, verdict, reason)` | optional: record your final call | instant |
| `read_guide()` | how transit signals fool you, and this server's limits | instant |

Every result carries `plain` (what it means in one sentence) and `next` (the sensible next step with exact values). Resource `kepler://guide` is the vetting guide ([tesshunt_mcp/guide.md](tesshunt_mcp/guide.md)).

Units: times are BTJD (BJD - 2457000), depths ppm, durations hours, periods days.

## How it adapts to the star
- Stars brighter than TESS magnitude 9 get a 25-pixel cutout and a stricter aperture; fainter stars a 15-pixel cutout.
- The candidate bar defaults to search SNR 9 with no depth floor, plus shape tests. Both thresholds are arguments to `search_transits`.
- Fast-moving nearby stars are tracked: each sector's cutout is centred where the star was at that time.
- When the catalogue has no radius or mass (common for the coolest dwarfs), they are estimated from the absolute K magnitude and flagged.
- Significance is judged against the measured scatter of the light curve, not a white-noise formula, so bright stars with correlated noise are not over-trusted.
- Every result carries a `caveats` list (saturation, crowding, estimated parameters, single sector).

## Limits
- Simple aperture photometry on full-frame images. Expect it to find hot, deep or frequent transits, not Earth analogues.
- One sector is never enough; only a signal that repeats across sectors with no secondary eclipse at any phase is called a candidate.
- An on-target verdict does not exclude a second star inside the same 21-arcsecond pixel.
- No survey-scale sweeps, no MCMC fits, no statistical validation, no submissions to any archive or journal.

## Data sources
Public archives only: TESS full-frame-image cubes and the TESS Input Catalog from MAST (STScI), the NASA Exoplanet Archive, ExoFOP-TESS, and Gaia DR3 (ESA). Please cite them, and the TESS mission, if you publish anything found with this tool.

## Running a public instance
[deploy/README.md](deploy/README.md) has a one-box Docker and Caddy setup. A public instance rate-limits per user (60 ordinary calls and 5 follow-ups an hour by default) and caches results.

**Logging.** By default the server appends tool name, arguments, numeric results, duration and an anonymous daily-rotating id to a local JSON-lines file. It stores no IP address, account, prompt or model reasoning. A hunter name, if someone picks one, is public on the board. Set `TESSHUNT_MCP_TRACES=0` to turn it off.

## Development
```bash
pip install -e ".[dev]" && pytest          # offline tests
python scripts/warm.py http://127.0.0.1:8000/mcp   # live smoke test against a running server
```
Leads come from `ops/`: `ops/dev_leads.jsonl` holds three well-known stars for testing; a real queue goes in `ops/leads.jsonl`.
```bash
```
MIT licence.

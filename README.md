# Kepler

MCP server for transit search and vetting on NASA TESS full-frame images. It finds transit-shaped signals around stars nobody has searched, tests them against the common false positives, and tracks which survive. Results are **planet candidates**; confirmation requires other instruments. Named for Johannes Kepler; the data is from the TESS mission.

Three workflows:
- **Check a star.** Name any star TESS observed: light curve from raw pixels, transit search, false-positive tests, multi-sector repetition.
- **Vet a lead.** Take a queued signal from a searched patch of sky, run the tests, record a verdict. A lead is listed as a candidate after two independent candidate verdicts and agreement from the server's own results.
- **Search new sky.** Claim a patch of about 150 unsearched stars; a local helper searches it on your own computer in about seven minutes and uploads the numbers. Each patch is searched once and stored for everyone.

Output follows AAS-journal and TESS follow-up wording (planet candidate, false positive, false alarm, recovered in N of M Sectors), gives epochs in BJD_TDB, and returns a record shaped for a Research Note table or an ExoFOP CTOI upload when a signal is recovered. Users are volunteers, colloquially planet hunters; the project is not affiliated with Planet Hunters TESS. An optional guided lesson (`lesson`) recovers a known planet step by step.

## Quick start
```bash
git clone https://github.com/khariha/kepler-mcp.git && cd kepler-mcp
pip install -e .
python start.py
claude mcp add --transport http kepler http://127.0.0.1:8000/mcp
```
Then, in Claude: "Use Kepler to check Pi Mensae", "give me a lead to vet", or "search some new sky".

`start.py` checks the dependencies, loads the leads queue, and prints the board address. For a client that launches the server itself: `claude mcp add kepler -- kepler-mcp --stdio`.

## Leads queue
| Tool | What it does |
|---|---|
| `join(handle, recovery_word)` | register a handle or sign back in; returns the `hunter_id` credential. The same handle and word always give the same id |
| `next_lead(hunter_id, star_type?, planet_size?)` | one unvetted lead with the arguments for the tests |
| `submit_vet(hunter_id, lead_id, verdict, reason, ...)` | record a verdict; accepted only after the tests have run |
| `find_planets(hunter_id?)` | entry point: opening notice, the three ways to proceed, current counts |
| `board()`, `my_progress(hunter_id)` | standings and one handle's tally |
| `lesson()` | optional guided lesson on a known planet |

The board is also a web page at `/board` (JSON at `/board.json`).

## Opening a patch of sky
Leads come from searching stars nobody has searched, and planet hunters do that themselves. The hub divides each open TESS sector into **patches** of about 150 faint red dwarfs. `open_patch` hands one to a hunter with a single command:

```bash
kepler-sweep s7-c1-1-02 --hunter <hunter_id> --hub https://your-kepler-host
```
That helper runs on the hunter's own computer for about seven minutes. It gets the star list from the hub, reads each star's pixels straight from NASA's public files, runs the search, and uploads one row of numbers per star. An interrupted run resumes. The patch is then stored for everyone and never searched again.

| Tool | What it does |
|---|---|
| `sky_map(sector?)` | which patches are searched, unopened, or in progress, and leads found |
| `open_patch(hunter_id, patch_id?)` | claim a patch for two hours and get the command |
| `patch_status(patch_id)` | what came back |

**The hub checks before it believes.** Uploaded results are untrusted. Every signal an upload reports is re-run by the hub itself, and only signals it reproduces become leads, credited to the hunter whose computer found them. One quiet star per patch is re-run too; an upload that disagrees is rejected and the patch reopens.

`kepler-sweep --mcp` runs the same helper as a local MCP server with one tool, `sweep_patch`, for people who would rather add it once than paste commands.

**Integrity of the board.** A verdict is refused until the vetting and the multi-sector follow-up have actually been run on the lead's exact values. The server keeps its own reading of every lead from those results. A lead is listed as surviving only when two different planet hunters call it a candidate **and** the server's reading agrees, so nobody can vote an impostor onto the board. Nothing is submitted to any archive.

Prompts: `hunt` (procedure for vetting leads), `check_star(star)` (procedure for one star).

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

**Readbacks.** Almost every result carries `display`, a short plain-text readback that the client prints verbatim, in the form that suits the event: an identity card for a star lookup, a one-line catalogue status, a staged checklist with timings for a transit search, a pass/fail table of tests for vetting, a table of neighbours, a live Sector-by-Sector table for the multi-sector follow-up, a live panel for a field search, a progress line while a detector is being mapped, and one-line confirmations for sign-in, signals and verdicts. A signal that is listed as a planet candidate gets its own block.

**Result contract.** Every result carries `summary` (what was measured or decided, one or two neutral sentences), `next_actions` (suggested calls as `{tool, args, why}` with complete arguments), and `caveats`. Resource `kepler://guide` is the vetting guide ([kepler_mcp/guide.md](kepler_mcp/guide.md)).

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

**Logging.** By default the server appends tool name, arguments, numeric results, duration and an anonymous daily-rotating id to a local JSON-lines file. It stores no IP address, account, prompt or model reasoning. A hunter name, if someone picks one, is public on the board. Set `KEPLER_TRACES=0` to turn it off.

## Development
```bash
pip install -e ".[dev]" && pytest          # offline tests
python scripts/warm.py http://127.0.0.1:8000/mcp   # live smoke test against a running server
```
`ops/dev_leads.jsonl` holds three well-known practice stars. Real leads come from searched patches. Hub settings: `KEPLER_OPEN_SECTORS` (default "7 8"), `KEPLER_PATCH_SIZE` (150), `KEPLER_PATCHES_PAUSED=1`, `KEPLER_EXCLUDE` (a file of TICs that must never be searched), `KEPLER_PUBLIC_URL`.
```bash
```
MIT licence.

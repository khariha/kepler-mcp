# Kepler (kepler-mcp)

MCP server for finding and vetting transiting planet candidates in TESS full-frame images. General-purpose: it must work for any TESS star a user names and must not refer to any particular discovery, candidate list or manuscript.

## Layout
- tesshunt/          science library: pipeline.py (clean, flatten, search, vet, centroid), sweep_core.py (pixel fetch, candidate_bar), cubes.py (pixel cache), vetting.py (catalogues, neighbours, verdict), followup.py (multi-sector recovery), star.py (TIC lookup)
- tesshunt_mcp/      core.py (tools as plain functions), store.py (cache, traces, rate limits, jobs), server.py (MCP wiring), guide.md (vetting guide served to clients)
- tesshunt_mcp/leads.py  the leads queue and public board (SQLite); ops/ holds leads files (dev_leads.jsonl = known stars for testing)
- tests/             offline tests; scripts/warm.py is the live smoke test
- tesshunt_mcp/sky.py  patches of sky: mapping, claims, uploads; tesshunt/targets.py picks a detector's unclaimed stars
- kepler_sweep/      the local helper hunters run to search a patch (command and --mcp modes); worker.py is the per-star search
- start.py           one-command local launcher
- deploy/            Docker + Caddy runbook

## Rules
- Output says "planet candidate", never "planet" or "discovered".
- Vocabulary: AAS-journal and TESS follow-up usage (see the Terminology table in tesshunt_mcp/guide.md). Epochs also in BJD_TDB. No "discovered", "confirmed", "validated"; no TOI numbers; disposition codes are ours, not TFOPWG's.
- Voice: instrument. Tool output is terse and neutral: no greetings, encouragement, second person, or coaching. The `lesson` tool is the one exception.
- Result contract: every result has `summary` (neutral, one or two sentences), `next_actions` (list of {tool, args, why} built with core.act, arguments complete), and `caveats`. No prose instructions to the model inside results.
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
- Vetting and neighbours failed with "'_ValidHDU' object has no attribute 'keys'" → a client ran two tools in parallel on one cached pixel file, and astropy's lazy FITS loading is not thread-safe → one lock per (star, sector) around every pixel operation, and finish the lazy loading under it.
- Follow-up crashed on a 0.34 d signal ("maximum transit duration must be shorter than the minimum period"), then with "'DataFrame' object has no attribute 'snr'" → duration grid ignored the period, and the no-usable-sector case was never handled → cap durations at half the period; return a normal empty result; refuse dips longer than a quarter of the period with a sentence saying why.
- A nearby M dwarf (1.6 arcsec/yr) was cut out 1.8 px off centre → catalogue positions are epoch 2000 → move every position by proper motion to the sector's date (star.position_at) and cache those cutouts under their own name.
- The coolest dwarfs have no radius or mass in the catalogue, so checks silently used the Sun → estimate from absolute K magnitude (Mann 2015, 2019), flag it, and never fall back silently.
- Follow-up said "repeats" for a noise peak, and a bright star failed odd/even at "3 sigma" → white-noise errors are far too small once a period search has picked the best alignment or the star is bright → judge every depth against the scatter of same-width boxes at other phases; require the stitched dip over 7 sigma for "repeats".
- One bad stretch of data in one orbit produced an "eclipsing binary" verdict → a deepest-box scan cannot tell an eclipse from a glitch → a secondary must recur in at least half the orbits (single sector) or half the sectors (follow-up); otherwise report it as a single event to check.
- A confirmed planet was called "anomaly" on a centroid shift of 0.003 px → at high signal-to-noise tiny real shifts are "significant" and the neighbour model is crude → a clear on-target difference image outranks the centroid.
- Clients did not see progress → progress notifications need a streamed response (json_response=False) and a tool that stays open → job_status waits up to 25 s and forwards the job's progress lines.
- "Keep going until you find a candidate" ended with Claude checking famous nearby stars and finding nothing → one star at a time, picked by name, has odds near 1 in 20,000 → the product hands out pre-screened leads (next_lead) and the instructions open with welcome; do not ask a client to pick stars from memory.
- A known planet host could have been listed as a "surviving candidate" → the consensus rule looked only at vetting and follow-up → the server's reading returns known_object when the cached catalog check lists the star, except for development leads, which the board labels as such.
- A Haiku client told the user Pi Mensae is "18 light-years away" (it is 18 parsecs, 60 light-years) → the tool returned a bare `d` → every quantity a client may read aloud carries its unit in the field name or a `units` map.
- The same client called a stacked 333 ppm off-phase dip "a possible second body" though the server had judged it noise → the number was prominent and the judgement was buried in caveats → put the conclusion next to the number (secondary_check.conclusion).
- The helper reported "0 of 5 stars usable" and uploaded nothing → started with `python -m`, its worker function lived in `__main__`, which spawned processes cannot import; the failures were swallowed → the worker lives in kepler_sweep/worker.py, and a run where most stars fail exits with the first real error.
- Every lead was labelled "practice", including one from a real searched patch → the practice flag was global to the queue → practice is per lead (file-loaded with no patch), and patch leads are handed out first.
- Do not quote odds that were never measured ("one patch in three has a signal") → say "many patches find nothing" until the hub has real counts to report.
- Measured on a laptop (8 workers, home connection): about 22 stars a minute, 20 s per star, download-bound → patch size 150 for a seven-minute search; mapping one detector into patches takes 5-6 minutes of catalogue queries.
- The client narrated the server's coaching as its own ("Your job is to try to break it", "Promising, but...") and the product read like a tour, not a tool → results carried prose hints addressed to the model and a chatty `plain` field → results state facts in `summary` and suggest calls in structured `next_actions`; tests scan emitted strings for second person and exclamation marks.
- A returning user could not use their own handle: "taken", and the hunter_id from the earlier session was gone → the id was random, issued once, and chat sessions do not persist → hunter_id is derived from handle + a user-chosen recovery word, so join is also sign-in; the word is never logged. Real accounts (OAuth) are the later answer for a public host.
- The helper's progress bar was invisible: first because piped stdout is block-buffered (print without flush), then because the client folds a running command to one line → flush every line, and report progress to the hub so patch_status can return a `display` block that the assistant prints in the conversation.
- patch_status returned instantly before the first progress reading, so a polling client made 388 calls in 27 s → a waiting call must wait in every non-terminal state, including "no reading yet".
- A BJD epoch came back as 2458326.0 → the JSON cleaner rounded floats to 7 significant figures → 12 significant figures; add a test whenever a new quantity needs more digits than the last one did.


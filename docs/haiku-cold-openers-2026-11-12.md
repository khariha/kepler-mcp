
## 0. Use Kepler to check Pi Mensae.

*55s; tools: ['resolve_star', 'catalog_check', 'search_transits', 'search_transits', 'vet_signal', 'followup_start', 'job_status']*


**Pi Mensae (TIC 261136679) is a recovery of a known signal, not a new candidate.** The star already hosts planets HD 39091 b, pi Men c and pi Men d, and its 6.27-day signal is listed as TOI-144.01.

- **Catalog:** TOI-144.01, disposition CP, period 6.267814 d, depth 321 ppm.
- **Single sector (S1):** period 6.2736 d, depth 259 ppm, SNR 14.7, 5 transits. Odd/even depths agree, and there is no secondary eclipse at any phase.
- **Vetting (S1):** `likely_on_target`. The centroid shift is consistent with the target, but the difference image is too weak to locate the dip (3σ).
- **Multi-sector follow-up:** `repeats`. 12 of 12 searched sectors agree and none contradict. The stitched ephemeris is P = 6.267819 d, T0 = 1325.506 BTJD, depth 254 ppm, duration 3.0 h, 48 transits, with no secondary (2.4σ).

The follow-up covered only the first 12 of 24 sectors. Sectors 61–95 were skipped because of the 12-sector limit, so the result rests on sectors 1–39.

Full paths for the plots are in the tool results folder under the scratchpad directory.

## 1. Use Kepler: give me a lead to vet. Call me opener_test.

*16s; tools: ['join', 'next_lead', 'vet_signal', 'catalog_check']*


The lead is a known object, so it's a recovery test, not a new candidate.

- **Lead 2** (practice lead, `lead_id` 2): TIC 410153553, red dwarf, Tmag 11.9, sector 1
- **Signal:** P = 0.4626 d, depth 1473 ppm, duration 0.75 h, SNR 38.5, implied radius 0.79 R⊕
- **Catalog:** Already a known planet host, **LHS 3844 b** (TOI-136.01, disposition CP). The signal is a recovery.
- **Vet (`vet_signal`, sector 1):** `likely_on_target`. Odd/even depths agree (1331 vs 1538 ppm), no secondary at any phase (deepest box 1.8σ), centroid shift consistent with on-target. Caveats: two faint neighbors could fake the dip, and the signal is single-sector.

Not yet done: the multi-sector follow-up and `submit_vet`. The follow-up uses one of your 5 hourly quota slots. I can run it and then submit a `known_object` verdict with the catalog as the deciding test, if you want the full workflow completed. Your `hunter_id` is <redacted>. It isn't recoverable if lost.

## 2. Use Kepler to search some new sky. Call me sky_test.

*15s; tools: ['join', 'sky_map', 'open_patch']*

- TOOL ERROR: This Bash command contains multiple operations. The following parts require approval: cd "~/tess-hunt-mcp", "~/.pyenv/versions/3

The patch is claimed, but I haven't run the search yet. Running the command needs your approval, and the permission prompt was refused.

- **Handle:** `sky_test` (hunter_id <redacted>). Keep this ID; it can't be recovered if lost.
- **Patch:** `s7-c2-1-r02`, sector 7, 14 unsearched red dwarfs. Only sector 7 is open, and red is the only star type mapped.
- **Hold:** about 119 minutes before it expires.

To run it yourself, from a shell with Python 3.11+:

```
cd "~/tess-hunt-mcp" && "~/.venv/bin/python" -W ignore -m kepler_sweep s7-c2-1-r02 --hunter <redacted> --hub http://127.0.0.1:8774
```

It takes about seven minutes. Tell me when it finishes, or ask me to run it with approval, and I'll check the patch status and pull the next lead.

## 3. I have Kepler connected. Teach me how this works.

*13s; tools: ['read_guide']*


Kepler finds planet candidates in NASA TESS light curves. It does not confirm planets. Every result is a candidate until other instruments check it.

**Two ways to use it**

- **Check a named star:** `resolve_star` → `catalog_check` → `search_transits` → `vet_signal` → `neighbors` → `followup_start` → `job_status`.
- **Vet a queued lead:** `join` → `next_lead` → `vet_signal` → `neighbors` → `followup_start` → `job_status` → `submit_vet`.
- **Search new sky:** `join` → `sky_map` → `open_patch`. You run the returned command on your own computer, then `patch_status` → `next_lead`. A patch covers about 150 unsearched stars of one type and takes about seven minutes. Many patches find nothing, and that sky is then cleared for everyone.

**What a candidate needs**

1. A dip that repeats at a fixed period in every sector that saw the star well.
2. A shape a small body can make: short relative to the orbit, not too long for the star's density, same depth on odd and even transits.
3. No second dip elsewhere in the orbit.
4. The dip sits on the target, not on a neighbour sharing its pixels.
5. No existing catalogue entry, if you claim it is new.

**Common false positives**

- Eclipsing binaries, circular or eccentric. The second eclipse may sit away from phase 0.5, so `vet_signal` scans phases 0.15 to 0.85.
- Spotted rotating stars. These give smooth waves, not boxes, often with periods under a day.
- Neighbour eclipses. `neighbors` lists stars bright enough to fake the depth.
- Scattered light, pointing jitter, and single-point glitches.

**Reading output**

- `SNR` under about 8 is usually noise.
- `depth_sigma` is depth over its uncertainty in the fold. Real signals are often many sigma here.
- Two transits in one sector is weak evidence. Long periods need several sectors.
- Verdicts from `vet_signal`: `eclipsing_binary`, `not_significant`, `not_transit_shaped`, or a location (`on_target`, `likely_on_target`, `blended`, `anomaly`, `off_target`, `untestable`).
- Follow-up statuses: `repeats` is the strong result. `does_not_repeat` and `eclipsing_binary` rule a candidate out. `single_sector_only` and `inconclusive` mean you need more data.

**Board and limits**

- A lead reaches the board only when two different hunters call it a candidate and the server's results agree. Nothing is submitted anywhere.
- Period search covers 0.2 to 30 days. Durations run 0.5 to 6 hours. Very faint (TESS mag 15+) and saturated (brighter than about 6.5) stars are unreliable.
- Limits: 60 calls and 5 follow-ups per hour. Logged tool names, arguments and numbers are kept under an anonymous daily id. A handle you choose is public on the board.

Want the guided lesson? It's opt-in, five steps, and recovers the known planet pi Mensae c from raw data. Or tell me a star or a workflow above and we'll run it.

## 4. lets hunt for planets

*9s; tools: ['board', 'read_guide']*


The board has no real leads yet. It holds 3 development test leads (well-known stars used to test the loop), and nothing is confirmed.

To start hunting I need a handle: 3–20 letters, digits, dash or underscore. It's public on the board and is how I register you for the lead queue. Once you give me one, I'll pick a path:

- **Vet a queued lead:** take the next unvetted signal and run the false-positive tests on it.
- **Search new sky:** map a patch of unsearched stars. This runs on your computer for about seven minutes, and the results are stored for everyone.

Which path do you want?

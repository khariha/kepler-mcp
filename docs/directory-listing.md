# Connectors Directory submission: Kepler MCP (DRAFT)

Submit at https://claude.ai/directory/manage -> Submit new -> MCP connector (paid plan; on Team/Enterprise, an Owner).
Questions: mcp-review@anthropic.com. Requirements source: https://claude.com/docs/connectors/building/submission

## Connection
- URL: https://kepler.genexperiences.com/mcp (Streamable HTTP)
- Authentication: none (authless). No account is connected; handles are optional and chosen in conversation.

## Listing
- **Name (≤100):** Kepler MCP
- **One-liner (≤200):** Search NASA TESS telescope images for transiting planet candidates, test each signal against
  known false positives, and help vet signals other volunteers found.
- **Description (≤2,000):**
  Kepler searches NASA TESS full-frame images for the small, regular dips in starlight a transiting planet causes, then
  tests each signal against the usual false positives: eclipsing binaries (odd and even depths, a secondary eclipse at
  any phase), neighbouring stars (centroid shift, difference image, Gaia neighbours) and noise. A multi-Sector follow-up
  checks whether a signal repeats years apart and returns a row in the shape astronomers use for candidate tables.

  Three ways in: check a star you know of ("does TOI-700 have transits?"), vet a signal from the shared queue, or search
  a patch of about 150 stars no one has searched, in your own browser in about six minutes, with no install. Signals two different
  people vet as candidates, and that pass the server's own tests, are listed on a public board with credit.

  Results use the wording of the TESS follow-up community: transit-like signal, planet candidate, false positive.
  Nothing Kepler reports is a confirmed planet; confirmation needs follow-up observations.
- **Categories (1-5):** Science, Education, Research [check the portal's list]
- **Documentation URL:** [the public README or a page on genexperiences.com]
- **Privacy policy URL:** https://kepler.genexperiences.com/privacy
- **Support contact:** company@genexperiences.com

## Example prompts (at least three)
1. "Does Pi Mensae have a transiting planet? Check it with Kepler." (recovers pi Men c at 6.27 d)
2. "Give me a signal to vet and walk me through whether it's a planet candidate."
3. "Search a patch of unsearched sky for planets." (opens the Field Search Tool in the browser; about six minutes, no install)
4. "Is TOI-700 b visible in TESS Sector 4, and how deep is the transit?"

## Use cases (form step 4; shown to reviewers only)
| Use case | Example prompt |
|---|---|
| Check a star for a transiting planet | Does Pi Mensae have a transiting planet? Check it with Kepler. |
| Vet a signal from the shared queue | Give me a signal to vet and walk me through whether it's a planet candidate. |
| Search unsearched sky for new candidates | Search a patch of unsearched sky for planets. |

**Prerequisites:** None. No account, sign-in or API key: the server is authless and reads only public NASA and ESA archives.
Optional: a public handle and recovery word, chosen in the chat, to vet signals or search new sky with credit on the board.
Searching new sky opens the Field Search Tool in a web browser (a recent desktop browser; nothing to install).

**Read/write capabilities:** Read and write. Most tools only read public archives; `join`, `next_lead`, `submit_vet`,
`report_verdict` and `open_patch` write to Kepler's own records (a handle, a lead claim, a verdict, a field claim).
None is destructive, and none writes to any outside service or account.

## Tools (all have a title and hints)
Read-only (readOnlyHint true): resolve_star, catalog_check, light_curve, search_transits, vet_signal, neighbors,
fold_plot, followup_start, job_status, read_guide, lesson, find_planets, board, my_progress, sky_map, patch_status.
Writes, not destructive (readOnlyHint false, destructiveHint false): join, next_lead, submit_vet, report_verdict, open_patch.

## Open questions before submitting
- **Field search (resolved).** It no longer runs anything on the user's computer outside the browser: open_patch returns a
  link to the Field Search Tool page, which reads NASA's public TESS data and runs the search in the tab (Pyodide), and in chat
  clients that show MCP Apps the same page runs as a panel (resource ui://kepler/field-search). No install, no shell command.
- **Logging for training.** Allowed as far as the published pages say, but it must be stated in the privacy policy
  (it is in the draft) and in the server's own notice (it is).
- **Test & launch step:** confirm every tool was run via MCP Inspector or as a custom connector before submitting.

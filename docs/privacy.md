# Kepler MCP privacy policy (DRAFT, not yet published)

*Kepler MCP is operated by Generative Experiences. Last updated: [date of publication].*

## What Kepler is
Kepler is a remote MCP server that searches NASA TESS telescope images for transit-like signals and lets volunteers vet them.
It reads public astronomy archives (NASA MAST, ESA Gaia, the NASA Exoplanet Archive, ExoFOP). It does not connect to any
account of yours and never asks for credentials.

## What we collect
- **Tool calls.** For each call: the tool name, its arguments (star identifiers, periods, Sectors and similar numbers),
  the numeric result, how long it took, and the time.
- **An anonymous session id.** A one-way hash of your network address and client name, salted and rotated daily. Your
  network address itself is never written to disk.
- **A handle, if you choose one.** Handles are public: they appear on the board with the signals you searched and vetted.
  The recovery word you pick is never stored; only a one-way hash of it, used to recognise you when you sign in again.
- **Verdicts you submit** and the short reason you write with them. These are public on the board when a signal is listed.
- **Field-search uploads.** If you run a field search, the Field Search Tool page in your browser uploads one row of numbers per star
  (period, depth, signal strength and similar). No files, paths or personal data are uploaded.

## What we do not collect
Your conversation with Claude, prompts, model reasoning, files, chat history or memory. Kepler only sees the arguments of
the tools Claude chooses to call. No account, email address, IP address or location is stored.

## How we use it
- To run the service: caching results, rate limiting, the queue of signals, the public board and search coverage.
- To improve the tools and the science, and **to train open models** on the anonymous tool-call records (tool names,
  arguments and numeric results only).

## Sharing
The board (handles, signals, verdicts, search coverage) is public. Anonymous tool-call records may be released as part of
open datasets or models. We do not sell data and share nothing else with third parties.

## Retention
[DECIDE: e.g. tool-call records kept for N months; handles and verdicts kept while the board exists.]
A handle and its verdicts can be removed on request.

## Contact
company@genexperiences.com, Generative Experiences.

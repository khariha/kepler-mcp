# Kepler MCP privacy policy

*Kepler MCP is operated by Generative Experiences. Last updated: 2026-11-14. Published at https://kepler.genexperiences.com/privacy*

## What Kepler is
Kepler is a remote MCP server that searches NASA TESS telescope images for transit-like signals and lets volunteers vet them.
It reads public astronomy archives (NASA MAST, ESA Gaia, the NASA Exoplanet Archive, ExoFOP). It does not connect to any
account of yours and never asks for credentials.

## What we collect
- **Tool calls.** For each call: the tool name, its arguments (star identifiers, periods, Sectors and similar numbers),
  the numeric result, how long it took, and the time.
- **An anonymous session id.** A one-way hash of your network address and client name, salted and rotated daily. Kepler's
  own records never contain your network address.
- **A handle, if you choose one.** Handles are public: they appear on the board with the signals you searched and vetted.
  The recovery word you pick is never stored or logged; only a one-way hash of it, used to recognise you when you sign in again.
- **Verdicts you submit** and the short reason you write with them. These are public on the board when a signal is listed.
- **Field-search uploads.** If you run a field search, the Field Search Tool page in your browser uploads one row of numbers
  per star (period, depth, signal strength and similar). No files, paths or personal data are uploaded. The page keeps its
  progress in your browser's local storage so a closed tab can resume; that copy never leaves your computer and is deleted
  when the field is done.
- **Web server logs.** Like any website, the web proxy in front of Kepler and Cloudflare, our network provider, record the IP
  address, time and requested path of each request in their access logs, for security and abuse prevention.

## What we do not collect
Your conversation with Claude, prompts, model reasoning, files, chat history or memory. Kepler only sees the arguments of
the tools Claude chooses to call. No account, email address or location.

## How we use it
- To run the service: caching results, rate limiting, the queue of signals, the public board and search coverage.
- To improve the tools and the science, and **to train open models** on the anonymous tool-call records (tool names,
  arguments and numeric results only).

## Sharing
The board (handles, signals, verdicts, search coverage) is public. Anonymous tool-call records may be released as part of
open datasets or models. We do not sell data and share nothing else with third parties, apart from the network providers
named above, which carry the traffic.

## Retention
Anonymous tool-call records are kept indefinitely. Handles, verdicts and search coverage are kept while the board exists.
A handle and its verdicts can be removed on request.

## Contact
company@genexperiences.com, Generative Experiences.

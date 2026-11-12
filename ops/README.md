# ops: building the leads queue

Operator tools. None of this is exposed through the MCP server.

1. `python ops/select_targets.py --sector 7` builds `ops/data/targets_s7.csv`: stars in the sector that are not known planet hosts, TOIs or CTOIs. Defaults select faint red dwarfs (`--tmag 12 15 --teff-max 4000 --rad-max 0.6`); widen with `--teff-min/--teff-max/--rad-max`.
2. `modal run --detach ops/sweep_modal.py --sectors "7 8"` does selection and the sweep in the cloud, one sector at a time. About 8 CPU-seconds per star. Check with `--sector 7 --mode status`, fetch with `--sector 7 --mode pull`.
3. `python ops/build_leads.py --sectors 7 8` writes `ops/leads.jsonl`. `start.py` loads it automatically in place of the practice leads.

Rules: at most 50 containers and one sweep at a time; stop a run with `modal app stop <app-id>`. Spot-check ten leads by hand before opening a new queue to hunters.

`ops/private_exclude.txt` (one TIC per line, never committed) lists stars that must not become leads. `ops/dev_leads.jsonl` holds three well-known stars for testing the hunting loop.

# ops

- `dev_leads.jsonl`: three well-known stars used as practice leads.
- `private_exclude.txt` (never committed): one TIC per line; these stars are never put in a patch. The hub reads it at `KEPLER_EXCLUDE`.

There is no bulk sweep to run. Hunters search patches on their own computers (`kepler-sweep`); the hub maps a detector into patches the first time someone asks for sky, which takes about five minutes.

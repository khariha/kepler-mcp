# Deploying the Kepler MCP server: EC2 behind Cloudflare

One EC2 instance runs two containers: the Kepler server and Caddy (HTTPS). Cloudflare sits in front for DNS, caching
of the board page, and rate limiting. Images come from GitHub Container Registry (`ghcr.io/khariha/kepler-mcp`) or are
built on the box from the repository; nothing is pushed to Docker Hub.

```
visitor ──HTTPS──> Cloudflare ──HTTPS──> EC2: Caddy :443 ──> kepler :8000
                                          (only Cloudflare may reach :443)
```

## 1. The instance
- Region **us-east-1** if possible: the TESS pixel cubes live there, so downloads are fast and free of transfer fees.
- Size: 2 vCPU / 4 GB is the floor; 4 vCPU / 8 GB is safer (vetting is CPU-bound). 40-60 GB gp3 disk for the pixel cache.
- Ubuntu 24.04. Install Docker:
  ```bash
  sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
  ```
- Security group: **22** from your own address only; **443** (and 80) from Cloudflare's address ranges only
  (https://www.cloudflare.com/ips-v4 and /ips-v6). This matters: the rate limiter trusts the `CF-Connecting-IP` header,
  which only Cloudflare can set if nobody else can reach the box.

## 2. The code and image
Either build on the box:
```bash
git clone https://github.com/khariha/kepler-mcp.git && cd kepler-mcp/deploy
```
or pull the published image (after `docker login ghcr.io` with a GitHub token that has `read:packages`, if the package is private)
and set `image: ghcr.io/khariha/kepler-mcp:<tag>` in place of `build: ..` in `docker-compose.yml`.

To publish an image from your machine (only to ghcr). EC2 is x86_64, so build for that platform:
```bash
docker buildx build --platform linux/amd64 -t ghcr.io/khariha/kepler-mcp:0.3.1 --push .
```
(after `echo "$GITHUB_TOKEN" | docker login ghcr.io -u khariha --password-stdin`).

## 3. Settings (deploy/.env, never committed)
```bash
cat > .env <<EOF
MCP_DOMAIN=kepler.genexperiences.com
KEPLER_SALT=$(openssl rand -hex 16)
KEPLER_PUBLIC_URL=https://kepler.genexperiences.com
EOF
```
`KEPLER_SALT` keys the anonymous session ids; keep it secret and stable.

## 4. The private exclusion list
Field searches skip stars listed in `ops/private_exclude.txt`. It is not in the repository. Copy it into the data volume once:
```bash
scp ops/private_exclude.txt ubuntu@<host>:~/kepler-mcp/deploy/
sudo docker compose cp private_exclude.txt mcp:/app/data/private_exclude.txt
```
`docker-compose.yml` already points `KEPLER_EXCLUDE` at that path.

## 5. Start
```bash
sudo docker compose up -d --build
sudo docker compose logs -f mcp        # wait for "Uvicorn running"
curl -s https://kepler.genexperiences.com/health
```

## 6. Cloudflare
1. DNS: an `A` record `kepler` -> the instance's Elastic IP, **proxied** (orange cloud).
2. SSL/TLS mode: **Full (strict)**. Caddy holds a real certificate; to issue it, either leave port 80 open to Cloudflare
   (HTTP challenge) or use a Cloudflare Origin Certificate in the Caddyfile.
3. Rules:
   - Cache `/board` and `/board.json` for 60 s.
   - Do not cache `/mcp`, `/patch/*`, `/health`.
   - Rate limiting rule on `/mcp` and `/patch/*`, e.g. 120 requests per minute per IP, on top of the server's own limits.
4. Cloudflare closes any request that sends nothing for 100 s. The server caps every tool call at 85 s
   (`KEPLER_TIMEOUT_S`) and runs long work (follow-ups) as polled jobs, so this does not bite.

## 7. Before announcing
- Warm a few well-known stars: `python scripts/warm.py https://kepler.genexperiences.com/mcp "Pi Mensae" "TOI-700"`.
- Map the open Sectors so the first visitors do not wait for mapping (call `sky_map` and `open_patch` once per star type).
- Field searches by visitors need the helper installable: the repository public, or the package on PyPI
  (set `KEPLER_SWEEP_INSTALL` to the install line).
- Connect Claude Code to the public URL and run one vet and one field search end to end.

## Operating it
- Logs: `sudo docker compose logs -f mcp`
- Usage traces (anonymous JSON lines) in the `mcpdata` volume under `mcp/traces/`.
- Back up `leads.db` and `sky2.db` (in `mcp/`) nightly, e.g. to S3: they are the queue, verdicts, handles and search coverage.
- Disk: pixel cubes are 5-40 MB per star and Sector. Prune: `sudo docker compose exec mcp find /app/data/cubes -atime +30 -delete`.
- Pause new field searches: set `KEPLER_PATCHES_PAUSED: "1"` and restart.
- Update: `git pull && sudo docker compose up -d --build` (or bump the ghcr tag).
- Stop serving at once: `sudo docker compose down`.

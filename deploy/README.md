# Deploying the tess-hunt MCP server on AWS

One small instance in **us-east-1**, the region that holds the MAST full-frame-image cubes, so pixel fetches are fast and free of egress.

## What you create
1. EC2 instance: `c7i.large` (2 vCPU, 4 GB), Ubuntu 24.04, 40 GB gp3 disk, region us-east-1. About $65 a month on demand; a `t3.medium` at about $30 works for a quiet launch.
2. Security group: inbound 80 and 443 from anywhere, 22 from your address only.
3. An Elastic IP attached to the instance.
4. DNS: an `A` record for your host name (written `mcp.example.org` below) pointing at the Elastic IP.

## On the instance
```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
git clone https://github.com/khariha/tess-hunt-mcp.git && cd tess-hunt-mcp/deploy
printf 'MCP_DOMAIN=mcp.example.org\nTESSHUNT_MCP_SALT=%s\n' "$(openssl rand -hex 16)" > .env
sudo docker compose up -d --build
curl https://mcp.example.org/health
```
Caddy obtains the TLS certificate by itself on first start. The MCP endpoint is `https://mcp.example.org/mcp`.

## Warm the cache before launch
From any machine, run a few stars once so first-time visitors get instant answers:
```bash
python scripts/warm.py https://mcp.example.org/mcp "Pi Mensae" "TOI-700"
```

## Operating it
- Logs: `sudo docker compose logs -f mcp`
- Usage traces (anonymous, JSON lines): inside the `mcpdata` volume under `mcp/traces/`. Copy out with
  `sudo docker compose cp mcp:/app/data/mcp/traces ./traces`.
- Disk: cached pixel cubes are 5-40 MB per star and sector. Prune ones unused for 30 days with
  `sudo docker compose exec mcp find /app/data/cubes -atime +30 -delete`.
- Update: `git pull && sudo docker compose up -d --build`.
- Stop serving at once: `sudo docker compose down`.
- Limits are environment variables in `docker-compose.yml`: light calls and follow-ups per hour per user, worker counts, timeout.

## What it will not do
No sector sweeps, no MCMC, no writes to ExoFOP or any journal, no user-supplied URLs or file paths. It reads public NASA and ESA archives only.

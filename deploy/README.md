# Deploying Garmin Insights

The hosted server runs the same app as local mode, plus:

- a password sign-in for the dashboard (`/login`)
- the remote MCP server at `/mcp`, protected by OAuth (for claude.ai, the Claude mobile apps and
  the companion Android app)
- AI insights through the Claude API
- background Garmin sync inside the server

Two containers run: the app, and Caddy in front of it for automatic HTTPS. Data lives in
the `garmin_data` Docker volume (database, Garmin sign-in token, settings, `auth.db`).

## 1. Create the server (Oracle Cloud Always Free)

1. Sign up at cloud.oracle.com. Optionally upgrade to *Pay As You Go* so idle Always Free
   machines are not reclaimed (Always Free resources stay free), and set a US$1 budget alert.
2. **Compute → Instances → Create instance**
   - Image: **Ubuntu 24.04**
   - Shape: **Ampere A1.Flex**, 1–2 OCPUs and 6–12 GB memory (within the free allowance).
     If it says *out of capacity*, try another availability domain or retry later.
   - Add your SSH public key, and keep the public IPv4 address it assigns.
3. **Open ports 80 and 443**
   - In the instance's subnet → *Security List* → add ingress rules for TCP 80 and 443 from
     `0.0.0.0/0`.
   - Oracle's Ubuntu images also block them in the VM's own firewall. The rules must sit above
     the `REJECT` rule (line 5 on a fresh image; check with `sudo iptables -L INPUT --line-numbers`):
     ```bash
     sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 443 -j ACCEPT
     sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 80 -j ACCEPT
     sudo netfilter-persistent save
     ```

## 2. Point the domain (Namecheap)

Domain List → `example.com` (your domain) → **Advanced DNS** → add an **A Record**:
Host `garmin`, Value = the server's public IP, TTL automatic.

## 3. Install Docker and the app

```bash
ssh ubuntu@<server-ip>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/mik91/garmin-insights.git   # private: use a GitHub token or deploy key
cd garmin-insights/deploy
cp .env.example .env              # GARMIN_DOMAIN=garmin.example.com
cp server.env.example server.env
```

Create the dashboard password hash (on your PC, in the project folder) and paste it, the
Claude API key and your Garmin Personal API key into `server.env`:

```bash
python -m garmin_ai.auth hash-password
```

Then start it:

```bash
docker compose up -d --build
docker compose logs -f app          # Ctrl+C to stop following
```

Open https://garmin.example.com and sign in. Caddy fetches the certificate on the first
request once DNS points at the server.

## 4. Bring your history and Garmin sign-in (optional)

Copy your local database (and Garmin sign-in token, so the server can sync without a new
sign-in) from your PC:

```bash
scp data/garmin.db .garmin_tokens/garmin_tokens.json ubuntu@<server-ip>:~/
```

On the server:

```bash
cd ~/garmin-insights/deploy
docker compose stop app
docker run --rm -v garmin-insights_garmin_data:/data -v ~/:/upload alpine sh -c \
  "mkdir -p /data/garmin_tokens && cp /upload/garmin.db /data/ && \
   cp /upload/garmin_tokens.json /data/garmin_tokens/ && chown -R 10001:10001 /data"
docker compose start app && rm ~/garmin.db ~/garmin_tokens.json
```

Without this you can sign in to Garmin from the hosted **Connections** page instead and
press **Sync now** (Garmin sometimes challenges sign-ins from data-centre addresses; copying
the token avoids that).

## 5. Connect Claude

claude.ai → **Settings → Connectors → Add custom connector** → URL
`https://garmin.example.com/mcp`. You are sent to your own sign-in page, then an
**Allow access** page. The connector then works in claude.ai and the Claude Android app.

## Hevy lifting data (optional)

With Hevy Pro, create an API key at hevy.com > Settings > Developer and add it to
`server.env` on the server (not to git):

```bash
HEVY_API_KEY=<your key>
```

Hevy always reports weights in kg. `/api/strength` also says which unit you log in
(`weight_unit`), detected from the weights; set `HEVY_WEIGHT_UNIT=kg` or `HEVY_WEIGHT_UNIT=lb`
in `server.env` if the guess is wrong.

Then `docker compose up -d` to restart with it. The next sync backfills every Hevy workout,
and later syncs fetch only changes. Workouts are linked to the overlapping Garmin strength
activity and served at `/api/strength` and by the `strength_workouts` MCP tool; see
[docs/strength-api.md](../docs/strength-api.md). Sync problems show up in `sync.error` of `/api/strength`
and as `hevy_error` in the `sync_status` MCP tool.

## Updating

The server folder is a copy of the code, not a git clone, so updates are uploaded from your
PC. `git archive` packs only committed, tracked files, so `.env` and `server.env` are never
in it; the server keeps its own. Use `master`, or any branch you want to test there.

On your PC, in the project folder:

```bash
git archive --format=tar.gz -o ../garmin-insights.tar.gz master
scp ../garmin-insights.tar.gz ubuntu@<server-ip>:~/
```

On the server, swap the folder in, keeping the old one and the secrets, then rebuild:

```bash
mv garmin-insights garmin-insights.old
mkdir garmin-insights && tar xzf garmin-insights.tar.gz -C garmin-insights
cp -p garmin-insights.old/deploy/.env garmin-insights.old/deploy/server.env garmin-insights/deploy/
rm garmin-insights.tar.gz
cd garmin-insights/deploy && docker compose up -d --build
docker compose ps                   # app should show (healthy)
```

Only the app container is rebuilt; the data in the `garmin_data` volume is untouched. To go
back, swap the folders and rebuild:

```bash
mv ~/garmin-insights ~/garmin-insights.failed && mv ~/garmin-insights.old ~/garmin-insights
cd ~/garmin-insights/deploy && docker compose up -d --build
```

Once the new version works, remove the old copy with `rm -rf ~/garmin-insights.old`.

## Signing out everywhere

To revoke every dashboard session and every connected app (for example after a lost phone):

```bash
docker compose exec app python -c "from garmin_ai import auth; auth.revoke_all()"
```

Change the password by putting a new hash in `server.env` and running `docker compose up -d`.

## Backups

```bash
docker compose cp app:/app/data/garmin.db ./garmin-backup-$(date +%F).db
```

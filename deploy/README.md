# Deploy

Conduit deploys to the lon1 box (`165.232.110.29`). The box can't reach GitHub,
so deploys go through a bare repo with a `post-receive` hook that checks the code
out straight into the served production directory — one source of truth, no
duplicate copies.

## One-time setup (already done)

```bash
# On the box: bare repo + deploy hook -> /opt/scripts/conduit
git init --bare /opt/git/conduit.git
cat > /opt/git/conduit.git/hooks/post-receive <<'HOOK'
#!/bin/bash
GIT_WORK_TREE=/opt/scripts/conduit git checkout -f main
HOOK
chmod +x /opt/git/conduit.git/hooks/post-receive

# On the dev machine:
git remote add box ssh://root@165.232.110.29/opt/git/conduit.git
```

Generated artifacts (`gas_dashboard.html`, `gas_cache.json`,
`natgas_catalogue.json`, `data/`) are gitignored, so `checkout -f` never
clobbers the live page or the SQLite store.

## Deploy

```bash
git push box main      # -> checks out into /opt/scripts/conduit
```

## Daily run

`deploy/conduit_run.sh` (symlinked/copied to `/opt/scripts/conduit_run.sh`) is
invoked by cron at 08:00 UTC:

```cron
0 8 * * * /opt/scripts/conduit_run.sh
```

It runs `pipeline.py`, which writes `gas_dashboard.html` (served by nginx at
`/conduit/`) and pushes the morning brief to Telegram. On non-zero exit it sends
a Telegram failure alert.

## First-time data backfill (on the box)

```bash
cd /opt/scripts/conduit
/opt/scripts/venv/bin/python3 backfill.py --all     # seasonality + prices + 2y flows
```

# Deploying SpeedLab to a permanent URL

The quick tunnel (`./serve-public.sh`) is fine for occasional use but its URL changes on
every restart. These are the options for a URL that does not.

Two constraints decide everything, and most "just deploy it free" advice fails one of them:

1. **The encode runs after the HTTP response is sent.** A background thread does minutes of
   100% CPU once the upload returns. Any host that suspends, freezes or scales-to-zero an
   idle instance kills jobs half-finished, usually with no error you would recognise.
2. **It needs a persistent disk.** `inbox/`, `work/`, `outbox/` and the SQLite database must
   survive restarts. Most free tiers give no writable volume at all.

## Measured, not assumed

Upload ceiling through a Cloudflare quick tunnel is **512 MiB** — tested against the live
tunnel: 495 MiB returns 200, 514 MiB returns `413 Payload Too Large` from Cloudflare's edge,
rejected after ~1 MB on `Content-Length`. The same file posted straight at the container
succeeds, so the limit is Cloudflare's, not SpeedLab's.

## Options

### 1. A small VPS — recommended

DigitalOcean Basic, 2 vCPU / 4 GiB / 80 GB, **$24/month**, 4,000 GiB transfer included.
No request lifecycle at all: no scale-to-zero, no idle freeze, no execution ceiling, no
body-size cap beyond what you configure. `deploy/vps-setup.sh` installs Docker, clones the
repo, brings the container up and — if you pass a domain — installs Caddy for automatic
HTTPS with a 2 GB request body limit.

```
curl -fsSL https://raw.githubusercontent.com/rohitdhuriya-debug/upsurge-speedlab/main/deploy/vps-setup.sh \
  | bash -s -- speedlab.yourdomain.com
```

You own OS patching and backups. Roughly 30 minutes of setup.

> Hetzner is cheaper (~$11/month) and would be the better deal, but at the time of writing
> its CX/CAX and CCX lines all show as unavailable for new orders. Check
> `status.hetzner.com` before assuming it is an option.

### 2. Render — easiest, no domain needed

Copy `deploy/render.yaml` to the repo root, then **New > Blueprint** in Render.
Gives a free permanent `https://upsurge-speedlab.onrender.com` with zero-config TLS.

- **Standard plan is the floor: $25 + $5 for a 20 GB disk = $30/month.** Free and Starter
  cannot work — no disk, and free spins down after 15 minutes idle.
- **Outbound bandwidth is 5 GB/month included, then $0.15/GB, uncapped.** One 500 MB
  finished video is 10% of the monthly allowance, and downloading outputs is the whole point
  of this app. Watch this.
- Attaching a disk disables zero-downtime deploys, and Render force-restarts disk-attached
  services for maintenance roughly quarterly. Both kill an in-flight encode.
- No documented request body cap, but that is absence of a limit, not a guarantee — smoke
  test a large upload before relying on it.

### 3. Fly.io

Copy `deploy/fly.toml` to the repo root, then `fly launch --no-deploy && fly deploy`.
Around $38/month once you account for the two traps the config already handles:

- `auto_stop_machines` **must** be `false`. Fly's proxy cannot see the background worker;
  with auto-stop on it stops the machine minutes after the upload response and the encode
  dies. A keepalive ping does not work around it.
- Shared vCPUs collapse to 6.25% of a core after a ~500 s burst balance, which drops x264 to
  1/16 speed mid-encode. `cpu_kind = "performance"` is required.
- Fly's default kill signal is SIGINT, which CPython delivers only to the main thread — the
  worker would never hear it. The config sets `SIGTERM` explicitly.

### 4. Cloudflare named tunnel — permanent URL, app stays on your Mac

Cheapest permanent URL (~$10–12/year for a domain, no server). The encode does not get
slower, because it never leaves your machine.

```
cloudflared tunnel login
cloudflared tunnel create speedlab
cloudflared tunnel route dns speedlab speedlab.yourdomain.com
cloudflared tunnel run --url http://127.0.0.1:5090 speedlab
```

Caveats: your Mac must be awake, online and on AC for the service to exist, and a proxied
Cloudflare zone applies a plan-based request body cap — 100 MB on Free and Pro. That cap is
**not** what the quick tunnel enforces (measured 512 MiB above), so verify it against your own
domain before relying on it for large files.

### 5. Google Cloud Run — does not work

Passes the background-CPU question with instance-based billing and `--min-instances=1`, which
surprises people, but there is a **32 MiB request body cap on HTTP/1** enforced at the Google
Front End before the request reaches the container. Not raisable by any setting. Uploads of
real video are impossible.

### Railway

Not covered — the research agent for it failed mid-run, so there is no verified data here
rather than a verdict.

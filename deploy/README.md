# Hosting the demo

How the live demo for judges runs: on the Tencent Cloud Lighthouse server that already hosts
FirstRoll (Singapore, 2 vCPU, 2 GB), at `https://glassvault.luo-zy.com`, behind a password.

```
judge's browser ──HTTPS──> FirstRoll's Caddy (ports 80/443, Let's Encrypt)
                              ├─ firstroll.app, api.firstroll.app ──> FirstRoll
                              └─ glassvault.luo-zy.com ──> glassvault:8000 (this container)
```

| File | Purpose |
|---|---|
| `Dockerfile` | The demo server image: Python 3.12, locked dependencies, no root |
| `docker-compose.yml` | The container: on FirstRoll's network, read-only, no capabilities, 512 MB, one CPU |
| `deploy.sh` | First-time setup (`--init`) and every deploy, from your machine over SSH |

The site itself is a block in **FirstRoll's** Caddyfile (`infra/vps/Caddyfile` in that
repository), released with FirstRoll after the owner approves the release. Keeping it there
means FirstRoll's CI checks it and nothing outside FirstRoll's repository can change its
Caddy; without this container, the demo's host answers 502 and FirstRoll is unaffected. This
repository never writes to `/opt/firstroll`.

## Why it is safe to put online

Every chat turn calls DeepSeek and Hunyuan, and voice and Help call Tencent RTC and ADP, all on
your credit. So the hosted server turns on settings the local demo leaves off
(`src/glassvault/web/server.py`, "Hosting"):

- **A password** (`GV_ACCESS_PASSWORD`): every request needs it, as HTTP basic auth with the user
  name `judge`. Both end the submission's Project Description (`docs/submission/description.md`),
  so they are as public as the submission and this repository: the password keeps out bots and
  passers-by, not a determined visitor, and the cap below and the provider limits bound what one
  could spend. To change it after judging, delete `GV_ACCESS_PASSWORD` from your local `.env`,
  then rerun `--init` and deploy.
- **A demo per visitor** (`GV_SESSIONS=1`): each browser gets its own bank, conversation, rogue
  switch and passkey, so judges trying it at the same time never see each other's actions. The 16
  most recently used demos are kept; requests without the visitor cookie never displace one.
- **A cap on paid calls** (`GV_PAID_CALLS_PER_HOUR`, 300 by default): live model turns, speech
  synthesis, speech recognition and ADP answers together, for all visitors. Requests that spend
  nothing are not counted. A judge's full tour uses about 30.
- Always on: request bodies over 4 MB are refused, writes from other sites are refused, and a demo
  whose audit log passes 2,000 entries asks to be reset.

Also set a spending limit with each provider, because a leaked password could still spend up to
the hourly cap:

- **DeepSeek:** prepaid, so keep only a few dollars on the account; calls stop when it is empty.
- **OpenRouter:** give the key a credit limit (for example US$5) in its settings.
- **Tencent Cloud:** ideally use a CAM sub-user's key with access to TRTC and ADP only, not the
  root account's, and set a budget alert in Billing. Anyone who can run Docker on the server,
  FirstRoll's deploy account included, can read these keys with `docker inspect`.

The demo's passkeys need HTTPS and a domain name, and push-to-talk needs HTTPS, so it is served
by name, not by IP: `glassvault.luo-zy.com`, an A record at Spaceship pointing at the server. A
real domain rather than an sslip.io name, because corporate networks often block those "dynamic
DNS" hosts. Without a domain of your own, `deploy.sh --init` falls back to `<ip>.sslip.io`.
Changing the host means changing it in FirstRoll's Caddyfile (a FirstRoll release) and rerunning
`--init` with `GLASSVAULT_HOST` set, then restarting the container.

## First time

1. **FirstRoll release with the site block.** Merge FirstRoll's pull request that adds the block,
   then approve its release. Until then the deploy below runs the container but its final check
   from outside fails.
2. **The server's settings.** With the admin login (it needs sudo):

   ```bash
   GLASSVAULT_HOST=glassvault.luo-zy.com SSH_OPTS="-i ~/.ssh/firstroll-admin.pem" \
     deploy/deploy.sh ubuntu@119.28.111.192 --init
   ```

   This copies your `.env` (the API keys) to `/opt/glassvault/.env` (root, mode 0600) and adds
   the host name, a new password (saved to your local `.env` as `GV_ACCESS_PASSWORD`, never
   printed) and the hourly cap. Rerunning it replaces the server's settings; deploy afterwards
   to apply them.
3. **Deploy**, as below.

## Every deploy

```bash
SSH_OPTS="-i ~/.ssh/firstroll-admin.pem" deploy/deploy.sh ubuntu@119.28.111.192
```

It builds the committed code (git `HEAD`; never the working tree or `.env`) for linux/amd64 on
your machine with Docker, loads the image on the server (which never builds, so FirstRoll keeps
its CPU), installs `docker-compose.yml` in `/opt/glassvault` and starts the container. If the
new container does not become healthy, the previous image is started again. Finally it checks
from your machine that `/healthz` answers and everything else asks for the password; the very
first visit waits a few seconds for the certificate.

A deploy restarts the container, which clears every visitor's demo, so avoid deploying while
judges are using it. To go back to older code, check out that commit and deploy it.

## Operating it

```bash
ssh -i ~/.ssh/firstroll-admin.pem ubuntu@119.28.111.192 sudo docker logs --tail 50 glassvault-glassvault-1
ssh -i ~/.ssh/firstroll-admin.pem ubuntu@119.28.111.192 sudo docker stats --no-stream   # both apps' memory
ssh -i ~/.ssh/firstroll-admin.pem ubuntu@119.28.111.192 sudo docker compose -f /opt/glassvault/docker-compose.yml restart
```

Restarting clears every visitor's demo (they are in memory) and nothing else. To take the demo
offline: `sudo docker compose -f /opt/glassvault/docker-compose.yml down` on the server; the
host then answers 502 until the site block is removed from FirstRoll's Caddyfile. If FirstRoll's
network is ever recreated, start the demo again with `up -d`.

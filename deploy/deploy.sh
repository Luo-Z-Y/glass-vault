#!/usr/bin/env bash
# Deploy Glass Vault beside FirstRoll on the Lighthouse server (deploy/README.md).
#
#   deploy/deploy.sh <ssh-destination> --init   # first time: write the server's .env from yours
#   deploy/deploy.sh <ssh-destination>          # build the committed code and (re)start it
#
# <ssh-destination> is the admin login, e.g. ubuntu@119.28.111.192 (it needs sudo); pass a key
# with SSH_OPTS="-i ~/.ssh/key.pem" if your ssh config does not name one.
#
# --init copies your local .env (the API keys) to /opt/glassvault/.env (root, mode 0600) and adds
# the hosting settings: GLASSVAULT_HOST, the server's IP as an sslip.io name (or the
# GLASSVAULT_HOST you set when running this), and GV_PAID_CALLS_PER_HOUR. Deploy afterwards to
# apply it.
#
# A deploy builds git HEAD (never the working tree or .env) for linux/amd64 on this machine, loads
# the image on the server, which never builds, installs docker-compose.yml and starts the
# container. If it does not become healthy, the previous image is started again. Then it checks
# the site from here: /healthz answers and the page opens, with no sign-in. The site is a
# block in FirstRoll's Caddyfile, released with FirstRoll; this script never touches FirstRoll.
set -euo pipefail

dest=${1:?usage: deploy/deploy.sh <ssh-destination> [--init]}
mode=${2:-deploy}
root=${GLASSVAULT_ROOT:-/opt/glassvault}  # overridable only for a rehearsal against a local Docker
compose="sudo docker compose -f $root/docker-compose.yml"
container=glassvault-glassvault-1
cd "$(git rev-parse --show-toplevel)"

# shellcheck disable=SC2086  # SSH_OPTS is a list of options
remote() { ssh -o BatchMode=yes ${SSH_OPTS:-} "$dest" "$@"; }

if [ "$mode" = "--init" ]; then
  [ -f .env ] || { echo "no local .env with the API keys" >&2; exit 1; }
  host=${GLASSVAULT_HOST:-}
  if [ -z "$host" ]; then
    ip=${dest#*@}
    [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "give the server by IP, or set GLASSVAULT_HOST" >&2; exit 1; }
    host="${ip//./-}.sslip.io"
  fi
  {
    grep -v -E '^(GLASSVAULT_HOST|GV_PAID_CALLS_PER_HOUR|GV_ORIGIN|GV_RP_ID|GV_SESSIONS|GV_HOST|GV_PORT|PORT)=' .env
    printf '\n# Hosting (deploy/deploy.sh --init)\nGLASSVAULT_HOST=%s\nGV_PAID_CALLS_PER_HOUR=300\n' "$host"
  } | remote "set -e; umask 077; sudo install -d -m 0700 $root; sudo tee $root/.env >/dev/null; sudo chmod 0600 $root/.env"
  echo "Wrote $root/.env on the server for https://$host (open to anyone with the link)"
  echo "Now deploy to apply it."
  exit 0
fi

remote "sudo test -f $root/.env" </dev/null || { echo "no $root/.env on the server yet: run with --init first" >&2; exit 1; }
host=$(remote "sudo sed -n 's/^GLASSVAULT_HOST=//p' $root/.env | tail -n 1" </dev/null)
[ -n "$host" ] || { echo "GLASSVAULT_HOST is missing from $root/.env" >&2; exit 1; }
remote "sudo docker network inspect firstroll_default >/dev/null" </dev/null \
  || { echo "FirstRoll's network firstroll_default is missing: is FirstRoll running?" >&2; exit 1; }
sha=$(git rev-parse --short HEAD)
[ -z "$(git status --porcelain)" ] || echo "note: uncommitted changes are not deployed; building $sha" >&2

echo "== building $sha for linux/amd64"
git archive --format=tar HEAD \
  | docker buildx build --quiet --platform linux/amd64 -f deploy/Dockerfile -t "glassvault:$sha" --load - >/dev/null

# The server keeps the running image as glassvault:previous, then runs the new one as glassvault:local.
echo "== loading the image on the server"
docker save "glassvault:$sha" | gzip | remote "gunzip | sudo docker load >/dev/null"
remote "set -e
  if sudo docker image inspect glassvault:local >/dev/null 2>&1; then sudo docker tag glassvault:local glassvault:previous; fi
  sudo docker tag glassvault:$sha glassvault:local && sudo docker rmi glassvault:$sha >/dev/null" </dev/null
git show HEAD:deploy/docker-compose.yml | remote "sudo tee $root/docker-compose.yml >/dev/null"
docker rmi "glassvault:$sha" >/dev/null 2>&1 || true  # this machine needs no copy

echo "== starting"
remote "set -e
  $compose up -d --remove-orphans
  state=starting
  for _ in \$(seq 30); do
    state=\$(sudo docker inspect --format '{{.State.Health.Status}}' $container)
    [ \"\$state\" = healthy ] && break
    sleep 2
  done
  if [ \"\$state\" != healthy ]; then
    sudo docker logs --tail 40 $container
    if sudo docker image inspect glassvault:previous >/dev/null 2>&1; then
      sudo docker tag glassvault:previous glassvault:local && $compose up -d
      echo 'not healthy: the previous image is running again' >&2
    fi
    exit 1
  fi
  sudo docker exec $container python -c \"import json, os
for k in ('GV_DRAFTER_EXTRA_BODY', 'GV_VALIDATOR_EXTRA_BODY'):
    json.loads(os.environ.get(k) or '{}')\"" </dev/null

echo "== checking https://$host"
code=000
for _ in $(seq 60); do
  code=$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' "https://$host/healthz" || true)
  [ "$code" = 200 ] && break
  sleep 3  # the first visit waits for the certificate
done
if [ "$code" != 200 ]; then
  echo "https://$host/healthz answered $code. Is FirstRoll released with the Glass Vault site block, and" >&2
  echo "has its certificate been issued? ssh $dest 'sudo docker logs firstroll-caddy-1 2>&1 | grep -i $host | tail'" >&2
  exit 1
fi
page=$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' "https://$host/")
[ "$page" = 200 ] || { echo "https://$host/ answered $page, not 200" >&2; exit 1; }
echo "Deployed $sha: https://$host (open, no sign-in)"

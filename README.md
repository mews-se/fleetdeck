# fleetdeck

[![CI](https://github.com/mews-se/fleetdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/mews-se/fleetdeck/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python_3.13-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A web console for a two-site homelab. One screen for the hosts, the guests on
both Proxmox nodes, the containers, the network and the upstream threads you
are waiting on, plus an allowlisted catalog of actions that run over ssh or
the Proxmox API and are logged.

It does not replace the tools it reads from. Beszel, Dockhand, Uptime Kuma,
AdGuard Home, speedtest-tracker and GitHub keep their own interfaces and get
linked; fleetdeck is the page you open first.

## What it shows

- **Overview**: what needs attention, the NAS window, the last speed test,
  what moved upstream in the last day, quick actions and the state of every
  collector.
- **Hosts**: every Beszel agent with CPU, memory, disk, temperature and uptime,
  joined with the host list from the config so a test VM that is off by rule
  shows grey, not red.
- **Guests**: both Proxmox nodes with their guests and storages. Guests listed
  as free start and stop from here; everything else links to PDM.
- **Containers**: every Dockhand environment with image, state and pending
  image updates.
- **Network**: Uptime Kuma monitors, AdGuard Home statistics with the last
  24 hours as a curve, and seven days of speed tests per site.
- **Upstream**: the issues and pull requests you are waiting on, and the
  releases you run against the latest tag.
- **Actions**: the catalog, live output over server-sent events, and the run
  history.

## How it works

One Python process. A scheduler runs one asyncio task per source with its own
interval and writes the latest state and the numeric series to SQLite in WAL
mode. FastAPI serves the pages, one JSON endpoint per page that the page
refreshes from, and two event streams. Actions come only from the catalog:
an argv list or a PVE power operation, never a shell string from the browser.

There is no login. The console is meant for a LAN and a tailnet, never a
public reverse proxy. Every POST still has to come from the console's own
origin, and actions marked `confirm` need a one-time token issued with the
page.

## Running it

The compose file expects a folder with the layout below and the secrets in a
separate directory that is mounted read-only. One file per secret, one line
each; the config refers to them by file name only.

```
docker-fleetdeck/
  docker-compose.yml
  config/
    fleetdeck.yml      hosts, sites, sources, links
    catalog.yml        the actions
    known_hosts        pinned host keys for every ssh target
  keys/
    fleetdeck_ed25519  the console's own key pair
    fleetdeck_ed25519.pub
  data/                created on first start
```

```bash
mkdir -p docker-fleetdeck/{config,keys,data} && cd docker-fleetdeck
curl -O https://raw.githubusercontent.com/mews-se/fleetdeck/main/docker-compose.yml
curl -o config/fleetdeck.yml https://raw.githubusercontent.com/mews-se/fleetdeck/main/config/fleetdeck.example.yml
curl -o config/catalog.yml https://raw.githubusercontent.com/mews-se/fleetdeck/main/config/catalog.example.yml
ssh-keygen -t ed25519 -f keys/fleetdeck_ed25519 -C fleetdeck -N ""
ssh-keyscan -H 10.0.0.6 10.0.0.99 >> config/known_hosts
docker compose up -d
```

Edit the two config files for your fleet, point the secrets mount in the
compose file at your secrets directory, and authorise the public key on each
ssh target with a line restricted to the console's address:

```
from="10.0.0.6",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... fleetdeck
```

The container runs as uid 1000, so the bind-mounted directories must be
writable by that user. The image tag in the compose file is pinned on
purpose: bump it yourself.

## Configuration

`config/fleetdeck.yml` lists the sites, the hosts and the sources. A host is
the join key: its `beszel` name, `dockhand_env` and `guest` fields tie the
sources together, and its `rule` decides what the catalog may run there.

| rule | meaning |
|---|---|
| `test` | anything in the catalog may target it, including `free` actions |
| `prod` | `read` and `confirm` actions only; PVE power operations on guests listed in `free_guests` are still free |
| `readonly` | `read` actions only |

`config/catalog.yml` is the allowlist. Every entry is checked when the app
starts and a wrong one stops it: policy against the host's rule, PVE
operations against `free_guests`, parameters against their patterns, and a
short list of commands that are never allowed. Runs are serialised per target
and kept in `data/runs/`.

Both files have a complete example next to them in `config/`.

## Secrets

| file | content | used by |
|---|---|---|
| `pve-home.token`, `pve-brk.token` | `user@realm!tokenid=uuid` | Proxmox API |
| `beszel.auth` | `email:password` of a read-only hub user | Beszel |
| `dockhand.token` | API token | Dockhand |
| `kuma.key` | API key | Uptime Kuma `/metrics` |
| `adguard.auth` | `user:password` | AdGuard Home |
| `speedtest-home.token`, `speedtest-brk.token` | API token with Read Results | speedtest-tracker |
| `github.token` | fine-grained token, public repositories read-only | GitHub |

A source whose secret file is missing is left out and listed as not
configured; everything else keeps running.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio ruff
.venv/bin/ruff check . && .venv/bin/pytest -q
FLEETDECK_CONFIG=config/fleetdeck.example.yml FLEETDECK_CATALOG=config/catalog.example.yml \
  FLEETDECK_DB=data/fleetdeck.db FLEETDECK_RUNS=data/runs FLEETDECK_SECRETS=/nonexistent \
  .venv/bin/python -m app
```

The tests run every collector against recorded responses in `tests/fixtures/`;
nothing in the test suite talks to a real host.

## License

MIT. Vendored: uPlot (MIT) and IBM Plex (SIL OFL 1.1), licence files next to
them under `static/`.

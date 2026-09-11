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
AdGuard Home, speedtest-tracker, pfSense and GitHub keep their own interfaces
and get linked; fleetdeck is the page you open first.

## What it shows

- **Overview**: what needs attention, the NAS window, the last speed test,
  what moved upstream in the last day, quick actions and the state of every
  collector. An attention item can be marked read (it stays but stops
  counting) or resolved (hidden until it clears); either mark comes off by
  itself if the item gets worse. The rules: a host down in Beszel for five
  minutes (a host on the NAS schedule only inside its window, one marked
  off by default never), a disk past 85 or 95 percent on a host or a PVE
  storage, CPU or memory averaging 90 percent over five minutes, a
  temperature reading of 60 or 70 degrees, a 15 minute load at or above the
  thread count, 100 MB/s of traffic for five minutes, a failed systemd
  service, an unhealthy container, an Uptime Kuma monitor down, image
  updates waiting in Dockhand, a speed test instance failing more than 40
  percent of the day, a source failing for 45 minutes, an upstream thread
  that moved, and the pfSense rules below.
- **Hosts**: every Beszel agent with CPU, memory, disk, temperature and uptime,
  joined with the host list from the config so a test VM that is off by rule
  shows grey, not red, plus the MAC and the kind of DHCP entry pfSense has for
  its address. A pfSense box counts as up on its own answers, with its
  version, uptime, temperature, load and memory in the row. Below the table,
  one list per site of the DHCP mappings and leases pfSense knows that are
  not hosts in the config, quiet ranges left out. Each host has its own page with 24 hours of curves, the guest
  it runs as and its config read from PVE (with the lease behind every NIC),
  its containers, the Uptime Kuma monitors that point at it, its share of the
  day's DNS queries, and the catalog entries and runs that concern it.
- **Guests**: both Proxmox nodes with their guests and storages. Guests listed
  as free start and stop from here; everything else links to PDM.
- **Containers**: every Dockhand environment with image, state and pending
  image updates, and a button per row for the container operations the
  catalog allows there.
- **Network**: Uptime Kuma monitors, AdGuard Home statistics with the last
  24 hours as a curve, seven days of speed tests per site, and per pfSense
  box its state table, load, temperature, WAN throughput over the day, the
  Tailscale peers, and every DHCP mapping, lease and ARP entry with the
  fleetdeck host it belongs to.
- **Upstream**: the issues and pull requests you are waiting on, and the
  releases you run against the latest tag. The running version comes from
  the image's version label, the app's own API or a versioned image tag.
- **Actions**: the catalog, live output over server-sent events, and the run
  history.

## How it works

One Python process. A scheduler runs one asyncio task per source with its own
interval and writes the latest state and the numeric series to SQLite in WAL
mode. FastAPI serves the pages, one JSON endpoint per page that the page
refreshes from, and two event streams. Actions come only from the catalog:
an argv list over ssh, a PVE power operation or a container operation through
Dockhand's API, never a shell string from the browser.

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

| kind | run | what happens |
|---|---|---|
| `ssh` | an argv list, `{name}` placeholders filled from `params` | the command runs on the target with the console's key |
| `pve` | `{vmid, type, op}` with op `start`, `shutdown`, `stop` or `reboot` | a power operation through the PVE API, only for guests in `free_guests` |
| `dockhand` | `{op, container}` with op `start`, `stop`, `restart` or `update` | the operation through Dockhand's API in the target's environment |

An entry names one `target` or a list of `targets`; with a list the Actions
page shows a host picker and every host is checked against the policy. A
`dockhand` entry whose container is a parameter appears as a button on every
container row of the environments it targets. An `ssh` entry is killed after
15 minutes unless it sets its own `timeout` in seconds.

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

## pfSense

pfSense is read over ssh with the console key, but the key never gets a
shell there: the authorized key line carries `command=`, so every login runs
`contrib/pfsense/fleetdeck-read.sh`, which answers three read-only
subcommands (`dhcp`, `status`, `tailscale`) and refuses anything else. Put
the script at `/root/fleetdeck-read.sh` on each box (`chmod 755`) and add the
line from `contrib/pfsense/authorized_keys.example` under System → User
Manager → the ssh user → Authorized SSH Keys, with your own `from=` address
and public key. Then list the boxes under `sources.pfsense` with the host
that carries their ssh address:

```yaml
sources:
  pfsense:
    home: {host: pfsense-home, timezone: Europe/Stockholm,
           bonds: [["00:11:32:aa:bb:01", "00:11:32:aa:bb:02"]]}
    brk:  {host: pfsense-brk, timezone: Europe/Stockholm,
           quiet: [10.0.1.40-10.0.1.50, 10.0.1.60-10.0.1.75]}
```

`timezone` is the box's own, used to read lease times. `quiet` lists
addresses, ranges or networks that stay in the device table but never raise
attention, for the access points and cameras nobody manages from here.
`bonds` lists groups of MAC addresses that belong to one device: a NIC bond
answers ARP from whichever port is active, so a static mapping on one of
them is not a mismatch when the other answers. Quote the addresses, YAML
reads an all-digit one as a number.
Everything is read every five minutes (the lease read is one PHP start,
about a third of a second on a C3000 Atom); a failing box is retried with a
growing pause, up to an hour, so a wrong key line cannot trip sshguard on
the firewall.

The attention list gets four rules from it: a host in the config whose
address has no static mapping, a static mapping whose address answers from
another MAC, a running PVE guest whose NIC has neither mapping nor lease,
and a resolver that restarted in the last hour.

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

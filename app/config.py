"""Configuration file loading with validation.

Hosts are the join key between the sources: a Beszel system, a Dockhand
environment and a PVE guest all resolve to one host id. Secrets are never in
the file, only the name of a file in the secrets directory.
"""

import ipaddress
import os
import re
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RULES = ("prod", "test", "readonly")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SSH_RE = re.compile(r"^[a-z_][a-z0-9_-]*@[A-Za-z0-9.\-]+$")
CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    pass


@dataclass
class Site:
    id: str
    name: str


@dataclass
class Guest:
    pve: str
    vmid: int


@dataclass
class Host:
    id: str
    site: str
    ip: str
    rule: str
    ssh: str | None = None
    ssh_port: int = 22
    beszel: str | None = None
    dockhand_env: int | None = None
    pve: str | None = None
    guest: Guest | None = None
    off_by_default: bool = False
    window: str | None = None
    role: str = ""


@dataclass
class Pve:
    id: str
    url: str
    node: str
    secret: str
    free_guests: list[int] = field(default_factory=list)
    verify_tls: bool = False


@dataclass
class Endpoint:
    id: str
    url: str
    secret: str | None = None
    site: str | None = None
    timezone: str = "UTC"


@dataclass
class Pfsense:
    id: str
    host: str
    site: str
    timezone: str = "UTC"
    # address ranges that never raise attention, as (first, last) integers
    quiet: list[tuple[int, int]] = field(default_factory=list)
    # groups of MACs that belong to one device, for NIC bonds that answer
    # ARP from either port
    bonds: list[frozenset[str]] = field(default_factory=list)

    def is_quiet(self, ip: str) -> bool:
        try:
            n = int(ipaddress.ip_address(ip))
        except ValueError:
            return False
        return any(lo <= n <= hi for lo, hi in self.quiet)

    def same_device(self, mac_a: str | None, mac_b: str | None) -> bool:
        if not mac_a or not mac_b:
            return False
        if mac_a == mac_b:
            return True
        return any(mac_a in group and mac_b in group for group in self.bonds)


@dataclass
class WatchThread:
    repo: str
    numbers: list[int]


@dataclass
class WatchRelease:
    repo: str
    running_from: dict


@dataclass
class Github:
    secret: str | None = None
    threads: list[WatchThread] = field(default_factory=list)
    releases: list[WatchRelease] = field(default_factory=list)


@dataclass
class Window:
    start: str
    end: str


@dataclass
class Config:
    sites: dict[str, Site]
    hosts: dict[str, Host]
    pve: dict[str, Pve]
    sources: dict[str, Endpoint]
    speedtests: dict[str, Endpoint]
    pfsense: dict[str, Pfsense]
    github: Github
    links: dict[str, str]
    nas_window: Window | None
    title: str = "fleetdeck"

    def host_by_beszel(self, name: str) -> Host | None:
        return next((h for h in self.hosts.values() if h.beszel == name), None)

    def host_by_guest(self, pve: str, vmid: int) -> Host | None:
        return next(
            (h for h in self.hosts.values()
             if h.guest and h.guest.pve == pve and h.guest.vmid == vmid),
            None,
        )

    def host_by_env(self, env: int) -> Host | None:
        return next((h for h in self.hosts.values() if h.dockhand_env == env), None)

    def pve_host(self, pve_id: str) -> Host | None:
        return next((h for h in self.hosts.values() if h.pve == pve_id), None)

    def pfsense_for_site(self, site: str) -> Pfsense | None:
        return next((b for b in self.pfsense.values() if b.site == site), None)


class Secrets:
    """One file per secret, one line each, read on demand."""

    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)

    def get(self, name: str | None) -> str | None:
        if not name:
            return None
        path = self.dir / name
        try:
            value = path.read_text().strip()
        except OSError:
            return None
        return value or None

    def has(self, name: str | None) -> bool:
        return self.get(name) is not None


def _mapping(value, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping")
    return value


def _str(d: dict, key: str, where: str, required=True, default=None) -> str | None:
    value = d.get(key, default)
    if value is None:
        if required:
            raise ConfigError(f"{where}: {key} is required")
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {key} must be a non-empty string")
    return value


def _int(d: dict, key: str, where: str, required=True, default=None) -> int | None:
    value = d.get(key, default)
    if value is None:
        if required:
            raise ConfigError(f"{where}: {key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: {key} must be an integer")
    return value


def _bool(d: dict, key: str, where: str, default=False) -> bool:
    value = d.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false")
    return value


def _id(value: str, where: str) -> str:
    if not ID_RE.match(value):
        raise ConfigError(f"{where}: '{value}' is not a valid id (lowercase, digits, dashes)")
    return value


def _url(d: dict, where: str) -> str:
    url = _str(d, "url", where)
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}: url must start with http:// or https://")
    return url.rstrip("/")


def _timezone(d: dict, where: str) -> str:
    tz = _str(d, "timezone", where, default="UTC")
    try:
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{where}: unknown timezone '{tz}'") from None
    return tz


def _endpoint(id_: str, d: dict, where: str, site_required=False) -> Endpoint:
    d = _mapping(d, where)
    return Endpoint(
        id=id_,
        url=_url(d, where),
        secret=_str(d, "secret", where, required=False),
        site=_str(d, "site", where, required=site_required),
        timezone=_timezone(d, where),
    )


def _quiet(values, where: str) -> list[tuple[int, int]]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ConfigError(f"{where}: quiet must be a list")
    out = []
    for v in values:
        try:
            if not isinstance(v, str):
                raise ValueError
            if "-" in v:
                lo, hi = (ipaddress.ip_address(x.strip()) for x in v.split("-", 1))
            elif "/" in v:
                net = ipaddress.ip_network(v, strict=False)
                lo, hi = net[0], net[-1]
            else:
                lo = hi = ipaddress.ip_address(v)
        except ValueError:
            raise ConfigError(
                f"{where}: quiet entry {v!r} is not an address, a range or a network"
            ) from None
        if int(lo) > int(hi):
            raise ConfigError(f"{where}: quiet range '{v}' ends before it starts")
        out.append((int(lo), int(hi)))
    return out


_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _bonds(values, where: str) -> list[frozenset[str]]:
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(g, list) for g in values):
        raise ConfigError(f"{where}: bonds must be a list of MAC address lists")
    out = []
    for group in values:
        macs = set()
        for v in group:
            # an all-digit MAC reads as a base-60 integer in YAML unless quoted
            if not isinstance(v, str) or not _MAC.match(v.lower()):
                raise ConfigError(f"{where}: bonds entry {v!r} is not a MAC address"
                                  " (quote it in YAML)")
            macs.add(v.lower())
        if len(macs) < 2:
            raise ConfigError(f"{where}: a bond needs at least two different MAC addresses")
        out.append(frozenset(macs))
    return out


def _pfsense(id_: str, d: dict, where: str, hosts: dict[str, Host]) -> Pfsense:
    d = _mapping(d, where)
    host = _str(d, "host", where)
    if host not in hosts:
        raise ConfigError(f"{where}: unknown host '{host}'")
    if not hosts[host].ssh:
        raise ConfigError(f"{where}: host '{host}' has no ssh address")
    return Pfsense(
        id=id_,
        host=host,
        site=hosts[host].site,
        timezone=_timezone(d, where),
        quiet=_quiet(d.get("quiet"), where),
        bonds=_bonds(d.get("bonds"), where),
    )


def parse(data: dict) -> Config:
    data = _mapping(data, "config")

    sites = {}
    for sid, s in _mapping(data.get("sites"), "sites").items():
        _id(sid, "sites")
        s = _mapping(s, f"sites.{sid}")
        sites[sid] = Site(id=sid, name=_str(s, "name", f"sites.{sid}", default=sid))
    if not sites:
        raise ConfigError("sites: at least one site is required")

    pve = {}
    for pid, p in _mapping(data.get("pve"), "pve").items():
        where = f"pve.{pid}"
        _id(pid, "pve")
        p = _mapping(p, where)
        guests = p.get("free_guests", [])
        if not isinstance(guests, list) or any(
            isinstance(g, bool) or not isinstance(g, int) for g in guests
        ):
            raise ConfigError(f"{where}: free_guests must be a list of vmids")
        pve[pid] = Pve(
            id=pid,
            url=_url(p, where),
            node=_str(p, "node", where),
            secret=_str(p, "secret", where),
            free_guests=list(guests),
            verify_tls=_bool(p, "verify_tls", where),
        )

    hosts = {}
    for hid, h in _mapping(data.get("hosts"), "hosts").items():
        where = f"hosts.{hid}"
        _id(hid, "hosts")
        h = _mapping(h, where)
        site = _str(h, "site", where)
        if site not in sites:
            raise ConfigError(f"{where}: unknown site '{site}'")
        rule = _str(h, "rule", where)
        if rule not in RULES:
            raise ConfigError(f"{where}: rule must be one of {', '.join(RULES)}")
        ssh = _str(h, "ssh", where, required=False)
        if ssh and not SSH_RE.match(ssh):
            raise ConfigError(f"{where}: ssh must look like user@host")
        pve_id = _str(h, "pve", where, required=False)
        if pve_id and pve_id not in pve:
            raise ConfigError(f"{where}: unknown pve '{pve_id}'")
        guest = None
        if "guest" in h:
            g = _mapping(h["guest"], f"{where}.guest")
            gp = _str(g, "pve", f"{where}.guest")
            if gp not in pve:
                raise ConfigError(f"{where}.guest: unknown pve '{gp}'")
            guest = Guest(pve=gp, vmid=_int(g, "vmid", f"{where}.guest"))
        window = _str(h, "window", where, required=False)
        if window and window != "nas":
            raise ConfigError(f"{where}: window must be 'nas'")
        hosts[hid] = Host(
            id=hid,
            site=site,
            ip=_str(h, "ip", where),
            rule=rule,
            ssh=ssh,
            ssh_port=_int(h, "ssh_port", where, default=22),
            beszel=_str(h, "beszel", where, required=False),
            dockhand_env=_int(h, "dockhand_env", where, required=False),
            pve=pve_id,
            guest=guest,
            off_by_default=_bool(h, "off_by_default", where),
            window=window,
            role=_str(h, "role", where, required=False) or "",
        )
    if not hosts:
        raise ConfigError("hosts: at least one host is required")
    for pid in pve:
        if not any(h.pve == pid for h in hosts.values()):
            raise ConfigError(f"pve.{pid}: no host carries pve: {pid}")

    sources = {}
    speedtests = {}
    pfsense = {}
    for name, s in _mapping(data.get("sources"), "sources").items():
        where = f"sources.{name}"
        if name == "speedtest":
            for iid, inst in _mapping(s, where).items():
                _id(iid, where)
                ep = _endpoint(iid, inst, f"{where}.{iid}", site_required=True)
                if ep.site not in sites:
                    raise ConfigError(f"{where}.{iid}: unknown site '{ep.site}'")
                speedtests[iid] = ep
        elif name == "pfsense":
            for bid, box in _mapping(s, where).items():
                _id(bid, where)
                pfsense[bid] = _pfsense(bid, box, f"{where}.{bid}", hosts)
        elif name in ("beszel", "dockhand", "kuma", "adguard"):
            sources[name] = _endpoint(name, s, where)
        else:
            raise ConfigError(f"{where}: unknown source")

    gh = _mapping(data.get("github"), "github")
    threads = []
    for i, t in enumerate(gh.get("watch_threads") or []):
        where = f"github.watch_threads[{i}]"
        t = _mapping(t, where)
        repo = _str(t, "repo", where)
        if repo.count("/") != 1:
            raise ConfigError(f"{where}: repo must be owner/name")
        numbers = t.get("numbers")
        if not isinstance(numbers, list) or not numbers or any(
            isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in numbers
        ):
            raise ConfigError(f"{where}: numbers must be a list of issue or PR numbers")
        threads.append(WatchThread(repo=repo, numbers=list(numbers)))
    releases = []
    for i, r in enumerate(gh.get("watch_releases") or []):
        where = f"github.watch_releases[{i}]"
        r = _mapping(r, where)
        repo = _str(r, "repo", where)
        if repo.count("/") != 1:
            raise ConfigError(f"{where}: repo must be owner/name")
        running = _mapping(r.get("running_from"), f"{where}.running_from")
        if len(running) > 1:
            raise ConfigError(f"{where}.running_from: one source only")
        for kind, spec in running.items():
            if kind == "dockhand":
                spec = _mapping(spec, f"{where}.running_from.dockhand")
                _int(spec, "env", f"{where}.running_from.dockhand")
                _str(spec, "container", f"{where}.running_from.dockhand")
            elif kind == "npm":
                spec = _mapping(spec, f"{where}.running_from.npm")
                running[kind] = {"url": _url(spec, f"{where}.running_from.npm")}
            elif kind == "pve":
                if spec not in pve:
                    raise ConfigError(f"{where}.running_from.pve: unknown pve '{spec}'")
            elif kind not in ("kuma", "adguard", "dockhand_version"):
                raise ConfigError(f"{where}.running_from: unknown kind '{kind}'")
        releases.append(WatchRelease(repo=repo, running_from=dict(running)))
    github = Github(
        secret=_str(gh, "secret", "github", required=False),
        threads=threads,
        releases=releases,
    )

    links = {}
    for k, v in _mapping(data.get("links"), "links").items():
        if not isinstance(v, str) or not v.startswith(("http://", "https://")):
            raise ConfigError(f"links.{k}: must be an http(s) URL")
        links[k] = v

    nas_window = None
    if "nas_window" in data:
        w = _mapping(data["nas_window"], "nas_window")
        start = _str(w, "start", "nas_window")
        end = _str(w, "end", "nas_window")
        if not CLOCK_RE.match(start) or not CLOCK_RE.match(end):
            raise ConfigError("nas_window: start and end must be HH:MM")
        nas_window = Window(start=start, end=end)

    return Config(
        sites=sites,
        hosts=hosts,
        pve=pve,
        sources=sources,
        speedtests=speedtests,
        pfsense=pfsense,
        github=github,
        links=links,
        nas_window=nas_window,
        title=_str(data, "title", "config", default="fleetdeck"),
    )


def load(path: str | os.PathLike) -> Config:
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(data)


def missing_secrets(config: Config, secrets: Secrets) -> list[str]:
    """Names referenced by the config that have no file yet."""
    names = [p.secret for p in config.pve.values()]
    names += [s.secret for s in config.sources.values()]
    names += [s.secret for s in config.speedtests.values()]
    names.append(config.github.secret)
    return sorted({n for n in names if n and not secrets.has(n)})

"""pfSense over ssh, behind a forced command on the firewall side.

The key line on each box runs contrib/pfsense/fleetdeck-read.sh for every
login, so the three subcommands below are all fleetdeck can ask for: the
DHCP leases with the ARP table, a few counters, and Tailscale's peer list.
"""

import ipaddress
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Pfsense
from app.sources import Context, Source, SourceError
from app.ssh import capture

STATUS_INTERVAL = 300
DHCP_INTERVAL = 300
ONLINE = "active/online"
SECTION_RE = re.compile(r"^== (\S+)(?: (\S+))?$")
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def quiet(box: Pfsense, ip: str) -> bool:
    try:
        n = int(ipaddress.ip_address(ip))
    except ValueError:
        return False
    return any(lo <= n <= hi for lo, hi in box.quiet)


def sections(text: str) -> dict[str, tuple[str | None, list[str]]]:
    """The '== name arg' blocks the status subcommand prints."""
    out: dict[str, tuple[str | None, list[str]]] = {}
    name, arg, lines = None, None, []
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            if name:
                out[name] = (arg, lines)
            name, arg, lines = m.group(1), m.group(2), []
        elif name:
            lines.append(line)
    if name:
        out[name] = (arg, lines)
    return out


def _num(text: str | None) -> float | None:
    m = NUM_RE.search(text or "")
    return float(m.group()) if m else None


def _ints(lines: list[str]) -> list[int]:
    return [int(x) for x in lines if x.strip().lstrip("-").isdigit()]


def parse_status(box_id: str, text: str, previous: dict | None, previous_ts: int | None,
                 ts: int) -> tuple[dict, list]:
    s = sections(text)

    def lines(name):
        return s.get(name, (None, []))[1]

    def first(name):
        return (lines(name) or [""])[0].strip()

    boot = re.search(r"sec = (\d+)", first("boottime"))
    mem = _ints(lines("mem"))
    mem_pct = None
    if len(mem) == 4 and mem[0]:
        physmem, pagesize, free, inactive = mem
        mem_pct = round(100 * (1 - (free + inactive) * pagesize / physmem), 1)
    states = limit = None
    for line in lines("states"):
        if "current entries" in line:
            states = int(_num(line.split("entries", 1)[1]) or 0)
        elif line.startswith("states"):
            limit = int(_num(line.split("limit", 1)[1]) or 0)
    wan_if, wan_lines = s.get("wan", (None, []))
    wan_in = wan_out = None
    if wan_lines:
        header = wan_lines[0].split()
        for line in wan_lines[1:]:
            cols = line.split()
            if len(cols) == len(header) and cols[2].startswith("<Link"):
                wan_in = int(cols[header.index("Ibytes")])
                wan_out = int(cols[header.index("Obytes")])
                break
    unbound = _ints(lines("unbound"))
    box = {
        "version": first("version") or None,
        "uptime": ts - int(boot.group(1)) if boot else None,
        "temp": _num(first("temp")),
        "load": [float(x) for x in re.findall(r"\d+\.\d+", first("load"))],
        "mem_pct": mem_pct,
        "states": states,
        "state_limit": limit,
        "wan_if": wan_if,
        "wan_in_bytes": wan_in,
        "wan_out_bytes": wan_out,
        "unbound_uptime": unbound[0] if unbound else None,
    }
    samples = [
        (f"pfsense.{box_id}.{k}", ts, float(box[k]))
        for k in ("states", "temp", "mem_pct")
        if isinstance(box[k], int | float)
    ]
    if box["load"]:
        samples.append((f"pfsense.{box_id}.load", ts, box["load"][0]))
    prev = previous or {}
    dt = ts - previous_ts if previous_ts else 0
    if dt > 0:
        for key in ("wan_in", "wan_out"):
            cur, old = box[f"{key}_bytes"], prev.get(f"{key}_bytes")
            if cur is not None and old is not None and cur >= old:
                rate = round((cur - old) * 8 / dt / 1e6, 2)
                samples.append((f"pfsense.{box_id}.{key}", ts, rate))
        # etimes counts from the process start, so less than the gap since
        # the last tick means unbound came up again in between
        if box["unbound_uptime"] is not None and box["unbound_uptime"] < dt:
            samples.append((f"pfsense.{box_id}.unbound_restart", ts, 1.0))
    return box, samples


def _when(value, tz: ZoneInfo) -> int | None:
    try:
        when = datetime.strptime(value or "", "%Y/%m/%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None
    return int(when.timestamp())


def parse_dhcp(data: dict, box: Pfsense) -> dict[str, dict]:
    tz = ZoneInfo(box.timezone)
    snaps: dict[str, dict] = {}
    for row in data.get("lease") or []:
        ip = row.get("ip")
        # the leases file keeps every lease ever handed out; the status page
        # hides the expired ones too, and BRK has a hundred of them
        if not ip or row.get("act") == "expired":
            continue
        snaps[f"lease:{ip}"] = {
            "ip": ip,
            "mac": (row.get("mac") or "").lower() or None,
            "hostname": row.get("hostname") or None,
            "descr": row.get("descr") or None,
            "kind": "static" if row.get("type") == "static" else "dynamic",
            "act": row.get("act") or None,
            "online": row.get("online") == ONLINE,
            "starts": _when(row.get("starts"), tz),
            "ends": _when(row.get("ends"), tz),
            "if": row.get("if") or None,
            "quiet": quiet(box, ip),
        }
    for row in data.get("arp") or []:
        ip = row.get("ip-address")
        if not ip or row.get("incomplete"):
            continue
        snaps[f"arp:{ip}"] = {
            "ip": ip,
            "mac": (row.get("mac-address") or "").lower() or None,
            "if": row.get("interface"),
            "permanent": bool(row.get("permanent")),
            "expires": row.get("expires"),
            "quiet": quiet(box, ip),
        }
    return snaps


def parse_tailscale(data: dict) -> tuple[dict[str, dict], dict]:
    # the MagicDNS label is the name the tailnet knows; HostName is whatever
    # the machine calls itself ("pfsense" on both boxes, "localhost" on iOS)
    def node(n: dict) -> dict:
        dns = n.get("DNSName") or ""
        return {
            "name": dns.split(".")[0] or n.get("HostName"),
            "hostname": n.get("HostName"),
            "dns": dns or None,
            "ip": (n.get("TailscaleIPs") or [None])[0],
            "os": n.get("OS"),
            "online": bool(n.get("Online")),
            "last_seen": n.get("LastSeen"),
            "rx": n.get("RxBytes"),
            "tx": n.get("TxBytes"),
            "direct": bool(n.get("CurAddr")),
            "relay": n.get("Relay") or None,
            "exit_node": bool(n.get("ExitNode")),
            "exit_option": bool(n.get("ExitNodeOption")),
            "routes": n.get("PrimaryRoutes") or [],
        }

    self_ = node(data.get("Self") or {})
    self_["version"] = data.get("Version")
    self_["state"] = data.get("BackendState")
    peers = {}
    for p in (data.get("Peer") or {}).values():
        n = node(p)
        peers[(n["name"] or n["ip"] or "?").lower()] = n
    return peers, self_


class PfsenseSource(Source):
    backoff = True

    def __init__(self, ctx: Context, box: Pfsense):
        super().__init__(ctx)
        self.box = box
        self.host = ctx.config.hosts[box.host]

    async def read(self, sub: str) -> str:
        code, out, err = await capture(self.ctx.ssh_argv(self.host, [sub]))
        if code != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            last = f": {detail[-1][:160]}" if detail else ""
            raise SourceError(f"{sub}: ssh exit {code}{last}")
        return out.decode("utf-8", "replace")


class PfsenseStatus(PfsenseSource):
    interval = STATUS_INTERVAL

    def __init__(self, ctx: Context, box: Pfsense):
        super().__init__(ctx, box)
        self.name = f"pfsense.{box.id}"

    async def collect(self):
        text = await self.read("status")
        ts = self.ctx.now()
        rows = self.ctx.db.get_snapshots(self.name, "box")
        prev_ts, prev = (rows[0][1], rows[0][2]) if rows else (None, None)
        box, samples = parse_status(self.box.id, text, prev, prev_ts, ts)
        snaps = {"box": box}
        error = None
        try:
            peers, box["tailscale"] = parse_tailscale(json.loads(await self.read("tailscale")))
            snaps.update({f"peer:{k}": v for k, v in peers.items()})
        except (SourceError, json.JSONDecodeError) as e:
            error = f"tailscale: {e}"
        self.ctx.db.put_snapshots(self.name, snaps, ts=ts)
        self.ctx.db.add_samples(samples)
        if error:
            raise SourceError(error)


class PfsenseDhcp(PfsenseSource):
    interval = DHCP_INTERVAL
    timeout = 120

    def __init__(self, ctx: Context, box: Pfsense):
        super().__init__(ctx, box)
        self.name = f"pfsense.{box.id}.dhcp"

    async def collect(self):
        text = await self.read("dhcp")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise SourceError(f"dhcp: not JSON ({e})") from None
        self.ctx.db.put_snapshots(self.name, parse_dhcp(data, self.box), ts=self.ctx.now())


def build(ctx: Context):
    sources: list[Source] = []
    missing: dict[str, str] = {}
    for box in ctx.config.pfsense.values():
        if ctx.ssh_argv is None:
            missing[f"pfsense.{box.id}"] = "no ssh key configured"
            continue
        sources += [PfsenseStatus(ctx, box), PfsenseDhcp(ctx, box)]
    return sources, missing

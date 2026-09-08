"""Proxmox VE, one API token per host. The same client serves the runner."""

import re

import httpx

from app.config import Pve
from app.sources import Context, Source, SourceError

NET_RE = re.compile(r"^net\d+$")
DISK_RE = re.compile(r"^(scsi|sata|virtio|ide|efidisk|tpmstate|rootfs|mp)\d*$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class PveApi:
    def __init__(self, ctx: Context, pve: Pve):
        self.pve = pve
        self.http = ctx.client(pve.verify_tls)
        self.base = f"{pve.url}/api2/json"
        self.headers = {"Authorization": f"PVEAPIToken={ctx.secrets.get(pve.secret)}"}

    async def get(self, path: str, **params):
        r = await self.http.get(self.base + path, params=params or None, headers=self.headers)
        r.raise_for_status()
        return r.json().get("data")

    async def post(self, path: str, data: dict | None = None):
        r = await self.http.post(self.base + path, data=data, headers=self.headers)
        r.raise_for_status()
        return r.json().get("data")


class PveResources(Source):
    interval = 60

    def __init__(self, ctx: Context, pve: Pve):
        super().__init__(ctx)
        self.name = f"pve.{pve.id}"
        self.pve = pve
        self.api = PveApi(ctx, pve)

    async def collect(self):
        resources = await self.api.get("/cluster/resources")
        status = await self.api.get(f"/nodes/{self.pve.node}/status")
        ts = self.ctx.now()
        snaps, samples = parse_resources(self.pve.id, resources or [], status or {}, ts)
        self.ctx.db.put_snapshots(self.name, snaps, ts=ts)
        self.ctx.db.add_samples(samples)


class PveVersion(Source):
    interval = 6 * 3600

    def __init__(self, ctx: Context, pve: Pve):
        super().__init__(ctx)
        self.name = f"pve.{pve.id}.version"
        self.api = PveApi(ctx, pve)

    async def collect(self):
        data = await self.api.get("/version") or {}
        self.ctx.db.put_snapshot(
            self.name,
            "version",
            {"version": data.get("version"), "release": data.get("release"),
             "repoid": data.get("repoid")},
        )


class PveGuestConfig(Source):
    interval = 6 * 3600
    timeout = 120

    def __init__(self, ctx: Context, pve: Pve):
        super().__init__(ctx)
        self.name = f"pve.{pve.id}.config"
        self.pve = pve
        self.api = PveApi(ctx, pve)

    async def collect(self):
        resources = await self.api.get("/cluster/resources", type="vm") or []
        snaps, errors = {}, []
        for res in resources:
            kind, vmid = res.get("type"), res.get("vmid")
            if kind not in ("qemu", "lxc") or vmid is None or res.get("node") != self.pve.node:
                continue
            try:
                raw = await self.api.get(f"/nodes/{self.pve.node}/{kind}/{vmid}/config")
            except httpx.HTTPError as e:
                errors.append(f"{vmid}: {e}")
                continue
            snaps[f"guest:{vmid}"] = parse_config(kind, vmid, raw or {})
        self.ctx.db.put_snapshots(self.name, snaps, prefix="guest:", ts=self.ctx.now())
        if errors:
            raise SourceError("; ".join(errors))


def _pct(used, total):
    if not total:
        return None
    return round(100.0 * float(used or 0) / float(total), 1)


def _options(value) -> tuple[str | None, dict[str, str]]:
    """A PVE property string: an optional bare first token, then key=value pairs."""
    head, opts = None, {}
    for i, part in enumerate(str(value).split(",")):
        key, sep, val = part.partition("=")
        if sep:
            opts[key.strip()] = val.strip()
        elif i == 0 and part.strip():
            head = part.strip()
    return head, opts


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_config(kind: str, vmid: int, raw: dict) -> dict:
    nics, disks = [], []
    for key, value in sorted(raw.items()):
        if NET_RE.match(key):
            _head, o = _options(value)
            mac = o.get("hwaddr")
            model = o.get("type")
            # qemu writes the address as the value of the model: virtio=BC:24:...
            for k, v in o.items():
                if MAC_RE.match(v) and k != "hwaddr":
                    model, mac = k, v
            nics.append({
                "name": key,
                "model": model,
                "mac": mac.lower() if mac else None,
                "bridge": o.get("bridge"),
                "ip": o.get("ip"),
                "vlan": _int(o.get("tag")),
            })
        elif DISK_RE.match(key):
            head, o = _options(value)
            if not head or head == "none" or o.get("media") == "cdrom":
                continue
            disks.append({
                "name": key,
                "volume": head,
                "storage": head.partition(":")[0] if ":" in head else None,
                "size": o.get("size"),
            })
    return {
        "vmid": vmid,
        "type": kind,
        "name": raw.get("name") or raw.get("hostname"),
        "cores": _int(raw.get("cores")),
        "sockets": _int(raw.get("sockets")),
        "memory": _int(raw.get("memory")),
        "swap": _int(raw.get("swap")),
        "onboot": bool(_int(raw.get("onboot"))),
        "startup": raw.get("startup"),
        "ostype": raw.get("ostype"),
        "agent": raw.get("agent"),
        "unprivileged": bool(_int(raw.get("unprivileged"))) if kind == "lxc" else None,
        "tags": raw.get("tags"),
        "nics": nics,
        "disks": disks,
    }


def parse_resources(pve_id: str, resources: list[dict], status: dict, ts: int):
    snaps = {}
    total = running = 0
    for res in resources:
        kind = res.get("type")
        if kind in ("qemu", "lxc"):
            vmid = res.get("vmid")
            total += 1
            if res.get("status") == "running":
                running += 1
            snaps[f"guest:{vmid}"] = {
                "vmid": vmid,
                "type": kind,
                "name": res.get("name"),
                "status": res.get("status"),
                "template": bool(res.get("template")),
                "cpu": res.get("cpu"),
                "maxcpu": res.get("maxcpu"),
                "mem": res.get("mem"),
                "maxmem": res.get("maxmem"),
                "disk": res.get("disk"),
                "maxdisk": res.get("maxdisk"),
                "uptime": res.get("uptime"),
                "tags": res.get("tags"),
                "node": res.get("node"),
            }
        elif kind == "storage":
            snaps[f"storage:{res.get('storage')}"] = {
                "storage": res.get("storage"),
                "plugin": res.get("plugintype"),
                "disk": res.get("disk"),
                "maxdisk": res.get("maxdisk"),
                "pct": _pct(res.get("disk"), res.get("maxdisk")),
                "status": res.get("status"),
                "content": res.get("content"),
                "shared": res.get("shared"),
                "node": res.get("node"),
            }
    mem = status.get("memory") or {}
    root = status.get("rootfs") or {}
    cpuinfo = status.get("cpuinfo") or {}
    load = []
    for x in status.get("loadavg") or []:
        try:
            load.append(float(x))
        except (TypeError, ValueError):
            pass
    cpu = round(float(status.get("cpu") or 0) * 100, 1)
    iowait = round(float(status.get("wait") or 0) * 100, 1)
    snaps["node"] = {
        "cpu": cpu,
        "iowait": iowait,
        "load": load,
        "mem_used": mem.get("used"),
        "mem_total": mem.get("total"),
        "mem_pct": _pct(mem.get("used"), mem.get("total")),
        "root_used": root.get("used"),
        "root_total": root.get("total"),
        "root_pct": _pct(root.get("used"), root.get("total")),
        "uptime": status.get("uptime"),
        "pveversion": status.get("pveversion"),
        "kversion": status.get("kversion"),
        "cpu_model": cpuinfo.get("model"),
        "cores": cpuinfo.get("cores"),
        "cpus": cpuinfo.get("cpus"),
        "guests": total,
        "running": running,
    }
    samples = [(f"pve.{pve_id}.cpu", ts, cpu), (f"pve.{pve_id}.iowait", ts, iowait)]
    if snaps["node"]["mem_pct"] is not None:
        samples.append((f"pve.{pve_id}.mem", ts, snaps["node"]["mem_pct"]))
    if load:
        samples.append((f"pve.{pve_id}.load", ts, load[0]))
    return snaps, samples


def build(ctx: Context):
    sources, missing = [], {}
    for pve in ctx.config.pve.values():
        if not ctx.secrets.has(pve.secret):
            missing[f"pve.{pve.id}"] = f"secret {pve.secret} is missing"
            continue
        sources += [PveResources(ctx, pve), PveVersion(ctx, pve), PveGuestConfig(ctx, pve)]
    return sources, missing

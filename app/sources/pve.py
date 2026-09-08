"""Proxmox VE, one API token per host. The same client serves the runner."""

from app.config import Pve
from app.sources import Context, Source


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


def _pct(used, total):
    if not total:
        return None
    return round(100.0 * float(used or 0) / float(total), 1)


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
        sources += [PveResources(ctx, pve), PveVersion(ctx, pve)]
    return sources, missing

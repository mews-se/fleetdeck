"""Beszel hub, read through its PocketBase API with a read-only user."""

from app.sources import Context, Source

SAMPLED = ("cpu", "mem", "disk", "temp", "bandwidth")
# systemd_services.state as the hub stores it
SERVICE_FAILED = 2


class BeszelSystems(Source):
    interval = 60

    def __init__(self, ctx: Context, endpoint):
        super().__init__(ctx)
        self.name = "beszel"
        self.url = endpoint.url
        self.secret = endpoint.secret
        self.token: str | None = None

    async def _auth(self):
        identity, _, password = (self.ctx.secrets.get(self.secret) or "").partition(":")
        r = await self.ctx.http.post(
            f"{self.url}/api/collections/users/auth-with-password",
            json={"identity": identity, "password": password},
        )
        r.raise_for_status()
        self.token = r.json()["token"]

    async def _records(self, collection: str, **params):
        return await self.ctx.http.get(
            f"{self.url}/api/collections/{collection}/records",
            params={"perPage": 500, **params},
            headers={"Authorization": self.token or ""},
        )

    async def collect(self):
        if not self.token:
            await self._auth()
        r = await self._records("systems", sort="name")
        if r.status_code in (401, 403):
            await self._auth()
            r = await self._records("systems", sort="name")
        r.raise_for_status()
        d = await self._records("system_details")
        d.raise_for_status()
        details = {x.get("system"): x for x in d.json().get("items", [])}
        items = r.json().get("items", [])
        failed: dict[str, list[str]] = {}
        # the names are only worth a request when some host reports a failure
        if any(((it.get("info") or {}).get("sv") or [0, 0])[1] for it in items):
            f = await self._records("systemd_services", filter=f"state={SERVICE_FAILED}")
            f.raise_for_status()
            for row in f.json().get("items", []):
                failed.setdefault(row.get("system"), []).append(row.get("name"))
        ts = self.ctx.now()
        previous = {k: v for k, _, v in self.ctx.db.get_snapshots("beszel", "system:")}
        snaps, samples = parse_systems(items, details, previous, ts, failed)
        self.ctx.db.put_snapshots("beszel", snaps, prefix="system:", ts=ts)
        self.ctx.db.add_samples(samples)


def parse_systems(items: list[dict], details: dict[str, dict], previous: dict[str, dict],
                  ts: int, failed: dict[str, list[str]] | None = None):
    snaps, samples = {}, []
    for it in items:
        info = it.get("info") or {}
        bb = info.get("bb")
        services = info.get("sv")
        det = details.get(it.get("id")) or {}
        name = it.get("name") or it.get("host")
        key = f"system:{name}"
        status = it.get("status") or "unknown"
        down_since = None
        if status != "up":
            down_since = (previous.get(key) or {}).get("down_since") or ts
        snap = {
            "name": name,
            "host": it.get("host"),
            "port": it.get("port"),
            "status": status,
            "down_since": down_since,
            "updated": it.get("updated"),
            "cpu": info.get("cpu"),
            "mem": info.get("mp"),
            "disk": info.get("dp"),
            "temp": info.get("dt"),
            "uptime": info.get("u"),
            "agent": info.get("v"),
            "load": info.get("la"),
            "extra_fs": info.get("efs"),
            "bandwidth": bb / 1e6 if isinstance(bb, int | float) else None,
            "services": services if isinstance(services, list) and len(services) == 2 else None,
            "failed_services": sorted((failed or {}).get(it.get("id")) or []),
            "hostname": det.get("hostname"),
            "os": (det.get("os_name") or "").strip() or None,
            "kernel": det.get("kernel") or None,
            "model": det.get("cpu"),
            "cores": det.get("cores"),
            "threads": det.get("threads"),
            "arch": det.get("arch"),
            "memory": det.get("memory"),
        }
        snaps[key] = snap
        if status != "up":
            continue
        for field in SAMPLED:
            v = snap[field]
            if isinstance(v, int | float) and not (field == "temp" and v == 0):
                samples.append((f"beszel.{field}.{name}", ts, float(v)))
    return snaps, samples


def build(ctx: Context):
    ep = ctx.config.sources.get("beszel")
    if ep is None:
        return [], {}
    if not ctx.secrets.has(ep.secret):
        return [], {"beszel": f"secret {ep.secret} is missing"}
    return [BeszelSystems(ctx, ep)], {}

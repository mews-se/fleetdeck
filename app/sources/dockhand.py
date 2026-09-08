"""Dockhand, the container source for every environment it manages."""

import httpx

from app.sources import Context, Source, SourceError


class DockhandApi:
    def __init__(self, ctx: Context, endpoint):
        self.http = ctx.http
        self.url = endpoint.url
        self.headers = {"Authorization": f"Bearer {ctx.secrets.get(endpoint.secret)}"}

    async def get(self, path: str, **params):
        r = await self.http.get(self.url + path, params=params or None, headers=self.headers)
        r.raise_for_status()
        return r.json()


def _envs(ctx: Context) -> list[tuple[int, str]]:
    return sorted(
        (h.dockhand_env, h.id) for h in ctx.config.hosts.values() if h.dockhand_env is not None
    )


class DockhandContainers(Source):
    interval = 60

    def __init__(self, ctx: Context, endpoint):
        super().__init__(ctx)
        self.name = "dockhand"
        self.api = DockhandApi(ctx, endpoint)

    async def collect(self):
        errors = []
        for env, host in _envs(self.ctx):
            try:
                items = await self.api.get("/api/containers", env=env, all="true")
            except httpx.HTTPError as e:
                errors.append(f"env {env}: {e}")
                continue
            snaps = parse_containers(env, host, items or [])
            self.ctx.db.put_snapshots(self.name, snaps, prefix=f"{env}:", ts=self.ctx.now())
        if errors:
            raise SourceError("; ".join(errors))


class DockhandUpdates(Source):
    interval = 900

    def __init__(self, ctx: Context, endpoint):
        super().__init__(ctx)
        self.name = "dockhand.updates"
        self.api = DockhandApi(ctx, endpoint)

    async def collect(self):
        errors = []
        for env, host in _envs(self.ctx):
            try:
                data = await self.api.get("/api/containers/pending-updates", env=env)
            except httpx.HTTPError as e:
                errors.append(f"env {env}: {e}")
                continue
            items = [
                {
                    "name": u.get("containerName"),
                    "image": u.get("currentImage"),
                    "newer": u.get("newerVersion"),
                    "checked": u.get("checkedAt"),
                }
                for u in (data or {}).get("pendingUpdates") or []
                if u.get("hasImageUpdate")
            ]
            self.ctx.db.put_snapshot(
                self.name, f"updates:{env}", {"env": env, "host": host, "items": items}
            )
        if errors:
            raise SourceError("; ".join(errors))


class DockhandSystem(Source):
    interval = 3600

    def __init__(self, ctx: Context, endpoint):
        super().__init__(ctx)
        self.name = "dockhand.system"
        self.api = DockhandApi(ctx, endpoint)

    async def _optional(self, path: str, **params):
        try:
            return await self.api.get(path, **params)
        except httpx.HTTPError:
            return None

    async def collect(self):
        errors = []
        for env, host in _envs(self.ctx):
            try:
                system = await self.api.get("/api/system", env=env)
            except httpx.HTTPError as e:
                errors.append(f"env {env}: {e}")
                continue
            disk = await self._optional("/api/system/disk", env=env) or {}
            vulns = await self._optional("/api/vulnerabilities/count", env=env) or {}
            self.ctx.db.put_snapshot(
                self.name, f"system:{env}", parse_system(env, host, system or {}, disk, vulns)
            )
        spec = await self._optional("/openapi.json")
        if spec:
            self.ctx.db.put_snapshot(
                self.name, "version", {"version": (spec.get("info") or {}).get("version")}
            )
        if errors:
            raise SourceError("; ".join(errors))


def parse_containers(env: int, host: str, items: list[dict]) -> dict[str, dict]:
    snaps = {}
    for it in items:
        name = (it.get("name") or "").lstrip("/")
        if not name:
            continue
        snaps[f"{env}:{name}"] = {
            "env": env,
            "host": host,
            "id": it.get("id"),
            "name": name,
            "image": it.get("image"),
            "state": it.get("state"),
            "status": it.get("status"),
        }
    return snaps


def parse_system(env: int, host: str, system: dict, disk: dict, vulns: dict) -> dict:
    docker = system.get("docker") or {}
    hostinfo = system.get("host") or {}
    usage = disk.get("diskUsage") or {}
    return {
        "env": env,
        "host": host,
        "docker": docker.get("version"),
        "host_name": hostinfo.get("name"),
        "cpus": hostinfo.get("cpus"),
        "memory": hostinfo.get("memory"),
        "stats": system.get("stats"),
        "layers_size": usage.get("LayersSize"),
        "vulns": {"total": vulns.get("total"), "summary": vulns.get("summary")}
        if vulns else None,
    }


def build(ctx: Context):
    ep = ctx.config.sources.get("dockhand")
    if ep is None:
        return [], {}
    if not ctx.secrets.has(ep.secret):
        return [], {"dockhand": f"secret {ep.secret} is missing"}
    return [DockhandContainers(ctx, ep), DockhandUpdates(ctx, ep), DockhandSystem(ctx, ep)], {}

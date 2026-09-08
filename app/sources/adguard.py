"""AdGuard Home statistics and version, basic auth with a dedicated user."""

from app.sources import Context, Source


def _top(entries) -> list[dict]:
    out = []
    for e in entries or []:
        if isinstance(e, dict):
            for k, v in e.items():
                out.append({"name": k, "count": v})
    return out


def parse_stats(stats: dict, status: dict) -> dict[str, dict]:
    avg = stats.get("avg_processing_time")
    return {
        "stats": {
            "queries": stats.get("num_dns_queries"),
            "blocked": stats.get("num_blocked_filtering"),
            "safebrowsing": stats.get("num_replaced_safebrowsing"),
            "avg_ms": round(float(avg) * 1000, 1) if isinstance(avg, int | float) else None,
            "time_units": stats.get("time_units"),
            "hourly_queries": stats.get("dns_queries"),
            "hourly_blocked": stats.get("blocked_filtering"),
            "top_queried": _top(stats.get("top_queried_domains")),
            "top_blocked": _top(stats.get("top_blocked_domains")),
            "top_clients": _top(stats.get("top_clients")),
        },
        "status": {
            "version": status.get("version"),
            "protection_enabled": status.get("protection_enabled"),
            "running": status.get("running"),
            "dns_addresses": status.get("dns_addresses"),
        },
    }


class AdguardStats(Source):
    interval = 300

    def __init__(self, ctx: Context, endpoint):
        super().__init__(ctx)
        self.name = "adguard"
        self.url = endpoint.url
        self.secret = endpoint.secret

    async def collect(self):
        user, _, password = (self.ctx.secrets.get(self.secret) or "").partition(":")
        auth = (user, password)
        r = await self.ctx.http.get(f"{self.url}/control/stats", auth=auth)
        r.raise_for_status()
        s = await self.ctx.http.get(f"{self.url}/control/status", auth=auth)
        s.raise_for_status()
        ts = self.ctx.now()
        snaps = parse_stats(r.json() or {}, s.json() or {})
        self.ctx.db.put_snapshots(self.name, snaps, ts=ts)
        stats = snaps["stats"]
        self.ctx.db.add_samples(
            (f"adguard.{k}", ts, float(stats[k]))
            for k in ("queries", "blocked", "avg_ms")
            if isinstance(stats.get(k), int | float)
        )


def build(ctx: Context):
    ep = ctx.config.sources.get("adguard")
    if ep is None:
        return [], {}
    if not ctx.secrets.has(ep.secret):
        return [], {"adguard": f"secret {ep.secret} is missing"}
    return [AdguardStats(ctx, ep)], {}

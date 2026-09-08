"""speedtest-tracker results, fetched incrementally by created_at."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.sources import Context, Source

BACKFILL = 7 * 86400
MAX_PAGES = 10


def _mbps(bits, bytes_):
    if bits is None and bytes_ is not None:
        bits = float(bytes_) * 8
    return round(float(bits) / 1e6, 1) if bits is not None else None


def parse_results(items: list[dict], tz: ZoneInfo):
    rows = []
    for it in items:
        raw = it.get("created_at")
        if not raw or it.get("id") is None:
            continue
        try:
            created = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except ValueError:
            continue
        data = it.get("data") or {}
        server = it.get("server_name") or (data.get("server") or {}).get("name")
        rows.append((
            int(it["id"]),
            int(created.timestamp()),
            _mbps(it.get("download_bits"), it.get("download")),
            _mbps(it.get("upload_bits"), it.get("upload")),
            it.get("ping"),
            it.get("status"),
            server,
        ))
    return rows


class Speedtest(Source):
    interval = 1800

    def __init__(self, ctx: Context, endpoint):
        super().__init__(ctx)
        self.name = f"speedtest.{endpoint.id}"
        self.instance = endpoint.id
        self.url = endpoint.url
        self.secret = endpoint.secret
        self.tz = ZoneInfo(endpoint.timezone)

    async def collect(self):
        db = self.ctx.db
        since = db.last_speedtest_ts(self.instance) or self.ctx.now() - BACKFILL
        since_local = datetime.fromtimestamp(since, tz=self.tz).strftime("%Y-%m-%d %H:%M:%S")
        headers = {"Authorization": f"Bearer {self.ctx.secrets.get(self.secret)}"}
        url = f"{self.url}/api/v1/results"
        params = {
            "filter[start_at]": f">{since_local}",
            "page[size]": 500,
            "sort": "created_at",
        }
        for page in range(MAX_PAGES):
            r = await self.ctx.http.get(url, params=params if page == 0 else None,
                                        headers=headers)
            r.raise_for_status()
            body = r.json() or {}
            db.upsert_speedtests(self.instance, parse_results(body.get("data") or [], self.tz))
            url = (body.get("links") or {}).get("next")
            if not url:
                break


def build(ctx: Context):
    sources, missing = [], {}
    for ep in ctx.config.speedtests.values():
        if not ctx.secrets.has(ep.secret):
            missing[f"speedtest.{ep.id}"] = f"secret {ep.secret} is missing"
            continue
        sources.append(Speedtest(ctx, ep))
    return sources, missing

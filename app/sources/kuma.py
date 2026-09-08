"""Uptime Kuma through its Prometheus endpoint: basic auth, empty user, the key."""

import math
import re

from app.sources import Context, Source

METRIC_RE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
STATUS = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}


def _unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def _label(labels: dict, name: str):
    v = labels.get(name)
    # port monitors carry a bare scheme in monitor_url
    return None if v in (None, "", "null", "undefined", "http://", "https://") else v


def parse_metrics(text: str):
    monitors: dict[str, dict] = {}
    app: dict = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = METRIC_RE.match(line)
        if not m:
            continue
        name, raw_labels, raw_value = m.group(1), m.group(2) or "", m.group(3)
        labels = {k: _unescape(v) for k, v in LABEL_RE.findall(raw_labels)}
        if name == "app_version":
            app["version"] = labels.get("version")
            continue
        if name not in ("monitor_status", "monitor_response_time"):
            continue
        mid = _label(labels, "monitor_id")
        if mid is None:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if math.isnan(value):
            continue
        mon = monitors.setdefault(
            mid,
            {
                "id": int(mid) if mid.isdigit() else mid,
                "name": _label(labels, "monitor_name"),
                "type": _label(labels, "monitor_type"),
                "hostname": _label(labels, "monitor_hostname"),
                "port": _label(labels, "monitor_port"),
                "url": _label(labels, "monitor_url"),
                "status": None,
                "state": None,
                "rtt": None,
            },
        )
        if name == "monitor_status":
            mon["status"] = int(value)
            mon["state"] = STATUS.get(int(value), "unknown")
        else:
            mon["rtt"] = value
    return monitors, app


class KumaMetrics(Source):
    interval = 60

    def __init__(self, ctx: Context, endpoint):
        super().__init__(ctx)
        self.name = "kuma"
        self.url = endpoint.url
        self.secret = endpoint.secret

    async def collect(self):
        r = await self.ctx.http.get(
            f"{self.url}/metrics", auth=("", self.ctx.secrets.get(self.secret) or "")
        )
        r.raise_for_status()
        ts = self.ctx.now()
        monitors, app = parse_metrics(r.text)
        snaps = {f"monitor:{mid}": mon for mid, mon in monitors.items()}
        snaps["app"] = app
        self.ctx.db.put_snapshots(self.name, snaps, ts=ts)
        self.ctx.db.add_samples(
            (f"kuma.rtt.{mid}", ts, mon["rtt"])
            for mid, mon in monitors.items()
            if mon["status"] == 1 and mon["rtt"] is not None
        )


def build(ctx: Context):
    ep = ctx.config.sources.get("kuma")
    if ep is None:
        return [], {}
    if not ctx.secrets.has(ep.secret):
        return [], {"kuma": f"secret {ep.secret} is missing"}
    return [KumaMetrics(ctx, ep)], {}

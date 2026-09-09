"""The rules behind the needs-attention list.

compute() returns everything that is wrong right now, keyed by (source, key).
The store turns that into rows with first_seen and cleared_ts, so an item is
one entry for as long as its condition holds.
"""

from app.clock import in_window
from app.config import Config
from app.db import Database

DOWN_GRACE = 300
DISK_WARN = 85
DISK_CRIT = 95
ERROR_GRACE = 900
THREAD_WINDOW = 86400
SPEEDTEST_MIN_RESULTS = 5
SPEEDTEST_FAIL_RATE = 0.4


def _disk_item(key: tuple, what: str, pct: float, detail: str, items: dict):
    if pct < DISK_WARN:
        return
    items[key] = {
        "severity": "crit" if pct >= DISK_CRIT else "warn",
        "title": f"{what} is {pct:.0f} % full",
        "detail": detail,
    }


def _beszel(db: Database, config: Config, ts: int, items: dict):
    nas_on = in_window(config.nas_window, ts)
    for _key, snap_ts, s in db.get_snapshots("beszel", "system:"):
        name = s.get("name")
        host = config.host_by_beszel(name)
        if s.get("status") != "up":
            if s.get("status") == "paused":
                continue
            if host and host.off_by_default:
                continue
            if host and host.window == "nas" and not nas_on:
                continue
            since = s.get("down_since") or snap_ts
            if ts - since < DOWN_GRACE:
                continue
            items[("beszel", name)] = {
                "severity": "crit" if host and host.rule == "prod" else "warn",
                "title": f"{name} is {s.get('status')} in Beszel",
                "detail": f"{s.get('host') or ''} · no agent data for {(ts - since) // 60} min",
                "first_seen": since,
            }
            continue
        disk = s.get("disk")
        if isinstance(disk, int | float):
            _disk_item(("beszel", f"disk:{name}"), f"{name} root disk", disk,
                       "Beszel agent", items)
        for mount, pct in (s.get("extra_fs") or {}).items():
            if isinstance(pct, int | float):
                _disk_item(("beszel", f"disk:{name}:{mount}"), f"{name} {mount}", pct,
                           "Beszel agent", items)


def _kuma(db: Database, items: dict):
    for _key, _ts, m in db.get_snapshots("kuma", "monitor:"):
        if m.get("status") != 0:
            continue
        where = m.get("url") or ":".join(str(x) for x in (m.get("hostname"), m.get("port")) if x)
        items[("kuma", str(m.get("id")))] = {
            "severity": "warn",
            "title": f"{m.get('name')} is down in Uptime Kuma",
            "detail": f"{m.get('type')} {where}".strip(),
        }


def _pve(db: Database, config: Config, items: dict):
    for pve_id in config.pve:
        source = f"pve.{pve_id}"
        node = db.get_snapshot(source, "node")
        if node and isinstance(node.get("root_pct"), int | float):
            _disk_item((source, "rootfs"), f"{pve_id} PVE root", node["root_pct"],
                       "node status", items)
        for _key, _ts, st in db.get_snapshots(source, "storage:"):
            pct = st.get("pct")
            if st.get("status") == "available" and isinstance(pct, int | float):
                _disk_item((source, f"storage:{st.get('storage')}"),
                           f"{pve_id} storage {st.get('storage')}", pct,
                           st.get("plugin") or "", items)


def _dockhand(db: Database, items: dict):
    for _key, _ts, u in db.get_snapshots("dockhand.updates", "updates:"):
        names = [i.get("name") for i in u.get("items") or [] if i.get("name")]
        if not names:
            continue
        items[("dockhand", f"updates:{u.get('env')}")] = {
            "severity": "info",
            "title": f"{len(names)} image update{'s' if len(names) > 1 else ''}"
                     f" available on {u.get('host')}",
            "detail": ", ".join(names),
        }


def _speedtest(db: Database, config: Config, ts: int, items: dict):
    for iid in config.speedtests:
        rows = db.speedtests(iid, ts - 86400)
        if len(rows) < SPEEDTEST_MIN_RESULTS:
            continue
        failed = sum(1 for r in rows if r.get("status") != "completed")
        rate = failed / len(rows)
        if rate > SPEEDTEST_FAIL_RATE:
            items[("speedtest", iid)] = {
                "severity": "warn",
                "title": f"speedtest {iid}: {rate:.0%} of the last 24 h failed",
                "detail": f"{failed} of {len(rows)} results",
            }


def _threads(db: Database, ts: int, items: dict):
    for t in db.threads():
        changed = t.get("changed_ts")
        if changed and ts - changed < THREAD_WINDOW:
            items[("github", f"{t['repo']}#{t['number']}")] = {
                "severity": "info",
                "title": f"{t['repo']} #{t['number']} moved",
                "detail": f"{t['title']} · {t['state']}, {t['comments']} comments",
                "first_seen": changed,
            }


def _sources(db: Database, ts: int, items: dict):
    for source, st in db.source_status().items():
        if st.get("ok") or st.get("skipped"):
            continue
        since = db.error_streak_since(source)
        if since is None or ts - since < ERROR_GRACE:
            continue
        items[("source", source)] = {
            "severity": "warn",
            "title": f"{source} has been failing for {(ts - since) // 60} min",
            "detail": st.get("error") or "",
            "first_seen": since,
        }


def compute(db: Database, config: Config, ts: int) -> dict[tuple[str, str], dict]:
    items: dict[tuple[str, str], dict] = {}
    _beszel(db, config, ts, items)
    _kuma(db, items)
    _pve(db, config, items)
    _dockhand(db, items)
    _speedtest(db, config, ts, items)
    _threads(db, ts, items)
    _sources(db, ts, items)
    return items

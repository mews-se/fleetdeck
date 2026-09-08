from app.views import State, link

DAY = 86400


def kuma(state: State) -> dict:
    db = state.db
    monitors = [m for _k, _ts, m in db.get_snapshots("kuma", "monitor:")]
    monitors.sort(key=lambda m: (m.get("status") if m.get("status") is not None else 9,
                                 str(m.get("name"))))
    ts = next((t for _k, t, _ in db.get_snapshots("kuma", "app")), None)
    return {
        "version": (db.get_snapshot("kuma", "app") or {}).get("version"),
        "monitors": monitors,
        "counts": {
            "up": sum(1 for m in monitors if m.get("status") == 1),
            "down": sum(1 for m in monitors if m.get("status") == 0),
            "pending": sum(1 for m in monitors if m.get("status") == 2),
            "maintenance": sum(1 for m in monitors if m.get("status") == 3),
            "total": len(monitors),
        },
        "url": link(state.config, "kuma"),
        "ts": ts,
    }


def adguard(state: State) -> dict:
    db = state.db
    stats = db.get_snapshot("adguard", "stats") or {}
    status = db.get_snapshot("adguard", "status") or {}
    ts = next((t for _k, t, _ in db.get_snapshots("adguard", "stats")), None)
    blocked_pct = None
    if stats.get("queries"):
        blocked_pct = round(100.0 * (stats.get("blocked") or 0) / stats["queries"], 1)
    return {
        "stats": stats,
        "blocked_pct": blocked_pct,
        "status": status,
        "url": link(state.config, "adguard"),
        "ts": ts,
    }


def speedtests(state: State) -> list[dict]:
    db, config = state.db, state.config
    now = state.now
    out = []
    for ep in config.speedtests.values():
        week = db.speedtests(ep.id, now - 7 * DAY)
        day = [r for r in week if r["created_at"] >= now - DAY]
        completed = [r for r in week if r["status"] == "completed" and r["download"] is not None]
        latest = week[-1] if week else None
        out.append({
            "id": ep.id,
            "site": config.sites[ep.site].name if ep.site else "",
            "url": ep.url,
            "latest": latest,
            "day": {
                "count": len(day),
                "failed": sum(1 for r in day if r["status"] != "completed"),
            },
            "week": {
                "count": len(week),
                "failed": sum(1 for r in week if r["status"] != "completed"),
                "min": min((r["download"] for r in completed), default=None),
                "max": max((r["download"] for r in completed), default=None),
            },
            "series": [[r["created_at"] for r in completed],
                       [r["download"] for r in completed],
                       [r["upload"] for r in completed]],
        })
    return out


def counts(state: State) -> dict:
    return {"down": kuma(state)["counts"]["down"]}


def build(state: State) -> dict:
    return {
        "kuma": kuma(state),
        "adguard": adguard(state),
        "speedtests": speedtests(state),
        "ts": state.now,
    }

import ipaddress

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


def _ipkey(ip: str) -> int:
    try:
        return int(ipaddress.ip_address(ip))
    except ValueError:
        return 0


def devices(state: State, box) -> list[dict]:
    """Every address the box knows: static mappings, leases and bare ARP entries."""
    db, config = state.db, state.config
    source = f"pfsense.{box.id}.dhcp"
    leases = {v["ip"]: v for _k, _t, v in db.get_snapshots(source, "lease:")}
    # the ARP table also carries the WAN side: the ISP gateway is not a device
    wan_if = (db.get_snapshot(f"pfsense.{box.id}", "box") or {}).get("wan_if")
    arps = {v["ip"]: v for _k, _t, v in db.get_snapshots(source, "arp:")
            if not wan_if or v.get("if") != wan_if}
    by_ip = {h.ip: h.id for h in config.hosts.values() if h.site == box.site}
    out = []
    for ip in sorted(set(leases) | set(arps), key=_ipkey):
        lease, arp = leases.get(ip), arps.get(ip)
        known = lease or arp
        out.append({
            "ip": ip,
            "kind": lease["kind"] if lease else "arp",
            "mac": (lease or {}).get("mac") or (arp or {}).get("mac"),
            "arp_mac": arp["mac"] if arp else None,
            "mismatch": bool(lease and arp and lease["mac"] and arp["mac"]
                             and lease["mac"] != arp["mac"]),
            "hostname": lease["hostname"] if lease else None,
            "descr": lease["descr"] if lease else None,
            "online": bool(arp) or bool(lease and lease["online"]),
            "act": lease["act"] if lease else None,
            "ends": lease["ends"] if lease else None,
            "if": (lease or {}).get("if") or (arp or {}).get("if"),
            "host": by_ip.get(ip),
            "quiet": bool(known.get("quiet")),
        })
    return out


def device_index(state: State) -> dict[str, dict[str, dict]]:
    """Per site, the device rows keyed by address."""
    return {box.site: {d["ip"]: d for d in devices(state, box)}
            for box in state.config.pfsense.values()}


def pfsense(state: State) -> list[dict]:
    db, config = state.db, state.config
    now = state.now
    out = []
    for box in config.pfsense.values():
        host = config.hosts[box.host]
        source = f"pfsense.{box.id}"
        rows = db.get_snapshots(source, "box")
        ts, snap = (rows[0][1], rows[0][2]) if rows else (None, {})
        peers = [p for _k, _t, p in db.get_snapshots(source, "peer:")]
        peers.sort(key=lambda p: (not p.get("online"), str(p.get("name"))))
        dev = devices(state, box)
        dhcp_ts = next((t for _k, t, _v in db.get_snapshots(f"{source}.dhcp", "lease:")), None)
        series = {}
        for name in ("states", "temp", "wan_in", "wan_out"):
            points = db.series(f"{source}.{name}", now - DAY)
            series[name] = [[p[0] for p in points], [p[1] for p in points]]
        out.append({
            "id": box.id,
            "site": config.sites[box.site].name,
            "host": host.id,
            "ip": host.ip,
            "box": snap,
            "ts": ts,
            "dhcp_ts": dhcp_ts,
            "peers": peers,
            "devices": dev,
            "counts": {
                "static": sum(1 for d in dev if d["kind"] == "static"),
                "dynamic": sum(1 for d in dev if d["kind"] == "dynamic"),
                "arp": sum(1 for d in dev if d["kind"] == "arp"),
                "online": sum(1 for d in dev if d["online"]),
                "quiet": sum(1 for d in dev if d["quiet"]),
                "mismatch": sum(1 for d in dev if d["mismatch"]),
            },
            "series": series,
            "url": link(config, f"pfsense-{box.id}"),
        })
    return out


def counts(state: State) -> dict:
    return {"down": kuma(state)["counts"]["down"]}


def build(state: State) -> dict:
    return {
        "kuma": kuma(state),
        "adguard": adguard(state),
        "speedtests": speedtests(state),
        "pfsense": pfsense(state),
        "ts": state.now,
    }

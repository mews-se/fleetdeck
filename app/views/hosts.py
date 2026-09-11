from app.clock import in_window
from app.views import State, link, network

# a pfSense box counts as up while its status source keeps answering
PFSENSE_STALE = 900


def rows(state: State) -> list[dict]:
    db, config = state.db, state.config
    ts = state.now
    systems = {v["name"]: (snap_ts, v) for _k, snap_ts, v in db.get_snapshots("beszel", "system:")}
    nas_on = in_window(config.nas_window, ts)
    beszel_url = link(config, "beszel")
    known = network.device_index(state)
    boxes = {b.host: b for b in config.pfsense.values()}
    out = []
    seen = set()
    for host in config.hosts.values():
        snap_ts, s = systems.get(host.beszel, (None, None)) if host.beszel else (None, None)
        if host.beszel:
            seen.add(host.beszel)
        if s is None and host.id in boxes:
            snap_ts, s = _pfsense_system(db, boxes[host.id], ts)
        if s is None:
            status = "off" if host.off_by_default else "noagent"
        elif s["status"] == "up":
            status = "up"
        elif s["status"] == "paused":
            status = "paused"
        elif host.off_by_default or (host.window == "nas" and not nas_on):
            status = "off"
        else:
            status = "down"
        out.append(_row(host.id, host.ip, config.sites[host.site].name, host.role, host.rule,
                        status, s, snap_ts, beszel_url, host.beszel,
                        known.get(host.site, {}).get(host.ip)))
    for name, (snap_ts, s) in systems.items():
        if name in seen:
            continue
        status = "up" if s["status"] == "up" else "down"
        out.append(_row(name, s.get("host"), "", "not in fleetdeck.yml", None, status, s,
                        snap_ts, beszel_url, name))
    return out


def _pfsense_system(db, box, ts):
    """A Beszel-shaped row from the box's own status snapshot."""
    rows = db.get_snapshots(f"pfsense.{box.id}", "box")
    if not rows:
        return None, None
    _key, snap_ts, b = rows[0]
    fresh = ts - snap_ts <= PFSENSE_STALE
    return snap_ts, {
        "status": "up" if fresh else "down",
        "down_since": None if fresh else snap_ts,
        "cpu": None,
        "mem": b.get("mem_pct"),
        "disk": None,
        "temp": b.get("temp"),
        "uptime": b.get("uptime"),
        "load": b.get("load"),
        "agent": f"pfSense {b.get('version') or ''}".strip(),
        "kernel": None,
    }


def devices(state: State) -> list[dict]:
    """Per site, what pfSense knows that is not a host in the config."""
    config = state.config
    out = []
    for box in config.pfsense.values():
        rows = network.devices(state, box)
        shown = [d for d in rows if d["kind"] != "arp" and not d["host"] and not d["quiet"]]
        out.append({
            "site": config.sites[box.site].name,
            "box": box.host,
            "devices": shown,
            "online": sum(1 for d in shown if d["online"]),
            "quiet": sum(1 for d in rows if d["quiet"]),
        })
    return out


def _row(id_, ip, site, role, rule, status, s, snap_ts, beszel_url, beszel_name, device=None):
    s = s or {}
    device = device or {}
    return {
        "id": id_,
        "ip": ip,
        "site": site,
        "role": role,
        "rule": rule,
        "status": status,
        "cpu": s.get("cpu"),
        "mem": s.get("mem"),
        "disk": s.get("disk"),
        "temp": s.get("temp") or None,
        "uptime": s.get("uptime"),
        "agent": s.get("agent"),
        "kernel": s.get("kernel"),
        "load": s.get("load"),
        "down_since": s.get("down_since"),
        "snap_ts": snap_ts,
        "beszel": f"{beszel_url}/system/{beszel_name}" if beszel_url and beszel_name else None,
        "mac": device.get("mac"),
        "mapping": device.get("kind"),
        "mismatch": device.get("mismatch", False),
    }


def counts(state: State) -> dict:
    r = rows(state)
    return {
        "total": sum(1 for h in r if h["status"] in ("up", "down")),
        "up": sum(1 for h in r if h["status"] == "up"),
        "down": sum(1 for h in r if h["status"] == "down"),
        "off": sum(1 for h in r if h["status"] == "off"),
        "noagent": sum(1 for h in r if h["status"] == "noagent"),
    }


def build(state: State) -> dict:
    r = rows(state)
    return {
        "hosts": r,
        "counts": counts(state),
        "devices": devices(state),
        "links": {"beszel": link(state.config, "beszel"), "termix": link(state.config, "termix")},
        "ts": state.now,
    }

from app.clock import in_window
from app.views import State, link, network


def rows(state: State) -> list[dict]:
    db, config = state.db, state.config
    ts = state.now
    systems = {v["name"]: (snap_ts, v) for _k, snap_ts, v in db.get_snapshots("beszel", "system:")}
    nas_on = in_window(config.nas_window, ts)
    beszel_url = link(config, "beszel")
    known = network.device_index(state)
    out = []
    seen = set()
    for host in config.hosts.values():
        snap_ts, s = systems.get(host.beszel, (None, None)) if host.beszel else (None, None)
        if host.beszel:
            seen.add(host.beszel)
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
        "links": {"beszel": link(state.config, "beszel"), "termix": link(state.config, "termix")},
        "ts": state.now,
    }

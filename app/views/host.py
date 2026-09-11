"""One host, everything the sources know about it."""

from urllib.parse import urlsplit

from app.views import State, containers, hosts, link, network
from app.views.actions import catalog

DAY = 86400
SERIES = ("cpu", "mem", "temp")


def _series(state: State, name: str | None) -> dict[str, list]:
    out = {}
    for field in SERIES:
        points = state.db.series(f"beszel.{field}.{name}", state.now - DAY) if name else []
        out[field] = [[p[0] for p in points], [p[1] for p in points]]
    return out


def _monitors(state: State, host) -> list[dict]:
    names = {n for n in (host.ip, host.id, host.beszel) if n}
    out = []
    for _k, _ts, m in state.db.get_snapshots("kuma", "monitor:"):
        candidates = {m.get("hostname") or ""}
        if m.get("url"):
            candidates.add(urlsplit(m["url"]).hostname or "")
        hit = any(c in names or c.split(".")[0] in names for c in candidates if c)
        if hit:
            out.append(m)
    out.sort(key=lambda m: str(m.get("name")))
    return out


def _guest(state: State, host, known: dict) -> dict | None:
    if not host.guest:
        return None
    config, db = state.config, state.db
    g = host.guest
    live = db.get_snapshot(f"pve.{g.pve}", f"guest:{g.vmid}") or {}
    cfg = db.get_snapshot(f"pve.{g.pve}.config", f"guest:{g.vmid}")
    by_mac = {d["mac"]: d for d in known.get(host.site, {}).values() if d.get("mac")}
    for nic in (cfg or {}).get("nics") or []:
        d = by_mac.get(nic.get("mac"))
        nic["lease"] = {"ip": d["ip"], "kind": d["kind"]} if d else None
    node = config.pve_host(g.pve)
    power = {
        a.run["op"]: a.id for a in state.actions
        if a.kind == "pve" and a.run["vmid"] == g.vmid and node and a.target == node.id
    }
    maxmem = live.get("maxmem")
    return {
        "pve": g.pve,
        "vmid": g.vmid,
        "node": node.id if node else g.pve,
        "type": "VM" if (live.get("type") or (cfg or {}).get("type")) == "qemu" else "CT",
        "status": "template" if live.get("template") else live.get("status"),
        "cpu": round(float(live.get("cpu") or 0) * 100, 1),
        "mem_pct": round(100.0 * float(live.get("mem") or 0) / maxmem, 1) if maxmem else None,
        "maxmem": maxmem,
        "uptime": live.get("uptime"),
        "tags": live.get("tags"),
        "free": g.vmid in config.pve[g.pve].free_guests,
        "actions": power,
        "config": cfg,
        "links": {
            "pve": link(config, f"pve-{g.pve}") or config.pve[g.pve].url,
            "pdm": link(config, "pdm"),
        },
    }


def _pve_node(state: State, host) -> dict | None:
    if not host.pve:
        return None
    source = f"pve.{host.pve}"
    node = state.db.get_snapshot(source, "node") or {}
    version = (state.db.get_snapshot(f"{source}.version", "version") or {}).get("version")
    return {
        "id": host.pve,
        "version": version,
        "guests": node.get("guests"),
        "running": node.get("running"),
        "root_pct": node.get("root_pct"),
        "url": link(state.config, f"pve-{host.pve}") or state.config.pve[host.pve].url,
    }


def _network(state: State, host, known: dict) -> dict | None:
    box = state.config.pfsense_for_site(host.site)
    if box is None:
        return None
    return {"box": box.host, "device": known.get(host.site, {}).get(host.ip)}


def build(state: State, host_id: str) -> dict:
    config, db = state.config, state.db
    host = config.hosts[host_id]
    known = network.device_index(state)
    summary = next(r for r in hosts.rows(state) if r["id"] == host_id)
    system = db.get_snapshot("beszel", f"system:{host.beszel}") if host.beszel else None
    if system is not None:
        system["snap_ts"] = summary["snap_ts"]
    top = (db.get_snapshot("adguard", "stats") or {}).get("top_clients") or []
    dns = next((c.get("count") for c in top if c.get("name") == host.ip), None)
    actions = []
    for a in catalog(state):
        if host_id not in [t["id"] for t in a["targets"]]:
            continue
        mine = [t for t in a["targets"] if t["id"] == host_id]
        actions.append({**a, "target": host_id, "targets": mine,
                        "target_label": mine[0]["label"]})
    group = None
    if host.dockhand_env is not None or (host.off_by_default and host.guest):
        group = next((g for g in containers.groups(state) if g["host"] == host_id), None)
    links = {}
    if summary.get("beszel"):
        links["beszel"] = summary["beszel"]
    if host.dockhand_env is not None and link(config, "dockhand"):
        links["dockhand"] = f"{link(config, 'dockhand')}/?env={host.dockhand_env}"
    if host.ssh and link(config, "termix"):
        links["termix"] = link(config, "termix")
    return {
        "host": {
            "id": host.id,
            "ip": host.ip,
            "site": config.sites[host.site].name,
            "role": host.role,
            "rule": host.rule,
            "ssh": host.ssh,
            "beszel": host.beszel,
            "status": summary["status"],
            "off_by_default": host.off_by_default,
            "window": host.window,
        },
        "system": system,
        "series": _series(state, host.beszel),
        "guest": _guest(state, host, known),
        "pve_node": _pve_node(state, host),
        "network": _network(state, host, known),
        "containers": group,
        "monitors": _monitors(state, host),
        "dns": {"queries": dns},
        "actions": actions,
        "runs": db.runs_for_target(host_id),
        "running": state.running,
        "links": links,
        "ts": state.now,
    }

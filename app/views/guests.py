from app.views import State, link


def _pct(a, b):
    if not b:
        return None
    return round(100.0 * float(a or 0) / float(b), 1)


def pves(state: State) -> list[dict]:
    db, config = state.db, state.config
    power = {}
    for a in state.actions:
        if a.kind == "pve":
            power.setdefault((a.target, a.run["vmid"]), {})[a.run["op"]] = a.id
    out = []
    for pve in config.pve.values():
        host = config.pve_host(pve.id)
        source = f"pve.{pve.id}"
        node = db.get_snapshot(source, "node") or {}
        node_ts = next((t for k, t, _ in db.get_snapshots(source, "node")), None)
        version = (db.get_snapshot(f"{source}.version", "version") or {}).get("version")
        if not version and node.get("pveversion"):
            # pve-manager/9.2.11/abcdef
            parts = str(node["pveversion"]).split("/")
            version = parts[1] if len(parts) > 1 else parts[0]
        guests = []
        for _k, _ts, g in db.get_snapshots(source, "guest:"):
            vmid = g["vmid"]
            mapped = config.host_by_guest(pve.id, vmid)
            actions = power.get((host.id if host else None, vmid), {})
            guests.append({
                "vmid": vmid,
                "type": "VM" if g["type"] == "qemu" else "CT",
                "name": g.get("name"),
                "status": "template" if g.get("template") else g.get("status"),
                "cpu": round(float(g.get("cpu") or 0) * 100, 1),
                "mem_pct": _pct(g.get("mem"), g.get("maxmem")),
                "maxmem": g.get("maxmem"),
                "uptime": g.get("uptime"),
                "tags": g.get("tags"),
                "free": vmid in pve.free_guests,
                "host": mapped.id if mapped else None,
                "rule": mapped.rule if mapped else None,
                "actions": actions,
            })
        guests.sort(key=lambda g: g["vmid"])
        storages = [
            {
                "name": s.get("storage"),
                "plugin": s.get("plugin"),
                "pct": s.get("pct"),
                "disk": s.get("disk"),
                "maxdisk": s.get("maxdisk"),
                "status": s.get("status"),
                "content": s.get("content"),
            }
            for _k, _ts, s in db.get_snapshots(source, "storage:")
        ]
        out.append({
            "id": pve.id,
            "host": host.id if host else pve.node,
            "ip": host.ip if host else None,
            "site": config.sites[host.site].name if host else "",
            "node_name": pve.node,
            "version": version,
            "kversion": node.get("kversion"),
            "cpu_model": node.get("cpu_model"),
            "url": link(config, f"pve-{pve.id}") or pve.url,
            "pdm": link(config, "pdm"),
            "node": {
                "cpu": node.get("cpu"),
                "iowait": node.get("iowait"),
                "mem_pct": node.get("mem_pct"),
                "load": node.get("load"),
                "uptime": node.get("uptime"),
                "root_pct": node.get("root_pct"),
                "running": node.get("running"),
                "guests": node.get("guests"),
                "ts": node_ts,
            },
            "guests": guests,
            "storages": storages,
        })
    return out


def counts(state: State) -> dict:
    p = pves(state)
    return {
        "running": sum(1 for x in p for g in x["guests"] if g["status"] == "running"),
        "total": sum(1 for x in p for g in x["guests"] if g["status"] != "template"),
    }


def build(state: State) -> dict:
    return {"pves": pves(state), "counts": counts(state), "ts": state.now}

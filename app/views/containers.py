from app.views import State, link
from app.views.actions import catalog, container_actions


def groups(state: State) -> list[dict]:
    db, config = state.db, state.config
    containers = {}
    for _k, ts, c in db.get_snapshots("dockhand"):
        containers.setdefault(c["env"], []).append((ts, c))
    updates = {u["env"]: u for _k, _ts, u in db.get_snapshots("dockhand.updates", "updates:")}
    systems = {s["env"]: s for _k, _ts, s in db.get_snapshots("dockhand.system", "system:")}
    dockhand = link(config, "dockhand")
    out = []
    for host in config.hosts.values():
        env = host.dockhand_env
        if env is None and not (host.off_by_default and host.guest):
            continue
        guest_status = None
        if host.guest:
            g = db.get_snapshot(f"pve.{host.guest.pve}", f"guest:{host.guest.vmid}")
            guest_status = (g or {}).get("status")
        rows = []
        newer = {u["name"]: u for u in (updates.get(env) or {}).get("items") or []}
        ops = container_actions(state, host.id) if env is not None else []
        for ts, c in containers.get(env, []):
            rows.append({
                "name": c["name"],
                "image": c.get("image"),
                "state": c.get("state"),
                "status": c.get("status"),
                "update": (newer.get(c["name"]) or {}).get("newer") if c["name"] in newer
                else None,
                "ts": ts,
                "actions": [{"id": o["id"], "op": o["op"], "policy": o["policy"]}
                            for o in ops if o["container"] in (None, c["name"])],
            })
        rows.sort(key=lambda r: r["name"])
        system = systems.get(env) or {}
        stats = system.get("stats") or {}
        out.append({
            "host": host.id,
            "ip": host.ip,
            "site": config.sites[host.site].name,
            "env": env,
            "rule": host.rule,
            "off": host.off_by_default and guest_status != "running" and not rows,
            "guest_status": guest_status,
            "containers": rows,
            "system": {
                "docker": system.get("docker"),
                "containers": stats.get("containers"),
                "images": stats.get("images"),
                "layers_size": system.get("layers_size"),
                "vulns": system.get("vulns"),
            } if system else None,
            "links": {
                "dockhand": f"{dockhand}/?env={env}" if dockhand and env is not None else dockhand,
            },
        })
    return out


def counts(state: State) -> dict:
    g = groups(state)
    rows = [c for x in g for c in x["containers"]]
    return {
        "running": sum(1 for c in rows if c["state"] == "running"),
        "exited": sum(1 for c in rows if c["state"] not in ("running", None)),
        "updates": sum(1 for c in rows if c["update"]),
        "total": len(rows),
    }


def build(state: State) -> dict:
    return {
        "groups": groups(state),
        "counts": counts(state),
        "actions": [a for a in catalog(state) if a["kind"] == "dockhand"],
        "ts": state.now,
    }

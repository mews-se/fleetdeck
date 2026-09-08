from app.clock import window_state
from app.views import State, containers, guests, hosts, network, upstream

DAY = 86400


def stats(state: State, h, g, c, k, ag, st) -> list[dict]:
    dns = None
    if ag["stats"].get("queries") is not None:
        q = ag["stats"]["queries"]
        dns = {"value": f"{q / 1000:.1f}k" if q >= 1000 else str(q),
               "sub": f"{ag['blocked_pct']} % blocked · {ag['stats'].get('avg_ms')} ms upstream"}
    wan = next((s for s in st if s["latest"]), None)
    return [
        {"label": "Hosts up", "value": h["up"], "of": h["total"],
         "sub": f"{h['off']} off by rule" + (f" · {h['noagent']} without agent"
                                             if h["noagent"] else ""),
         "kind": "warn" if h["down"] else ""},
        {"label": "Guests running", "value": g["running"], "of": g["total"],
         "sub": f"on {len(state.config.pve)} PVE host{'s' if len(state.config.pve) != 1 else ''}",
         "kind": ""},
        {"label": "Containers", "value": c["running"], "of": c["total"],
         "sub": f"{c['updates']} image update{'s' if c['updates'] != 1 else ''} available"
         if c["updates"] else "all images current",
         "kind": "info" if c["updates"] else ""},
        {"label": "Monitors up", "value": k["counts"]["up"], "of": k["counts"]["total"],
         "sub": f"{k['counts']['down']} down · {k['counts']['maintenance']} in maintenance",
         "kind": "warn" if k["counts"]["down"] else ""},
        {"label": "DNS 24 h", "value": dns["value"] if dns else "—",
         "sub": dns["sub"] if dns else "AdGuard not read yet", "kind": ""},
        {"label": "WAN", "value": f"{wan['latest']['download']:.0f}" if wan and wan["latest"].get(
            "download") else "—",
         "unit": "Mbit/s",
         "sub": f"{wan['latest']['upload']:.0f} up · {wan['latest']['ping']} ms"
         if wan and wan["latest"].get("download") else "no speedtest yet", "kind": ""},
    ]


def build(state: State) -> dict:
    db, config = state.db, state.config
    h = hosts.counts(state)
    g = guests.counts(state)
    c = containers.counts(state)
    k = network.kuma(state)
    ag = network.adguard(state)
    st = network.speedtests(state)
    attention = db.open_attention()
    moved = [t for t in upstream.threads(state) if t["moved"]]
    updates = [r for r in upstream.releases(state) if r["state"] == "update"]
    nas = window_state(config.nas_window, state.now)
    nas_host = next((x for x in config.hosts.values() if x.window == "nas"), None)
    nas_status = None
    if nas_host and nas_host.beszel:
        nas_status = (db.get_snapshot("beszel", f"system:{nas_host.beszel}") or {}).get("status")
    single = [a for a in state.actions if len(a.targets) == 1]
    quick = [a for a in single if a.policy == "free"][:4]
    quick += [a for a in single if a.policy == "read"][:2]
    return {
        "stats": stats(state, h, g, c, k, ag, st),
        "attention": attention,
        "nas": {"window": nas, "status": nas_status, "host": nas_host.id if nas_host else None,
                "ip": nas_host.ip if nas_host else None},
        "wan": st[0] if st else None,
        "moved": [{"kind": "thread", "title": f"{t['repo']} #{t['number']}: {t['title']}",
                   "detail": f"{t['state']} · {t['comments']} comments", "url": t["url"],
                   "ts": t["changed_ts"]} for t in moved]
        + [{"kind": "release", "title": f"{r['repo']} {r['latest_tag']} is out",
            "detail": f"running {r['running_version']} ({r['running_source']})",
            "url": r["url"], "ts": r["seen_ts"]} for r in updates],
        "quick": [{"id": a.id, "title": a.title, "policy": a.policy, "target": a.target}
                  for a in quick],
        "sources": db.source_status(),
        "unconfigured": state.unconfigured,
        "ts": state.now,
    }

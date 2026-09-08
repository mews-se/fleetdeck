from app.views import State, link


def catalog(state: State) -> list[dict]:
    config = state.config
    last = state.db.last_run_per_action()
    out = []
    for a in state.actions:
        host = config.hosts[a.target]
        run = last.get(a.id)
        out.append({
            "id": a.id,
            "title": a.title,
            "target": a.target,
            "target_label": f"{a.target} · {config.sites[host.site].name}",
            "rule": host.rule,
            "kind": a.kind,
            "policy": a.policy,
            "summary": a.summary(),
            "params": [{"name": p.name, "default": p.default, "pattern": p.pattern.pattern}
                       for p in a.params.values()],
            "note": a.note,
            "last": {"id": run["id"], "ts": run["started_ts"], "exit": run["exit_code"]}
            if run else None,
        })
    return out


def runs(state: State, limit: int = 30) -> list[dict]:
    return state.db.runs(limit)


def build(state: State) -> dict:
    config = state.config
    termix = link(config, "termix")
    return {
        "actions": catalog(state),
        "runs": runs(state),
        "running": state.running,
        "termix": termix,
        "hosts": [{"id": h.id, "ssh": h.ssh, "site": config.sites[h.site].name}
                  for h in config.hosts.values() if h.ssh],
        "ts": state.now,
    }

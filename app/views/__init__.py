"""One builder per page. Each returns the JSON the page renders from."""

from dataclasses import dataclass

from app.catalog import Action
from app.config import Config
from app.db import Database


@dataclass
class State:
    db: Database
    config: Config
    actions: list[Action]
    unconfigured: dict[str, str]
    running: list[int]
    version: str

    @property
    def now(self) -> int:
        from app.db import now
        return now()


def link(config: Config, name: str) -> str | None:
    return config.links.get(name)


def build(name: str, state: State) -> dict:
    from app.views import actions, containers, guests, hosts, network, overview, upstream

    builders = {
        "overview": overview.build,
        "hosts": hosts.build,
        "guests": guests.build,
        "containers": containers.build,
        "network": network.build,
        "upstream": upstream.build,
        "actions": actions.build,
    }
    return builders[name](state)


def nav(state: State) -> dict:
    """The counts shown next to the section names."""
    from app.views import containers, guests, hosts, network, upstream

    h = hosts.counts(state)
    g = guests.counts(state)
    c = containers.counts(state)
    n = network.counts(state)
    u = upstream.counts(state)
    fresh = state.db.unacked_attention()
    return {
        "overview": {"n": len(fresh) or "",
                     "warn": any(a["severity"] in ("warn", "crit") for a in fresh)},
        "hosts": {"n": f"{h['up']}/{h['total']}", "warn": h["down"] > 0},
        "guests": {"n": str(g["running"]), "warn": False},
        "containers": {"n": f"{c['updates']}↑" if c["updates"] else str(c["running"]),
                       "warn": c["exited"] > 0},
        "network": {"n": f"{n['down']}!" if n["down"] else "", "warn": n["down"] > 0},
        "upstream": {"n": str(u["moved"]) if u["moved"] else "", "warn": False},
        "actions": {"n": str(len(state.running)) if state.running else "", "warn": False},
    }

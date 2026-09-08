import re

from app.views import State

DAY = 86400


def normalise(version: str | None) -> str | None:
    if not version:
        return None
    v = version.strip().lstrip("vV")
    v = re.split(r"[-+_ ]", v, maxsplit=1)[0]
    return v or None


def release_state(latest: str | None, running: str | None) -> str:
    a, b = normalise(latest), normalise(running)
    # a tag like "latest" says nothing about the version
    if not a or not b or not re.search(r"\d", b):
        return "unknown"
    if a == b or b.startswith(a + ".") or a.startswith(b + "."):
        return "current"
    return "update"


def threads(state: State) -> list[dict]:
    now = state.now
    out = []
    for t in state.db.threads():
        t["moved"] = bool(t.get("changed_ts") and now - t["changed_ts"] < DAY)
        out.append(t)
    out.sort(key=lambda t: (t["state"] != "open", t["repo"], t["number"]))
    return out


def releases(state: State) -> list[dict]:
    out = []
    for r in state.db.releases():
        r["state"] = release_state(r.get("latest_tag"), r.get("running_version"))
        out.append(r)
    out.sort(key=lambda r: ({"update": 0, "unknown": 1, "current": 2}[r["state"]], r["repo"]))
    return out


def counts(state: State) -> dict:
    return {
        "open": sum(1 for t in state.db.threads() if t["state"] == "open"),
        "moved": sum(1 for t in threads(state) if t["moved"])
        + sum(1 for r in releases(state) if r["state"] == "update"),
    }


def build(state: State) -> dict:
    return {
        "threads": threads(state),
        "releases": releases(state),
        "counts": counts(state),
        "ts": state.now,
    }

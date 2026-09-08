import json
import re

import httpx
import pytest

from app import attention, sources
from app.db import Database
from app.sources import adguard, beszel, dockhand, github, kuma, pve, speedtest
from tests.conftest import fixture

NOW = 1_800_000_000


def make_ctx(cfg, secrets, handler):
    transport = httpx.MockTransport(handler)
    return sources.Context(
        db=Database(":memory:"),
        config=cfg,
        secrets=secrets,
        http=httpx.AsyncClient(transport=transport),
        http_insecure=httpx.AsyncClient(transport=transport),
    )


def route(table):
    """Map (method, path) or path to a fixture name, a dict or a status code."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path
        hit = table.get((request.method, key), table.get(key))
        if hit is None:
            return httpx.Response(404, json={"message": f"no route for {key}"})
        if isinstance(hit, int):
            return httpx.Response(hit)
        if isinstance(hit, str):
            body = fixture(hit)
            if hit.endswith(".json"):
                return httpx.Response(200, content=body,
                                      headers={"content-type": "application/json"})
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=hit)

    return handler


@pytest.mark.asyncio
async def test_beszel(cfg, secrets):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("auth-with-password"):
            assert json.loads(request.content) == {"identity": "user", "password": "secret"}
            return httpx.Response(200, json={"token": "tok"})
        if request.headers.get("authorization") != "tok":
            return httpx.Response(401, json={})
        return httpx.Response(200, content=fixture("beszel_systems.json"))

    ctx = make_ctx(cfg, secrets, handler)
    src = beszel.build(ctx)[0][0]
    await src.collect()
    snaps = {k: v for k, _, v in ctx.db.get_snapshots("beszel", "system:")}
    assert snaps["system:dellpi"]["cpu"] == 4.2 and snaps["system:dellpi"]["temp"] == 41
    assert snaps["system:dellpi"]["extra_fs"] == {"ssd": 34.2}
    assert snaps["system:nas"]["status"] == "down"
    down_since = snaps["system:nas"]["down_since"]
    assert down_since
    assert ctx.db.latest_sample("beszel.disk.testpi5") is not None
    assert ctx.db.latest_sample("beszel.temp.testpi5") is None
    assert ctx.db.latest_sample("beszel.cpu.nas") is None
    await src.collect()
    assert ctx.db.get_snapshot("beszel", "system:nas")["down_since"] == down_since
    assert calls[0][1].endswith("auth-with-password")
    src.token = "stale"
    await src.collect()
    assert calls[-1][2] == "tok"


@pytest.mark.asyncio
async def test_beszel_needs_secret(cfg, tmp_path):
    from app.config import Secrets
    ctx = make_ctx(cfg, Secrets(tmp_path), route({}))
    built, missing = beszel.build(ctx)
    assert built == [] and "beszel" in missing


@pytest.mark.asyncio
async def test_pve(cfg, secrets):
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return route({
            "/api2/json/cluster/resources": "pve_resources.json",
            "/api2/json/nodes/proxmox/status": "pve_status.json",
            "/api2/json/nodes/pve/status": "pve_status.json",
            "/api2/json/version": "pve_version.json",
        })(request)

    ctx = make_ctx(cfg, secrets, handler)
    built, missing = pve.build(ctx)
    assert missing == {} and len(built) == 4
    for s in built:
        await s.collect()
    assert seen[0] == "PVEAPIToken=token"
    node = ctx.db.get_snapshot("pve.home", "node")
    assert node["cpu"] == 7.1 and node["load"] == [0.52, 0.61, 0.58]
    assert node["root_pct"] == 48.7 and node["guests"] == 4 and node["running"] == 2
    guests = {k: v for k, _, v in ctx.db.get_snapshots("pve.home", "guest:")}
    assert guests["guest:301"]["template"] is True
    assert guests["guest:104"]["status"] == "running"
    storages = {k: v for k, _, v in ctx.db.get_snapshots("pve.home", "storage:")}
    assert storages["storage:sda"]["pct"] == 95.8
    assert storages["storage:nas-backup"]["pct"] is None
    assert ctx.db.get_snapshot("pve.home.version", "version")["version"] == "9.2.11"
    assert ctx.db.latest_sample("pve.brk.load") == (ctx.db.latest_sample("pve.brk.load")[0], 0.52)


@pytest.mark.asyncio
async def test_dockhand(cfg, secrets):
    def handler(request):
        env = request.url.params.get("env")
        if env == "5":
            return httpx.Response(502, json={"error": "hawser unreachable"})
        assert request.headers["authorization"] == "Bearer token"
        return route({
            "/api/containers": "dockhand_containers.json",
            "/api/containers/pending-updates": "dockhand_updates.json",
            "/api/system": "dockhand_system.json",
            "/api/system/disk": {"diskUsage": {"LayersSize": 1420000000}},
            "/api/vulnerabilities/count": {"total": 2, "summary": {"high": 2}},
            "/openapi.json": {"info": {"version": "1.0.46"}},
        })(request)

    ctx = make_ctx(cfg, secrets, handler)
    built, missing = dockhand.build(ctx)
    assert len(built) == 3 and missing == {}
    for s in built:
        with pytest.raises(sources.SourceError, match="env 5"):
            await s.collect()
    snaps = {k: v for k, _, v in ctx.db.get_snapshots("dockhand")}
    assert snaps["1:docker-nginxproxymanager-app-1"]["image"] == "jc21/nginx-proxy-manager:2.15.1"
    assert snaps["4:uptime-kuma"]["state"] == "exited"
    assert not any(k.startswith("5:") for k in snaps)
    updates = ctx.db.get_snapshot("dockhand.updates", "updates:1")
    assert [u["name"] for u in updates["items"]] == ["beszel"]
    system = ctx.db.get_snapshot("dockhand.system", "system:1")
    assert system["docker"] == "28.3.2" and system["layers_size"] == 1420000000
    assert system["vulns"]["total"] == 2
    assert ctx.db.get_snapshot("dockhand.system", "version") == {"version": "1.0.46"}


def test_kuma_parse():
    monitors, app = kuma.parse_metrics(fixture("kuma_metrics.txt"))
    assert app == {"version": "2.5.3"}
    assert monitors["6"]["name"] == 'dellpi "ssh"' and monitors["6"]["rtt"] == 3
    assert monitors["6"]["state"] == "up" and monitors["6"]["url"] is None
    assert monitors["23"]["status"] == 3 and monitors["23"]["hostname"] == "10.0.0.100"
    assert monitors["23"]["url"] is None
    assert monitors["40"]["state"] == "down" and monitors["40"]["port"] is None


@pytest.mark.asyncio
async def test_kuma(cfg, secrets):
    def handler(request):
        assert request.headers["authorization"] == "Basic OnRva2Vu"
        return httpx.Response(200, text=fixture("kuma_metrics.txt"))

    ctx = make_ctx(cfg, secrets, handler)
    await kuma.build(ctx)[0][0].collect()
    assert ctx.db.get_snapshot("kuma", "app") == {"version": "2.5.3"}
    assert ctx.db.get_snapshot("kuma", "monitor:40")["status"] == 0
    assert ctx.db.latest_sample("kuma.rtt.6")[1] == 3
    assert ctx.db.latest_sample("kuma.rtt.23") is None


@pytest.mark.asyncio
async def test_adguard(cfg, secrets):
    def handler(request):
        assert request.headers["authorization"] == "Basic dXNlcjpzZWNyZXQ="
        return route({
            "/control/stats": "adguard_stats.json",
            "/control/status": "adguard_status.json",
        })(request)

    ctx = make_ctx(cfg, secrets, handler)
    await adguard.build(ctx)[0][0].collect()
    stats = ctx.db.get_snapshot("adguard", "stats")
    assert stats["queries"] == 21400 and stats["avg_ms"] == 18.0
    assert stats["top_blocked"][0] == {"name": "eu-mobile.events.data.microsoft.com", "count": 640}
    assert len(stats["hourly_queries"]) == 24
    assert ctx.db.get_snapshot("adguard", "status")["version"] == "v0.107.79"
    assert ctx.db.latest_sample("adguard.blocked")[1] == 3850


@pytest.mark.asyncio
async def test_speedtest(cfg, secrets):
    params = []

    def handler(request):
        params.append(dict(request.url.params))
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, content=fixture("speedtest_results.json"))

    ctx = make_ctx(cfg, secrets, handler)
    built, missing = speedtest.build(ctx)
    assert len(built) == 2 and missing == {}
    home = next(s for s in built if s.instance == "home")
    await home.collect()
    assert params[0]["page[size]"] == "500"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", params[0]["filter[start_at]"])
    rows = ctx.db.speedtests("home", 0)
    assert [r["id"] for r in rows] == [1440, 1441, 1442]
    assert rows[0]["download"] == 938.0 and rows[0]["server"] == "Telia Stockholm"
    assert rows[1]["status"] == "failed" and rows[1]["download"] is None
    assert rows[2]["created_at"] - rows[0]["created_at"] == 3601
    await home.collect()
    assert params[1]["filter[start_at]"] == "2026-09-08"
    assert len(ctx.db.speedtests("home", 0)) == 3


@pytest.mark.asyncio
async def test_github(cfg, secrets):
    def handler(request):
        assert request.headers["authorization"] == "Bearer token"
        p = request.url.path
        if p.endswith("/issues/98"):
            return httpx.Response(200, content=fixture("github_pr.json"))
        if p.endswith("/issues/100"):
            return httpx.Response(200, content=fixture("github_issue.json"))
        if "/issues/" in p:
            return httpx.Response(404, json={"message": "Not Found"})
        if p == "/repos/henrygd/beszel/releases/latest":
            return httpx.Response(200, content=fixture("github_release.json"))
        if p == "/repos/Finsys/dockhand/releases/latest":
            return httpx.Response(404, json={})
        if p == "/repos/Finsys/dockhand/tags":
            return httpx.Response(200, content=fixture("github_tags.json"))
        if p.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": "v1", "published_at": None,
                                             "html_url": "u"})
        return httpx.Response(404, json={})

    ctx = make_ctx(cfg, secrets, handler)
    ctx.db.put_snapshot("dockhand", "1:beszel", {"image": "henrygd/beszel:0.19.0"})
    ctx.db.put_snapshot("kuma", "app", {"version": "2.5.3"})
    ctx.db.put_snapshot("dockhand.system", "version", {"version": "1.0.46"})
    threads, releases = github.build(ctx)[0]
    with pytest.raises(sources.SourceError, match="#99"):
        await threads.collect()
    by = {(t["repo"], t["number"]): t for t in ctx.db.threads()}
    assert by[("Hosteroid/domain-monitor", 98)]["state"] == "merged"
    assert by[("Hosteroid/domain-monitor", 100)]["kind"] == "issue"
    await releases.collect()
    rel = {r["repo"]: r for r in ctx.db.releases()}
    assert rel["henrygd/beszel"]["latest_tag"] == "v0.19.1"
    assert rel["henrygd/beszel"]["running_version"] == "0.19.0"
    assert rel["louislam/uptime-kuma"]["running_version"] == "2.5.3"
    assert rel["Finsys/dockhand"]["latest_tag"] == "1.0.46"
    assert rel["Finsys/dockhand"]["running_version"] == "1.0.46"


def test_image_tag():
    assert github.image_tag("henrygd/beszel:0.19.0") == "0.19.0"
    assert github.image_tag("ghcr.io/x/y:1.2.3") == "1.2.3"
    assert github.image_tag("localhost:5000/x/y") is None
    assert github.image_tag("nginx") is None
    assert github.image_tag(None) is None


def test_attention_rules(cfg):
    db = Database(":memory:")
    prev = {}
    snaps, _ = beszel.parse_systems(json.loads(fixture("beszel_systems.json"))["items"], prev,
                                    NOW - 600)
    db.put_snapshots("beszel", snaps, ts=NOW - 600)
    monitors, _ = kuma.parse_metrics(fixture("kuma_metrics.txt"))
    db.put_snapshots("kuma", {f"monitor:{k}": v for k, v in monitors.items()})
    snaps, _ = pve.parse_resources("home", json.loads(fixture("pve_resources.json"))["data"],
                                   json.loads(fixture("pve_status.json"))["data"], NOW)
    db.put_snapshots("pve.home", snaps)
    db.put_snapshot("dockhand.updates", "updates:1",
                    {"env": 1, "host": "dellpi", "items": [{"name": "beszel"}]})
    db.upsert_speedtests("home", [(i, NOW - i * 60, None, None, None,
                                   "failed" if i < 4 else "completed", None)
                                  for i in range(1, 11)])
    db.upsert_thread("o/r", 1, "pr", "t", "open", "a", 0, "u", None, ts=NOW - 100)
    db.upsert_thread("o/r", 1, "pr", "t", "open", "b", 1, "u", None, ts=NOW - 50)
    for ts in (NOW - 2000, NOW - 1000, NOW - 10):
        db.record_run("kuma", False, 1, error="timeout", ts=ts)
    db.record_run("beszel", False, 1, error="fresh", ts=NOW - 10)

    items = attention.compute(db, cfg, NOW)
    keys = set(items)
    assert ("beszel", "disk:testpi5") in keys
    assert ("kuma", "40") in keys and ("kuma", "23") not in keys
    assert items[("pve.home", "storage:sda")]["severity"] == "crit"
    assert ("pve.home", "storage:nas-backup") not in keys
    assert ("dockhand", "updates:1") in keys
    assert ("speedtest", "home") in keys and "30%" in items[("speedtest", "home")]["title"]
    assert ("github", "o/r#1") in keys
    assert ("source", "kuma") in keys and ("source", "beszel") not in keys
    # nas is down but inside the window only counts; outside it is expected
    from app.clock import in_window
    assert (("beszel", "nas") in keys) == in_window(cfg.nas_window, NOW)

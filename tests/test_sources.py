import json
import re

import httpx
import pytest

from app import attention, sources
from app.db import Database
from app.sources import adguard, beszel, dockhand, github, kuma, pfsense, pve, speedtest
from tests.conftest import FIXTURES, fixture

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
        if request.url.path.endswith("/system_details/records"):
            return httpx.Response(200, content=fixture("beszel_system_details.json"))
        return httpx.Response(200, content=fixture("beszel_systems.json"))

    ctx = make_ctx(cfg, secrets, handler)
    src = beszel.build(ctx)[0][0]
    await src.collect()
    snaps = {k: v for k, _, v in ctx.db.get_snapshots("beszel", "system:")}
    dellpi = snaps["system:dellpi"]
    assert dellpi["cpu"] == 0.42 and dellpi["temp"] == 45 and dellpi["extra_fs"] == {"sda1": 8.91}
    assert dellpi["hostname"] == "dellpi" and dellpi["os"] == "Debian GNU/Linux 13 (trixie)"
    assert dellpi["kernel"] == "6.12.107+deb13-amd64" and dellpi["arch"] == "x86_64"
    assert dellpi["model"].startswith("Intel(R) Core(TM) i5-10500T")
    assert dellpi["cores"] == 6 and dellpi["threads"] == 12 and dellpi["memory"] == 16507842560
    assert snaps["system:nas"]["status"] == "down" and snaps["system:nas"]["os"] == "Synology NAS"
    down_since = snaps["system:nas"]["down_since"]
    assert down_since
    assert ctx.db.latest_sample("beszel.disk.testpi5") is not None
    assert ctx.db.latest_sample("beszel.temp.teslamate") is None
    assert ctx.db.latest_sample("beszel.cpu.nas") is None
    await src.collect()
    assert ctx.db.get_snapshot("beszel", "system:nas")["down_since"] == down_since
    assert calls[0][1].endswith("auth-with-password")
    src.token = "stale"
    await src.collect()
    assert calls[-1][2] == "tok"


def test_beszel_without_details():
    items = json.loads(fixture("beszel_systems.json"))["items"]
    snaps, _ = beszel.parse_systems(items, {}, {}, NOW)
    assert snaps["system:dellpi"]["cpu"] == 0.42
    assert snaps["system:dellpi"]["hostname"] is None and snaps["system:dellpi"]["os"] is None


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
            "/api2/json/nodes/proxmox/qemu/104/config": "pve_config_qemu.json",
            "/api2/json/nodes/proxmox/qemu/904/config": "pve_config_qemu.json",
            "/api2/json/nodes/proxmox/qemu/301/config": "pve_config_qemu.json",
            "/api2/json/nodes/proxmox/lxc/108/config": "pve_config_lxc.json",
        })(request)

    ctx = make_ctx(cfg, secrets, handler)
    built, missing = pve.build(ctx)
    assert missing == {} and len(built) == 6
    for s in built:
        await s.collect()
    vm = ctx.db.get_snapshot("pve.home.config", "guest:904")
    assert vm["name"] == "testdebug" and vm["cores"] == 2 and vm["memory"] == 4096
    assert vm["onboot"] is False and vm["unprivileged"] is None
    assert vm["nics"] == [{"name": "net0", "model": "virtio", "mac": "bc:24:11:8d:69:3c",
                           "bridge": "vmbr0", "ip": None, "vlan": None}]
    assert [d["name"] for d in vm["disks"]] == ["efidisk0", "scsi0"]
    assert vm["disks"][1] == {"name": "scsi0", "volume": "local:904/vm-904-disk-0.qcow2",
                              "storage": "local", "size": "20G"}
    ct = ctx.db.get_snapshot("pve.home.config", "guest:108")
    assert ct["name"] == "adguard" and ct["onboot"] is True and ct["unprivileged"] is True
    assert ct["startup"] == "order=1,up=10" and ct["swap"] == 512
    assert ct["nics"][0]["mac"] == "bc:24:11:cc:60:ea" and ct["nics"][0]["ip"] == "dhcp"
    assert ct["disks"] == [{"name": "rootfs", "volume": "sda:108/vm-108-disk-0.raw",
                            "storage": "sda", "size": "10G"}]
    assert ctx.db.get_snapshots("pve.brk.config", "guest:") == []
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


def test_label_version():
    label = dockhand.VERSION_LABEL
    assert dockhand.label_version({label: "v4.2.0"}) == "v4.2.0"
    assert dockhand.label_version({label: "refs/tags/v4.2.0"}) == "v4.2.0"
    assert dockhand.label_version({}) is None and dockhand.label_version(None) is None


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
    assert snaps["1:beszel"]["version"] == "0.19.0" and snaps["4:uptime-kuma"]["version"] is None
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
        p = request.url.path
        if request.url.host == "10.0.0.6":
            assert p == "/api/" and "authorization" not in request.headers
            return httpx.Response(200, json={"status": "OK", "setup": True,
                                             "version": {"major": 2, "minor": 15, "revision": 1}})
        assert request.headers["authorization"] == "Bearer token"
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
    ctx.db.put_snapshot("dockhand", "1:beszel", {"image": "henrygd/beszel", "version": "0.19.0"})
    ctx.db.put_snapshot("dockhand", "4:teslamate-teslamate-1",
                        {"image": "teslamate/teslamate:latest", "version": "v4.2.0"})
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
    assert rel["NginxProxyManager/nginx-proxy-manager"]["running_version"] == "2.15.1"
    assert rel["NginxProxyManager/nginx-proxy-manager"]["running_source"] == "npm"
    assert rel["teslamate-org/teslamate"]["running_version"] == "v4.2.0"
    assert rel["louislam/uptime-kuma"]["running_version"] == "2.5.3"
    assert rel["Finsys/dockhand"]["latest_tag"] == "1.0.46"
    assert rel["Finsys/dockhand"]["running_version"] == "1.0.46"


def test_image_tag():
    assert github.image_tag("henrygd/beszel:0.19.0") == "0.19.0"
    assert github.image_tag("ghcr.io/x/y:1.2.3") == "1.2.3"
    assert github.image_tag("localhost:5000/x/y") is None
    assert github.image_tag("nginx") is None
    assert github.image_tag(None) is None
    assert github.tag_version("louislam/uptime-kuma:2") == "2"
    assert github.tag_version("jc21/nginx-proxy-manager:latest") is None
    assert github.tag_version("getgrav/grav:php8.3") == "php8.3"


@pytest.mark.asyncio
async def test_npm_version(cfg, secrets):
    def handler(request):
        if request.url.host == "down":
            return httpx.Response(502)
        if request.url.host == "odd":
            return httpx.Response(200, json={"version": {"major": 2}})
        return httpx.Response(200, json={"version": {"major": 2, "minor": 15, "revision": 1}})

    ctx = make_ctx(cfg, secrets, handler)
    assert await github.npm_version(ctx, "http://npm:81") == "2.15.1"
    assert await github.npm_version(ctx, "http://down") is None
    assert await github.npm_version(ctx, "http://odd") is None
    assert await github.resolve_running(ctx, {"dockhand": {"env": 1, "container": "x"}}) == (
        None, "dockhand env 1")


def test_attention_rules(cfg):
    db = Database(":memory:")
    prev = {}
    details = {d["system"]: d for d in json.loads(fixture("beszel_system_details.json"))["items"]}
    snaps, _ = beszel.parse_systems(json.loads(fixture("beszel_systems.json"))["items"], details,
                                    prev, NOW - 600)
    db.put_snapshots("beszel", snaps, ts=NOW - 600)
    monitors, _ = kuma.parse_metrics(fixture("kuma_metrics.txt"))
    db.put_snapshots("kuma", {f"monitor:{k}": v for k, v in monitors.items()})
    snaps, _ = pve.parse_resources("home", json.loads(fixture("pve_resources.json"))["data"],
                                   json.loads(fixture("pve_status.json"))["data"], NOW)
    db.put_snapshots("pve.home", snaps)
    db.put_snapshot("dockhand.updates", "updates:1",
                    {"env": 1, "host": "dellpi", "items": [{"name": "beszel"}]})
    db.upsert_speedtests("home", [(i, NOW - i * 60, None, None, None,
                                   "failed" if i < 6 else "completed", None)
                                  for i in range(1, 11)])
    db.upsert_speedtests("brk", [(i, NOW - i * 60, None, None, None,
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
    assert ("speedtest", "home") in keys and "50%" in items[("speedtest", "home")]["title"]
    assert ("speedtest", "brk") not in keys
    assert ("github", "o/r#1") in keys
    assert ("source", "kuma") in keys and ("source", "beszel") not in keys
    # nas is down but inside the window only counts; outside it is expected
    from app.clock import in_window
    assert (("beszel", "nas") in keys) == in_window(cfg.nas_window, NOW)


def test_pfsense_status():
    text = fixture("pfsense_status.txt")
    box, samples = pfsense.parse_status("home", text, None, None, NOW)
    assert box["version"] == "26.07-RELEASE" and box["uptime"] == 1_000_000
    assert box["temp"] == 43.0 and box["load"] == [0.2, 0.18, 0.15] and box["mem_pct"] == 27.5
    assert box["states"] == 12345 and box["state_limit"] == 400000
    assert box["wan_if"] == "ix3" and box["wan_in_bytes"] == 1580000000000
    assert box["wan_out_bytes"] == 2740000000000 and box["unbound_uptime"] == 86400
    assert {s[0] for s in samples} == {"pfsense.home.states", "pfsense.home.temp",
                                       "pfsense.home.mem_pct", "pfsense.home.load"}
    prev = {**box, "wan_in_bytes": box["wan_in_bytes"] - 37_500_000,
            "wan_out_bytes": box["wan_out_bytes"] - 75_000_000}
    _, samples = pfsense.parse_status("home", text, prev, NOW - 300, NOW)
    by = {s[0]: s[2] for s in samples}
    assert by["pfsense.home.wan_in"] == 1.0 and by["pfsense.home.wan_out"] == 2.0
    assert "pfsense.home.unbound_restart" not in by
    _, samples = pfsense.parse_status("home", text.replace("86400", "100"), prev, NOW - 300, NOW)
    assert ("pfsense.home.unbound_restart", NOW, 1.0) in samples
    _, samples = pfsense.parse_status("home", text, {**prev, "wan_in_bytes": 9e15}, NOW - 300, NOW)
    assert "pfsense.home.wan_in" not in {s[0] for s in samples}
    bare, samples = pfsense.parse_status("home", "== version\n", None, None, NOW)
    assert bare["states"] is None and bare["uptime"] is None and bare["load"] == []
    assert samples == []


def test_pfsense_dhcp(cfg):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    data = json.loads(fixture("pfsense_dhcp.json"))
    snaps = pfsense.parse_dhcp(data, cfg.pfsense["home"])
    lease = snaps["lease:10.0.0.6"]
    assert lease["kind"] == "static" and lease["online"] is True
    assert lease["descr"] == "Docker host" and lease["starts"] is None and lease["if"] == "lan"
    dyn = snaps["lease:10.0.0.201"]
    assert dyn["kind"] == "dynamic" and dyn["act"] == "active" and dyn["online"] is False
    assert dyn["ends"] == datetime(2026, 9, 11, 20, tzinfo=ZoneInfo("Europe/Stockholm")).timestamp()
    assert "arp:10.0.0.250" not in snaps and "lease:10.0.0.202" not in snaps
    assert snaps["arp:10.0.0.1"]["permanent"] is True and snaps["arp:10.0.0.77"]["expires"] == 300
    assert snaps["arp:10.0.0.181"]["mac"] == "bc:24:11:aa:bb:dd"
    assert snaps["arp:85.229.40.1"]["if"] == "ix3"
    assert snaps["lease:10.0.1.45"]["quiet"] is False
    brk = pfsense.parse_dhcp(data, cfg.pfsense["brk"])
    assert brk["lease:10.0.1.45"]["quiet"] is True and brk["lease:10.0.0.6"]["quiet"] is False
    assert pfsense.parse_dhcp({}, cfg.pfsense["home"]) == {}


def test_pfsense_tailscale():
    peers, me = pfsense.parse_tailscale(json.loads(fixture("pfsense_tailscale.json")))
    assert me["name"] == "pfsense-home" and me["hostname"] == "pfsense"
    assert me["version"].startswith("1.98.5")
    assert me["state"] == "Running" and me["exit_option"] is True
    assert me["routes"] == ["10.0.0.0/24"]
    assert peers["pfsense-brk"]["direct"] is True and peers["pfsense-brk"]["online"] is True
    assert peers["pfsense-brk"]["routes"] == ["10.0.1.0/24"]
    assert peers["m-iphone"]["online"] is False and peers["m-iphone"]["direct"] is False
    assert peers["m-iphone"]["hostname"] == "localhost" and "pfsense" not in peers
    assert peers["m-iphone"]["routes"] == [] and peers["m-iphone"]["relay"] == "sto"
    assert pfsense.parse_tailscale({})[0] == {}


def fake_ssh(host, argv):
    name = {"status": "pfsense_status.txt", "dhcp": "pfsense_dhcp.json",
            "tailscale": "pfsense_tailscale.json"}[argv[0]]
    return ["cat", str(FIXTURES / name)]


@pytest.mark.asyncio
async def test_pfsense_collect(cfg, secrets):
    ctx = make_ctx(cfg, secrets, route({}))
    assert pfsense.build(ctx) == ([], {"pfsense.home": "no ssh key configured",
                                       "pfsense.brk": "no ssh key configured"})
    ctx.ssh_argv = fake_ssh
    built, missing = pfsense.build(ctx)
    assert missing == {}
    assert [s.name for s in built] == ["pfsense.home", "pfsense.home.dhcp", "pfsense.brk",
                                       "pfsense.brk.dhcp"]
    assert all(s.backoff for s in built) and built[1].timeout == 120
    for s in built:
        await s.collect()
    box = ctx.db.get_snapshot("pfsense.home", "box")
    assert box["states"] == 12345 and box["tailscale"]["name"] == "pfsense-home"
    assert ctx.db.get_snapshot("pfsense.home", "peer:m-iphone")["online"] is False
    assert ctx.db.get_snapshot("pfsense.brk.dhcp", "lease:10.0.1.45")["quiet"] is True
    assert ctx.db.get_snapshot("pfsense.home.dhcp", "lease:10.0.1.45")["quiet"] is False
    assert ctx.db.latest_sample("pfsense.home.states")[1] == 12345

    def half(host, argv):
        return fake_ssh(host, argv) if argv[0] == "status" else ["sh", "-c", "exit 1"]

    ctx.ssh_argv = half
    with pytest.raises(sources.SourceError, match="tailscale"):
        await built[0].collect()
    assert ctx.db.get_snapshot("pfsense.home", "box")["states"] == 12345
    assert ctx.db.get_snapshots("pfsense.home", "peer:") == []
    ctx.ssh_argv = lambda host, argv: ["sh", "-c",
                                       "echo 'Permission denied (publickey).' >&2; exit 255"]
    with pytest.raises(sources.SourceError, match=r"exit 255.*Permission denied"):
        await built[0].collect()
    ctx.ssh_argv = lambda host, argv: ["sh", "-c", "echo not json"]
    with pytest.raises(sources.SourceError, match="not JSON"):
        await built[1].collect()


def test_next_delay():
    from app.scheduler import next_delay

    class S(sources.Source):
        interval = 300
        backoff = True

    s = S.__new__(S)
    assert [next_delay(s, n) for n in (0, 1, 2, 3, 4, 40)] == [300, 600, 1200, 2400, 3600, 3600]
    s.backoff = False
    assert next_delay(s, 5) == 300


def test_pfsense_attention(cfg):
    db = Database(":memory:")
    assert attention.compute(db, cfg, NOW) == {}
    data = json.loads(fixture("pfsense_dhcp.json"))
    db.put_snapshots("pfsense.home.dhcp", pfsense.parse_dhcp(data, cfg.pfsense["home"]), ts=NOW)
    db.put_snapshot("pfsense.home", "box", {"states": 350000, "state_limit": 400000})
    db.put_snapshot("pve.home", "guest:108", {"vmid": 108, "name": "adguard", "status": "running"})
    db.put_snapshot("pve.home.config", "guest:108",
                    {"vmid": 108, "nics": [{"name": "eth0", "mac": "bc:24:11:cc:60:ea",
                                            "bridge": "vmbr0", "ip": "dhcp"}]})
    db.put_snapshot("pve.home", "guest:104",
                    {"vmid": 104, "name": "teslamate", "status": "running"})
    db.put_snapshot("pve.home.config", "guest:104",
                    {"vmid": 104, "nics": [{"name": "net0", "mac": "d8:9e:f3:11:22:33"}]})
    db.put_snapshot("pve.home", "guest:904",
                    {"vmid": 904, "name": "testdebug", "status": "stopped"})
    db.put_snapshot("pve.home.config", "guest:904",
                    {"vmid": 904, "nics": [{"name": "net0", "mac": "00:00:00:00:00:01"}]})
    db.put_snapshot("pve.home", "guest:110", {"vmid": 110, "name": "static", "status": "running"})
    db.put_snapshot("pve.home.config", "guest:110",
                    {"vmid": 110, "nics": [{"name": "eth0", "mac": "00:00:00:00:00:02",
                                            "ip": "10.0.0.20/24"}]})
    db.add_samples([("pfsense.home.unbound_restart", NOW - 600, 1.0),
                    ("pfsense.home.unbound_restart", NOW - 7200, 1.0)])
    items = attention.compute(db, cfg, NOW)
    keys = set(items)
    assert ("pfsense.home", "mapping:proxmox") in keys
    assert items[("pfsense.home", "mapping:proxmox")]["detail"].endswith("on pfsense-home")
    assert ("pfsense.home", "mapping:dellpi") not in keys
    assert ("pfsense.home", "mapping:testdebug") not in keys
    assert ("pfsense.home", "mapping:pfsense-home") not in keys
    assert not any(k[0] == "pfsense.brk" for k in keys)
    assert items[("pfsense.home", "mac:10.0.0.181")]["severity"] == "warn"
    assert ("pfsense.home", "mac:10.0.0.6") not in keys
    assert items[("pfsense.home", "guest:home:108:eth0")]["severity"] == "info"
    assert not any(k[1].startswith(("guest:home:104", "guest:home:904", "guest:home:110"))
                   for k in keys)
    assert items[("pfsense.home", "states")]["title"].endswith("88 % full")
    unbound = items[("pfsense.home", "unbound")]
    assert unbound["severity"] == "info" and "restarted 1 time in" in unbound["title"]
    db.add_samples([("pfsense.home.unbound_restart", NOW - 300 * n, 1.0) for n in (1, 2)])
    assert attention.compute(db, cfg, NOW)[("pfsense.home", "unbound")]["severity"] == "warn"

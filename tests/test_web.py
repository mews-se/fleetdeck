import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.conftest import ROOT

SAME = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def client(tmp_path, secrets):
    settings = Settings(
        config=str(ROOT / "config" / "fleetdeck.example.yml"),
        catalog=str(ROOT / "config" / "catalog.example.yml"),
        known_hosts=str(tmp_path / "known_hosts"),
        db=str(tmp_path / "fleetdeck.db"),
        runs=str(tmp_path / "runs"),
        secrets=str(secrets.dir),
        key=str(tmp_path / "key"),
    )
    app = create_app(settings, run_scheduler=False)
    with TestClient(app) as c:
        # commands run locally instead of over ssh
        app.state.console.runner.ssh_argv = lambda host, argv: argv
        yield c


@pytest.mark.parametrize("path", ["/", "/hosts", "/guests", "/containers", "/network",
                                  "/upstream", "/actions"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert 'name="confirm-token"' in r.text
    assert '<script id="data" type="application/json">' in r.text


def test_host_page(client):
    db = client.app.state.console.db
    db.put_snapshot("beszel", "system:dellpi", {"name": "dellpi", "status": "up", "cpu": 12.5,
                                                "mem": 40, "disk": 55, "temp": 45, "uptime": 99,
                                                "kernel": "6.18", "hostname": "dellpi",
                                                "load": [0.1, 0.2, 0.3]})
    t = int(time.time()) - 120
    db.add_samples([("beszel.cpu.dellpi", t, 10.0), ("beszel.cpu.dellpi", t + 60, 12.5),
                    ("beszel.cpu.dellpi", t - 100000, 99.0)])
    db.put_snapshot("kuma", "monitor:7", {"id": 7, "name": "dellpi ssh", "type": "port",
                                          "hostname": "10.0.0.6", "port": "22", "url": None,
                                          "status": 1, "state": "up", "rtt": 3.0})
    db.put_snapshot("kuma", "monitor:8", {"id": 8, "name": "pfsense", "type": "port",
                                          "hostname": "10.0.0.1", "port": "8000", "url": None,
                                          "status": 1, "state": "up", "rtt": 1.0})
    db.put_snapshot("adguard", "stats", {"top_clients": [{"name": "10.0.0.6", "count": 321}]})
    db.put_snapshot("dockhand", "1:grav", {"env": 1, "host": "dellpi", "id": "abc",
                                           "name": "grav", "image": "grav", "state": "running",
                                           "status": "Up"})
    db.put_snapshot("pve.home", "guest:904", {"vmid": 904, "type": "qemu", "name": "testdebug",
                                              "status": "stopped", "maxmem": 4294967296})
    db.put_snapshot("pve.home.config", "guest:904",
                    {"vmid": 904, "type": "qemu", "cores": 2, "memory": 4096, "onboot": False,
                     "nics": [{"name": "net0", "mac": "bc:24:11:8d:69:3c"}], "disks": []})
    r = client.get("/hosts/dellpi")
    assert r.status_code == 200 and "<title>dellpi" in r.text
    assert '"/api/view/host/dellpi"' in r.text
    d = client.get("/api/view/host/dellpi").json()
    assert d["host"]["status"] == "up" and d["system"]["kernel"] == "6.18"
    assert d["series"]["cpu"] == [[t, t + 60], [10.0, 12.5]]
    assert d["series"]["temp"] == [[], []]
    assert [m["id"] for m in d["monitors"]] == [7]
    assert d["dns"]["queries"] == 321
    assert d["containers"]["containers"][0]["name"] == "grav"
    assert {a["id"] for a in d["actions"]} >= {"uptime", "seeda-status", "container-restart"}
    assert all(a["target"] == "dellpi" and len(a["targets"]) == 1 for a in d["actions"])
    assert d["guest"] is None and d["links"]["dockhand"].endswith("env=1")
    g = client.get("/api/view/host/testdebug").json()
    assert g["guest"]["vmid"] == 904 and g["guest"]["status"] == "stopped"
    assert g["guest"]["actions"] == {"start": "start-vm-904", "shutdown": "shutdown-vm-904"}
    assert g["guest"]["config"]["nics"][0]["mac"] == "bc:24:11:8d:69:3c"
    assert g["guest"]["free"] is True and g["containers"]["off"] is True
    p = client.get("/api/view/host/proxmox").json()
    assert p["pve_node"]["id"] == "home" and p["guest"] is None
    assert client.get("/hosts/nope").status_code == 404
    assert client.get("/api/view/host/nope").status_code == 404
    r = client.post("/api/actions/uptime/run", headers=SAME, json={"target": "dellpi"})
    read_stream(client, r.json()["run_id"])
    runs = client.get("/api/view/host/dellpi").json()["runs"]
    assert runs[0]["target"] == "dellpi" and runs[0]["action_id"] == "uptime"
    assert client.get("/api/view/host/testdebug").json()["runs"] == []


def test_page_query_does_not_switch_view(client):
    r = client.get("/hosts?name=guests")
    assert "<title>Hosts" in r.text


@pytest.mark.parametrize("name", ["overview", "hosts", "guests", "containers", "network",
                                  "upstream", "actions"])
def test_view_json(client, name):
    r = client.get(f"/api/view/{name}")
    assert r.status_code == 200
    assert "ts" in r.json()
    assert client.get("/api/view/nope").status_code == 404


def test_health_and_nav(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["unconfigured"] == {}
    n = client.get("/api/nav").json()
    assert set(n["nav"]) == {"overview", "hosts", "guests", "containers", "network",
                             "upstream", "actions"}
    assert n["chips"]["nas"]["text"].startswith("nas ")


def test_post_needs_same_origin(client):
    assert client.post("/api/actions/uptime/run").status_code == 403
    assert client.post("/api/actions/uptime/run",
                       headers={"Origin": "http://evil", "Host": "console"}).status_code == 403
    r = client.post("/api/actions/uptime/run",
                    headers={"Origin": "http://console:8310", "Host": "console:8310"})
    assert r.status_code == 202
    assert client.get("/api/token").status_code == 403


def read_stream(client, run_id):
    lines, exit_code = [], None
    with client.stream("GET", f"/api/actions/runs/{run_id}/stream") as r:
        event = None
        for raw in r.iter_lines():
            if raw.startswith("event: "):
                event = raw[7:]
            elif raw.startswith("data: "):
                data = json.loads(raw[6:])
                if event == "line":
                    lines.append(data)
                elif event == "end":
                    exit_code = data["exit"]
                    break
    return lines, exit_code


def test_free_action_runs_and_streams(client):
    r = client.post("/api/actions/uptime/run", headers=SAME, json={"target": "testdebug"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    lines, exit_code = read_stream(client, run_id)
    assert lines[0].startswith("$ uptime") and exit_code == 0
    page = client.get(f"/actions/runs/{run_id}")
    assert page.status_code == 200 and "Run #" in page.text
    assert client.get("/actions/runs/999").status_code == 404
    runs = client.get("/api/view/actions").json()["runs"]
    assert runs[0]["id"] == run_id and runs[0]["exit_code"] == 0
    assert runs[0]["target"] == "testdebug"
    assert client.get("/api/health").json()["running"] == []
    assert client.post("/api/actions/uptime/run", headers=SAME,
                       json={"target": "nas"}).status_code == 400
    assert client.post("/api/actions/uptime/run", headers=SAME,
                       json={"target": 5}).status_code == 400
    catalog = client.get("/api/view/actions").json()["actions"]
    uptime = next(a for a in catalog if a["id"] == "uptime")
    assert uptime["target_label"].endswith("hosts") and len(uptime["targets"]) > 1


def test_confirm_action_needs_token(client):
    r = client.post("/api/actions/npm-reload/run", headers=SAME, json={})
    assert r.status_code == 403
    token = client.get("/api/token", headers=SAME).json()["token"]
    r = client.post("/api/actions/npm-reload/run", headers=SAME,
                    json={"token": token, "params": {}})
    assert r.status_code == 202
    read_stream(client, r.json()["run_id"])
    r = client.post("/api/actions/npm-reload/run", headers=SAME, json={"token": token})
    assert r.status_code == 403
    html = client.get("/actions").text
    page_token = html.split('name="confirm-token" content="')[1].split('"')[0]
    r = client.post("/api/actions/npm-reload/run", headers=SAME, json={"token": page_token})
    assert r.status_code == 202
    read_stream(client, r.json()["run_id"])


def test_container_rows_carry_their_operations(client):
    db = client.app.state.console.db
    db.put_snapshot("dockhand", "1:grav", {"env": 1, "host": "dellpi", "id": "abc",
                                           "name": "grav", "image": "grav", "state": "running",
                                           "status": "Up"})
    d = client.get("/api/view/containers").json()
    dellpi = next(g for g in d["groups"] if g["host"] == "dellpi")
    ops = {a["op"] for a in dellpi["containers"][0]["actions"]}
    assert ops == {"restart", "update"}
    assert {a["id"] for a in d["actions"]} == {"container-restart", "container-update"}
    r = client.post("/api/actions/container-restart/run", headers=SAME,
                    json={"params": {"container": "-x"}, "target": "dellpi",
                          "token": client.get("/api/token", headers=SAME).json()["token"]})
    assert r.status_code == 400


def test_bad_requests(client):
    assert client.post("/api/actions/nope/run", headers=SAME, json={}).status_code == 404
    r = client.post("/api/actions/seeda-status/run", headers=SAME,
                    json={"params": {"seconds": "x"}})
    assert r.status_code == 400
    r = client.post("/api/actions/seeda-status/run", headers=SAME, json={"params": {"seconds": 5}})
    assert r.status_code == 400
    r = client.post("/api/actions/seeda-status/run",
                    headers={**SAME, "Content-Type": "application/json"}, content=b"nope")
    assert r.status_code == 400
    assert client.get("/api/actions/runs/999/stream").status_code == 404


def test_attention_marks(client):
    db = client.app.state.console.db
    db.sync_attention({("s", "a"): {"severity": "crit", "title": "a", "detail": None},
                       ("s", "b"): {"severity": "info", "title": "b", "detail": None}})
    items = client.get("/api/view/overview").json()["attention"]
    assert [a["ack"] for a in items] == [None, None]
    assert client.get("/api/nav").json()["chips"]["attention"]["text"] == "2 need attention"
    a, b = items[0]["id"], items[1]["id"]
    assert client.post(f"/api/attention/{a}/ack", json={"kind": "read"}).status_code == 403
    assert client.post(f"/api/attention/{a}/ack", headers=SAME,
                       json={"kind": "read"}).status_code == 200
    n = client.get("/api/nav").json()
    assert n["nav"]["overview"] == {"n": 1, "warn": False}
    assert n["chips"]["attention"]["kind"] == "info"
    assert client.post(f"/api/attention/{b}/ack", headers=SAME,
                       json={"kind": "resolved"}).status_code == 200
    n = client.get("/api/nav").json()
    assert n["nav"]["overview"]["n"] == ""
    assert n["chips"]["attention"]["text"] == "nothing new needs attention"
    assert client.get("/api/view/overview").json()["attention"][1]["ack"] == "resolved"
    assert client.post(f"/api/attention/{a}/ack", headers=SAME,
                       json={"kind": "later"}).status_code == 400
    assert client.post("/api/attention/999/ack", headers=SAME,
                       json={"kind": "read"}).status_code == 404
    assert client.post(f"/api/attention/{a}/ack", headers=SAME, json={}).status_code == 200
    assert client.get("/api/nav").json()["nav"]["overview"] == {"n": 1, "warn": True}


def test_release_state():
    from app.views.upstream import release_state
    assert release_state("v2.15.1", "2.15.1") == "current"
    assert release_state("v0.19.1", "0.19.0") == "update"
    assert release_state("v2.15.1", "latest") == "unknown"
    assert release_state("v2.15.1", None) == "unknown"
    assert release_state("2.5", "2.5.3") == "current"


def test_pfsense_in_views(client):
    db = client.app.state.console.db
    n = client.get("/api/view/network").json()
    assert [p["id"] for p in n["pfsense"]] == ["home", "brk"]
    assert n["pfsense"][0]["host"] == "pfsense-home" and n["pfsense"][0]["devices"] == []
    assert client.get("/api/view/host/dellpi").json()["network"] == {"box": "pfsense-home",
                                                                     "device": None}
    db.put_snapshot("pfsense.home.dhcp", "lease:10.0.0.6",
                    {"ip": "10.0.0.6", "mac": "d8:9e:f3:11:22:33", "hostname": "dellpi",
                     "descr": None, "kind": "static", "act": "static", "online": True,
                     "starts": None, "ends": None, "if": "lan", "quiet": False})
    db.put_snapshot("pfsense.home.dhcp", "arp:10.0.0.6",
                    {"ip": "10.0.0.6", "mac": "d8:9e:f3:11:22:34", "if": "igc0",
                     "permanent": False, "expires": 100, "quiet": False})
    db.put_snapshot("pfsense.home.dhcp", "arp:10.0.0.77",
                    {"ip": "10.0.0.77", "mac": "aa:bb:cc:dd:ee:ff", "if": "igc0",
                     "permanent": False, "expires": 100, "quiet": False})
    db.put_snapshot("pfsense.home.dhcp", "arp:85.229.40.1",
                    {"ip": "85.229.40.1", "mac": "00:11:22:33:44:55", "if": "ix3",
                     "permanent": False, "expires": 100, "quiet": False})
    db.put_snapshot("pfsense.home", "box", {"states": 10, "state_limit": 100, "load": [0.1],
                                            "wan_if": "ix3"})
    db.put_snapshot("pfsense.home", "peer:brk", {"name": "pfsense-brk", "online": True})
    db.put_snapshot("pve.home.config", "guest:904",
                    {"vmid": 904, "type": "qemu", "nics": [{"name": "net0",
                                                            "mac": "aa:bb:cc:dd:ee:ff"}],
                     "disks": []})
    d = client.get("/api/view/host/dellpi").json()["network"]
    assert d["device"]["kind"] == "static" and d["device"]["mismatch"] is True
    assert d["device"]["arp_mac"] == "d8:9e:f3:11:22:34" and d["device"]["host"] == "dellpi"
    rows = {h["id"]: h for h in client.get("/api/view/hosts").json()["hosts"]}
    assert rows["dellpi"]["mapping"] == "static" and rows["dellpi"]["mismatch"] is True
    assert rows["proxmox"]["mapping"] is None and rows["brkpi5"]["mac"] is None
    p = client.get("/api/view/network").json()["pfsense"][0]
    assert [x["ip"] for x in p["devices"]] == ["10.0.0.6", "10.0.0.77"]  # not the WAN gateway
    assert p["devices"][1]["kind"] == "arp" and p["devices"][1]["host"] is None
    assert p["counts"] == {"static": 1, "dynamic": 0, "arp": 1, "online": 2, "quiet": 0,
                           "mismatch": 1}
    assert p["peers"][0]["name"] == "pfsense-brk" and p["box"]["states"] == 10
    g = client.get("/api/view/host/testdebug").json()["guest"]
    assert g["config"]["nics"][0]["lease"] == {"ip": "10.0.0.77", "kind": "arp"}
    assert client.get("/network").status_code == 200


def test_pfsense_box_as_host(client):
    db = client.app.state.console.db
    now = int(time.time())
    rows = {h["id"]: h for h in client.get("/api/view/hosts").json()["hosts"]}
    assert rows["pfsense-home"]["status"] == "noagent"
    db.put_snapshot("pfsense.home", "box", {"version": "26.07-RELEASE", "uptime": 1000,
                                            "temp": 44.0, "load": [0.2, 0.1, 0.1],
                                            "mem_pct": 20.7, "states": 700, "state_limit": 395000,
                                            "unbound_uptime": 50, "wan_if": "ix3"}, ts=now - 60)
    db.put_snapshot("pfsense.brk", "box", {"version": "26.07-RELEASE"}, ts=now - 3600)
    db.add_samples([("pfsense.home.states", now - 120, 650.0),
                    ("pfsense.home.states", now - 60, 700.0)])
    d = client.get("/api/view/hosts").json()
    rows = {h["id"]: h for h in d["hosts"]}
    home, brk = rows["pfsense-home"], rows["pfsense-brk"]
    assert home["status"] == "up" and home["agent"] == "pfSense 26.07-RELEASE"
    assert home["temp"] == 44.0 and home["mem"] == 20.7 and home["uptime"] == 1000
    assert home["cpu"] is None and home["disk"] is None
    assert brk["status"] == "down" and brk["down_since"] == now - 3600
    assert d["counts"]["up"] >= 1 and d["counts"]["down"] >= 1
    p = client.get("/api/view/host/pfsense-home").json()
    assert p["host"]["status"] == "up" and p["system"] is None
    assert p["pfsense"]["box"]["states"] == 700 and p["pfsense"]["id"] == "home"
    assert p["pfsense"]["series"]["states"] == [[now - 120, now - 60], [650.0, 700.0]]
    assert client.get("/api/view/host/dellpi").json()["pfsense"] is None
    assert client.get("/hosts/pfsense-home").status_code == 200


def test_other_devices(client):
    db = client.app.state.console.db
    lease = {"descr": None, "kind": "static", "act": "static", "online": True, "starts": None,
             "ends": None, "if": "lan", "quiet": False}
    db.put_snapshot("pfsense.home.dhcp", "lease:10.0.0.6",
                    {**lease, "ip": "10.0.0.6", "mac": "d8:9e:f3:11:22:33", "hostname": "dellpi"})
    db.put_snapshot("pfsense.home.dhcp", "lease:10.0.0.180",
                    {**lease, "ip": "10.0.0.180", "mac": "f0:2f:74:42:36:88",
                     "hostname": "RT-AX56U", "descr": "mesh node", "online": False})
    db.put_snapshot("pfsense.home.dhcp", "arp:10.0.0.77",
                    {"ip": "10.0.0.77", "mac": "aa:bb:cc:dd:ee:ff", "if": "igc0",
                     "permanent": False, "expires": 100, "quiet": False})
    db.put_snapshot("pfsense.brk.dhcp", "lease:10.0.1.45",
                    {**lease, "ip": "10.0.1.45", "mac": "28:70:4e:00:00:45", "hostname": "U6",
                     "quiet": True})
    db.put_snapshot("pfsense.brk.dhcp", "lease:10.0.1.230",
                    {**lease, "ip": "10.0.1.230", "mac": "00:04:13:00:02:30", "hostname": "snom",
                     "kind": "dynamic", "act": "active", "ends": 1_900_000_000})
    groups = {g["site"]: g for g in client.get("/api/view/hosts").json()["devices"]}
    home, brk = groups["Stockzell"], groups["BRK"]
    assert [x["ip"] for x in home["devices"]] == ["10.0.0.180"] and home["online"] == 0
    assert home["box"] == "pfsense-home" and home["quiet"] == 0
    assert [x["ip"] for x in brk["devices"]] == ["10.0.1.230"] and brk["quiet"] == 1
    assert brk["devices"][0]["kind"] == "dynamic" and brk["devices"][0]["ends"] == 1_900_000_000

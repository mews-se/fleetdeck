import json

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
    assert client.post("/api/actions/uptime-904/run").status_code == 403
    assert client.post("/api/actions/uptime-904/run",
                       headers={"Origin": "http://evil", "Host": "console"}).status_code == 403
    r = client.post("/api/actions/uptime-904/run",
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
    r = client.post("/api/actions/uptime-904/run", headers=SAME, json={})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    lines, exit_code = read_stream(client, run_id)
    assert lines[0].startswith("$ uptime") and exit_code == 0
    page = client.get(f"/actions/runs/{run_id}")
    assert page.status_code == 200 and "Run #" in page.text
    assert client.get("/actions/runs/999").status_code == 404
    runs = client.get("/api/view/actions").json()["runs"]
    assert runs[0]["id"] == run_id and runs[0]["exit_code"] == 0
    assert client.get("/api/health").json()["running"] == []


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


def test_release_state():
    from app.views.upstream import release_state
    assert release_state("v2.15.1", "2.15.1") == "current"
    assert release_state("v0.19.1", "0.19.0") == "update"
    assert release_state("v2.15.1", "latest") == "unknown"
    assert release_state("v2.15.1", None) == "unknown"
    assert release_state("2.5", "2.5.3") == "current"

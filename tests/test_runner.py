import asyncio
import json

import httpx
import pytest

from app import catalog, sources
from app.db import Database
from app.events import EventBus
from app.runner import Runner


def local_argv(host, argv):
    return argv


def make_ctx(cfg, secrets, handler=None):
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(404)))
    return sources.Context(
        db=Database(":memory:"),
        config=cfg,
        secrets=secrets,
        http=httpx.AsyncClient(transport=transport),
        http_insecure=httpx.AsyncClient(transport=transport),
    )


async def drain(runner, run_id):
    backlog, q = runner.subscribe(run_id)
    lines = list(backlog)
    if q is not None:
        while True:
            line = await asyncio.wait_for(q.get(), 5)
            if line is None:
                break
            lines.append(line)
    return lines


@pytest.mark.asyncio
async def test_ssh_run_streams_and_logs(cfg, secrets, tmp_path):
    ctx = make_ctx(cfg, secrets)
    actions = catalog.parse([
        {"id": "hello", "title": "Hello", "target": "testdebug", "kind": "ssh",
         "run": ["sh", "-c", "echo hi {who}; echo err >&2; exit 3"],
         "params": {"who": {"pattern": "[a-z]+", "default": "world"}}, "policy": "free"},
    ], cfg)
    events = EventBus()
    q = events.subscribe()
    runner = Runner(ctx, actions, tmp_path / "runs", events, local_argv)
    run_id = await runner.start(actions[0], {"who": "there"}, "test", False)
    lines = await drain(runner, run_id)
    assert lines[0].startswith("$ sh -c")
    assert "hi there" in lines and "err" in lines and lines[-1] == "exit 3"
    row = ctx.db.run(run_id)
    assert row["exit_code"] == 3 and row["output_file"].endswith(f"{run_id}.log")
    assert runner.read_output(run_id) == lines
    assert runner.subscribe(run_id) == (lines, None)
    assert q.get_nowait()[0] == "run"
    assert json.loads(q.get_nowait()[1])["state"] == "finished"


@pytest.mark.asyncio
async def test_bad_param_rejected(cfg, secrets, tmp_path):
    ctx = make_ctx(cfg, secrets)
    action = next(a for a in catalog.load("config/catalog.example.yml", cfg)
                  if a.id == "seeda-status")
    runner = Runner(ctx, [action], tmp_path / "runs", EventBus(), local_argv)
    with pytest.raises(ValueError):
        await runner.start(action, {"seconds": "10; id"}, None, False)
    assert ctx.db.runs() == []


@pytest.mark.asyncio
async def test_runs_serialise_per_target(cfg, secrets, tmp_path):
    ctx = make_ctx(cfg, secrets)
    actions = catalog.parse([
        {"id": "slow", "title": "s", "target": "testdebug", "kind": "ssh",
         "run": ["sh", "-c", "sleep 0.3; echo one"], "policy": "free"},
        {"id": "fast", "title": "f", "target": "testdebug", "kind": "ssh",
         "run": ["echo", "two"], "policy": "free"},
    ], cfg)
    runner = Runner(ctx, actions, tmp_path / "runs", EventBus(), local_argv)
    a = await runner.start(actions[0], None, None, False)
    b = await runner.start(actions[1], None, None, False)
    assert runner.running() == [a, b]
    la, lb = await drain(runner, a), await drain(runner, b)
    assert "one" in la and "two" in lb
    assert "waiting for another run on testdebug" in lb
    assert ctx.db.run(a)["finished_ts"] <= ctx.db.run(b)["finished_ts"]


@pytest.mark.asyncio
async def test_pve_power_operation(cfg, secrets, tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert request.headers["authorization"] == "PVEAPIToken=token"
            return httpx.Response(200, json={"data": "UPID:proxmox:0001:qmstart:904:root@pam:"})
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
        if request.url.path.endswith("/log"):
            return httpx.Response(200, json={"data": [{"n": 1, "t": "starting"},
                                                      {"n": 2, "t": "TASK OK"}]})
        return httpx.Response(404)

    ctx = make_ctx(cfg, secrets, handler)
    action = next(a for a in catalog.load("config/catalog.example.yml", cfg)
                  if a.id == "start-vm-904")
    runner = Runner(ctx, [action], tmp_path / "runs", EventBus(), local_argv)
    run_id = await runner.start(action, None, None, False)
    lines = await drain(runner, run_id)
    assert calls[0] == ("POST", "/api2/json/nodes/proxmox/qemu/904/status/start")
    assert "starting" in lines and lines[-1] == "exit 0"
    assert ctx.db.run(run_id)["exit_code"] == 0


@pytest.mark.asyncio
async def test_pve_error_is_reported(cfg, secrets, tmp_path):
    def handler(request):
        return httpx.Response(403, text="Permission check failed")

    ctx = make_ctx(cfg, secrets, handler)
    action = next(a for a in catalog.load("config/catalog.example.yml", cfg)
                  if a.id == "start-vm-902")
    runner = Runner(ctx, [action], tmp_path / "runs", EventBus(), local_argv)
    run_id = await runner.start(action, None, None, False)
    lines = await drain(runner, run_id)
    assert any("403" in line for line in lines) and lines[-1] == "exit 1"

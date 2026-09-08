"""Runs catalog actions and streams their output.

PVE power operations go through the API token; everything else is an ssh
subprocess with the console key. Runs are serialised per target, every run
gets a row in action_runs and an output file, and late subscribers get the
backlog before the live lines.
"""

import asyncio
import logging
import shlex
from collections.abc import Callable
from pathlib import Path

import httpx

from app.catalog import Action
from app.events import EventBus
from app.sources import Context
from app.sources.pve import PveApi

log = logging.getLogger("fleetdeck.runner")

MAX_OUTPUT = 256 * 1024
PVE_TASK_TIMEOUT = 180
SSH_TIMEOUT = 900
END = None


class Runner:
    def __init__(self, ctx: Context, actions: list[Action], runs_dir: str | Path,
                 events: EventBus, ssh_argv: Callable[[object, list[str]], list[str]]):
        self.ctx = ctx
        self.actions = {a.id: a for a in actions}
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.events = events
        self.ssh_argv = ssh_argv
        self.locks: dict[str, asyncio.Lock] = {}
        self.live: dict[int, list[str]] = {}
        self.subscribers: dict[int, set[asyncio.Queue]] = {}
        self.tasks: set[asyncio.Task] = set()

    def get(self, action_id: str) -> Action | None:
        return self.actions.get(action_id)

    def running(self) -> list[int]:
        return sorted(self.live)

    async def start(self, action: Action, params: dict[str, str] | None,
                    requested_by: str | None, confirmed: bool, target: str | None = None) -> int:
        target = target or action.target
        if target not in action.targets:
            raise ValueError(f"{target} is not a target of {action.id}")
        filled = action.fill(params)
        argv = action.argv(filled) if action.kind == "ssh" else []
        output_file = ""
        run_id = self.ctx.db.new_run(
            action.id, target, action.summary(filled), requested_by, confirmed, output_file
        )
        output_file = str(self.runs_dir / f"{run_id}.log")
        self.ctx.db.set_run_output(run_id, output_file)
        self.live[run_id] = []
        self.subscribers[run_id] = set()
        task = asyncio.create_task(self._execute(run_id, action, target, argv, output_file))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self.events.publish("run", {"id": run_id, "action": action.id, "state": "started"})
        return run_id

    def subscribe(self, run_id: int) -> tuple[list[str], asyncio.Queue | None]:
        """Backlog so far and a queue for what follows, or None when the run is over."""
        if run_id not in self.live:
            return self.read_output(run_id), None
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers[run_id].add(q)
        return list(self.live[run_id]), q

    def read_output(self, run_id: int) -> list[str]:
        row = self.ctx.db.run(run_id)
        if not row or not row.get("output_file"):
            return []
        try:
            return Path(row["output_file"]).read_text().splitlines()
        except OSError:
            return []

    async def _execute(self, run_id: int, action: Action, target: str, argv: list[str],
                       output_file: str):
        lock = self.locks.setdefault(target, asyncio.Lock())
        code = 1
        written = 0
        f = open(output_file, "w")

        def emit(line: str | None):
            nonlocal written
            if line is END:
                f.close()
                for q in self.subscribers.get(run_id, ()):
                    q.put_nowait(END)
                return
            if written < MAX_OUTPUT:
                f.write(line + "\n")
                f.flush()
                written += len(line) + 1
                if written >= MAX_OUTPUT:
                    f.write("[output truncated]\n")
            self.live[run_id].append(line)
            for q in self.subscribers.get(run_id, ()):
                q.put_nowait(line)

        try:
            if lock.locked():
                emit(f"waiting for another run on {target}")
            async with lock:
                if action.kind == "pve":
                    code = await self._run_pve(action, emit)
                else:
                    code = await self._run_ssh(target, argv, emit)
        except asyncio.CancelledError:
            emit("cancelled")
            code = 130
            raise
        except Exception as e:
            log.exception("run %s failed", run_id)
            emit(f"error: {e}")
            code = 255
        finally:
            emit(f"exit {code}")
            self.ctx.db.finish_run(run_id, code)
            emit(END)
            self.live.pop(run_id, None)
            self.subscribers.pop(run_id, None)
            self.events.publish(
                "run", {"id": run_id, "action": action.id, "state": "finished", "exit": code}
            )

    async def _run_ssh(self, target: str, argv: list[str], emit) -> int:
        host = self.ctx.config.hosts[target]
        cmd = self.ssh_argv(host, argv)
        emit(f"$ {shlex.join(argv)}  # {host.ssh}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(SSH_TIMEOUT):
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    emit(raw.decode("utf-8", "replace").rstrip("\r\n"))
                return await proc.wait()
        except TimeoutError:
            proc.kill()
            emit(f"killed after {SSH_TIMEOUT} s")
            return 124

    async def _run_pve(self, action: Action, emit) -> int:
        host = self.ctx.config.hosts[action.target]
        pve = self.ctx.config.pve[host.pve]
        api = PveApi(self.ctx, pve)
        run = action.run
        path = f"/nodes/{pve.node}/{run['type']}/{run['vmid']}/status/{run['op']}"
        emit(f"POST {pve.url}/api2/json{path}")
        try:
            upid = await api.post(path)
        except httpx.HTTPStatusError as e:
            emit(f"{e.response.status_code}: {e.response.text.strip()[:300]}")
            return 1
        emit(f"task {upid}")
        for _ in range(PVE_TASK_TIMEOUT // 2):
            await asyncio.sleep(2)
            status = await api.get(f"/nodes/{pve.node}/tasks/{upid}/status") or {}
            if status.get("status") == "stopped":
                for entry in await api.get(f"/nodes/{pve.node}/tasks/{upid}/log", limit=50) or []:
                    text = entry.get("t")
                    if text and text != "TASK OK":
                        emit(text)
                exit_status = status.get("exitstatus") or ""
                emit(f"task {exit_status}")
                return 0 if exit_status == "OK" else 1
        emit("timed out waiting for the task")
        return 124

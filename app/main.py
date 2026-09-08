"""The web layer: pages, one JSON endpoint per page, actions and two event streams.

There is no login. The console is reachable only on the LAN and through the
Tailscale subnet routes, so the guard on every POST is same-origin: the
browser's Sec-Fetch-Site header or a matching Origin. Confirm actions also
echo a one-time token issued with the page.
"""

import asyncio
import json
import logging
import secrets as secretmod
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__, catalog, views
from app import config as configmod
from app.clock import window_state
from app.db import Database
from app.events import EventBus
from app.runner import Runner
from app.scheduler import Scheduler
from app.settings import Settings
from app.sources import build_sources, make_context
from app.ssh import ssh_argv
from app.views import host as hostview

log = logging.getLogger("fleetdeck")

ROOT = Path(__file__).resolve().parent.parent
PAGES = [
    ("overview", "Overview", "/"),
    ("hosts", "Hosts", "/hosts"),
    ("guests", "Guests", "/guests"),
    ("containers", "Containers", "/containers"),
    ("network", "Network", "/network"),
    ("upstream", "Upstream", "/upstream"),
    ("actions", "Actions", "/actions"),
]
TOKEN_TTL = 900
HEARTBEAT = 15


class Console:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tokens: dict[str, float] = {}
        self.events = EventBus()

    async def start(self, run_scheduler: bool = True):
        s = self.settings
        self.config = configmod.load(s.config)
        self.secrets = configmod.Secrets(s.secrets)
        if Path(s.catalog).exists():
            self.actions = catalog.load(s.catalog, self.config)
        else:
            log.warning("%s not found, the catalog is empty", s.catalog)
            self.actions = []
        for name in configmod.missing_secrets(self.config, self.secrets):
            log.warning("secret %s is missing in %s", name, s.secrets)
        self.db = Database(s.db)
        self.ctx = make_context(self.db, self.config, self.secrets)
        sources, self.unconfigured = build_sources(self.ctx)
        self.scheduler = Scheduler(self.ctx, sources, self.events)
        self.runner = Runner(
            self.ctx, self.actions, s.runs, self.events,
            lambda host, argv: ssh_argv(host, argv, s.key, s.known_hosts),
        )
        if run_scheduler:
            self.scheduler.start()
        log.info("fleetdeck %s: %d hosts, %d sources, %d actions",
                 __version__, len(self.config.hosts), len(sources), len(self.actions))

    async def stop(self):
        await self.scheduler.stop()
        await self.ctx.aclose()
        self.db.close()

    def state(self) -> views.State:
        return views.State(
            db=self.db,
            config=self.config,
            actions=self.actions,
            unconfigured=self.unconfigured,
            running=self.runner.running(),
            version=__version__,
        )

    def issue_token(self) -> str:
        now = time.monotonic()
        self.tokens = {t: exp for t, exp in self.tokens.items() if exp > now}
        token = secretmod.token_urlsafe(18)
        self.tokens[token] = now + TOKEN_TTL
        return token

    def consume_token(self, token) -> bool:
        exp = self.tokens.pop(token, None) if isinstance(token, str) else None
        return exp is not None and exp > time.monotonic()

    def chips(self, state: views.State) -> dict:
        nas = window_state(self.config.nas_window, state.now)
        nas_host = next((h for h in self.config.hosts.values() if h.window == "nas"), None)
        chips = {}
        if nas and nas_host:
            status = (self.db.get_snapshot("beszel", f"system:{nas_host.beszel}") or {}).get(
                "status") if nas_host.beszel else None
            if nas["on"]:
                text = f"{nas_host.id} on · closes {nas['end']}"
                kind = "good" if status in (None, "up") else "warn"
            else:
                text = f"{nas_host.id} off · opens {nas['start']}"
                kind = "off"
            chips["nas"] = {"text": text, "kind": kind}
        attention = self.db.open_attention()
        worst = next((a["severity"] for a in attention), None)
        chips["attention"] = {
            "text": f"{len(attention)} need attention" if attention else "nothing needs attention",
            "kind": {"crit": "crit", "warn": "warn", "info": "info"}.get(worst, "good"),
        }
        sources = self.db.source_status()
        failing = [n for n, r in sources.items() if not r["ok"] and not r["skipped"]]
        if failing:
            chips["sources"] = {"text": f"{len(failing)} source{'s' if len(failing) > 1 else ''}"
                                        f" failing", "kind": "warn"}
        elif self.unconfigured:
            chips["sources"] = {"text": f"{len(self.unconfigured)} sources not configured",
                                "kind": "off"}
        else:
            chips["sources"] = {"text": f"{len(sources)} sources OK", "kind": "good"}
        return chips


def same_origin(request: Request):
    if request.headers.get("sec-fetch-site") == "same-origin":
        return
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if origin and host and origin.split("://", 1)[-1] == host:
        return
    raise HTTPException(status_code=403, detail="cross-site request refused")


def sse(gen):
    async def body():
        async for event, data in gen:
            if event is None:
                yield ": ping\n\n"
            else:
                yield f"event: {event}\ndata: {data}\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def create_app(settings: Settings | None = None, run_scheduler: bool = True) -> FastAPI:
    settings = settings or Settings()
    console = Console(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await console.start(run_scheduler)
        try:
            yield
        finally:
            await console.stop()

    app = FastAPI(title="fleetdeck", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.console = console
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    def page(request: Request, name: str, title: str, template: str, data: dict,
             extra: dict | None = None):
        state = console.state()
        context = {
            "page": name,
            "title": title,
            "app_title": console.config.title,
            "pages": PAGES,
            "nav": views.nav(state),
            "chips": console.chips(state),
            "data": data,
            "token": console.issue_token(),
            "version": __version__,
            "sites": " · ".join(s.name for s in console.config.sites.values()),
            "links": console.config.links,
            "unconfigured": console.unconfigured,
        }
        context.update(extra or {})
        return templates.TemplateResponse(request, template, context)

    def page_route(name: str, title: str):
        def route(request: Request):
            return page(request, name, title, f"{name}.html", views.build(name, console.state()))
        return route

    for name, title, path in PAGES:
        app.get(path, include_in_schema=False)(page_route(name, title))

    @app.get("/hosts/{host_id}", include_in_schema=False)
    def host_page(request: Request, host_id: str):
        if host_id not in console.config.hosts:
            raise HTTPException(status_code=404)
        data = hostview.build(console.state(), host_id)
        return page(request, "hosts", host_id, "host.html", data,
                    {"view": f"/api/view/host/{host_id}"})

    @app.get("/api/view/host/{host_id}")
    def api_host(host_id: str):
        if host_id not in console.config.hosts:
            raise HTTPException(status_code=404)
        return hostview.build(console.state(), host_id)

    @app.get("/actions/runs/{run_id}", include_in_schema=False)
    def run_page(request: Request, run_id: int):
        row = console.db.run(run_id)
        if row is None:
            raise HTTPException(status_code=404)
        live = run_id in console.runner.live
        lines = list(console.runner.live[run_id]) if live else console.runner.read_output(run_id)
        action = console.runner.get(row["action_id"])
        data = {"run": row, "lines": lines, "live": live,
                "title": action.title if action else row["action_id"]}
        return page(request, "actions", f"Run #{run_id}", "run.html", data)

    @app.get("/api/view/{name}")
    def api_view(name: str):
        if name not in {p[0] for p in PAGES}:
            raise HTTPException(status_code=404)
        return views.build(name, console.state())

    @app.get("/api/nav")
    def api_nav():
        state = console.state()
        return {"nav": views.nav(state), "chips": console.chips(state)}

    @app.get("/api/health")
    def api_health():
        sources = console.db.source_status()
        failing = {n: r["error"] for n, r in sources.items() if not r["ok"] and not r["skipped"]}
        return {
            "status": "ok",
            "version": __version__,
            "sources": len(sources),
            "failing": failing,
            "unconfigured": console.unconfigured,
            "running": console.runner.running(),
            "db_bytes": console.db.size_bytes(),
        }

    @app.get("/api/token")
    def api_token(request: Request):
        same_origin(request)
        return {"token": console.issue_token()}

    @app.post("/api/actions/{action_id}/run")
    async def api_run(request: Request, action_id: str):
        same_origin(request)
        action = console.runner.get(action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="no such action")
        try:
            body = await request.json() if await request.body() else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="bad JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="bad JSON")
        params = body.get("params") or {}
        if not isinstance(params, dict) or any(not isinstance(v, str) for v in params.values()):
            raise HTTPException(status_code=400, detail="params must map names to strings")
        target = body.get("target")
        if target is not None and not isinstance(target, str):
            raise HTTPException(status_code=400, detail="target must be a host id")
        confirmed = False
        if action.policy == "confirm":
            if not console.consume_token(body.get("token")):
                raise HTTPException(status_code=403, detail="confirmation token missing or used")
            confirmed = True
        try:
            run_id = await console.runner.start(
                action, params, request.client.host if request.client else None, confirmed,
                target,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return JSONResponse({"run_id": run_id}, status_code=202)

    @app.get("/api/actions/runs/{run_id}/stream")
    def api_run_stream(run_id: int):
        row = console.db.run(run_id)
        if row is None:
            raise HTTPException(status_code=404)
        backlog, queue = console.runner.subscribe(run_id)

        async def gen():
            for line in backlog:
                yield "line", json.dumps(line)
            if queue is not None:
                while True:
                    try:
                        line = await asyncio.wait_for(queue.get(), HEARTBEAT)
                    except TimeoutError:
                        yield None, ""
                        continue
                    if line is None:
                        break
                    yield "line", json.dumps(line)
            final = console.db.run(run_id) or row
            yield "end", json.dumps({"exit": final.get("exit_code")})

        return sse(gen())

    @app.get("/api/events")
    def api_events():
        queue = console.events.subscribe()

        async def gen():
            try:
                while True:
                    try:
                        event, data = await asyncio.wait_for(queue.get(), HEARTBEAT)
                    except TimeoutError:
                        yield None, ""
                        continue
                    yield event, data
            finally:
                console.events.unsubscribe(queue)

        return sse(gen())

    return app

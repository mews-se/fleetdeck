"""Collectors. Each source has one interval and writes snapshots and samples."""

import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app import __version__
from app.config import Config, Secrets
from app.db import Database


class Skip(Exception):
    """Raised by a source whose tick falls outside its window."""


class SourceError(Exception):
    """A collect() that partly failed; what succeeded is already stored."""


@dataclass
class Context:
    db: Database
    config: Config
    secrets: Secrets
    http: httpx.AsyncClient
    http_insecure: httpx.AsyncClient
    ssh_argv: Callable[[object, list[str]], list[str]] | None = None

    @staticmethod
    def now() -> int:
        return int(time.time())

    def client(self, verify_tls: bool) -> httpx.AsyncClient:
        return self.http if verify_tls else self.http_insecure

    async def aclose(self):
        await self.http.aclose()
        await self.http_insecure.aclose()


def make_context(db: Database, config: Config, secrets: Secrets,
                 ssh_argv: Callable[[object, list[str]], list[str]] | None = None) -> Context:
    timeout = httpx.Timeout(30.0, connect=10.0)
    headers = {"User-Agent": f"fleetdeck/{__version__}"}
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    return Context(
        db=db,
        config=config,
        secrets=secrets,
        http=httpx.AsyncClient(timeout=timeout, headers=headers),
        http_insecure=httpx.AsyncClient(timeout=timeout, headers=headers, verify=insecure),
        ssh_argv=ssh_argv,
    )


class Source:
    name: str
    interval: int
    timeout: int = 60
    # a failing tick doubles the wait, for targets that lock out repeat offenders
    backoff: bool = False

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def collect(self) -> None:
        raise NotImplementedError


def build_sources(ctx: Context) -> tuple[list[Source], dict[str, str]]:
    """All configured sources, plus the names left out and why."""
    from app.sources import adguard, beszel, dockhand, github, kuma, pfsense, pve, speedtest

    sources: list[Source] = []
    unconfigured: dict[str, str] = {}
    for module in (beszel, pve, dockhand, kuma, adguard, speedtest, github, pfsense):
        built, missing = module.build(ctx)
        sources.extend(built)
        unconfigured.update(missing)
    return sources, unconfigured

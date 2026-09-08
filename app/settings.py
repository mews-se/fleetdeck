"""Paths and ports, all from the environment with the container layout as default."""

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FLEETDECK_{name}", default)


@dataclass
class Settings:
    config: str = _env("CONFIG", "/config/fleetdeck.yml")
    catalog: str = _env("CATALOG", "/config/catalog.yml")
    known_hosts: str = _env("KNOWN_HOSTS", "/config/known_hosts")
    db: str = _env("DB", "/data/fleetdeck.db")
    runs: str = _env("RUNS", "/data/runs")
    secrets: str = _env("SECRETS", "/secrets")
    key: str = _env("SSH_IDENTITY", "/keys/fleetdeck_ed25519")
    bind: str = _env("BIND", "0.0.0.0")
    port: int = int(_env("PORT", "8310"))

from pathlib import Path

import pytest

from app import catalog, config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg():
    return config.load(ROOT / "config" / "fleetdeck.example.yml")


@pytest.fixture
def actions(cfg):
    return catalog.load(ROOT / "config" / "catalog.example.yml", cfg)


@pytest.fixture
def secrets(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir()
    for name in (
        "pve-home.token", "pve-brk.token", "beszel.auth", "dockhand.token", "kuma.key",
        "adguard.auth", "speedtest-home.token", "speedtest-brk.token", "github.token",
    ):
        (d / name).write_text("user:secret\n" if name.endswith(".auth") else "token\n")
    return config.Secrets(d)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()

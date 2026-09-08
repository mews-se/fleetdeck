import copy

import pytest
import yaml

from app import config
from tests.conftest import ROOT


def base():
    with open(ROOT / "config" / "fleetdeck.example.yml") as f:
        return yaml.safe_load(f)


def test_example_loads(cfg):
    assert cfg.hosts["dellpi"].dockhand_env == 1
    assert cfg.hosts["teslamate"].guest.vmid == 104
    assert cfg.pve["home"].free_guests == [904, 905, 990, 991]
    assert cfg.speedtests["brk"].site == "brk"
    assert cfg.host_by_beszel("prox").id == "proxmox"
    assert cfg.host_by_guest("brk", 902).id == "testdebugbrk"
    assert cfg.host_by_env(5).id == "dietpibrk"
    assert cfg.pve_host("home").id == "proxmox"
    assert cfg.nas_window.start == "15:00"


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["hosts"]["dellpi"].update(site="nowhere"), "unknown site"),
        (lambda d: d["hosts"]["dellpi"].update(rule="admin"), "rule must be"),
        (lambda d: d["hosts"]["dellpi"].update(ssh="dellpi"), "user@host"),
        (lambda d: d["hosts"]["dellpi"].update(pve="nope"), "unknown pve"),
        (lambda d: d["hosts"]["dellpi"].update(guest={"pve": "home"}), "vmid is required"),
        (lambda d: d["hosts"]["dellpi"].update(window="night"), "window must be"),
        (lambda d: d["hosts"].update({"Bad Host": {"site": "home", "ip": "1", "rule": "test"}}),
         "not a valid id"),
        (lambda d: d["pve"]["home"].update(free_guests=["904"]), "free_guests"),
        (lambda d: d["pve"]["home"].update(url="10.0.0.99:8006"), "url must start"),
        (lambda d: d["pve"].update({"spare": {"url": "https://x", "node": "n", "secret": "s"}}),
         "no host carries"),
        (lambda d: d["sources"].update({"pihole": {"url": "http://x"}}), "unknown source"),
        (lambda d: d["sources"]["speedtest"]["home"].pop("site"), "site is required"),
        (lambda d: d["github"]["watch_threads"][0].update(repo="domain-monitor"), "owner/name"),
        (lambda d: d["github"]["watch_threads"][0].update(numbers=[]), "numbers must be"),
        (lambda d: d["github"]["watch_releases"][0].update(running_from={"beszel_os": "x"}),
         "unknown kind"),
        (lambda d: d["links"].update(bad="ftp://x"), "must be an http"),
        (lambda d: d["nas_window"].update(start="25:00"), "HH:MM"),
        (lambda d: d.pop("hosts"), "at least one host"),
    ],
)
def test_rejects(mutate, message):
    data = base()
    mutate(data)
    with pytest.raises(config.ConfigError, match=message):
        config.parse(data)


def test_missing_secrets(cfg, tmp_path):
    secrets = config.Secrets(tmp_path)
    assert "github.token" in config.missing_secrets(cfg, secrets)
    (tmp_path / "github.token").write_text("x\n")
    assert "github.token" not in config.missing_secrets(cfg, secrets)
    assert secrets.get("github.token") == "x"
    assert secrets.get("nope") is None


def test_yaml_errors(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("sites: [")
    with pytest.raises(config.ConfigError):
        config.load(p)
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path / "missing.yml")


def test_copy_is_independent():
    d = base()
    e = copy.deepcopy(d)
    e["hosts"].pop("dellpi")
    assert "dellpi" in d["hosts"]

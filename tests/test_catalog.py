import pytest

from app import catalog
from app.config import ConfigError


def entry(**kw):
    base = {
        "id": "x",
        "title": "X",
        "target": "testdebug",
        "kind": "ssh",
        "run": ["uptime"],
        "policy": "free",
    }
    base.update(kw)
    return base


def test_example_loads(actions):
    ids = [a.id for a in actions]
    assert "start-vm-904" in ids and "seeda-status" in ids
    seeda = next(a for a in actions if a.id == "seeda-status")
    assert seeda.argv() == ["/home/dietpi/seeda-rate.sh", "10"]
    assert seeda.argv({"seconds": "30"}) == ["/home/dietpi/seeda-rate.sh", "30"]
    with pytest.raises(ValueError):
        seeda.argv({"seconds": "10; rm -rf /"})
    start = next(a for a in actions if a.id == "start-vm-904")
    assert start.summary() == "start qemu 904"


@pytest.mark.parametrize(
    "kw, message",
    [
        (dict(target="pfsense-home", policy="confirm"), "read-only"),
        (dict(target="dellpi", policy="free"), "production"),
        (dict(target="nowhere"), "unknown target"),
        (dict(policy="sudo"), "policy must be"),
        (dict(kind="pve", run={"vmid": 904, "type": "qemu", "op": "start"}), "not a PVE host"),
        (dict(target="proxmox", policy="confirm", kind="pve",
              run={"vmid": 104, "type": "qemu", "op": "start"}), "free_guests"),
        (dict(target="proxmox", policy="confirm", kind="pve",
              run={"vmid": 904, "type": "qemu", "op": "destroy"}), "run.op"),
        (dict(target="proxmox", policy="read", kind="pve",
              run={"vmid": 904, "type": "qemu", "op": "start"}), "policy: read"),
        (dict(run=["rm", "-rf", "/tmp/x"]), "forbidden"),
        (dict(run=["sh", "-c", "dd if=/dev/zero of=/dev/sda"]), "forbidden"),
        (dict(run=["qm", "destroy", "904"]), "forbidden"),
        (dict(run=["vzdump", "904", "--remove", "1"]), "forbidden"),
        (dict(run=["echo", "{name}"]), "no params entry"),
        (dict(run=["echo"], params={"name": {"pattern": "a+", "default": "a"}}), "not used"),
        (dict(run=["echo", "{n}"], params={"n": {"pattern": "[0-9]+", "default": "x"}}),
         "default must be"),
        (dict(run=["echo", "{n}"], params={"n": {"pattern": "(", "default": "x"}}), "bad pattern"),
        (dict(run="uptime"), "non-empty list"),
        (dict(kind="docker"), "kind must be"),
        (dict(target="nas"), "read-only"),
    ],
)
def test_rejects(cfg, kw, message):
    with pytest.raises(ConfigError, match=message):
        catalog.parse([entry(**kw)], cfg)


def test_duplicate_ids(cfg):
    with pytest.raises(ConfigError, match="duplicate"):
        catalog.parse([entry(), entry()], cfg)


def test_empty_catalog(cfg):
    assert catalog.parse(None, cfg) == []

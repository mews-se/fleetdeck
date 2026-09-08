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
    return {k: v for k, v in base.items() if v is not None}


def test_example_loads(actions):
    ids = [a.id for a in actions]
    assert "start-vm-904" in ids and "seeda-status" in ids
    seeda = next(a for a in actions if a.id == "seeda-status")
    assert seeda.argv()[-1].startswith("timeout 10 /home/dietpi/seeda-rate.sh")
    assert seeda.argv({"seconds": "30"})[-1].startswith("timeout 30 ")
    with pytest.raises(ValueError):
        seeda.argv({"seconds": "10; rm -rf /"})
    start = next(a for a in actions if a.id == "start-vm-904")
    assert start.summary() == "start qemu 904" and start.targets == ["proxmox"]
    uptime = next(a for a in actions if a.id == "uptime")
    assert uptime.target == "dellpi" and "testdebugbrk" in uptime.targets
    assert seeda.summary({"seconds": "5"}).startswith("sh -c timeout 5 ")


def test_targets(cfg):
    a = catalog.parse([entry(target=None, targets=["testdebug", "dellpi"], policy="read")],
                      cfg)[0]
    assert a.targets == ["testdebug", "dellpi"] and a.target == "testdebug"
    with pytest.raises(ConfigError, match="not both"):
        catalog.parse([entry(targets=["testdebug"])], cfg)
    with pytest.raises(ConfigError, match="listed twice"):
        catalog.parse([entry(target=None, targets=["testdebug", "testdebug"])], cfg)
    with pytest.raises(ConfigError, match="unknown target"):
        catalog.parse([entry(target=None, targets=["testdebug", "nowhere"])], cfg)
    with pytest.raises(ConfigError, match="production"):
        catalog.parse([entry(target=None, targets=["testdebug", "dellpi"])], cfg)
    with pytest.raises(ConfigError, match="read-only"):
        catalog.parse([entry(target=None, targets=["testdebug", "nas"])], cfg)
    with pytest.raises(ConfigError, match="no ssh address"):
        catalog.parse([entry(target=None, targets=["testdebug", "nas"], policy="read")], cfg)
    a = catalog.parse([entry(target=None, targets=["testdebug", "pfsense-home"],
                             policy="read")], cfg)[0]
    assert a.targets == ["testdebug", "pfsense-home"]
    with pytest.raises(ConfigError, match="one target"):
        catalog.parse([entry(target=None, targets=["proxmox", "proxbrk"], policy="confirm",
                             kind="pve", run={"vmid": 904, "type": "qemu", "op": "start"})],
                      cfg)


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


def dh(**kw):
    base = dict(target="dellpi", kind="dockhand", policy="confirm",
                run={"op": "restart", "container": "{container}"},
                params={"container": {"pattern": "[a-z0-9_.-]+", "default": "grav"}})
    base.update(kw)
    return entry(**base)


def test_dockhand_kind(cfg):
    a = catalog.parse([dh()], cfg)[0]
    assert a.kind == "dockhand" and a.container({"container": "beszel"}) == "beszel"
    assert a.summary() == "restart container {container}"
    assert a.summary({"container": "beszel"}) == "restart container beszel"
    with pytest.raises(ValueError):
        a.container({"container": "-bad"})
    fixed = catalog.parse([dh(run={"op": "update", "container": "grav"}, params=None)], cfg)[0]
    assert fixed.container() == "grav" and fixed.summary() == "update container grav"


@pytest.mark.parametrize(
    "kw, message",
    [
        (dict(policy="read"), "policy: read"),
        (dict(policy="free"), "production"),
        (dict(target="testpi5"), "not a Dockhand environment"),
        (dict(run={"op": "exec", "container": "grav"}, params=None), "run.op"),
        (dict(run={"op": "restart"}, params=None), "run.container"),
        (dict(run=["docker", "restart"], params=None), "run must be a mapping"),
        (dict(params=None), "no params entry"),
    ],
)
def test_dockhand_rejects(cfg, kw, message):
    with pytest.raises(ConfigError, match=message):
        catalog.parse([dh(**kw)], cfg)


def test_duplicate_ids(cfg):
    with pytest.raises(ConfigError, match="duplicate"):
        catalog.parse([entry(), entry()], cfg)


def test_empty_catalog(cfg):
    assert catalog.parse(None, cfg) == []

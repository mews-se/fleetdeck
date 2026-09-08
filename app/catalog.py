"""The action catalog: an allowlist of what the console may run.

Every rule here is enforced at load time so that a wrong entry stops the app
rather than running. The PVE token ACL and the ssh key restrictions on the
hosts are the second line of defence, never the first.
"""

import os
import re
from dataclasses import dataclass, field

import yaml

from app.config import ID_RE, Config, ConfigError, Host

POLICIES = ("read", "free", "confirm")
PVE_OPS = ("start", "shutdown", "stop", "reboot")
PVE_TYPES = ("qemu", "lxc")
DOCKHAND_OPS = ("start", "stop", "restart", "update")
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Never a catalog operation, whatever the policy says.
FORBIDDEN = [
    re.compile(p)
    for p in (
        r"(^|[\s;|&])rm\s",
        r"(^|[\s;|&])dd\s",
        r"(^|[\s;|&])mkfs",
        r"(^|[\s;|&])qm\s+(destroy|create|clone)\b",
        r"(^|[\s;|&])pct\s+(destroy|create|clone)\b",
        r"vzdump\b.*--remove",
    )
]


@dataclass
class Param:
    name: str
    pattern: re.Pattern
    default: str


@dataclass
class Action:
    id: str
    title: str
    targets: list[str]
    kind: str
    policy: str
    run: list[str] | dict
    params: dict[str, Param] = field(default_factory=dict)
    note: str = ""

    @property
    def target(self) -> str:
        return self.targets[0]

    def fill(self, values: dict[str, str] | None = None) -> dict[str, str]:
        """Every parameter with its value, each checked against its pattern."""
        values = values or {}
        filled = {}
        for name, p in self.params.items():
            value = values.get(name, p.default)
            if not isinstance(value, str) or not p.pattern.fullmatch(value):
                raise ValueError(f"parameter {name} does not match its pattern")
            filled[name] = value
        return filled

    def argv(self, values: dict[str, str] | None = None) -> list[str]:
        filled = self.fill(values)
        return [PLACEHOLDER_RE.sub(lambda m: filled[m.group(1)], arg) for arg in self.run]

    def container(self, values: dict[str, str] | None = None) -> str:
        filled = self.fill(values)
        name = PLACEHOLDER_RE.sub(lambda m: filled[m.group(1)], self.run["container"])
        if not CONTAINER_RE.fullmatch(name):
            raise ValueError(f"{name} is not a container name")
        return name

    def summary(self, values: dict[str, str] | None = None) -> str:
        if self.kind == "pve":
            return f"{self.run['op']} {self.run['type']} {self.run['vmid']}"
        if self.kind == "dockhand":
            name = self.container(values) if values is not None else self.run["container"]
            return f"{self.run['op']} container {name}"
        return " ".join(self.argv(values) if values is not None else self.run)


def _targets(raw: dict, where: str, config: Config) -> list[Host]:
    if "target" in raw and "targets" in raw:
        raise ConfigError(f"{where}: give target or targets, not both")
    targets = raw.get("targets", [raw.get("target")])
    if (
        not isinstance(targets, list)
        or not targets
        or any(not isinstance(t, str) for t in targets)
    ):
        raise ConfigError(f"{where}: targets must be a non-empty list of host ids")
    if len(set(targets)) != len(targets):
        raise ConfigError(f"{where}: a target is listed twice")
    hosts = []
    for t in targets:
        host = config.hosts.get(t)
        if host is None:
            raise ConfigError(f"{where}: unknown target '{t}'")
        hosts.append(host)
    return hosts


def _params(raw: dict, where: str, used: set[str]) -> dict[str, Param]:
    params = {}
    for name, p in (raw.get("params") or {}).items():
        pw = f"{where}.params.{name}"
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", str(name)):
            raise ConfigError(f"{pw}: bad parameter name")
        if not isinstance(p, dict):
            raise ConfigError(f"{pw}: expected pattern and default")
        try:
            pattern = re.compile(str(p.get("pattern", "")))
        except re.error as e:
            raise ConfigError(f"{pw}: bad pattern ({e})") from None
        default = p.get("default")
        if not isinstance(default, str) or not pattern.fullmatch(default):
            raise ConfigError(f"{pw}: default must be a string matching the pattern")
        params[name] = Param(name, pattern, default)
    for name in used - set(params):
        raise ConfigError(f"{where}: placeholder {{{name}}} has no params entry")
    for name in set(params) - used:
        raise ConfigError(f"{where}: params.{name} is not used in run")
    return params


def _parse_action(i: int, raw, config: Config) -> Action:
    where = f"catalog[{i}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping")
    id_ = raw.get("id")
    if not isinstance(id_, str) or not ID_RE.match(id_):
        raise ConfigError(f"{where}: id must be lowercase letters, digits and dashes")
    where = f"catalog[{id_}]"
    title = raw.get("title")
    if not isinstance(title, str) or not title:
        raise ConfigError(f"{where}: title is required")
    policy = raw.get("policy")
    if policy not in POLICIES:
        raise ConfigError(f"{where}: policy must be one of {', '.join(POLICIES)}")
    hosts = _targets(raw, where, config)
    targets = [h.id for h in hosts]
    for host in hosts:
        if host.rule == "readonly" and policy != "read":
            raise ConfigError(f"{where}: {host.id} is read-only, only policy: read is allowed")
    kind = raw.get("kind")
    note = raw.get("note", "")
    if not isinstance(note, str):
        raise ConfigError(f"{where}: note must be a string")

    # A power operation on a free guest is free even though the PVE host is
    # production; free_guests is the gate, not the host rule.
    if kind == "pve":
        if len(hosts) != 1:
            raise ConfigError(f"{where}: a pve action takes one target")
        host = hosts[0]
        if not host.pve:
            raise ConfigError(f"{where}: {host.id} is not a PVE host")
        run = raw.get("run")
        if not isinstance(run, dict):
            raise ConfigError(f"{where}: run must be a mapping with vmid, type and op")
        vmid, vtype, op = run.get("vmid"), run.get("type"), run.get("op")
        if isinstance(vmid, bool) or not isinstance(vmid, int):
            raise ConfigError(f"{where}: run.vmid must be an integer")
        if vtype not in PVE_TYPES:
            raise ConfigError(f"{where}: run.type must be qemu or lxc")
        if op not in PVE_OPS:
            raise ConfigError(f"{where}: run.op must be one of {', '.join(PVE_OPS)}")
        if vmid not in config.pve[host.pve].free_guests:
            raise ConfigError(f"{where}: vmid {vmid} is not in {host.pve}'s free_guests")
        if policy == "read":
            raise ConfigError(f"{where}: a power operation cannot have policy: read")
        if raw.get("params"):
            raise ConfigError(f"{where}: pve actions take no params")
        return Action(id_, title, targets, "pve", policy, dict(vmid=vmid, type=vtype, op=op),
                      note=note)

    if kind not in ("ssh", "dockhand"):
        raise ConfigError(f"{where}: kind must be pve, ssh or dockhand")
    for host in hosts:
        if host.rule == "prod" and policy == "free":
            raise ConfigError(f"{where}: {host.id} is production, policy: free is not allowed")

    if kind == "dockhand":
        for host in hosts:
            if host.dockhand_env is None:
                raise ConfigError(f"{where}: {host.id} is not a Dockhand environment")
        run = raw.get("run")
        if not isinstance(run, dict):
            raise ConfigError(f"{where}: run must be a mapping with op and container")
        op, container = run.get("op"), run.get("container")
        if op not in DOCKHAND_OPS:
            raise ConfigError(f"{where}: run.op must be one of {', '.join(DOCKHAND_OPS)}")
        if not isinstance(container, str) or not container:
            raise ConfigError(f"{where}: run.container is required")
        if policy == "read":
            raise ConfigError(f"{where}: a container operation cannot have policy: read")
        params = _params(raw, where, set(PLACEHOLDER_RE.findall(container)))
        return Action(id_, title, targets, "dockhand", policy, dict(op=op, container=container),
                      params, note)

    for host in hosts:
        if not host.ssh:
            raise ConfigError(f"{where}: {host.id} has no ssh address")
    run = raw.get("run")
    if (
        not isinstance(run, list)
        or not run
        or any(not isinstance(a, str) or not a for a in run)
    ):
        raise ConfigError(f"{where}: run must be a non-empty list of strings")
    joined = " ".join(run)
    for pattern in FORBIDDEN:
        if pattern.search(joined):
            raise ConfigError(f"{where}: '{joined}' matches a forbidden command")
    used = {m for arg in run for m in PLACEHOLDER_RE.findall(arg)}
    params = _params(raw, where, used)
    return Action(id_, title, targets, "ssh", policy, list(run), params, note)


def parse(data, config: Config) -> list[Action]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ConfigError("catalog: expected a list of actions")
    actions = []
    seen = set()
    for i, raw in enumerate(data):
        action = _parse_action(i, raw, config)
        if action.id in seen:
            raise ConfigError(f"catalog[{action.id}]: duplicate id")
        seen.add(action.id)
        actions.append(action)
    return actions


def load(path: str | os.PathLike, config: Config) -> list[Action]:
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(data, config)

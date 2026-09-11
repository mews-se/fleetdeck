"""The rules behind the needs-attention list.

compute() returns everything that is wrong right now, keyed by (source, key).
The store turns that into rows with first_seen and cleared_ts, so an item is
one entry for as long as its condition holds.
"""

from app.clock import in_window, window_start
from app.config import Config
from app.db import Database

DOWN_GRACE = 300
DISK_WARN = 85
DISK_CRIT = 95
CPU_WARN = 90
CPU_WINDOW = 300
MEM_WARN = 90
MEM_WINDOW = 300
TEMP_WARN = 65
TEMP_CRIT = 70
BANDWIDTH_INFO = 100
BANDWIDTH_WINDOW = 300
SAMPLE_STEP = 60
# the nightly jobs on dellpi take a source down for half an hour
ERROR_GRACE = 2700
THREAD_WINDOW = 86400
SPEEDTEST_MIN_RESULTS = 5
SPEEDTEST_FAIL_RATE = 0.4
STATES_WARN = 0.8
UNBOUND_WINDOW = 3600


def _disk_item(key: tuple, what: str, pct: float, detail: str, items: dict):
    if pct < DISK_WARN:
        return
    items[key] = {
        "severity": "crit" if pct >= DISK_CRIT else "warn",
        "title": f"{what} is {pct:.0f} % full",
        "detail": detail,
    }


def _average(db: Database, series: str, ts: int, window: int) -> float | None:
    """Mean of the window, or None when too many samples are missing."""
    points = db.series(series, ts - window)
    if len(points) < window / SAMPLE_STEP / 1.2:
        return None
    return sum(v for _t, v in points) / len(points)


def _beszel(db: Database, config: Config, ts: int, items: dict):
    nas_on = in_window(config.nas_window, ts)
    for _key, snap_ts, s in db.get_snapshots("beszel", "system:"):
        name = s.get("name")
        host = config.host_by_beszel(name)
        if s.get("status") != "up":
            if s.get("status") == "paused":
                continue
            if host and host.off_by_default:
                continue
            if host and host.window == "nas":
                if not nas_on:
                    continue
                # the box boots when the window opens; count from there
                since = max(s.get("down_since") or snap_ts,
                            window_start(config.nas_window, ts))
            else:
                since = s.get("down_since") or snap_ts
            if ts - since < DOWN_GRACE:
                continue
            items[("beszel", name)] = {
                "severity": "crit" if host and host.rule == "prod" else "warn",
                "title": f"{name} is {s.get('status')} in Beszel",
                "detail": f"{s.get('host') or ''} · no agent data for {(ts - since) // 60} min",
                "first_seen": since,
            }
            continue
        disk = s.get("disk")
        if isinstance(disk, int | float):
            _disk_item(("beszel", f"disk:{name}"), f"{name} root disk", disk,
                       "Beszel agent", items)
        for mount, pct in (s.get("extra_fs") or {}).items():
            if isinstance(pct, int | float):
                _disk_item(("beszel", f"disk:{name}:{mount}"), f"{name} {mount}", pct,
                           "Beszel agent", items)
        _load_items(db, name, s, ts, items)


def _load_items(db: Database, name: str, s: dict, ts: int, items: dict):
    cpu = _average(db, f"beszel.cpu.{name}", ts, CPU_WINDOW)
    if cpu is not None and cpu >= CPU_WARN:
        items[("beszel", f"cpu:{name}")] = {
            "severity": "warn",
            "title": f"{name} CPU averaged {cpu:.0f} % for {CPU_WINDOW // 60} min",
            "detail": "Beszel agent",
        }
    mem = _average(db, f"beszel.mem.{name}", ts, MEM_WINDOW)
    if mem is not None and mem >= MEM_WARN:
        items[("beszel", f"mem:{name}")] = {
            "severity": "warn",
            "title": f"{name} memory averaged {mem:.0f} % for {MEM_WINDOW // 60} min",
            "detail": "Beszel agent",
        }
    temp = s.get("temp")
    if isinstance(temp, int | float) and temp >= TEMP_WARN:
        items[("beszel", f"temp:{name}")] = {
            "severity": "crit" if temp >= TEMP_CRIT else "warn",
            "title": f"{name} runs at {temp:.0f} °C",
            "detail": "Beszel agent, latest reading",
        }
    load = s.get("load") or []
    threads = s.get("threads") or s.get("cores")
    if len(load) == 3 and isinstance(load[2], int | float) and threads and load[2] >= threads:
        items[("beszel", f"load:{name}")] = {
            "severity": "warn",
            "title": f"{name} load is {load[2]:.1f} on {threads} threads",
            "detail": "15 minute average",
        }
    bw = _average(db, f"beszel.bandwidth.{name}", ts, BANDWIDTH_WINDOW)
    if bw is not None and bw >= BANDWIDTH_INFO:
        items[("beszel", f"bandwidth:{name}")] = {
            "severity": "info",
            "title": f"{name} moved {bw:.0f} MB/s for {BANDWIDTH_WINDOW // 60} min",
            "detail": "sent and received, Beszel agent",
        }
    failed = s.get("failed_services") or []
    count = len(failed) or ((s.get("services") or [0, 0])[1] or 0)
    if count:
        names = ", ".join(failed[:10]) + (f" and {len(failed) - 10} more" if len(failed) > 10
                                         else "")
        items[("beszel", f"services:{name}")] = {
            "severity": "warn",
            "title": f"{count} failed service{'s' if count > 1 else ''} on {name}",
            "detail": names or "systemd, names not read yet",
        }


def _kuma(db: Database, items: dict):
    for _key, _ts, m in db.get_snapshots("kuma", "monitor:"):
        if m.get("status") != 0:
            continue
        where = m.get("url") or ":".join(str(x) for x in (m.get("hostname"), m.get("port")) if x)
        items[("kuma", str(m.get("id")))] = {
            "severity": "warn",
            "title": f"{m.get('name')} is down in Uptime Kuma",
            "detail": f"{m.get('type')} {where}".strip(),
        }


def _pve(db: Database, config: Config, items: dict):
    for pve_id in config.pve:
        source = f"pve.{pve_id}"
        node = db.get_snapshot(source, "node")
        if node and isinstance(node.get("root_pct"), int | float):
            _disk_item((source, "rootfs"), f"{pve_id} PVE root", node["root_pct"],
                       "node status", items)
        for _key, _ts, st in db.get_snapshots(source, "storage:"):
            pct = st.get("pct")
            if st.get("status") == "available" and isinstance(pct, int | float):
                _disk_item((source, f"storage:{st.get('storage')}"),
                           f"{pve_id} storage {st.get('storage')}", pct,
                           st.get("plugin") or "", items)


def _dockhand(db: Database, items: dict):
    for key, _ts, c in db.get_snapshots("dockhand"):
        if "state" in c and "(unhealthy)" in str(c.get("status") or ""):
            items[("dockhand", f"unhealthy:{key}")] = {
                "severity": "warn",
                "title": f"{c.get('name')} is unhealthy on {c.get('host')}",
                "detail": f"{c.get('image') or ''} · {c.get('status')}".strip(" ·"),
            }
    for _key, _ts, u in db.get_snapshots("dockhand.updates", "updates:"):
        names = [i.get("name") for i in u.get("items") or [] if i.get("name")]
        if not names:
            continue
        items[("dockhand", f"updates:{u.get('env')}")] = {
            "severity": "info",
            "title": f"{len(names)} image update{'s' if len(names) > 1 else ''}"
                     f" available on {u.get('host')}",
            "detail": ", ".join(names),
        }


def _speedtest(db: Database, config: Config, ts: int, items: dict):
    for iid in config.speedtests:
        rows = db.speedtests(iid, ts - 86400)
        if len(rows) < SPEEDTEST_MIN_RESULTS:
            continue
        failed = sum(1 for r in rows if r.get("status") != "completed")
        rate = failed / len(rows)
        if rate > SPEEDTEST_FAIL_RATE:
            items[("speedtest", iid)] = {
                "severity": "warn",
                "title": f"speedtest {iid}: {rate:.0%} of the last 24 h failed",
                "detail": f"{failed} of {len(rows)} results",
            }


def _threads(db: Database, ts: int, items: dict):
    for t in db.threads():
        changed = t.get("changed_ts")
        if changed and ts - changed < THREAD_WINDOW:
            items[("github", f"{t['repo']}#{t['number']}")] = {
                "severity": "info",
                "title": f"{t['repo']} #{t['number']} moved",
                "detail": f"{t['title']} · {t['state']}, {t['comments']} comments",
                "first_seen": changed,
            }


def _sources(db: Database, ts: int, items: dict):
    for source, st in db.source_status().items():
        if st.get("ok") or st.get("skipped"):
            continue
        since = db.error_streak_since(source)
        if since is None or ts - since < ERROR_GRACE:
            continue
        items[("source", source)] = {
            "severity": "warn",
            "title": f"{source} has been failing for {(ts - since) // 60} min",
            "detail": st.get("error") or "",
            "first_seen": since,
        }


def _pfsense_dhcp(db: Database, config: Config, box, source: str, items: dict):
    leases = [v for _k, _t, v in db.get_snapshots(f"{source}.dhcp", "lease:")]
    if not leases:
        return
    arps = {v["ip"]: v for _k, _t, v in db.get_snapshots(f"{source}.dhcp", "arp:")}
    static = {v["ip"]: v for v in leases if v["kind"] == "static"}
    macs = {v["mac"] for v in leases if v["mac"]}
    for host in config.hosts.values():
        if host.site != box.site or host.id == box.host or host.ip in static:
            continue
        if box.is_quiet(host.ip):
            continue
        lease = next((v for v in leases if v["ip"] == host.ip), None)
        items[(source, f"mapping:{host.id}")] = {
            "severity": "warn",
            "title": f"{host.id} has no static DHCP mapping",
            "detail": f"{host.ip} · " + ("dynamic lease, the address can change"
                                         if lease else f"no lease or mapping on {box.host}"),
        }
    for ip, lease in static.items():
        arp = arps.get(ip)
        if not arp or not lease["mac"] or not arp["mac"]:
            continue
        if lease["quiet"] or box.same_device(lease["mac"], arp["mac"]):
            continue
        items[(source, f"mac:{ip}")] = {
            "severity": "warn",
            "title": f"{ip} answers from another MAC than its static mapping",
            "detail": f"mapping {lease['mac']} ({lease['hostname'] or lease['descr'] or '?'})"
                      f" · ARP {arp['mac']}",
        }
    for pve_id in config.pve:
        node = config.pve_host(pve_id)
        if node is None or node.site != box.site:
            continue
        for _k, _t, g in db.get_snapshots(f"pve.{pve_id}", "guest:"):
            if g.get("status") != "running" or g.get("template"):
                continue
            cfg = db.get_snapshot(f"pve.{pve_id}.config", f"guest:{g['vmid']}") or {}
            for nic in cfg.get("nics") or []:
                mac = nic.get("mac")
                if not mac or mac in macs or (nic.get("ip") and nic["ip"] != "dhcp"):
                    continue
                items[(source, f"guest:{pve_id}:{g['vmid']}:{nic['name']}")] = {
                    "severity": "info",
                    "title": f"{g.get('name') or g['vmid']} has no DHCP mapping or lease"
                             f" for {nic['name']}",
                    "detail": f"{mac} on {nic.get('bridge') or '?'} · guest {g['vmid']}"
                              f" on {node.id}",
                }


def _pfsense(db: Database, config: Config, ts: int, items: dict):
    for box in config.pfsense.values():
        source = f"pfsense.{box.id}"
        _pfsense_dhcp(db, config, box, source, items)
        snap = db.get_snapshot(source, "box") or {}
        states, limit = snap.get("states"), snap.get("state_limit")
        if isinstance(states, int | float) and limit and states / limit >= STATES_WARN:
            items[(source, "states")] = {
                "severity": "warn",
                "title": f"{box.host} state table is {100 * states / limit:.0f} % full",
                "detail": f"{states} of {limit} states",
            }
        restarts = len(db.series(f"{source}.unbound_restart", ts - UNBOUND_WINDOW))
        if restarts:
            items[(source, "unbound")] = {
                "severity": "warn" if restarts >= 3 else "info",
                "title": f"unbound on {box.host} restarted {restarts}"
                         f" time{'s' if restarts > 1 else ''} in the last hour",
                "detail": "the resolver cache starts empty after every restart",
            }


def compute(db: Database, config: Config, ts: int) -> dict[tuple[str, str], dict]:
    items: dict[tuple[str, str], dict] = {}
    _beszel(db, config, ts, items)
    _kuma(db, items)
    _pve(db, config, items)
    _dockhand(db, items)
    _speedtest(db, config, ts, items)
    _threads(db, ts, items)
    _pfsense(db, config, ts, items)
    _sources(db, ts, items)
    return items

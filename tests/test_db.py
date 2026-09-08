from app.db import Database


def test_snapshots_replace_under_prefix():
    db = Database(":memory:")
    db.put_snapshots("s", {"a:1": {"x": 1}, "a:2": {"x": 2}, "b:1": {"y": 1}})
    db.put_snapshots("s", {"a:1": {"x": 10}}, prefix="a:")
    keys = [k for k, _, _ in db.get_snapshots("s")]
    assert keys == ["a:1", "b:1"]
    assert db.get_snapshot("s", "a:1") == {"x": 10}
    assert db.get_snapshot("s", "nope") is None


def test_samples_and_series():
    db = Database(":memory:")
    db.add_samples([("cpu", 10, 1.0), ("cpu", 20, 2.0), ("mem", 20, 3.0)])
    assert db.series("cpu", 15) == [(20, 2.0)]
    assert db.latest_sample("mem") == (20, 3.0)
    assert db.latest_sample("none") is None


def test_source_status_and_error_streak():
    db = Database(":memory:")
    db.record_run("a", True, 5, ts=100)
    db.record_run("a", False, 5, error="boom", ts=200)
    db.record_run("a", False, 5, error="boom", ts=300)
    db.record_run("a", True, 5, ts=350, skipped=True)
    st = db.source_status()["a"]
    assert st["last_ok"] == 350 and st["skipped"] == 1
    assert db.error_streak_since("a") == 200
    db.record_run("a", True, 5, ts=400)
    assert db.error_streak_since("a") is None


def test_attention_lifecycle():
    db = Database(":memory:")
    item = {"severity": "warn", "title": "x down", "detail": "d"}
    opened, cleared = db.sync_attention({("s", "k"): item}, ts=100)
    assert opened == [("s", "k")] and cleared == []
    opened, cleared = db.sync_attention({("s", "k"): dict(item, severity="crit")}, ts=200)
    assert opened == [] and cleared == []
    rows = db.open_attention()
    assert len(rows) == 1 and rows[0]["severity"] == "crit" and rows[0]["first_seen"] == 100
    opened, cleared = db.sync_attention({}, ts=300)
    assert cleared == [("s", "k")] and db.open_attention() == []


def test_threads_report_changes():
    db = Database(":memory:")
    args = dict(kind="pr", title="t", state="open", updated_at="2026-09-01", comments=1,
                url="u", author="a")
    assert db.upsert_thread("o/r", 1, ts=10, **args) is False
    assert db.upsert_thread("o/r", 1, ts=20, **args) is False
    assert db.upsert_thread("o/r", 1, ts=30, **dict(args, updated_at="2026-09-02")) is True
    t = db.threads()[0]
    assert t["changed_ts"] == 30 and t["seen_ts"] == 30
    assert db.upsert_thread("o/r", 1, ts=40, **dict(args, updated_at="2026-09-02")) is False
    assert db.threads()[0]["changed_ts"] == 30


def test_runs():
    db = Database(":memory:")
    rid = db.new_run("a", "h", "uptime", None, False, "/tmp/x.log", ts=10)
    db.finish_run(rid, 0, ts=20)
    assert db.run(rid)["exit_code"] == 0
    assert db.last_run_per_action()["a"]["id"] == rid
    assert db.runs()[0]["id"] == rid


def test_prune_and_speedtests():
    db = Database(":memory:")
    now = 10_000_000
    db.upsert_speedtests("home", [(1, now - 400 * 86400, 900, 100, 3, "completed", "s")])
    db.upsert_speedtests("home", [(2, now - 3600, 930, 110, 2, "completed", "s")])
    db.add_samples([("x", now - 100 * 86400, 1.0), ("x", now, 2.0)])
    db.prune(ts=now)
    assert [r["id"] for r in db.speedtests("home", 0)] == [2]
    assert db.last_speedtest_ts("home") == now - 3600
    assert db.series("x", 0) == [(now, 2.0)]

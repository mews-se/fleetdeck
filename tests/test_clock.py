import time

from app.clock import in_window, window_start, window_state
from app.config import Window


def ts_at(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    t = time.localtime()
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1)))


def test_window():
    w = Window("15:00", "23:00")
    assert in_window(w, ts_at("15:00"))
    assert in_window(w, ts_at("22:59"))
    assert not in_window(w, ts_at("23:00"))
    assert not in_window(w, ts_at("03:00"))
    night = Window("23:00", "15:00")
    assert in_window(night, ts_at("03:00"))
    assert not in_window(night, ts_at("16:00"))
    assert in_window(None, 0)
    assert window_state(None, 0) is None
    assert window_state(w, ts_at("16:00"))["next"] == "23:00"
    assert window_start(w, ts_at("16:00")) == ts_at("15:00")
    assert window_start(w, ts_at("14:59")) == ts_at("15:00") - 86400
    assert window_start(night, ts_at("03:00")) == ts_at("23:00") - 86400
    assert window_start(night, ts_at("23:30")) == ts_at("23:00")

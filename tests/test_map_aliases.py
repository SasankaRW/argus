"""The map shows one box per real thing: a worker's fast lane and power's "pc" fold into the PC's worker."""

from argus.registry import _aliases, _fold_edges


def w(caps):
    return {"capabilities": caps, "state": "online"}


def e(src, dst, n, t):
    return {"src": src, "dst": dst, "count": n, "last_kind": f"k{t}", "first_seen": t, "last_seen": t}


def test_fast_lane_and_pc_fold_into_the_pc_worker():
    workers = {"worker-saspc": w(["desktop", "gpu"]), "worker-saspc-now": w(["desktop", "gpu"]),
               "worker-laptop": w(["cpu"])}
    alias = _aliases(workers)
    assert alias == {"worker-saspc-now": "worker-saspc", "pc": "worker-saspc"}
    edges = _fold_edges([e("worker-saspc", "argus", 5, 1), e("worker-saspc-now", "argus", 3, 9),
                         e("pc", "power", 2, 4), e("worker-saspc-now", "worker-saspc", 1, 2)], alias)
    assert edges == [{"src": "worker-saspc", "dst": "argus", "count": 8, "last_kind": "k9", "first_seen": 1,
                      "last_seen": 9},
                     {"src": "worker-saspc", "dst": "power", "count": 2, "last_kind": "k4", "first_seen": 4,
                      "last_seen": 4}]


def test_two_desktops_leave_pc_alone():
    assert _aliases({"a": w(["desktop"]), "b": w(["desktop"])}) == {}

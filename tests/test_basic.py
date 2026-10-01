"""创建幂等、轮次推进、重领规则、manual 行为。"""

import pytest

from paycomp import CompensationService


def test_create_is_idempotent(env):
    svc, clock = env
    svc.create("o1")
    first = svc.inspect("o1")
    assert first["status"] == "pending"
    assert first["round"] == 1
    assert first["next_due_at"] == first["created_at"] + 60

    clock.advance(120)
    svc.work_once(lambda o, q: "unknown")
    svc.create("o1")  # 重复创建不改变首次创建时间或状态
    again = svc.inspect("o1")
    assert again["created_at"] == first["created_at"]
    assert again["status"] == "pending"
    assert again["round"] == 2


def test_first_round_due_after_60s(env):
    svc, clock = env
    svc.create("o1")
    clock.advance(59)
    assert svc.work_once(lambda o, q: "unknown") is False
    clock.advance(1)
    assert svc.work_once(lambda o, q: "unknown") is True


def test_unknown_advances_round_and_schedules_from_record_time(env):
    svc, clock = env
    svc.create("o1")
    clock.advance(60)
    clock.advance(7)  # 到期后拖延一段时间才执行
    svc.work_once(lambda o, q: "unknown")
    st = svc.inspect("o1")
    assert st["round"] == 2
    # 从记录结果时采样的时刻再等 60 秒
    assert st["next_due_at"] == clock.now() + 60
    q1 = st["queries"][0]["query_id"]
    assert st["query_id"] != q1  # 新一轮换新编号
    clock.advance(60)
    svc.work_once(lambda o, q: "unknown")
    st = svc.inspect("o1")
    assert st["round"] == 3
    assert st["query_id"] != q1  # 新一轮新编号


def test_query_exception_and_bad_return_count_as_unknown(env):
    svc, clock = env
    svc.create("o1")
    clock.advance(60)

    def boom(o, q):
        raise RuntimeError("flaky")

    assert svc.work_once(boom) is True
    assert svc.inspect("o1")["round"] == 2

    clock.advance(60)
    assert svc.work_once(lambda o, q: "garbage") is True
    assert svc.inspect("o1")["round"] == 3


def test_third_unknown_goes_manual_and_stops_claims(env):
    svc, clock = env
    svc.create("o1")
    for _ in range(3):
        clock.advance(60)
        assert svc.work_once(lambda o, q: "unknown") is True
    st = svc.inspect("o1")
    assert st["status"] == "manual"
    assert st["next_due_at"] is None
    clock.advance(600)
    assert svc.work_once(lambda o, q: "unknown") is False  # manual 不再被领取


def test_reclaim_after_30s_keeps_query_id(env, db_path):
    import threading

    svc, clock = env
    svc.create("o1")
    clock.advance(60)
    started = threading.Event()
    release = threading.Event()

    def blocking(o, q):
        started.set()
        release.wait(10)
        return "unknown"

    def run_other():
        other = CompensationService(db_path, clock)
        try:
            other.work_once(blocking)
        finally:
            other.close()

    t = threading.Thread(target=run_other)
    t.start()
    assert started.wait(10)
    st = svc.inspect("o1")
    assert st["attempt"] == 1
    first_qid = st["query_id"]
    # 30 秒内不得再次领取
    clock.advance(29)
    assert svc.work_once(lambda o, q: "unknown") is False
    # 达到 30 秒允许重领，沿用编号、不算新轮次
    clock.advance(1)
    seen = []
    assert svc.work_once(lambda o, q: seen.append(q) or "unknown") is True
    st = svc.inspect("o1")
    assert seen[0] == first_qid
    assert st["round"] == 2  # 重领提交 unknown 后才推进
    assert st["queries"][0]["attempt"] == 1
    assert st["queries"][1]["attempt"] == 2
    assert st["queries"][1]["query_id"] == first_qid
    # 旧执行随后返回，不得改写已推进的状态
    release.set()
    t.join(10)
    st = svc.inspect("o1")
    assert st["round"] == 2
    assert st["queries"][0]["result"] == "unknown"  # 旧结果仅留痕


def test_final_query_result_finalizes_order(env):
    svc, clock = env
    svc.create("o1")
    clock.advance(60)
    assert svc.work_once(lambda o, q: "succeeded") is True
    st = svc.inspect("o1")
    assert st["status"] == "succeeded"
    assert st["final_result"] == "succeeded"
    clock.advance(600)
    assert svc.work_once(lambda o, q: "unknown") is False


def test_inspect_unknown_order_raises(env):
    svc, _ = env
    with pytest.raises(KeyError):
        svc.inspect("nope")


def test_shared_timeline_never_goes_backwards(db_path):
    from paycomp import CompensationService, ManualClock

    fast = ManualClock(2_000_000)
    slow = ManualClock(1_000_000)  # 落后的实例
    svc_fast = CompensationService(db_path, fast)
    svc_fast.create("o1")
    svc_slow = CompensationService(db_path, slow)
    svc_slow.create("o2")
    # 慢时钟实例写入的时间不得早于共享时间轴已到的时刻
    assert svc_slow.inspect("o2")["created_at"] >= svc_fast.inspect("o1")["created_at"]
    svc_fast.close()
    svc_slow.close()

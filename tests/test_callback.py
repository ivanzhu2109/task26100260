"""最终回调：查询返回前到达、manual 后到达；重复投递与迟到查询不回退状态。"""
import threading

import pytest

from paycomp import CompensationService


def test_callback_arrives_before_query_returns(db_path, clock):
    svc = CompensationService(db_path, clock)
    svc.create("o1")
    clock.advance(60)

    started = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def blocking(order_id, query_id):
        started.set()
        release.wait(10)
        return "unknown"  # 迟到结果：不得改写已终结状态

    def run_other():
        other = CompensationService(db_path, clock)
        try:
            other.work_once(blocking)
        finally:
            other.close()
            done.set()

    t = threading.Thread(target=run_other)
    t.start()
    assert started.wait(10)

    # 查询尚未返回，最终回调先到达
    svc.callback("o1", "succeeded")
    assert svc.inspect("o1")["status"] == "succeeded"

    # 已领取的调用可以完成，但不得改写已终结状态
    release.set()
    assert done.wait(10)
    t.join(10)
    st = svc.inspect("o1")
    assert st["status"] == "succeeded"
    assert st["final_result"] == "succeeded"
    assert st["queries"][0]["result"] == "unknown"  # 迟到结果仅留痕

    # 重复投递不回退、不产生新领取
    svc.callback("o1", "succeeded")
    assert svc.inspect("o1")["status"] == "succeeded"
    clock.advance(600)
    assert svc.work_once(lambda o, q: "unknown") is False
    svc.close()


def test_callback_after_manual_and_duplicate_delivery(env):
    svc, clock = env
    svc.create("o1")
    for _ in range(3):
        clock.advance(60)
        assert svc.work_once(lambda o, q: "unknown") is True
    assert svc.inspect("o1")["status"] == "manual"

    # manual 不是终态，仍须接收最终回调
    svc.callback("o1", "failed")
    st = svc.inspect("o1")
    assert st["status"] == "failed"
    assert st["final_result"] == "failed"
    finished_at = st["finished_at"]

    # 重复投递是幂等 no-op
    clock.advance(10)
    svc.callback("o1", "failed")
    st = svc.inspect("o1")
    assert st["status"] == "failed"
    assert st["finished_at"] == finished_at

    # 终态后不再产生新领取
    clock.advance(600)
    assert svc.work_once(lambda o, q: "unknown") is False


def test_callback_validates_input(env):
    svc, _ = env
    svc.create("o1")
    with pytest.raises(ValueError):
        svc.callback("o1", "unknown")
    with pytest.raises(KeyError):
        svc.callback("ghost", "succeeded")

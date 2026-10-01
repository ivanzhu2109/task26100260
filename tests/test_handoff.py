"""两个实例交接同一轮：旧执行结果失效；阻塞的查询不阻塞其他订单。"""
import threading

from paycomp import CompensationService


def test_handoff_stale_result_cannot_overwrite(db_path, clock):
    svc_b = CompensationService(db_path, clock)
    svc_b.create("o1")
    svc_b.create("o2")
    clock.advance(60)

    started = threading.Event()
    barrier = threading.Barrier(2)
    done = threading.Event()

    def blocking_query(order_id, query_id):
        started.set()
        barrier.wait(10)          # 阻塞，直到主线程放行
        return "succeeded"        # 旧执行最终返回最终结果

    def run_a():
        svc_a = CompensationService(db_path, clock)
        try:
            svc_a.work_once(blocking_query)
        finally:
            svc_a.close()
            done.set()

    t = threading.Thread(target=run_a)
    t.start()
    assert started.wait(10)
    assert svc_b.inspect("o1")["attempt"] == 1

    # o1 的查询阻塞期间，其他订单仍可推进
    assert svc_b.work_once(lambda o, q: "unknown") is True
    o2 = svc_b.inspect("o2")
    assert o2["round"] == 2 and o2["status"] == "pending"

    # 30 秒后实例 B 重领 o1 同一轮，沿用编号并提交 unknown
    clock.advance(30)
    first_qid = svc_b.inspect("o1")["query_id"]
    seen = []
    assert svc_b.work_once(lambda o, q: seen.append((o, q)) or "unknown") is True
    assert seen == [("o1", first_qid)]
    mid = svc_b.inspect("o1")
    assert mid["round"] == 2
    assert mid["next_due_at"] == clock.now() + 60

    # 放行旧执行：其结果失效，不得修改轮次及下次到期时间
    barrier.wait(10)
    assert done.wait(10)
    t.join(10)
    after = svc_b.inspect("o1")
    assert after["status"] == "pending"
    assert after["round"] == mid["round"]
    assert after["next_due_at"] == mid["next_due_at"]
    assert after["final_result"] is None
    logs = {q["attempt"]: q for q in after["queries"] if q["round"] == 1}
    assert logs[1]["result"] == "succeeded"   # 旧结果仅留痕
    assert logs[2]["result"] == "unknown"     # 有效结果推进了轮次
    svc_b.close()

"""子进程在 query 返回后、结果落库前被杀：重启后同轮编号不变，只推进一次。"""
import os
import subprocess
import sys

from paycomp import CompensationService

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHILD = os.path.join(REPO_ROOT, "tests", "crash_child.py")


def _run_child(db_path, call_file, now):
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    return subprocess.run(
        [sys.executable, CHILD, db_path, call_file, str(now)],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_crash_before_persist_keeps_round_and_query_id(db_path, clock):
    svc = CompensationService(db_path, clock)
    svc.create("o1")
    t0 = svc.inspect("o1")["created_at"]
    clock.advance(60)

    call_file = os.path.join(db_path + ".calls")
    proc = _run_child(db_path, call_file, clock.now())
    assert proc.returncode != 0, proc.stderr  # 子进程被杀死
    with open(call_file, encoding="utf-8") as fh:
        crashed_qid = fh.read().split()[1]

    # 重启：新实例、同一文件、同一时钟轴
    svc.close()
    svc = CompensationService(db_path, clock)
    st = svc.inspect("o1")
    assert st["status"] == "pending"
    assert st["round"] == 1            # 崩溃未耗掉轮次
    assert st["attempt"] == 1          # 领取记录已持久化
    assert st["query_id"] == crashed_qid
    # 重启不能提前重领：30 秒未到
    assert svc.work_once(lambda o, q: "unknown") is False

    # 到达 30 秒后重领：编号不变，提交 unknown 后仅推进一次
    clock.advance(30)
    seen = []
    assert svc.work_once(lambda o, q: seen.append(q) or "unknown") is True
    assert seen == [crashed_qid]
    st = svc.inspect("o1")
    assert st["round"] == 2
    assert st["next_due_at"] == t0 + 60 + 30 + 60  # 从记录结果的时刻再等 60 秒
    log1 = [q for q in st["queries"] if q["round"] == 1]
    assert [q["attempt"] for q in log1] == [1, 2]
    assert log1[0]["result"] is None   # 被杀的执行没来得及落库
    assert log1[1]["result"] == "unknown"

    # 后续两轮按 60 秒间隔推进，第三轮无结论进入 manual
    clock.advance(59)
    assert svc.work_once(lambda o, q: "unknown") is False
    clock.advance(1)
    assert svc.work_once(lambda o, q: "unknown") is True
    assert svc.inspect("o1")["round"] == 3
    assert svc.inspect("o1")["next_due_at"] == clock.now() + 60
    clock.advance(60)
    assert svc.work_once(lambda o, q: "unknown") is True
    st = svc.inspect("o1")
    assert st["status"] == "manual"
    assert st["next_due_at"] is None
    assert len({q["round"] for q in st["queries"]}) == 3  # 恰好 3 个逻辑轮次
    clock.advance(600)
    assert svc.work_once(lambda o, q: "unknown") is False
    svc.close()

"""子进程：领取并执行 query，query 已返回但结果尚未落库时被 SIGKILL。"""
import os
import signal
import sys

from paycomp import CompensationService, ManualClock


def main() -> None:
    db_path, call_file, now = sys.argv[1], sys.argv[2], int(sys.argv[3])
    svc = CompensationService(db_path, ManualClock(now))

    def query(order_id: str, query_id: str) -> str:
        with open(call_file, "a", encoding="utf-8") as fh:
            fh.write(f"{order_id} {query_id}\n")
        return "unknown"

    # query 已返回、结果提交事务开始前杀死自己
    orig = svc._normalize_result

    def kill_after_return(raw):
        os.kill(os.getpid(), signal.SIGKILL)
        return orig(raw)

    svc._normalize_result = kill_after_return
    svc.work_once(query)
    raise SystemExit("should have been killed")


if __name__ == "__main__":
    main()

"""Child-process helper: claims one task, records query_id, then dies
(SIGKILL) before the result is submitted, simulating a hard crash."""

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payment_queries import PaymentQueryStore


def file_clock(path):
    def read():
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())

    return read


def main() -> None:
    db_path, time_file, out_file, order_id = sys.argv[1:5]
    store = PaymentQueryStore(db_path, clock=file_clock(time_file))
    store.create(order_id)
    with open(time_file, "r", encoding="utf-8") as fh:
        now = int(fh.read().strip())
    with open(time_file, "w", encoding="utf-8") as fh:
        fh.write(str(now + 60))

    def query(oid, qid):
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(qid)
        os.kill(os.getpid(), signal.SIGKILL)
        return "unknown"

    store.work_once(query)


if __name__ == "__main__":
    main()

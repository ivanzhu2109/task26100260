"""Tests for payment_queries."""

import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payment_queries import PaymentQueryStore


class ManualClock:
    def __init__(self, start: int = 1_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


class FileClock:
    """Integer-second clock shared across processes via a file."""

    def __init__(self, path: str, start: int = 1_000_000) -> None:
        self.path = path
        if not os.path.exists(path):
            self.set(start)

    def __call__(self) -> int:
        with open(self.path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())

    def set(self, value: int) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(str(value))

    def advance(self, seconds: int) -> None:
        self.set(self() + seconds)


class CrashRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "db.sqlite")
        self.time_file = os.path.join(self.tmp.name, "clock.txt")
        self.out_file = os.path.join(self.tmp.name, "query_id.txt")
        self.clock = FileClock(self.time_file)

    def test_crash_before_submit_keeps_round_and_query_id(self) -> None:
        child = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_child.py")
        proc = subprocess.run(
            [sys.executable, child, self.db_path, self.time_file, self.out_file, "o1"],
            capture_output=True,
            timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0, "child must have been killed")
        with open(self.out_file, "r", encoding="utf-8") as fh:
            crashed_query_id = fh.read().strip()
        self.assertTrue(crashed_query_id)

        store = PaymentQueryStore(self.db_path, clock=self.clock)
        info = store.inspect("o1")
        self.assertEqual(info["status"], "pending")
        self.assertEqual(info["round"], 1)
        self.assertEqual(len(info["queries"]), 1)
        self.assertEqual(info["queries"][0]["query_id"], crashed_query_id)
        self.assertIsNone(info["queries"][0]["result"])

        # Lease (30s) persists across restart: cannot reclaim early.
        self.clock.advance(29)
        self.assertIsNone(store.work_once(lambda o, q: "unknown"))

        # After 30s the same round is reclaimed with the same query_id.
        self.clock.advance(1)
        first = store.work_once(lambda o, q: "unknown")
        self.assertIsNotNone(first)
        self.assertEqual(first["query_id"], crashed_query_id)
        self.assertEqual(first["round"], 1)
        self.assertTrue(first["accepted"])

        # Round 1 advanced exactly once; remaining rounds at 60s intervals.
        info = store.inspect("o1")
        self.assertEqual(info["round"], 2)
        recorded_at = info["queries"][0]["result_at"]
        self.assertEqual(info["next_due_at"], recorded_at + 60)

        self.clock.advance(59)
        self.assertIsNone(store.work_once(lambda o, q: "unknown"))
        self.clock.advance(1)
        second = store.work_once(lambda o, q: "unknown")
        self.assertEqual(second["round"], 2)
        self.assertNotEqual(second["query_id"], crashed_query_id)

        self.clock.advance(60)
        third = store.work_once(lambda o, q: "unknown")
        self.assertEqual(third["round"], 3)

        info = store.inspect("o1")
        self.assertEqual(info["status"], "manual")
        self.assertIsNone(info["next_due_at"])
        self.assertEqual(len(info["queries"]), 3)
        self.clock.advance(3600)
        self.assertIsNone(store.work_once(lambda o, q: "unknown"))


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "db.sqlite")
        self.clock = ManualClock()
        self.store_a = PaymentQueryStore(self.db_path, clock=self.clock)
        self.store_b = PaymentQueryStore(self.db_path, clock=self.clock)

    def test_reclaim_invalidates_old_execution_and_others_proceed(self) -> None:
        self.store_a.create("o1")
        self.clock.advance(15)
        self.store_a.create("o2")
        self.clock.advance(45)  # o1 due, o2 not yet due

        started = threading.Event()
        release = threading.Event()
        holder = {}

        def blocking_query(order_id, query_id):
            started.set()
            release.wait(timeout=30)
            return "unknown"

        def run_a():
            try:
                holder["result"] = self.store_a.work_once(blocking_query)
            except Exception as exc:  # pragma: no cover
                holder["error"] = exc

        thread = threading.Thread(target=run_a)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=30))
            old_query_id = self.store_a.inspect("o1")["queries"][0]["query_id"]

            # While o1's query is blocked, another order still proceeds.
            self.clock.advance(15)  # o2 due; o1 lease (15s) not expired
            other = self.store_b.work_once(lambda o, q: "succeeded")
            self.assertEqual(other["order_id"], "o2")
            self.assertEqual(self.store_b.inspect("o2")["status"], "succeeded")

            # 30s lease expired: B reclaims same round with same query_id.
            self.clock.advance(15)
            reclaimed = self.store_b.work_once(lambda o, q: "unknown")
            self.assertEqual(reclaimed["order_id"], "o1")
            self.assertEqual(reclaimed["query_id"], old_query_id)
            self.assertEqual(reclaimed["round"], 1)
            self.assertTrue(reclaimed["accepted"])
        finally:
            release.set()
            thread.join(timeout=30)
        self.assertNotIn("error", holder)

        # Old execution finishes late; its result must be discarded.
        self.assertFalse(holder["result"]["accepted"])

        info = self.store_b.inspect("o1")
        self.assertEqual(info["round"], 2)
        self.assertEqual(len(info["queries"]), 1)
        self.assertEqual(info["queries"][0]["result"], "unknown")
        recorded_at = info["queries"][0]["result_at"]
        self.assertEqual(info["next_due_at"], recorded_at + 60)

        # Next round only becomes due at the scheduled time.
        self.assertIsNone(self.store_b.work_once(lambda o, q: "unknown"))
        self.clock.advance(60)
        nxt = self.store_b.work_once(lambda o, q: "unknown")
        self.assertEqual(nxt["round"], 2)


class CallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "db.sqlite")
        self.clock = ManualClock()
        self.store = PaymentQueryStore(self.db_path, clock=self.clock)

    def _drive_to_manual(self, order_id: str) -> None:
        for _ in range(3):
            self.clock.advance(60)
            outcome = self.store.work_once(lambda o, q: "unknown")
            self.assertTrue(outcome["accepted"])
        self.assertEqual(self.store.inspect(order_id)["status"], "manual")

    def test_callback_before_query_returns(self) -> None:
        self.store.create("o1")
        self.clock.advance(60)

        started = threading.Event()
        release = threading.Event()
        holder = {}

        def blocking_query(order_id, query_id):
            started.set()
            release.wait(timeout=30)
            return "failed"

        def run():
            try:
                holder["result"] = self.store.work_once(blocking_query)
            except Exception as exc:  # pragma: no cover
                holder["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=30))
            self.store.callback("o1", "succeeded")
            self.store.callback("o1", "succeeded")  # duplicate delivery
        finally:
            release.set()
            thread.join(timeout=30)
        self.assertNotIn("error", holder)
        self.assertFalse(holder["result"]["accepted"])

        info = self.store.inspect("o1")
        self.assertEqual(info["status"], "succeeded")
        self.assertIsNone(info["next_due_at"])
        self.assertEqual(info["queries"][0]["result"], None)

        # Final state never regresses and no new claim is issued.
        self.store.callback("o1", "failed")
        self.assertEqual(self.store.inspect("o1")["status"], "succeeded")
        self.clock.advance(3600)
        self.assertIsNone(self.store.work_once(lambda o, q: "unknown"))
        self.assertEqual(self.store.inspect("o1")["status"], "succeeded")

    def test_callback_after_manual_and_late_query(self) -> None:
        self.store.create("o1")
        self._drive_to_manual("o1")

        self.store.callback("o1", "failed")
        self.store.callback("o1", "failed")  # duplicate delivery
        info = self.store.inspect("o1")
        self.assertEqual(info["status"], "failed")
        self.assertEqual(len(info["queries"]), 3)

        # No new claims after final state; state does not regress.
        self.clock.advance(3600)
        self.assertIsNone(self.store.work_once(lambda o, q: "succeeded"))
        self.assertEqual(self.store.inspect("o1")["status"], "failed")

    def test_create_is_idempotent(self) -> None:
        self.store.create("o1")
        created = self.store.inspect("o1")
        self.clock.advance(120)
        self.store.create("o1")
        again = self.store.inspect("o1")
        self.assertEqual(again["created_at"], created["created_at"])
        self.assertEqual(again["next_due_at"], created["next_due_at"])
        self.assertEqual(again["status"], "pending")


if __name__ == "__main__":
    unittest.main()

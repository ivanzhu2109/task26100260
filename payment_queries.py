"""Crash-safe payment order compensation query library (SQLite-backed)."""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, Optional

ClockFn = Callable[[], int]
QueryFn = Callable[[str, str], str]

STATUS_PENDING = "pending"
STATUS_MANUAL = "manual"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

RESULT_UNKNOWN = "unknown"
RESULT_SUCCEEDED = "succeeded"
RESULT_FAILED = "failed"

FIRST_DELAY_SECONDS = 60
RETRY_DELAY_SECONDS = 60
LEASE_SECONDS = 30
MAX_ROUNDS = 3


class PaymentQueryStore:
    def __init__(self, db_path: str, clock: Optional[ClockFn] = None) -> None:
        self._db_path = db_path
        self._clock: ClockFn = clock or (lambda: int(time.time()))
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id     TEXT PRIMARY KEY,
                    status       TEXT NOT NULL,
                    round        INTEGER NOT NULL,
                    created_at   INTEGER NOT NULL,
                    next_due_at  INTEGER
                );
                CREATE TABLE IF NOT EXISTS queries (
                    order_id     TEXT NOT NULL,
                    round        INTEGER NOT NULL,
                    query_id     TEXT NOT NULL,
                    epoch        INTEGER NOT NULL,
                    claimed_at   INTEGER NOT NULL,
                    result       TEXT,
                    result_at    INTEGER,
                    PRIMARY KEY (order_id, round)
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    def create(self, order_id: str) -> None:
        now = int(self._clock())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO orders"
                " (order_id, status, round, created_at, next_due_at)"
                " VALUES (?, ?, 1, ?, ?)",
                (order_id, STATUS_PENDING, now, now + FIRST_DELAY_SECONDS),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def work_once(self, query: QueryFn) -> Optional[Dict[str, Any]]:
        claim = self._claim_due()
        if claim is None:
            return None
        try:
            raw = query(claim["order_id"], claim["query_id"])
            result = raw if raw in (RESULT_SUCCEEDED, RESULT_FAILED) else RESULT_UNKNOWN
        except Exception:
            result = RESULT_UNKNOWN
        accepted = self._submit(claim, result)
        return {
            "order_id": claim["order_id"],
            "round": claim["round"],
            "query_id": claim["query_id"],
            "epoch": claim["epoch"],
            "result": result,
            "accepted": accepted,
        }

    def _claim_due(self) -> Optional[Dict[str, Any]]:
        now = int(self._clock())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT o.order_id, o.round, q.query_id, q.epoch, q.claimed_at"
                " FROM orders o"
                " LEFT JOIN queries q ON q.order_id = o.order_id AND q.round = o.round"
                " WHERE o.status = ? AND o.next_due_at IS NOT NULL AND o.next_due_at <= ?"
                " ORDER BY o.next_due_at, o.order_id",
                (STATUS_PENDING, now),
            ).fetchall()
            for row in rows:
                if row["query_id"] is not None and now - row["claimed_at"] < LEASE_SECONDS:
                    continue
                if row["query_id"] is None:
                    query_id = uuid.uuid4().hex
                    epoch = 1
                    conn.execute(
                        "INSERT INTO queries"
                        " (order_id, round, query_id, epoch, claimed_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (row["order_id"], row["round"], query_id, epoch, now),
                    )
                else:
                    query_id = row["query_id"]
                    epoch = row["epoch"] + 1
                    conn.execute(
                        "UPDATE queries SET epoch = ?, claimed_at = ?"
                        " WHERE order_id = ? AND round = ?",
                        (epoch, now, row["order_id"], row["round"]),
                    )
                conn.commit()
                return {
                    "order_id": row["order_id"],
                    "round": row["round"],
                    "query_id": query_id,
                    "epoch": epoch,
                }
            conn.commit()
            return None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _submit(self, claim: Dict[str, Any], result: str) -> bool:
        now = int(self._clock())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute(
                "SELECT status, round FROM orders WHERE order_id = ?",
                (claim["order_id"],),
            ).fetchone()
            current = conn.execute(
                "SELECT epoch, result FROM queries WHERE order_id = ? AND round = ?",
                (claim["order_id"], claim["round"]),
            ).fetchone()
            if (
                order is None
                or order["status"] != STATUS_PENDING
                or order["round"] != claim["round"]
                or current is None
                or current["epoch"] != claim["epoch"]
                or current["result"] is not None
            ):
                conn.commit()
                return False
            conn.execute(
                "UPDATE queries SET result = ?, result_at = ?"
                " WHERE order_id = ? AND round = ?",
                (result, now, claim["order_id"], claim["round"]),
            )
            if result in (RESULT_SUCCEEDED, RESULT_FAILED):
                conn.execute(
                    "UPDATE orders SET status = ?, next_due_at = NULL WHERE order_id = ?",
                    (result, claim["order_id"]),
                )
            elif claim["round"] >= MAX_ROUNDS:
                conn.execute(
                    "UPDATE orders SET status = ?, next_due_at = NULL WHERE order_id = ?",
                    (STATUS_MANUAL, claim["order_id"]),
                )
            else:
                conn.execute(
                    "UPDATE orders SET round = round + 1, next_due_at = ?"
                    " WHERE order_id = ?",
                    (now + RETRY_DELAY_SECONDS, claim["order_id"]),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def callback(self, order_id: str, result: str) -> None:
        if result not in (RESULT_SUCCEEDED, RESULT_FAILED):
            raise ValueError("callback result must be succeeded or failed")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE orders SET status = ?, next_due_at = NULL"
                " WHERE order_id = ? AND status IN (?, ?)",
                (result, order_id, STATUS_PENDING, STATUS_MANUAL),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def inspect(self, order_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                return None
            queries = conn.execute(
                "SELECT * FROM queries WHERE order_id = ? ORDER BY round",
                (order_id,),
            ).fetchall()
            return {
                "order_id": row["order_id"],
                "status": row["status"],
                "round": row["round"],
                "created_at": row["created_at"],
                "next_due_at": row["next_due_at"],
                "queries": [
                    {
                        "round": q["round"],
                        "query_id": q["query_id"],
                        "epoch": q["epoch"],
                        "claimed_at": q["claimed_at"],
                        "result": q["result"],
                        "result_at": q["result_at"],
                    }
                    for q in queries
                ],
            }
        finally:
            conn.close()

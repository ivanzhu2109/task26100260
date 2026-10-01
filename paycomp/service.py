"""支付订单补偿查询服务。

持久化全部状态到 SQLite；多实例可共享同一数据库文件。
所有计时基于可注入的整数秒时钟，并通过 meta 表保证共享时间轴不倒退。
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Callable, Optional

from .clock import Clock, SystemClock

RESULT_UNKNOWN = "unknown"
RESULT_SUCCEEDED = "succeeded"
RESULT_FAILED = "failed"
FINAL_RESULTS = (RESULT_SUCCEEDED, RESULT_FAILED)

STATUS_PENDING = "pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_MANUAL = "manual"
FINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)

FIRST_ROUND_DELAY = 60   # 创建后首轮到期秒数
ROUND_INTERVAL = 60      # 每轮间隔秒数
RECLAIM_AFTER = 30       # 同轮重领间隔秒数
MAX_ROUNDS = 3           # 最大逻辑轮次

QueryFn = Callable[[str, str], Optional[str]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_now INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id      TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    round         INTEGER NOT NULL,
    query_id      TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    last_claim_at INTEGER,
    next_due_at   INTEGER NOT NULL,
    final_result  TEXT,
    finished_at   INTEGER
);
CREATE TABLE IF NOT EXISTS query_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    round      INTEGER NOT NULL,
    attempt    INTEGER NOT NULL,
    query_id   TEXT NOT NULL,
    claimed_at INTEGER NOT NULL,
    finished_at INTEGER,
    result     TEXT
);
CREATE INDEX IF NOT EXISTS idx_query_log_order ON query_log(order_id);
"""


class CompensationService:
    """补偿查询服务。每个实例持有自己的连接，可跨进程共享同一文件。"""

    def __init__(self, db_path: str, clock: Optional[Clock] = None) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CompensationService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 内部工具 ----

    def _now(self) -> int:
        """采样时钟并保证共享时间轴单调不倒退（须在事务内调用）。"""
        now = int(self._clock.now())
        row = self._conn.execute("SELECT last_now FROM meta WHERE id = 1").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta(id, last_now) VALUES (1, ?)", (now,))
        else:
            if now < row["last_now"]:
                now = row["last_now"]
            else:
                self._conn.execute("UPDATE meta SET last_now = ? WHERE id = 1", (now,))
        return now

    @staticmethod
    def _normalize_result(value: Any) -> str:
        return value if value in FINAL_RESULTS else RESULT_UNKNOWN

    # ---- 四个操作 ----

    def create(self, order_id: str) -> None:
        """创建 pending 订单；重复创建不改变首次创建时间或状态。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = self._now()
            self._conn.execute(
                """
                INSERT OR IGNORE INTO orders
                    (order_id, status, created_at, round, query_id, attempt,
                     last_claim_at, next_due_at)
                VALUES (?, ?, ?, 1, ?, 0, NULL, ?)
                """,
                (order_id, STATUS_PENDING, now, uuid.uuid4().hex,
                 now + FIRST_ROUND_DELAY),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def work_once(self, query: QueryFn) -> bool:
        """领取一项到期查询并同步执行。返回是否领取到任务。"""
        # 1) 短事务领取（或重领）
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = self._now()
            row = self._conn.execute(
                """
                SELECT order_id, round, query_id, attempt FROM orders
                WHERE status = ? AND next_due_at <= ?
                  AND (last_claim_at IS NULL OR last_claim_at + ? <= ?)
                ORDER BY next_due_at, order_id
                LIMIT 1
                """,
                (STATUS_PENDING, now, RECLAIM_AFTER, now),
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return False
            order_id = row["order_id"]
            round_no = row["round"]
            query_id = row["query_id"]  # 同轮重领沿用编号
            attempt = row["attempt"] + 1
            self._conn.execute(
                "UPDATE orders SET attempt = ?, last_claim_at = ? WHERE order_id = ?",
                (attempt, now, order_id),
            )
            log_id = self._conn.execute(
                """
                INSERT INTO query_log(order_id, round, attempt, query_id, claimed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, round_no, attempt, query_id, now),
            ).lastrowid
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

        # 2) 事务外同步调用 query，不持有任何锁
        try:
            raw = query(order_id, query_id)
            result = self._normalize_result(raw)
        except Exception:
            result = RESULT_UNKNOWN

        # 3) 短事务原子提交：结果记录 + 轮次推进/下次安排/终结 同事务生效
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            done_at = self._now()
            self._conn.execute(
                "UPDATE query_log SET finished_at = ?, result = ? WHERE id = ?",
                (done_at, result, log_id),
            )
            cur = self._conn.execute(
                "SELECT status, round, attempt FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            # 已终结、已重领（旧执行失效）或轮次已推进：只留日志，不改状态
            if (
                cur is not None
                and cur["status"] == STATUS_PENDING
                and cur["round"] == round_no
                and cur["attempt"] == attempt
            ):
                if result in FINAL_RESULTS:
                    self._conn.execute(
                        """
                        UPDATE orders SET status = ?, final_result = ?, finished_at = ?
                        WHERE order_id = ?
                        """,
                        (result, result, done_at, order_id),
                    )
                elif round_no >= MAX_ROUNDS:
                    self._conn.execute(
                        "UPDATE orders SET status = ?, finished_at = ? WHERE order_id = ?",
                        (STATUS_MANUAL, done_at, order_id),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE orders
                        SET round = ?, query_id = ?, attempt = 0,
                            last_claim_at = NULL, next_due_at = ?
                        WHERE order_id = ?
                        """,
                        (round_no + 1, uuid.uuid4().hex,
                         done_at + ROUND_INTERVAL, order_id),
                    )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return True

    def callback(self, order_id: str, result: str) -> None:
        """接收可信最终结果，可重复投递；manual 不是终态仍接受回调。"""
        if result not in FINAL_RESULTS:
            raise ValueError(f"invalid final result: {result!r}")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = self._now()
            cur = self._conn.execute(
                "SELECT status FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if cur is None:
                self._conn.execute("ROLLBACK")
                raise KeyError(f"unknown order: {order_id!r}")
            if cur["status"] not in FINAL_STATUSES:
                self._conn.execute(
                    """
                    UPDATE orders SET status = ?, final_result = ?, finished_at = ?
                    WHERE order_id = ?
                    """,
                    (result, result, now, order_id),
                )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def inspect(self, order_id: str) -> dict[str, Any]:
        """读取订单状态、查询记录和下次到期时间。"""
        row = self._conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown order: {order_id!r}")
        logs = self._conn.execute(
            """
            SELECT round, attempt, query_id, claimed_at, finished_at, result
            FROM query_log WHERE order_id = ? ORDER BY id
            """,
            (order_id,),
        ).fetchall()
        status = row["status"]
        return {
            "order_id": row["order_id"],
            "status": status,
            "created_at": row["created_at"],
            "round": row["round"],
            "attempt": row["attempt"],
            "query_id": row["query_id"],
            "last_claim_at": row["last_claim_at"],
            "next_due_at": row["next_due_at"] if status == STATUS_PENDING else None,
            "final_result": row["final_result"],
            "finished_at": row["finished_at"],
            "queries": [dict(log) for log in logs],
        }

"""可注入的整数秒时钟。"""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """返回整数秒的时钟协议。所有实例共享的时间轴不得倒退。"""

    def now(self) -> int: ...


class SystemClock:
    """基于 wall clock 的默认时钟。"""

    def now(self) -> int:
        return int(time.time())


class ManualClock:
    """测试用可控时钟，只能前进。"""

    def __init__(self, start: int = 1_000_000) -> None:
        self._now = int(start)

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> int:
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        self._now += int(seconds)
        return self._now

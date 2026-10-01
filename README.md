# 支付订单补偿查询库

Python + SQLite 实现的支付订单补偿查询库。进程在任意时刻被杀后重启，
每轮查询仍只推进一次；重新执行不耗额外轮次；迟到结果不覆盖已终结状态。

## 接口

```python
from paycomp import CompensationService, ManualClock, SystemClock

svc = CompensationService("orders.db")            # 默认 SystemClock；测试可注入 ManualClock
svc.create("order-1")                             # 创建 pending 订单（幂等）
svc.work_once(query)                              # 领取一项到期查询并同步执行；无任务立即返回 False
svc.callback("order-1", "succeeded")              # 可信最终结果，可重复投递
svc.inspect("order-1")                            # 状态 / 查询记录 / 下次到期时间
```

`query(order_id, query_id)` 由调用方提供，可阻塞、抛异常或返回
`unknown` / `succeeded` / `failed`。

## 规则实现要点

- 首轮创建 60 秒后到期；`unknown` 或普通异常落库后，从记录结果时采样的时刻再等 60 秒；
  最多 3 个逻辑轮次，第三轮无结论进入 `manual`（非终态，仍接受回调）。
- 每轮有稳定且持久化的 `query_id`；领取后 30 秒内不得重领，重领沿用编号、
  递增 `attempt` 使旧执行结果失效；所有计时与额度只存 SQLite，重启不重置。
- 结果记录与轮次推进 / 下次安排 / 终结在同一事务生效；`query` 执行期间不持有
  任何事务或锁，多实例可共享同一数据库文件（WAL + `BEGIN IMMEDIATE` 短事务）。
- 共享时间轴通过 `meta` 表钳制为单调不倒退。

## 运行测试

```bash
python3 -m pytest
```

测试使用临时目录、`ManualClock` 可控时钟与 `threading` 同步屏障，不依赖 sleep；
崩溃场景通过 SIGKILL 子进程验证（`tests/crash_child.py`）。

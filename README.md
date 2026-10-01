# 支付订单补偿查询库

Python + SQLite 实现，崩溃安全：轮次、领取权、时间安排全部持久化，
重启后按既有时间轴继续，不会重复推进轮次或丢失 query_id。

## API

- `PaymentQueryStore(db_path, clock=None)`：`clock` 是返回整数秒的可调用对象，
  多实例应共享同一不倒退的时间轴（测试中用文件时钟跨进程共享）。
- `create(order_id)`：创建 pending 订单，重复创建幂等。
- `work_once(query)`：领取一项到期查询并同步调用 `query(order_id, query_id)`；
  无任务立即返回 `None`。
- `callback(order_id, result)`：可信最终结果（`succeeded`/`failed`），可重复投递。
- `inspect(order_id)`：返回状态、轮次、各轮查询记录与下次到期时间。

## 规则实现

- 首轮创建 60 秒后到期；`unknown`/异常结果落库后，从记录时刻再等 60 秒；
  最多 3 轮，第 3 轮无结论进入 `manual`（非终态，仍接受回调）。
- 每轮 `query_id` 稳定持久化；领取后 30 秒租约，过期可重领但沿用编号，
  重领使旧执行结果失效（epoch 校验），未重领的执行超时仍可提交。
- 结果、轮次推进、下次时间安排在同一事务内生效；query 执行期间不持有写事务。

## 运行测试

```
python3 -m unittest discover -s tests -v
```

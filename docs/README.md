# quant-backtest-engine 文档索引

面向代码助手按需读取。安装、运行、执行成本和风险见
[../README.md](../README.md)。

| 文档/配置 | 何时读 | 内容 |
| --- | --- | --- |
| [db_schema.md](db_schema.md) | 改表、视图、净值写入或指标时 | backtest.duckdb 表、主键、视图、幂等与版本 |
| [../configs/execution.yaml](../configs/execution.yaml) | 改手续费、滑点、成交约束时 | 固定值 YAML；动态策略注册见 README |

## 阅读顺序

1. `run_nav.py` / `demo_run.py` / `nav.py` 的任务，先读 `../README.md` 对应章节；
2. 涉及表字段和视图读 `db_schema.md`；
3. 涉及成交价/费用/约束读 `../README.md` 第 6 节与 `configs/execution.yaml`；
4. 当前项目仍是收益入库器，不是完整订单撮合器；不要假设 `bt_nav` 已扣费。

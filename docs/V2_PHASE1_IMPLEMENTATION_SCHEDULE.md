# V2.0 Phase 1 落地实施时序表

> 范围：只在 `D:\ZTFHQ\分仓之神V2.0` 内新增和修改文件。  
> 红线：不修改 `D:\ZTFHQ\分仓之神`、`D:\ZTFHQ\VIP`、`D:\ZTFHQ\limit_up_stats`、`D:\ZTFHQ\XGB`、`D:\ZTFHQ\XGBZX`、`D:\ZTFHQ\X1`、`D:\ZTFHQ\X1-XIN` 的原始文件。

## 1. 总时序

```text
09:00-14:19  只做离线开发、编译、历史 pipeline 回放，不触碰尾盘实时任务。
14:20-14:47  X1Beam 预热窗口；只允许轻量检查和预热，不做重构。
14:45-14:49  tail-readiness 质量闸门检查；阻断项必须先处理。
14:50-14:57  正式尾盘推送窗口；只运行本地已验证路径，不引入新模型和长耗时任务。
15:00以后   可以做代码优化、手动结果比对、追踪入库、因子宽表、盘后复盘。
次日盘后    回填前一交易日出票后的次日冲高、收盘收益、5日追踪进度。
周末        汇总策略胜率、模式标签、失败样本、调权建议。
```

## 2. 本阶段落地目标

1. 建立候选追踪库：每轮 pipeline 产生的 V10/V1/V4/X1Beam 候选和多策略交集都可入库。
2. 建立基础指标库：胜率、均值收益、最大回撤、Sharpe、Sortino、Calmar 等统一计算。
3. 建立候选因子宽表：把策略排名、XGB 诊断、P80/LiftScore/WR/风险标记摊平成可统计字段。
4. 增加安全 CLI：只读 pipeline、只写 V2.0 `outputs`，不影响外部原始程序。
5. 先做轻量自动入库：尾盘每轮写完 pipeline 后，非阻断式写入追踪库；失败只记录提醒，不影响推送。

## 3. 具体执行步骤

### Step 1：新增追踪库

文件：

```text
src/tracking_store.py
outputs/tracking/candidates.jsonl
outputs/tracking/tracking_ingest_*.json
```

操作：

```text
python main.py tracking-ingest --latest
python main.py tracking-report
```

验收：

- 同一个 pipeline 重复入库不会重复写入。
- 每条记录有 `pipeline_file`、`trade_date`、`code`、`strategy_sources`、`strategy_count`、各策略 rank、XGB 诊断字段。
- 只写 `D:\ZTFHQ\分仓之神V2.0\outputs\tracking`。

### Step 2：新增基础回测指标

文件：

```text
src/backtest_metrics.py
```

操作：

```text
python -m py_compile src/backtest_metrics.py
```

验收：

- 空数据、单条数据、非数字数据都不会报错。
- 后续追踪收益回填后，可以统一统计 1 日冲高胜率、5 日 5% 达标率和最大回撤。

### Step 3：新增候选因子宽表

文件：

```text
src/candidate_factor_panel.py
outputs/factors/candidate_factor_panel_*.json
outputs/factors/candidate_factor_panel_*.csv
```

操作：

```text
python main.py factor-panel --latest
python main.py factor-panel --all
```

验收：

- 每只候选股摊平成一行。
- 字段包含策略排名、策略共识、XGB 诊断分、风险数量、P80/LiftScore/WR、初步排序参考分。
- 只生成分析宽表，不反向修改策略结果。

### Step 4：CLI 接入

文件：

```text
main.py
```

新增命令：

```text
python main.py tracking-ingest --latest
python main.py tracking-ingest --all
python main.py tracking-report
python main.py factor-panel --latest
python main.py factor-panel --all
```

验收：

- `python main.py --help` 能看到新命令。
- 新命令失败不影响 `run`、`tail-once`、`tail-watch`。

### Step 5：尾盘轻量自动入库

文件：

```text
main.py
src/tail_automation.py
```

规则：

- 写完 pipeline 后尝试把当前 pipeline 入追踪库。
- 入库失败只打印提醒，不阻断推送。
- 不在尾盘路径里做历史收益回填、LLM、模型训练、长耗时回测。

验收：

- `tail-once` 每成功写一次 pipeline，就能在 `outputs/tracking/candidates.jsonl` 看到对应记录。
- 如果 tracking 写入异常，尾盘结果仍正常生成、推送和刷新控制台。

## 4. 回归命令

每次改动后按顺序执行：

```text
python -m py_compile main.py src/tracking_store.py src/backtest_metrics.py src/candidate_factor_panel.py src/tail_automation.py
python main.py tracking-ingest --latest
python main.py tracking-report
python main.py factor-panel --latest
python main.py run --dry-run --force
python main.py tail-readiness
```

## 5. 每日闭环

```text
尾盘前：
  python main.py x1-preheat
  python main.py tail-readiness

尾盘：
  python main.py tail-watch --push

盘后：
  python main.py tracking-ingest --latest
  python main.py factor-panel --latest
  手动 V10/V1/V4/X1Beam 结果到位后，再做 parity 对比。

次日盘后：
  回填昨日候选的次日冲高、收盘收益、最大回撤。

5个交易日后：
  统计 5日内是否达到 5%、最大回撤、失败原因。
```

## 6. 风险控制

- 任何数据质量阻断项未消除，不正式出票。
- X1Beam 没有完整预热缓存时，不作为第四对等策略参与交集。
- XGB 只做诊断验证层，不作为第五策略。
- 新增分析模块只消费 pipeline，不修改策略规则。
- 14:50-14:57 不做重训练、不做 LLM、不做全量历史回测。

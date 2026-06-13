# 分仓之神 V2.0 进化路线与实施目标

> 生成日期：2026-06-14  
> 范围：仅在 `D:\ZTFHQ\分仓之神V2.0` 内实施。外部项目 `分仓之神`、`VIP`、`limit_up_stats`、`XGB`、`XGBZX`、`X1`、`X1-XIN` 只作为只读参考或 vendor 镜像来源。

---

## 一、总判断

两份新方案都很有价值，但落地顺序必须克制。

当前 V2.0 最重要的不是立刻重构成复杂 Agent 网络，也不是先上 Transformer/GNN，而是先把以下四件事做成硬能力：

1. 数据必须连续、完整、可追溯。
2. 自动出票必须能与手动 V10/V1/V4/X1Beam 结果持续比对。
3. 每天出票后必须追踪次日冲高、5日达标、最大回撤和失败原因。
4. XGB 诊断层必须成为每只候选股旁边的“反方审查 + 风险解释”，而不是一个孤立分数。

前沿方向可以保留，但必须分阶段进入：

- V2.1：稳定、比对、追踪、验证。
- V2.2：因子面板、历史胜率、情绪周期、可交易性过滤。
- V2.5：轻量 Agent 化、动态权重、不确定性估计。
- V3.0：自进化、LLM 盘后审查、知识图谱、Digital Twin、Transformer/GNN。

尾盘实时路径的红线：

- 14:50-14:57 只做本地可控计算。
- 不把 LLM、长耗时全量 X1Beam、重训练模型放进尾盘路径。
- X1Beam 必须提前预热，尾盘只复用完整缓存。
- 质量闸门不通过时不能正式出票。

---

## 二、北极星目标

最终系统应从“多策略流水线”升级为：

```text
多源数据哨兵
  -> 四策略发现候选
  -> XGB/规则/历史胜率诊断
  -> 情绪周期和可交易性过滤
  -> 横截面综合排序
  -> 分级推送与控制台刷新
  -> 盘后追踪复盘
  -> 每周自适应权重建议
```

一句话目标：

> 尾盘自动出票要快、准、可解释；盘后复盘要能告诉我们为什么成功、为什么失败、下一次该如何调权。

---

## 三、实施原则

1. 先稳再强  
   数据质量、手动一致性、尾盘推送稳定性排在所有模型升级前面。

2. 先记录再学习  
   没有事件日志、候选因子表、未来收益标签，就不能谈自适应权重和自进化。

3. 先解释再自动调参  
   系统可以提出权重调整建议，但高风险改动必须人工确认。

4. XGB 是诊断层，不是第五策略  
   V10/V1/V4/X1Beam 是四个对等策略；XGB 做验证、过滤、风险解释、排序加权。

5. LLM 不参与实时选股  
   LLM 只做盘后复盘、异常解释、反方审查、周报归因，不进入尾盘实时推送链路。

---

## 四、阶段路线

### Phase 0：稳定基线与控制台清理

当前状态：已基本完成，但需要持续回归。

已落地能力：

- V10/V1/V4 使用 V2.0 vendor 镜像运行。
- X1Beam 已改为“完整预热缓存才参与第四策略”。
- XGB 诊断已回贴到策略 Top10 和交集候选。
- 控制台已显示 X1 预热状态、诊断标签、策略结果。
- 桌面 BAT 已收敛成单一菜单入口。

继续目标：

- 控制台每个栏目必须有内容或明确说明为空的原因。
- 每轮尾盘推送后必须写入最新 pipeline，并让控制台刷新到最新结果。
- `tail-readiness` 必须作为每天尾盘前的硬审计入口。

验收标准：

- `python main.py tail-readiness` 能清楚说明阻断项和提醒项。
- `python main.py run --serial --force` 能生成完整 pipeline。
- Dashboard API 能显示 `x1_preheat`、`xgb_diagnosis`、四策略 Top10、交集和历史运行。

---

### Phase 1：验证体系与盘后追踪

优先级：最高。

目标：

把“今天选了什么”升级成“这些票后来怎么样、哪类模式更稳定赚钱”。

新增模块建议：

```text
src/tracking_store.py
src/backtest_metrics.py
src/factor_eval.py
src/walk_forward.py
src/overfitting_bridge.py
```

核心数据结构：

```text
trade_date
code
name
strategy_sources
strategy_count
v10_rank
v1_rank
v4_rank
x1beam_rank
xgb_model_score
xgb_rule_score
xgb_blended_score
diagnosis_signal
diagnosis_risks
future_1d_high_return
future_1d_close_return
future_5d_max_return
future_5d_max_drawdown
hit_1d_profit
hit_5d_5pct
failure_reason
```

必须统计：

- 次日是否冲高盈利。
- 未来 5 日是否达到 5%。
- 未来 5 日最大回撤。
- 每个策略单独胜率。
- 多策略交集胜率。
- XGB 信号分层胜率。
- 诊断风险标记后的失败率。
- IC / RankIC / 分组收益。
- PSR / DSR / PBO，防止漂亮回测误导。

验收标准：

- 每天盘后自动生成前一交易日出票追踪结果。
- Dashboard 能看到“昨日出票成功率、5日追踪进度、失败样本”。
- 任意一只历史候选都能查到：为什么选中、后续表现、是否失败、失败原因。

---

### Phase 2：候选因子宽表与横截面排序

优先级：高。

目标：

从“取交集”升级成“候选股综合排序”。

新增模块建议：

```text
src/candidate_factor_panel.py
src/final_ranker.py
src/historical_pattern_tags.py
```

候选股因子分类：

```text
策略因子：
  v10_rank, v1_rank, v4_rank, x1beam_rank, strategy_count

诊断因子：
  xgb_model_score, xgb_rule_score, xgb_blended_score, diagnosis_signal

历史表现因子：
  same_pattern_1d_win_rate, same_pattern_5d_hit_rate, same_pattern_drawdown

风险因子：
  boundary_risk, zero_price_flag, stale_data_flag, multi_source_divergence

交易性因子：
  liquidity_score, amount, turnover, limit_buyable, suspended_flag
```

初始综合排序公式：

```text
final_score =
  0.25 * strategy_consensus_score
+ 0.25 * xgb_blended_score
+ 0.20 * historical_pattern_score
+ 0.15 * tradeability_score
- 0.15 * risk_penalty
```

注意：

- 初始权重只作为起点。
- 后续必须用 Walk-forward 校准。
- 不允许直接凭主观经验长期固定。

验收标准：

- 每只候选股都有 `final_score` 和分项解释。
- 推送里不只显示交集，还显示“为什么排在前面”。
- Dashboard 能按综合分、策略共识、XGB分、历史胜率、风险分排序。

---

### Phase 3：情绪周期与可交易性过滤

优先级：高。

目标：

把 A 股短线最关键的市场环境纳入系统，不再让同一套策略在冰点、修复、主升、退潮里无差别运行。

新增模块建议：

```text
src/sentiment_features.py
src/regime_filter.py
src/tradeability_filter.py
src/position_sizing.py
```

情绪字段：

```text
trade_date
sentiment_state
limit_up_count
limit_down_count
fried_board_rate
seal_board_rate
max_consecutive_board
up_down_ratio
profit_effect_score
risk_appetite_score
limit_ecology_score
```

状态定义：

```text
冰点：原则空仓，只观察
修复：低仓位试错
主升：正常进攻
加速：只做核心，防高位分歧
分歧：降仓，精选
退潮：空仓或极低仓
混沌：降低策略权重
```

可交易性过滤：

- ST / 停牌。
- 一字涨停不可买。
- 流动性不足。
- 多源价格分歧。
- 边界涨幅风险。
- 数据陈旧或断档。
- 盘口/成交异常。

验收标准：

- 每天尾盘显示市场状态和建议总仓位。
- 每只候选股显示“可买 / 谨慎 / 不可买”及原因。
- 退潮期系统自动降低推送置信度或只推观察票。

---

### Phase 4：XGB 诊断升级为 Meta-labeling 过滤层

优先级：中高。

目标：

XGB 不直接选股，而是判断“策略发现的机会是否值得保留”。

新增模块建议：

```text
src/labels/triple_barrier.py
src/meta_labeling.py
src/diagnosis/shap_explainer.py
src/diagnosis/calibration.py
```

标签体系：

```text
上障碍：未来 5 日最高收益 >= 5%
下障碍：未来 5 日最大回撤 <= -3%
时间障碍：5 个交易日

1：先触发上障碍
0：先触发下障碍
-1：到期无明显结果
```

XGB 输出应升级为：

```text
prob_1d_high
prob_5d_5pct
prob_drawdown
prob_failed
shap_positive_reasons
shap_negative_reasons
calibrated_confidence_interval
```

验收标准：

- 每只候选股有“模型支持理由”和“模型反对理由”。
- XGB 高分但历史失败率高的票会被降权。
- XGB 低分但四策略强共识的票进入观察池而非直接删除。

---

### Phase 5：轻量 Agent 化

优先级：中。

目标：

不立刻推翻现有 pipeline，而是在现有模块外包一层“角色化调度”，逐步演进为 Agent Network。

建议先做同步事件驱动，不急着全量 asyncio。

新增模块建议：

```text
src/agents/message.py
src/agents/event_log.py
src/agents/sentinel.py
src/agents/hunter.py
src/agents/oracle.py
src/agents/arbiter.py
src/agents/hermes.py
src/agents/scribe.py
```

角色拆分：

```text
Sentinel：数据哨兵，负责数据源健康、快照质量、市场状态。
Hunter：策略猎手，负责运行 V10/V1/V4/X1Beam。
Oracle：诊断审查，负责 XGB、规则、反方风险。
Arbiter：仲裁排序，负责分级候选、最终排序、推送决策。
Hermes：输出信使，负责企微、Dashboard、报告。
Scribe：记录官，负责事件日志和可回放决策链。
```

消息格式：

```json
{
  "from": "sentinel",
  "to": "hunter",
  "type": "market_condition",
  "timestamp": "2026-06-14T14:45:00+08:00",
  "correlation_id": "tail-20260614-cycle1",
  "body": {}
}
```

验收标准：

- 每次尾盘运行都有完整事件链。
- 可以回放某天某轮为什么推送、为什么降权、为什么阻断。
- Agent 层不改变策略原始结果，只增加调度、解释和审计。

---

### Phase 6：自适应权重与不确定性估计

优先级：中长期。

目标：

让系统不再永远固定 `strategy_count` 和 `xgb_weight`，而是根据历史表现、市场状态和不确定性动态调整。

新增模块建议：

```text
src/adaptive_weights.py
src/bandit/contextual_bandit.py
src/uncertainty/bootstrap_rank.py
src/uncertainty/conformal.py
```

动态权重来源：

```text
最近20日胜率
最近20日RankIC
当前市场状态
策略近期失败率
候选排名稳定性
XGB预测区间宽度
```

可先实现：

- Thompson Sampling 排序探索。
- Beta 分布记录策略/模式成功失败。
- Bootstrap 估计候选排名稳定性。
- Conformal Prediction 给 XGB 概率区间。

验收标准：

- Dashboard 显示今日 V10/V1/V4/X1Beam/XGB 权重及变化原因。
- 每周输出“建议调权报告”。
- 自动调权默认只进入建议态，需要人工确认后生效。

---

### Phase 7：LLM 盘后反方审查

优先级：中长期。

目标：

LLM 不预测涨跌，只做复盘、归因、异常解释、反方风险审查。

新增模块建议：

```text
src/llm/reviewer.py
src/llm/anomaly_explainer.py
src/llm/attribution.py
src/llm/prompts/
```

使用场景：

- 盘后复盘当天候选。
- 解释为什么某只成功、某只失败。
- 识别数据异常可能原因。
- 生成每周策略表现归因。
- 给高分候选找“不能买的理由”。

硬约束：

- 不进入 14:50-14:57 尾盘实时路径。
- 输出必须结构化。
- 高风险建议必须人工确认。

验收标准：

- 每天盘后生成复盘报告。
- 每周生成失败样本归因。
- LLM 输出能转为“降权建议 / 加入观察池 / 标记异常样本”等动作草案。

---

### Phase 8：V3.0 远景

优先级：长期研究。

方向：

1. 股票知识图谱  
   使用 NetworkX 构建股票、行业、概念、资金关联、联动关系。

2. Digital Twin 虚拟沙箱  
   找历史相似日、相似形态，给候选股提供参考分布。

3. Transformer 横截面排序  
   不替代四策略，只作为排序增强器。

4. GNN 题材传播  
   判断龙头、跟风、补涨、退潮拖累。

验收标准：

- 这些能力只在验证体系成熟后进入。
- 任何新模型必须先通过 Walk-forward、RankIC、DSR/PBO 和实盘观察期。

---

## 五、近期 10 个最该落地的任务

1. `tracking_store.py`：保存每轮候选、诊断、推送、后续收益。
2. `backtest_metrics.py`：统一胜率、收益、回撤、Sharpe、Sortino、Calmar。
3. `factor_eval.py`：计算 IC / RankIC / 分组收益。
4. `candidate_factor_panel.py`：生成候选股因子宽表。
5. `historical_pattern_tags.py`：给候选打历史高胜率/高回撤/高冲高标签。
6. `sentiment_features.py`：市场情绪和涨停生态评分。
7. `tradeability_filter.py`：可交易性过滤和风险原因。
8. `final_ranker.py`：综合排序分和解释。
9. `meta_labeling.py`：基于候选股的 XGB 二次过滤。
10. `agents/event_log.py`：事件日志和决策链回放。

---

## 六、下一个实施目标

建议下一步不要先做 Agent 全重构，而是先做：

```text
目标 1：候选追踪库
目标 2：盘后胜率统计
目标 3：候选因子宽表
目标 4：历史模式标签回贴到 Dashboard
```

这四项完成后，系统才具备“知道自己哪里准、哪里错、哪类票更强”的基础。之后再做动态权重和 Agent 化，才不会变成无依据调参。

---

## 七、每日运行闭环

目标流程：

```text
14:20  X1Beam 预热
14:45  快照质量检查
14:47  情绪周期刷新
14:49  尾盘监控启动
14:50-14:57  多轮推送，推一轮刷新一轮控制台
15:05  记录当日候选和推送结果
次日盘后  追踪前一交易日候选的冲高/回撤/收盘表现
每周末  生成策略表现、失败样本、调权建议
```

---

## 八、最终验收标准

V2.0 进化成功，不是看功能多少，而是看是否能回答这些问题：

1. 今天为什么选这只票？
2. 它是哪几个策略选中的？
3. XGB 支持还是反对？
4. 历史同类模式胜率多少？
5. 当前市场情绪是否适合做？
6. 它是否真的可交易？
7. 如果失败，失败原因是什么？
8. 下次类似情况是否应该降权？
9. 自动结果和手动 V10/V1/V4/X1Beam 是否能持续复核？
10. 任何一次推送是否能完整回放决策链？

只要这些问题能稳定回答，分仓之神就从“出票工具”进化成了“可学习、可复盘、可自我改进的短线决策系统”。

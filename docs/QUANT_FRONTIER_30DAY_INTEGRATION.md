# 30天量化前沿计划吸收结论

> 生成日期：2026-06-14  
> 来源：用户提供的“30天量化前沿学习计划”。  
> 范围：只作为 `D:\ZTFHQ\分仓之神V2.0` 的进化路线补充；外部项目只读参考，不改原文件。

---

## 一、总体判断

这份方案值得吸收，但不能照单全收、也不能把 Transformer/GNN/LLM 提前塞进尾盘实时链路。

最可取的部分不是“上更复杂模型”，而是把 V2.0 从“能出结果”继续推进到“能证明结果可靠、能解释失败、能稳定复盘”的验证体系：

1. IC / RankIC / 分组收益，验证候选因子是否真的有排序能力。
2. Walk-forward / 参数敏感性，验证权重和参数不是偶然拟合。
3. PSR / DSR / PBO，防止漂亮回测误导实盘。
4. Triple Barrier / Meta-labeling，让 XGB 从单一诊断分升级为“策略候选是否值得保留”的过滤层。
5. 失败样本库和反方审查，把重庆港这类“冲高后大幅回落”的样本沉淀成可学习经验。
6. 行业/概念暴露、题材拥挤度、可交易性过滤，补上短线实盘最容易出问题的部分。

---

## 二、和当前 V2.0 的关系

### 已经落地或已有雏形

| 能力 | 当前状态 | 后续动作 |
|---|---|---|
| 四策略并行 V10/V1/V4/X1Beam | 已有 | 继续做手动/自动一致性复核 |
| XGB 诊断验证层 | 已有 | 升级为多目标概率、风险解释、Meta-labeling |
| X1Beam 预热缓存 | 已有 | 保持“完整预热才参与第四策略”的尾盘红线 |
| 追踪库 | 已有 `tracking_store.py` | 增加失败样本分类和周报 |
| 基础回测指标 | 已有 `backtest_metrics.py` | 接入策略级、因子级统一输出 |
| 候选因子宽表 | 已有 `candidate_factor_panel.py` | 增加 IC/RankIC、概念暴露、可交易性字段 |
| 历史模式标签 | 已有 `historical_pattern_tags.py` | 回贴到 Dashboard 和推送 |
| 情绪周期桥接 | 已有 `sentiment_regime.py` / `sentiment_overlay.py` | 增加情绪分组胜率和仓位建议 |
| 控制台工作台 | 已有 | 继续补“验证/失败样本/概念拥挤”栏目 |

### 新方案里最该补的缺口

| 优先级 | 能力 | 建议模块 |
|---|---|---|
| P0 | 因子排序能力验证 | `src/factor_eval.py` |
| P0 | Walk-forward 稳定性 | `src/walk_forward.py` |
| P0 | DSR/PBO 防过拟合桥接 | `src/overfitting_bridge.py` |
| P0 | 参数敏感性矩阵 | `src/parameter_sensitivity.py` |
| P1 | 失败样本库 | `src/failure_cases.py` |
| P1 | 真实可交易性过滤 | `src/tradeability_filter.py` |
| P1 | 行业/概念暴露和拥挤度 | `src/concept_exposure.py` |
| P1 | Triple Barrier 标签 | `src/labels/triple_barrier.py` |
| P1 | Meta-labeling 过滤层 | `src/meta_labeling.py` |
| P2 | 自适应权重建议 | `src/adaptive_weights.py` |

---

## 三、采纳到路线图的具体改法

### Phase 1A：验证科学性补强

目标：先证明 V10/V1/V4/X1Beam、XGB 分数、策略共识数、历史标签这些字段确实有预测/排序价值。

落地任务：

1. 新增 `src/factor_eval.py`。
2. 对 `strategy_count`、`xgb_blended_score`、`historical_pattern_score`、`sentiment_fit_score` 计算 IC / RankIC。
3. 输出 TopK 命中率、分组收益、分组回撤。
4. 形成 `outputs/reports/factor_eval_YYYYMMDD.json`。
5. Dashboard 增加“因子有效性”摘要。

验收标准：

```text
每个核心因子都能回答：
- 对次日冲高是否有效？
- 对 5日5%达标是否有效？
- 对最大回撤是否有预警？
- 高分组是否显著强于低分组？
```

### Phase 1B：Walk-forward 与防过拟合

目标：0.72/0.28 这类固定权重必须有滚动窗口验证，不能凭经验长期固定。

落地任务：

1. 新增 `src/walk_forward.py`。
2. 训练窗口暂定过去 60 个交易日，验证窗口 10 个交易日，步长 5 个交易日。
3. 验证对象包括单策略、多策略交集、XGB 分数、不同 blend_weight。
4. 新增 `src/overfitting_bridge.py`，只读桥接 `D:\ZTFHQ\unified-quant-platform\backtest_engine\standards\overfitting_defense.py`。
5. 输出 PSR / DSR / PBO 和“是否允许进入实盘观察”。

验收标准：

```text
能回答：
- 最近3个月 0.72/0.28 是否稳定？
- 0.6/0.4、0.8/0.2 是否更稳？
- 不同情绪周期下最佳权重是否不同？
- 当前参数是否存在过拟合嫌疑？
```

### Phase 2A：可交易性和题材拥挤

目标：把“回测能买、实盘买不到或买错节奏”的问题前置过滤。

落地任务：

1. 新增 `src/tradeability_filter.py`。
2. 给每只候选输出：可买 / 谨慎 / 不可买。
3. 过滤/降权原因包括：停牌、ST、一字涨停、流动性不足、数据陈旧、多源价格分歧、边界涨幅风险。
4. 新增 `src/concept_exposure.py`。
5. 输出行业、概念、主概念、同概念入选数、题材拥挤度、龙头/跟风/补涨/后排标签。

验收标准：

```text
推送和 Dashboard 不只显示“选中了谁”，还显示：
- 是否真的能买？
- 买不到或不宜追的原因是什么？
- 是否同一题材过度拥挤？
- 是题材龙头、跟风、补涨还是后排？
```

### Phase 3A：XGB 多目标与 Meta-labeling

目标：XGB 不做第五策略，而是判断“四策略发现的机会是否值得保留”。

落地任务：

1. 新增 `src/labels/triple_barrier.py`。
2. 标签定义：
   - 上障碍：未来 5 日最高收益 >= 5%
   - 下障碍：未来 5 日最大回撤 <= -3%
   - 时间障碍：5 个交易日
3. 新增 `src/meta_labeling.py`。
4. XGB 输出升级为：
   - `prob_1d_high`
   - `prob_3d_gain`
   - `prob_5d_5pct`
   - `prob_drawdown`
   - `prob_failed`
5. 后续接入 SHAP 或规则贡献解释，输出正向/反向理由。

验收标准：

```text
每只候选都能看到：
- 次日冲高概率
- 5日5%达标概率
- 最大回撤风险
- 失败概率
- 模型支持理由
- 模型反对理由
```

### Phase 4A：失败样本库和反方审查

目标：把“成功样本”和“失败样本”都变成系统资产。

落地任务：

1. 新增 `src/failure_cases.py`。
2. 字段包括：
   - `trade_date`
   - `code`
   - `name`
   - `case_type`
   - `selected_by`
   - `scores`
   - `future_return`
   - `failure_reason`
   - `data_issue`
   - `model_issue`
   - `regime`
   - `review_note`
3. 周末生成失败样本复盘报告。
4. LLM 只在盘后做反方审查，不进入尾盘实时选股链路。

验收标准：

```text
每周能自动回答：
- 哪些票是高分失败？
- 哪些票是模型低分但后续大涨？
- 哪些失败来自数据问题？
- 哪些失败来自情绪周期判断错误？
- 下一次同类样本是否应该降权？
```

---

## 四、暂缓进入实时链路的内容

| 内容 | 判断 |
|---|---|
| Transformer / PatchTST / iTransformer | 只做设计文档和离线研究，暂不进尾盘 |
| GNN 股票图谱 | 适合作为题材传播/概念拥挤研究，等概念暴露字段稳定后再做 |
| LLM 反方审查 | 只做盘后报告，不参与实时打分和推送 |
| Agent Network | 先做事件日志和决策链回放，不急着改成复杂多 Agent |
| 自动调权 | 先输出建议，必须有足够追踪样本后再人工确认启用 |

尾盘红线不变：

```text
14:50-14:57 只做本地可控计算。
不重训模型。
不跑 LLM。
不临时全量 X1Beam。
不在质量闸门失败时正式出票。
```

---

## 五、更新后的最近 10 个落地任务

这份 30 天方案吸收后，近期优先级建议调整为：

1. `src/factor_eval.py`：IC / RankIC / 分组收益。
2. `src/walk_forward.py`：滚动窗口稳定性验证。
3. `src/overfitting_bridge.py`：PSR / DSR / PBO 桥接。
4. `src/parameter_sensitivity.py`：top_n、min_strategy_success、xgb_weight、持有期扰动实验。
5. `src/tradeability_filter.py`：可买 / 谨慎 / 不可买及原因。
6. `src/concept_exposure.py`：行业/概念暴露、题材拥挤、龙头/跟风/补涨标签。
7. `src/failure_cases.py`：失败样本库和周报。
8. `src/labels/triple_barrier.py`：交易目标标签。
9. `src/meta_labeling.py`：XGB 二次过滤层。
10. `docs/DAILY_AUTOMATION_FLOW.md`：把 14:20-15:20 的每日闭环固化为运行手册。

---

## 六、结论

这份方案最值得吸收的，是“先验证、再调权、再上深度模型”的顺序。

对 V2.0 来说，下一步不应优先追求模型复杂度，而应优先补齐：

```text
因子有效性验证
Walk-forward 稳定性
防过拟合审计
可交易性过滤
题材拥挤风险
失败样本库
Triple Barrier / Meta-labeling
```

这些补齐后，再做 Transformer、GNN、LLM 反方审查和自适应权重，系统才不会变成“模型更高级，但错误更隐蔽”。

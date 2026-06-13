## 分仓之神 V2.0 进化路线图
> 2026-06-12 | 基于对五个子项目的全面研究

---

### 项目全景

```
                    分仓之神 V2.0 (统一调度中心)
                   /       |        |        \
                  /        |        |         \
         分仓之神V1.0    X1-Beam   XGB诊断   XGBZX报告
        (数据+快照)   (统计选股)  (模型验证)  (规则引擎)
                  \        |        |         /
                   \       |        |        /
                共享数据层 D:\ZTFHQ\data (手动复核)
                          D:\ZTFHQ\分仓之神\snapshots (自动快照)
```

| 项目 | 定位 | 核心能力 | 对V2.0的贡献 |
|------|------|----------|-------------|
| 分仓之神 V1.0 | 数据基础 | 多源行情抓取、TDX快照构建、质量闸门、动态复核、边界审计、选股追踪、V10/V1/V4运行 | 快照数据、策略脚本、质量体系、Dashboard参考 |
| X1 | Beam Search | 21指标×4目标统计模式发现、批量Beam搜索 | V2.0的X1Beam策略数据来源 |
| X1-XIN | Beam进化版 | 23指标Forest Beam Search、pipeline、screener | V2.0的X1Beam适配器直接调用 |
| XGB | 诊断引擎 | realtime_xgb桥接、多目标模型、决策层、风险控制 | V2.0的XGB诊断验证层 |
| XGBZX | 规则系统 | xgb_bin_model规则库、训练、诊断报告 | V2.0的Beam规则匹配数据来源 |

---

### 当前阶段：Phase 1 — 基础架构 ✓ (已完成)

- [x] 四策略并行适配器 (V10/V1/V4/X1Beam)
- [x] 多策略交集分析引擎
- [x] XGB诊断验证层 (对接XGB realtime bridge)
- [x] 尾盘自动化 (14:50-14:57 多轮推送)
- [x] 双通道企微推送 (投资参考格式，四档置信度)
- [x] 快照质量闸门 (交易日校验+空文件率+覆盖率)
- [x] Web控制台 (状态概览+策略结果+交集+XGB)
- [x] 交互式命令行控制台 (console.py)
- [x] 数据质量检查命令 (quality/snapshot)

---

### Phase 2: 控制台融合升级 (2-3天)

**目标**: 将V1.0控制台的成熟功能融合进V2.0 Web控制台

| 功能 | 来源 | 实现方式 |
|------|------|----------|
| 系统自检面板 | V1.0 `self-check` | 检查快照/筛选脚本/XGB模型/企微配置/任务状态 |
| 尾盘就绪检查 | V1.0 `tail-readiness` | 启动前验证数据源/快照/任务锁 |
| 实时任务进度条 | V1.0 `JobManager` | 尾盘运行中显示进度/阶段/耗时 |
| 历史运行列表 | V1.0 `recent_tail_runs` | 展示最近N次尾盘运行状态 |
| 选股追踪分析 | V1.0 `selection_tracking` | 次日冲高/5日5%/回撤统计展示 |
| 数据质量仪表 | V1.0 `tail-data-quality` | 质量分/覆盖率/剔除率可视化 |
| VIP比对面板 | V1.0 `vip_parity` | 自动vs手动差异对比 |
| 修复台账展示 | V1.0 `repair_ledger` | 自愈/降级/拦截事件列表 |
| 一键运行按钮 | V1.0 dashboard | 单次分析/尾盘监控/推送开关 |

**技术方案**: 扩展 `src/dashboard.py` 增加 `/api/self-check` `/api/tail-readiness` `/api/tracking` `/api/history` 等端点；扩展 `dashboard.html` 增加对应面板。

---

### Phase 3: 策略增强 (3-5天)

**3.1 X1Beam 深度对接**
- 当前: 通过 subprocess 调用 X1-XIN 的 `screener_beam.py`
- 优化: 直接 import `beam_core.py` + `config.py`，省去进程开销
- 增强: 读取 X1-XIN 的 `merge_beam_summary.py` 输出，获取各Tier胜率统计

**3.2 XGB 诊断增强**
- 当前: 使用 XGB realtime bridge 对候选股评分
- 优化: 接入 `multitarget_model.py` 的 `score_candidate_features`，获取多目标概率
- 增强: 接入 `decision_layer.py`，对评分结果做风控过滤

**3.3 边界审计接入**
- 移植 V1.0 的 `boundary_audit.py` 逻辑到 V2.0
- 对9%涨幅阈值附近的票做多源交叉确认
- 在推送和控制台中标记"硬过滤风险"候选

**3.4 历史胜率回溯**
- 利用 V1.0 的 `selection_tracking` 数据
- 对每只候选股显示历史同类标签的胜率
- 推送中增加"该模式历史胜率XX%"标注

---

### Phase 4: 数据可靠性加固 (3-5天)

**4.1 多源实时校验**
- V2.0 尾盘运行前自动检查 V1.0 快照的交易日/覆盖率/主源
- 快照异常时: 自动触发 V1.0 的 `tail-once` 重跑 → 等待 → 再检查
- 仍不合格: 阻断推送并企微告警

**4.2 交叉验证层**
- 对四策略共识候选股做额外的多源价格确认
- 使用 V1.0 的 `source_results` 对象做 cross-check
- 价差异常标记 "多源分歧" 降低置信度

**4.3 回测验证**
- 实现 `python main.py backtest --days 30` 命令
- 对历史快照运行四策略，计算:
  - 次日冲高胜率
  - 5日5%命中率
  - 最大回撤
  - 按置信度分层的收益表现

**4.4 熔断机制**
- 连续3轮策略失败 → 停止推送
- 快照交易日落后1天以上 → 停止推送
- 交集候选为0 → 标记低质量但不阻断

---

### Phase 5: 自动化完善 (2-3天)

**5.1 Windows计划任务整合**

| 时间 | 任务 | 说明 |
|------|------|------|
| 14:30 | `fetch` → V1.0 `prewarm-tail` | 数据预热 |
| 14:40 | V1.0 `xdxr-refresh` | 除权缓存刷新 |
| 14:45 | V2.0 `quality` | 快照质量检查 |
| 14:49 | V2.0 `tail-watch --push` | 尾盘监控循环 |
| 15:05 | V2.0 `report --type daily --persist` | 日内复盘 |

**5.2 自启动服务**
- V2.0 Web控制台随系统启动 (`python main.py dashboard --no-open`)
- console.py 支持 `--watch` 模式持续刷新

**5.3 失败告警**
- 推送失败 → 企微告警
- 快照异常 → 企微告警
- 策略全部失败 → 企微告警

---

### Phase 6: 智能进化 (长期)

**6.1 自适应权重**
- 根据各策略历史胜率动态调整交集权重
- XGB模型评分与历史实际表现对比，自动校准 blend 权重

**6.2 策略自进化**
- 记录每轮推送后实际表现
- 识别高胜率策略组合模式
- 自动推荐策略权重调整

**6.3 多周期回测**
- 周度/月度自动回测
- 生成策略绩效报告
- 企微推送进化建议

---

### 当前优先任务

```
[最高] Phase 2 — 控制台融合: 系统自检 + 尾盘就绪 + 历史运行列表
[高]   Phase 3.1 — X1Beam深度对接
[高]   Phase 4.1 — 多源实时校验 + 熔断
[中]   Phase 3.3 — 边界审计接入
[中]   Phase 5.1 — 计划任务整合
```

---

### 验证命令

```powershell
# Web控制台
python main.py dashboard

# 命令行控制台
python console.py

# 当前状态
python main.py quality
python main.py test-push
python main.py run --dry-run
```

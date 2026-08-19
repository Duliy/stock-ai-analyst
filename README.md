# 美股 AI 分析师（模拟盘 v1）

自动收集新闻与 K 线数据，由 LLM 分层决策（摘要粗活 / 决策复盘分离），经硬风控闸门后在 Alpaca 模拟盘自动下单；每笔交易自动复盘并沉淀教训，注入后续决策形成学习闭环。

## 架构

```
新闻(Alpaca/Benzinga + Finnhub可选)
   │  flash 模型摘要打分 (deepseek-v4-flash-0731)
   ▼
K线技术指标(SMA/RSI/ATR/动量)  ──►  pro 模型综合决策 (deepseek-v4-pro-0813)
                                        │  下单提案
                                        ▼
                              反方风控官复审（devil's advocate）
                                        │
                                        ▼
                        硬风控闸门（仓位/止损/熔断/置信度）
                          │                     │
                       自动下单            挂起待批准（仪表盘/飞书）
                                        │
                        平仓 → LLM 复盘 → 教训入库 → 注入后续决策
```

## 硬风控（config.yaml，AI 无权豁免）

| 参数 | 默认值 |
|---|---|
| 单票市值上限 | 10% |
| 总持仓上限 | 5 只 |
| 日内熔断 | 浮亏 3% 当天停止开仓 |
| 连续熔断 | 3 天停机待人工 |
| 最低置信度 | 6/10 |
| 组合总敞口风险 | ≤ 3% 净值 |

## 交易策略引擎（v2）

原则：**LLM 管方向和置信度，仓位与出场由数学规则决定**。

**建仓（Van Tharp 风险百分比模型）**
- 单笔风险预算 = 净值 × 0.75%
- 止损距离 = 2×ATR(14)，波动大的股票自动买得少
- 股数 = 风险预算 ÷ 止损距离，首仓只建计划仓位的 1/2

**补仓（金字塔，只加赢家）**
- 浮盈 ≥1R 才允许补仓，最多 2 次；永不向下摊平

**出场（三层止损 + 分批止盈 + 时间止损，纯规则不走 LLM）**
- 初始止损 = 入场价 - 2×ATR（上限 7%）
- +1R → 止损上移至成本价（保本）
- +2R → 卖出一半锁定利润
- 之后吊灯移动止损 = 持仓期最高价 - 2.5×ATR（只上移不下移）
- 时间止损：持仓 4 天浮盈仍 <+0.5R → 离场（好的入场会很快见效，死仓位占坑占风险预算）

**全局环境规则（v3）**
- 大盘过滤：SPY 跌破日线 50 均线 → 禁止一切买入（做多胜率与大盘 regime 强相关）
- 尾盘禁开仓：距收盘 <30 分钟禁止买入（隔夜跳空风险 + 止损隔夜不生效；卖出不限）
- 板块集中度：同板块 ≤3 只（相关性风险不体现在个股止损上，必须在组合层限制）
- 财报黑洞：财报前 3 天禁开新仓（二元跳空事件会让止损失效；需配置 Finnhub key）

**新闻时效性（v3）**
- 每条新闻带年龄标注喂给 flash，>6h 降权、>12h 仅作背景
- 同批文章哈希缓存 2h，不重复调摘要模型；pro 上下文含"最新新闻年龄"字段

## 快速开始（本机 & 甲方新机通用）

```bash
# 1. 安装（需 python3 + venv）
bash scripts/install.sh

# 2. 配置密钥
nano .env   # 填 CHARMLAND_API_KEY / ALPACA_API_KEY / ALPACA_SECRET_KEY

# 3. 手动试跑一轮（市场开盘时）
.venv/bin/python run_cycle.py

# 4. 启动仪表盘
.venv/bin/python run_dashboard.py   # http://localhost:8100

# 5. 安装定时任务（每小时决策 + 每10分钟异动 + 收盘日报 + 仪表盘常驻）
bash scripts/install_systemd.sh
```

## 打包交付甲方

```bash
bash scripts/package.sh   # 生成 stock-ai-YYYYMMDD.tar.gz（不含 .env / data / .venv）
```

甲方收到后：解压 → `bash scripts/install.sh` → 编辑 `.env` 填密钥 → `bash scripts/install_systemd.sh`。

> 迁移注意：`data/` 目录（SQLite 数据库 + 日报归档）不在包内。
> 如需带历史数据迁移，额外拷贝 `data/` 即可，代码零改动（所有路径相对项目根目录）。

## 目录结构

```
stock_ai/            # 核心包
  config.py          # config.yaml + .env 加载
  market_data.py     # Alpaca 行情/账户/持仓
  news.py            # 新闻（Alpaca Benzinga + Finnhub 可选）
  indicators.py      # 技术指标（纯 pandas，无重依赖）
  llm.py             # charmland flash/pro 调用（摘要/决策/复审/复盘）
  risk.py            # 硬风控闸门
  execution.py       # 下单 + 对账 + 平仓复盘触发
  pipeline.py        # 决策轮编排 / 异动加轮 / 日报
  db.py              # SQLite（决策/订单/交易/熔断/事件）
  notify.py          # 飞书通知（可选）
dashboard/           # FastAPI Web 仪表盘（含批准/拒绝操作）
scripts/             # 安装 / systemd / 打包
run_cycle.py         # 决策轮入口
run_dashboard.py     # 仪表盘入口
config.yaml          # 标的名单 / 风控参数 / 模型配置
```

## 二期路线（未实现，预留）

- 做空（止损逻辑与做多不对称，需单独设计）
- 动态标的发现（异动扫描进观察名单）
- 社交媒体情绪分析（X/Discord/Reddit）
- L3 信号源权重自适应（需数百笔样本后开启）
- 完整多 agent 辩论（先 A/B 验证轻量双角色胜率）

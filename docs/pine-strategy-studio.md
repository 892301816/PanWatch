# Pine 策略工作台（对标 TideView 观潮）

> 本文记录「用 REA 逆向 TideView 观潮 → 在 PanWatch 策略中心复刻 Pine 脚本引擎」的设计、
> 契约、口径差异与验证方式。逆向证据与完整清单见仓库外的
> `_ops/re/pine-engine-spec.md`（引擎语义）、`_ops/re/tideview-ui-inventory.md`（UI 能力）、
> `_ops/re/panwatch-current.md`（改造前的前端/后端现状）。

## 1. 背景

策略中心原有的「脚本」是 4 行 DSL（`FAST/SLOW/BUY/SELL` + `MA/EMA` + `CROSSUP/DOWN`），
只能表达双均线交叉，没有参数、绘图、止损、统计口径。

桌面端「TideView 观潮」是一套 TradingView 风格的 A 股客户端：Pine Script v5 解释器、
131 个原生指标、19 种 K 线类型、74 个画线工具、Monaco 编辑器、带权益曲线的回测面板。

目标：在 PanWatch 里复刻**能用**的那部分——Pine 脚本解释器 + 指标绘图 + 策略撮合 + 统计面板。

## 2. 逆向结论（决定了实现口径）

用 `rea-agents`（REA 6.7.0）对 `观潮 TideView.exe` 的 `app.asar` 做静态分析后确认：

| 项 | 结论 |
|---|---|
| 引擎形态 | 手写词法 + 递归下降 → **AST 树遍历解释器**，逐 bar 重放（不是 WASM/字节码） |
| series 键控 | 按 **call-site** 存储（`__cs_{line}_{col}_{fn}_{name}`） |
| 成交价 | 默认 **信号 bar 的下一根 open**；`process_orders_on_close` 才用当根 close |
| 数量 | 在下单 bar 收盘**冻结成绝对股数**，不是成交时重算 |
| 盘中路径 | `up<dn ? O→H→L→C : O→L→H→C`（最近极值先到），决定同 bar 止盈/止损先后 |
| MACD 柱 | `2 × (MACD − Signal)`（通达信惯例，**与 TradingView 不同**） |
| 窗口函数 na | `sma/highest/lowest/stdev/bb` **跳过 na 往前补取**，不是「含 na 即 na」 |
| T+1/涨跌停/整手 | 引擎**默认不管**（`marketSimMode="off"`），`ashare` 模式才拦 |
| 统计 | 总收益分母是**初始资金**；夏普年化因子用 `barsPerYear` 而非 252 |

PanWatch 这边保留了更严格的 A 股撮合闸门（涨跌停/停牌/整手/T+1），因为现有回测内核
已经按这个口径产出报告，混用两套口径会让历史报告不可比。

## 3. 架构

```
src/modules/strategy/pine/
├── errors.py        PineError / PineSyntaxError / PineRuntimeError（带行列）
├── lexer.py         缩进感知词法器（INDENT/DEDENT、隐式续行）
├── nodes.py         AST 节点
├── parser.py        递归下降语法分析
├── series.py        Series / ArgView / SiteState（na 用 None）
├── runtime.py       PineContext：常量表、动态成员、绘图产物
├── evaluator.py     逐 bar 解释执行（表达式 / 语句 / 用户函数 / request.security）
├── functions.py     内置函数：math/str/color/input/plot/strategy/array/…
├── ta_core.py       ta.* 核心指标（口径对齐观潮）
├── ta.py            指标库入口（有 ta_full 用 ta_full，否则退 ta_core）
├── broker.py        券商撮合器（下一根开盘成交、持久出场单、手续费、盯市）
├── statistics.py    回测统计（观潮口径）
└── engine.py        对外三个函数：compile_source / run_script / signal_rows
```

数据流：

```
前端脚本 ──POST /api/strategy/chart-compile──▶ 语法诊断（不取行情）
         ──POST /api/strategy/chart-preview──▶ chart_script.series_with_meta
                                                 ├─ 旧 DSL → 4 行正则
                                                 └─ Pine   → pine.run_script
                                                              ├─ plots → bars[plotName]
                                                              ├─ 成交    → template_entry/exit
                                                              └─ stats/trades/equity_curve
         ──POST /api/strategy/backtest───────▶ 现有 A 股回测内核（零改动）
```

关键点：Pine 撮合器产出的成交会**反推回信号 bar**（成交在 i+1，信号打点在 i），
因此现有 `run_single_backtest` 的「收盘出信号 → 次日开盘成交」逻辑完全不用改。

## 4. API 变更

| 接口 | 变更 |
|---|---|
| `POST /api/strategy/chart-compile` | **新增**。`{script}` → `{ok, engine, diagnostics[{line,column,message}], title, kind, warnings, inputs, plots}` |
| `POST /api/strategy/chart-preview` | 请求加 `inputs?`；`script` 上限 2000→200000；响应加 `engine/plots/inputs/warnings/script_title/script_kind/stats/trades/equity_curve`，`bars[]` 透传任意 plot 列 |
| `POST /api/strategy/backtest` | `chart_script` 上限 2000→200000；**新增 `inputs?`**，会写进 `config.inputs` 并透传到脚本执行；`config` 额外接受 `engine`、`inputs` |
| `POST /api/strategy/chart-script/ai` | `current` 上限 2000→20000 |

前端配套（`frontend/`）：

| 文件 | 作用 |
|---|---|
| `packages/api/src/strategy.ts` | `StudioBar` 动态 plot 列、`StudioPlotSpec` / `StudioInputMeta` / `StudioStats` / `StudioTrade` / `ChartPreviewResult` / `ChartCompileResult`；新增 `chartPreview`、`chartCompile` |
| `src/components/StudioPineInputs.tsx` | 脚本参数面板（int/float/bool/string/text/color/timeframe/options，按 group 分组，300ms 防抖） |
| `src/components/StudioPineStats.tsx` | 回测统计面板（12 项指标 + 权益曲线 SVG + 成交表） |
| `src/components/StudioChart.tsx` | `plots` 动态曲线：overlay 进主图、非 overlay 按名字分副图 pane，纳入时间轴/十字线联动 |
| `src/pages/StrategyStudio.tsx` | 编辑器 20000 字上限 + 行号栏 + 「检查语法」（诊断可点击定位）+ 类型徽标 + 参数面板 + 统计面板 |

配置契约（`template_runtime.normalize_config`）从「只允许 `chart_script` 单键」放宽为
`{chart_script, engine?, inputs?}`，`inputs` 最多 64 项、值只允许数字/布尔/短字符串。

## 5. 与 TideView / TradingView 的口径对照

已对齐：下一根开盘成交、数量下单时冻结、手续费三种类型、`pyramiding`、持久 `strategy.exit`
（stop/limit/profit_pct/loss_pct/trail）、逐 bar 收盘盯市、期末强平、`stats` 全部字段公式、
`ta.rsi` 的 gain=loss=0 → 50、窗口函数跳过 na、MACD 柱 ×2。

**差异（务必知道）**：

1. PanWatch 的撮合闸门更严：涨跌停开盘、零成交量、整手、T+1 默认拦截（观潮默认不拦）。
2. `ta.macd` 柱是 ×2 的国内口径，直接和 TradingView 对数会对不上。
3. `request.security` 只支持**本品种**的日/周/月/季/年重采样；跨品种返回 `na` 并给出 warning。
4. `ta.vwap` 退化为全样本累计（管线只有日线，没有分钟级日内锚定）。
5. 未实现：`table.*`（记录 warning 后忽略）、`matrix.*`、`type/enum/method` 声明（跳过）、
   `strategy.risk.*`（占位不生效）、`polyline`、`chart.point` 等冷门 API。
6. 指标库是 **62 个** `ta.*` 函数，不是观潮的 131 个原生指标；`ta.donchian` 多返回一个中轨、
   `ta.supertrend` 的 direction 取 +1/-1（与 TV 符号相反）、`ta.fisher` 只返回单值。
7. 脚本里的 `plot` 若返回元组（未解构）会记 warning 并跳过该曲线，不会画错数据。

## 6. 用法

```pine
//@version=5
strategy("均线趋势跟随", overlay=true,
     initial_capital=100000,
     commission_type=strategy.commission.percent, commission_value=0.1,
     default_qty_type=strategy.percent_of_equity, default_qty_value=100,
     pyramiding=1)

len = input.int(20, "均线周期", minval=2, maxval=200)
atrLen = input.int(14, "ATR周期")
atrMult = input.float(2.0, "止损ATR倍数")

ma = ta.sma(close, len)
atrVal = ta.atr(atrLen)

if ta.crossover(close, ma)
    strategy.entry("多头", strategy.long)
    strategy.exit("止损", "多头", stop=close - atrMult * atrVal)
if ta.crossunder(close, ma)
    strategy.close("多头")

plot(ma, "均线", color.blue, linewidth=2)
plot(ta.rsi(close, 14), "RSI", color.orange)   // 名字含 RSI → 自动进副图
```

脚本会被自动识别：含 `//@version` 或 `indicator(` / `strategy(` / `library(` 声明走 Pine，
其余走旧的 4 行 DSL，历史草稿不需要迁移。

## 7. 验证

```powershell
cd PanWatch
# 后端
.\.venv\Scripts\python.exe -m pytest tests/test_pine_engine.py tests/test_pine_integration.py tests/test_pine_ta.py -q
.\.venv\Scripts\python.exe -m pytest tests -q -k "strategy or chart or backtest or pine" --ignore=tests/test_tradingagents_v050_compat.py
# 前端
cd frontend; npm.cmd run build; .\node_modules\.bin\vitest.cmd run
```

最近一次结果：后端 **192 passed / 1 skipped**；前端 `tsc -b && vite build` 通过，
`vitest` 31 个文件 126 个用例全绿（进程退出码 1 来自 vitest worker 的
`onTaskUpdate` 心跳超时，改动前的基线同样存在，与本功能无关）。

- `tests/test_pine_engine.py`：解析、绘图、历史引用、if/for/函数、成交与期末强平
- `tests/test_pine_integration.py`：引擎路由、plot 列透传、参数覆盖、回测链路 inputs
- `tests/test_pine_ta.py`：62 个指标的数值口径（46 例）
- `/api/strategy/chart-compile` 可直接用来做语法回归

## 8. 后续可做

1. 补 `ta_full.py`：把观潮 131 个原生指标里剩下的逐个对齐（优先 CVD/VWAP 家族/缠论接入）。
2. 画线工具（趋势线/斐波那契/文字）——前端已有 canvas 层，可复用。
3. Monaco 编辑器替代 textarea（含 Pine 语法高亮与 `setModelMarkers` 诊断）。
4. 多周期面板、`request.security` 跨品种（需要把其它标的历史一起装进引擎）。
5. 交易明细展开（逐笔成交 + 图上定位 + 闪烁），对标观潮 `TradesView`。

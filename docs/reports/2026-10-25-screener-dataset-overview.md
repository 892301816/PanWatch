# 2026-10-25 开发报告：选股页 / 全市场日K数据集 / 个股概览增强

> 分支：`codex/fundamentals-and-expiry`  
> 范围：选股模块、行情数据集（多源日K）、自选分组、配置包 v2、个股概览（业务结构/估值/财务）与 AI 上下文  
> 运行环境：本地构建镜像 `panwatch:local`，端口 8085，数据卷 `panwatch_panwatch_data`

## 1. 背景与目标

本次迭代围绕「选股 → 看详情 → 加自选 → AI 研判」这条主线展开，目标是：

1. 提供类似同花顺的选股页：动态分组、结构化条件、AI 一句话生成条件、连涨天梯、板块强弱。
2. 选股结果不再逐个联网取数，改用本地全市场快照 + 本地日K判断，支持分页展示全部结果。
3. 把 TSP（tick-stock-panel）的多源日K逻辑移植进来：`fuyao → tickflow → tdxquant` 优先级 + 自动切换 + 手动选择，并支持每日定时/手动全市场同步。
4. 选股结果可直接批量加入自选，自选支持分组。
5. 个股概览补齐业务与收入结构、估值与规模、财务基本面，并让这些数据进入 AI 建议的上下文。

## 2. 交付内容

### 2.1 选股模块（`src/modules/screener/`）

| 文件 | 职责 |
|---|---|
| `snapshot.py` | 新浪全市场快照 → `screener_snapshots` 表；TTL 交易时段 60s / 非交易时段 900s；启动预热 |
| `engine.py` | 条件求值、排序、板块归属、涨停池/连板天梯、概念板块强弱、sparkline 附加 |
| `constants.py` | 字段目录（含 `board`、`limit_up_count_5d` 等）、内置策略预设 |
| `ai_parse.py` | 一句话 → 条件 DSL；字段白名单校验，未知字段丢弃；条件数 < 2 时 `any` 强制改为 `all` |
| `concepts.py` | 东财 bkzj 概念板块与成分股 → `screener_concept_members`，默认前 160 个板块 |
| `api.py` | `/meta`、`/run`、`/ladder`、`/boards`、`/parse`、`/refresh`、`/status`、`/rules` CRUD |

关键行为：

- `limit=0` 表示返回全部结果（上限 6000），前端每页 80 条分页。
- `with_spark=false` 为默认：K线缩略图按页懒加载（`/api/dataset/spark`），避免一次算几千条。
- 涨停相关条件走本地 5 日涨停池聚合（`limit_up_count_5d` / `limit_up_days_5d` / `limit_up_last_days_ago`），修正了「只在今日涨停股里统计」导致的 0 结果问题。
- 排除创业板/科创板的自然语言条件解析为 `board` 字段，而不是概念名。

前端 `frontend/src/pages/Screener.tsx`：

- 三视图：条件选股、连涨天梯、板块强弱。
- 「添加条件」后内联 AI 一句话入口（`prompts/screener_parse.txt`）。
- 结果卡展示名称/代码/K线缩略图/涨跌幅/换手/市值/板块，支持应用条件回显（AND/OR 可见）。
- 批量勾选 → 加入自选（可选已有分组或新建分组）。
- 点击股票打开右侧抽屉（60 日 K、涨停统计、概念标签），抽屉底部新增「打开个股概览」按钮，直接打开 `StockInsightModal`。

### 2.2 行情数据集（`src/platform/dataset/`）

| 文件 | 职责 |
|---|---|
| `providers/fuyao.py` | 同花顺 REST，dump 缓存；窗口按 10 年上限裁剪（`days ≤ 2400`，起始回看 ≤ 3500 天） |
| `providers/tickflow.py` | TickFlow SDK，Free 档批量被拒，改用 `klines.get` 逐只；批量失败自动回退 |
| `providers/tdxquant.py` | 通达信原生 TCP（`his_dats`），快照走 tqs 包装 |
| `router.py` | 优先级链 + 60s 冷却 + 返回行数阈值判定，失败自动切下一个源 |
| `store.py` | 数据源读取（仅 `fuyao/tickflow/tdxquant`）、日K upsert、设置读写 |
| `sync.py` | 股票池（仅 6 位 A 股）、增量/全量、完成交易日推断、自动同步判断、进度 |
| `scheduler.py` | APScheduler 每日同步，默认 15:45，设置项 `dataset_sync_schedule` |
| `api.py` | `/status`、`/sync`、`/progress`、`/providers/{id}`、`/providers/{id}/test`、`/schedule`、`/bars/{symbol}`、`/spark` |

同时新增 marketdata 包内的三个 vendor（`packages/marketdata/src/marketdata/vendors/{fuyao,tickflow,tdxquant}.py`），并注册到 kline/quote 类型，使原有 `market_get` 链路也能用上新源。

### 2.3 数据源页面整合

- 不新增独立「行情」页，在「数据源」页内嵌入 `DatasetPanel`（同步状态、立即全量/增量同步、进度、定时时间、三个源的启用与测试）。
- fuyao / tickflow / tdxquant 的凭据字段直接加在原有数据源分区里。
- `server.py` 增加 `DATA_SOURCE_SEEDS` 与 `reconcile_market_sources()`：启动时补种数据源、迁移旧 Key、一次性设置优先级（`market_source_priority_v2`）。

### 2.4 本地日K兜底

`src/modules/market/api/klines.py`：

- 先走 `KlineCollector` 实时源；拿不到时，日线级别回退本地 `daily_bars`，返回体带 `source: live|local|none`。
- 个股页、选股抽屉、概览迷你 K 线因此不再因外部数据源抖动而空白。

### 2.5 自选分组

- `Stock.group_name`（迁移 132），`StockResponse` 暴露该字段；新增 `POST /api/stocks/batch` 与 `GET /api/stocks/groups`。
- 关注页支持分组筛选 chips、分组标签；分组筛选下禁用拖拽排序。
- 选股页批量加自选可直接写入分组。

### 2.6 配置包 v2（`src/modules/automation/api/templates.py`）

导出/导入覆盖：settings 白名单、agents、stocks（含分组）、AI 服务与模型（含 Key）、通知渠道、数据源（含 Key）、账户与持仓、价格提醒、选股策略。

- 导入时对模型/渠道做 ID 重映射；兼容 v1。
- 排除 `auth_password_hash`、`jwt_secret`、`market_source_priority_v2`。
- 导入接口改为 `async`，导入后重载调度器不再报「no running event loop」。
- 注意：导出文件包含明文 API Key，迁移后请妥善保管。

### 2.7 个股概览增强

新增 `src/modules/research/stock_overview.py`：

- `fetch_business()`：东财 F10 主营构成（`RPT_F10_FN_MAINOP`），缓存 6h；按产品/行业/地区分组，过滤「其中:xxx」明细，用收入金额指纹去掉换标签的重复分组。
- `business_summary()`：给 AI 的一行摘要，按 产品 > 行业 > 地区 取第一组真正拆开的（≥2 项）。
- `snapshot_metrics()`：从 `screener_snapshots` 取换手率、PE、市值、振幅。

接口：

- `GET /api/insights/business/{symbol}`：主营构成。
- `GET /api/insights/fundamentals/{symbol}`：财务基本面（marketdata fundamentals 数据类型，失败自动切源；估值缺失时用快照补）。
- `/api/quotes/{symbol}` 与 `/api/quotes/batch`：行情源不带估值时，用本地快照补换手率/PE/总市值/流通市值/振幅（市值统一为「元」）。
- `/api/insights/batch`：同样补数，供概览弹窗使用。

概览 UI（`frontend/packages/biz-ui/src/components/stock-insight-modal.tsx`）新增：

- 「业务与收入结构」卡片（报告期 + 各口径收入/占比/毛利率）。
- 「估值与规模」卡片（换手率、PE(TTM)、PB、总市值、流通市值、振幅）。
- 「财务基本面」卡片（营收、归母净利、同比、毛利率、净利率、ROE、资产负债率、EPS）。

### 2.8 AI 分析上下文

`src/modules/automation/intraday_monitor.py`：

- `collect()` 打开 `include_fundamentals=True`（此前默认关闭，导致财务小节永远为空）。
- `build_prompt()` 新增三段：`## 估值与规模`、`## 财务基本面`、`## 业务与收入结构`；业务结构取 `business_summary()`。
- `prompts/intraday_monitor.txt` 增加对应研判原则，明确「技术面决定何时动，估值/基本面/业务结构决定仓位上限与持股信心；缺失数据不得臆造」。
- 「加仓快速评估」的基本面上下文也补上主营构成，并修正市值单位（原先把「元」当「亿」写进 prompt）。

## 3. 关键设计决策

1. **数据获取分层**：选股走本地快照表，K 线走本地 `daily_bars`，实时行情/分钟线才联网。这样选股全市场扫描不依赖单次网络请求数量。
2. **多源优先级 + 冷却**：`fuyao → tickflow → tdxquant`，单源失败后 60s 内不再试，避免每次同步都卡在同一个坏源。
3. **估值补数下沉到行情接口**：概览弹窗调的是 `/quotes/{symbol}`，此前的补数只做在 `/insights/batch`，导致顶部三项显示 `--`。现统一在 `_quote_to_response()` 内补齐。
4. **业务结构去重按金额指纹**：东财不同口径经常是同一批数字换标签（如 000858 行业与地区收入完全相同），按类型优先级 + 金额指纹去重，避免 UI 冗余、AI 重复计权。
5. **AI 摘要选「拆分最细」的组**：`制造业 100%` 这类单行汇总没有信息量，优先取 ≥2 项且类型优先级最高的口径。

## 4. 数据源现状与限制

| 项 | 现状 |
|---|---|
| 东财 push2 / push2his | 本机网络被重置，不可用；选股快照改用新浪，概念用 `data.eastmoney.com` |
| 新浪全市场快照 | 可用，约 5500+ 只；**不含量比**，因此量比字段为空（AI 上下文直接省略该行） |
| 东财 F10 主营构成 | 可用（`datacenter.eastmoney.com`） |
| marketdata fundamentals | push2 段失败，自动落到 datacenter，字段完整（含 PE/PB） |
| fuyao | 可用；单次跨度最多 10 年，代码里裁剪为 2400 天 |
| TickFlow | 可用；Free 档不支持批量，已改为逐只 `klines.get` |
| TdxQuant | 可用；原生 TCP，不依赖 HTTP |

## 5. 关键文件索引

```
src/modules/screener/           选股引擎、快照、概念、AI 解析、API
src/platform/dataset/           多源日K：providers/router/store/sync/scheduler/api
packages/marketdata/.../vendors/ fuyao.py tickflow.py tdxquant.py（+ registry.py）
src/modules/research/stock_overview.py   主营构成 / 快照估值指标
src/modules/research/api/insights.py     业务、财务、批量洞察接口
src/modules/market/api/quotes.py         单只/批量行情 + 快照补数
src/modules/market/api/klines.py         本地日K兜底
src/modules/automation/api/templates.py  配置包 v2
src/modules/automation/intraday_monitor.py  盘中 prompt 与财务采集
prompts/intraday_monitor.txt     AI 研判规则
prompts/screener_parse.txt       一句话选股解析规则
frontend/src/pages/Screener.tsx  选股页
frontend/src/components/DatasetPanel.tsx  数据源页同步面板
frontend/packages/biz-ui/src/components/stock-insight-modal.tsx  个股概览
docs/reports/2026-10-25-screener-dataset-overview.md  本报告
```

## 6. 验证记录

- 本地日K数据量：约 3.84M 条（全市场，3 年窗口）。
- 三个数据源 K线/行情连通性测试通过（`/api/dataset/providers/{id}/test`）。
- 配置包 v2 导出包含全部配置分区，导入回环验证通过。
- AI 一句话解析可正确生成 `board` 条件（如「排除创业板」）。
- 主营构成实测：
  - 600900 境内水电 84.9%
  - 000858 酒类产品 94.5%
  - 600519 茅台酒 85.7%
  - 002380 工业自动化与信息化 99.0%
- 财务基本面实测：600900 营收 379.29 亿 / ROE 6.55%；000858 净利同比 +89.30%；002380 PE 40.21。
- 行情补数实测 `/quotes/600900`：换手 0.7353%、PE 20.885、总市值 7205.89 亿、振幅 2.21%。
- 盘中 prompt 渲染实测包含「估值与规模 / 财务基本面 / 业务与收入结构」三段。
- 前端 `corepack pnpm build` 通过；容器重启后 healthy。
- `pytest tests/test_stocks_quote_routing.py tests/test_stock_quote_tail_routing.py tests/test_marketdata_client.py -q`：8 passed。

## 7. 已知问题与后续

1. **量比缺失**：新浪快照不提供，若需要可在同步流程里单独取量比字段，或盘中用「当日量 / 5 日均量」做近似（需注意非时间归一，会高估早盘）。
2. **概念覆盖有限**：只索引按资金排序的前 160 个概念，冷门股可能显示「暂无概念归属」。
3. **业务结构缓存**：进程内 6h 缓存，重启后首次请求会稍慢；暂无持久化。
4. **财务报告期滞后**：用的是最近一期财报，Q3 数据在 10 月底前后才齐。
5. **新模块缺单测**：选股引擎、数据集路由、业务结构去重均无自动化测试，目前靠容器内脚本验证。
6. **配置包含明文 Key**：迁移场景方便，但导出文件不能外传。

## 8. 运维注意

- 代码通过只读挂载生效（`src`、`prompts`、`packages`、`server.py`、`frontend/dist`），改 Python 后重启容器即可，不必重建镜像。
- 前端改动必须先在 `frontend/` 执行 `corepack pnpm build`，产物挂载到 `/app/static`。
- 额外依赖（pyarrow / tickflow / tdxaidata 等）装在宿主机 `./python-deps`，挂载到 `/app/extra-deps` 并通过 `PYTHONPATH` 生效；`docker compose up -d` 重建容器不会丢。
- 数据源 Key、模型 Key 均存在数据库，迁移请用「配置包导出」。

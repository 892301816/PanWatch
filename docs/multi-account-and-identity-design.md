# PanWatch 多账户、身份绑定与数据隔离 — 改造方案

> 状态：**背景分析（已被定稿取代部分结论）**
> 适用版本：`main`（迁移最大版本号 136）
> 目标：一个 PanWatch 实例支持多账户注册；每个账户可绑定 邮箱 / 手机 / QQ / 微信；每个账户拥有独立的 AI 配置、数据源、通知渠道与持仓数据，互不可见。
>
> ⚠️ **范围已定稿**：决策为「单实例多租户 + 仅管理员建号 + 本期只做邮箱密码 + 老数据归现有账号」。自助注册、短信、QQ/微信、SMTP 已移出本期，以 [multi-account-plan-v1.md](./multi-account-plan-v1.md) 为准；本文保留作为现状证据与方案对比的参考。

---

## 0. 一页结论

**现状是典型的单租户应用**：账号只是 `app_settings` 里的两行 KV（`auth_username` / `auth_password_hash`），JWT 的 `sub` 恒为字符串 `"user"`，72 张表没有一张带 owner 列（`src/modules/administration/api/auth.py:29-31,87-91,186`）。所以"加一个账户"不是加一张表，而是把整个应用从单租户改成多租户。

需求实际是**三件独立的事**，成本差一个数量级：

| # | 需求 | 本质 | 主要改动 | 估算 |
|---|---|---|---|---|
| 1 | 账户可绑定邮箱/手机/QQ/微信，可注册新账户 | 新增**身份层** | 新表 + 新 API + 前端登录注册页 | 中 |
| 2 | 每账户独立 AI / 数据源 / 通知配置 | 拆掉**全局单例** | 4 张配置表加 owner + 修 7 处进程级缓存 | 中 |
| 3 | 每账户独立持仓等业务数据 | **全站数据隔离** | 51 张表加 owner + 553 处查询 + 8 个调度器 | 大 |

**推荐路线**：`ContextVar 租户作用域 + SQLAlchemy ORM 自动过滤（fail-closed）`。
一次改造 `get_db` 覆盖 181 个端点、一次改造 `SessionLocal` 覆盖 159 处服务层会话，不必逐条改 553 个 `.query()`。SQLite 没有行级安全（RLS），只能在 ORM 层模拟，因此必须配一套跨租户测试矩阵兜底。

**三个最容易翻车的点**（按风险排序）：

1. **后台调度器没有请求上下文** —— 8 个调度器现在都是全局遍历，`build_context(agent_name)` 只有一个参数（`server.py:1264`），拿不到"这是谁的股票"。
2. **进程级单例与全局唯一语义** —— `marketdata_client._md`、`dataset/router._instances`、`qfq._FUYAO_KEY`、`ai_failover._AI_FAIL_UNTIL`、`accounts._PORTFOLIO_RESULT_CACHE`，以及 5 处"设默认值时清全表"（`providers.py:149,169,241`、`channels.py:54,70`）。
3. **`stocks` 一表两用** —— 它同时是"自选股列表"和"持仓/提醒/计划的外键锚点"，且 `Stock.positions` 带 `cascade="all, delete-orphan"`（`models.py:112-114`）。多用户下 A 删自选会级联删掉自己的持仓记录，A、B 还会各自建一条同代码的 stocks 行。

**必须先说清的现实约束**：QQ / 微信登录不是写代码就能用的。需要企业主体、ICP 备案域名、开放平台应用审核（详见 §6.4）。而且自托管项目的 OAuth client_secret 不能随镜像分发——每个部署实例都得自己注册一个应用。建议把 OAuth 做成可插拔 provider，先落**邮箱 + 密码**这条零门槛路径。

**工作量**：完整方案约 **40~50 人日**；MVP（邮箱注册 + 全量数据隔离，不做第三方登录）约 **22~28 人日**。

> ⚠️ 在动手前请先看 §2 的"另一个选项"：如果目标只是"我和家人各用各的"，**跑多个实例**比改造多租户便宜一个数量级。

---

## 1. 现状诊断

### 1.1 身份与认证

| 事实 | 证据 |
|---|---|
| 账号 = `app_settings` 两行 KV，「用户」这个概念在库里不存在 | `api/auth.py:29-31` |
| 密码哈希是**无盐单轮 sha256**，登录用字符串等值比较 | `api/auth.py:78-80,229` |
| JWT 为 HS256 / 30 天，payload 只有 `exp/iat/sub`，`sub` 恒为 `"user"` | `api/auth.py:83-93` |
| `verify_token` 只返回 bool，**丢弃 payload**，身份无法从 token 还原 | `api/auth.py:96-104` |
| `get_current_user` 正常分支恒 `return "user"` | `api/auth.py:186` |
| **空库期间完全放行**：没有密码哈希时直接 `return None`，所有受保护接口匿名可写 | `api/auth.py:166-169` |
| 改密码不校验旧密码、无登录限流、token 无吊销机制 | `api/auth.py:218-249` |
| `personal_access_tokens` 没有 `user_id`，注释自认「单用户应用」 | `models.py:1370-1392` |

好消息：鉴权是在 `bootstrap/application.py:76` 用一个依赖对象复用到 34 个 router 上的（`dependencies=protected`），各模块的 `api.py` **并没有** import `get_current_user`。这意味着身份层改造的接触面很小。

坏消息：全库没有任何地方取用 `get_current_user` 的返回值（都写成 `_`），也就是说身份信息今天根本没有流到业务层。

### 1.2 配置存储

- `app_settings` 是纯 KV 表（`key` 全局唯一，`models.py:242`），混装了两类东西：部署级（`http_proxy`、`jwt_secret`、`dataset_sync_schedule`）与用户级（`notify_*` 策略、`stock_link_platform`、`ui_avatar`）。
- `ai_services.api_key` **明文入库**，且 `GET /providers/services` **明文返回**给前端（`providers.py:58-63`）；`data_sources.config`、`notify_channels.config` 同样明文返回（`datasources.py:111`、`channels.py:42`）。
- `is_default`（AI 模型、通知渠道）是**全局单例**语义：设置时 `db.query(...).update({"is_default": False})` 清整张表（`providers.py:149/169/241`、`channels.py:54/70`）。
- 头像已经是事实上的用户态却全局：单键 `ui_avatar` + 固定文件名 `avatar.{ext}`（`api/settings.py:104-110,165`）——多用户会共用同一张头像。

对比证据：数据源侧已经有成熟脱敏实现（`_mask` 在 `dataset/api.py:41-46`，"掩码含 `****` 则保留原 Key" 在 `dataset/api.py:141-147`），AI 服务商侧只是没接上。

### 1.3 数据归属

`models.py` 里 72 个 `__tablename__`。按"一份数据该归谁"分类见 §4.3，这里只说结构性问题：

**外键链**：`accounts → positions → stocks` 全链 CASCADE；`trade_records.account_id` CASCADE、`stock_id` SET NULL；`dca_plans`、`dividend_ledger`、`price_alert_rules.stock_id`、`price_alert_hits`、`stock_agents`、`operation_plans.stock_id` 都挂在 `stocks.id` 上。

**`stocks` 的双身份**：它既是自选清单（`market/api/stocks.py:210,268,356` 的 CRUD），又是所有"某只股票"记录的唯一锚点。这带来两个多用户问题：

1. `cascade="all, delete-orphan"` 让"删自选"直接级联删持仓（`models.py:112-114`）。现在靠 API 层手工拦截"有持仓不许删"（`stocks.py:362-364`）兜底，多用户下这个兜底更容易漏。
2. `group_name` / `sort_order` 是**用户字段**，却和"标的信息"挤在同一行。A 的分组名会写进 B 也看得到的那条记录（改造后两人各自有行，这点自然解决，但历史数据回填要小心）。

**`trade_ingest` 会自动建自选**（`portfolio/trade_ingest.py:68-72`），且 `resolve_account` 按 id/名称全表查、无 owner 过滤（`trade_ingest.py:32-52`）——成交同步接口（走 PAT）今天可以命中任意人的账户。

### 1.4 访问方式与改造面（决定技术方案的关键数字）

| 模式 | 数量 | 说明 |
|---|---|---|
| `Depends(get_db)` | **181** | 端点取会话的唯一姿势，改造的**单点杠杆** |
| `SessionLocal()` 直连 | **159**（60 文件） | 服务层/调度器自建会话，如 `db = db or SessionLocal()`（`strategy/gate.py:234`、`playbooks.py:157`、`factor_weights.py:43`） |
| `.query(...)` | **553**（仅 `src/modules`） | 逐条加过滤是不可能的 |
| `query().delete()` / `query().update()` | **3 / 6** | 批量写很少，风险可控 |
| `db.delete(obj)` | 20 | 走 ORM，天然被过滤覆盖 |
| `text(...)` 原生 SQL | **145** | 其中 78 在 `migrations.py`、44 在 `database.py`，**业务代码只有 23 处**且基本打公共表 |
| Repository 层 | **2 个**（`portfolio/repository.py`、`assistant/repository.py`） | 等于没有数据访问层 |

结论：**逐查询改造（改 553 处）不现实；在会话层拦截能覆盖 95%+ 的读写路径；原生 SQL 风险远小于预期。**

### 1.5 后台任务

8 个调度器（`agent_scheduler`、`price_alert_scheduler`、`paper_trading_scheduler`、`news_brief_scheduler`、`context_maintenance_scheduler`、`dataset_scheduler`、`wealth_scheduler`，加上 MCP 日志清理），全部是**全局遍历**：

| 位置 | 现在遍历什么 | 多用户下的问题 |
|---|---|---|
| `server.py:937` `load_watchlist_for_agent` | 该 Agent 绑定的全部 `StockAgent` | 无 user 维度，A 的股票会进 B 的上下文 |
| `server.py:968-1036` `load_portfolio_for_agent` | `Account.enabled == True` 的全部账户 | 直接把全站持仓喂给一个 AI |
| `server.py:1115` `resolve_ai_model` | `AIModel.is_default == True`（全局唯一） | 所有人共用同一个模型/额度 |
| `server.py:1165` `resolve_notify_channels` | `NotifyChannel.is_default == True`（全局唯一） | 所有人推送到同一个 IM |
| `price_alert_engine.py:325` | 全部 enabled 提醒规则 | 扫描全站 |
| `paper_trading_engine.py:242` | `PaperTradingAccount.first()` | 硬编码单例账户 |
| `wealth_scheduler.py:61,64` | 全部 enabled 账户跑定投/分红 | 跨账户入账 |
| `entry_candidates.py:573-580` | 全表 `Position` join `Stock` | 把所有人持仓当"已持有" |
| `assistant/task_runner.py:144` | 注入 `session_factory=SessionLocal` | 后台会话拿不到"我是谁" |

采集器（`platform/marketdata/collectors/*`）**零写入**，K 线落 `dataset/store.py`、题材落 `theme_service.py`、新闻落 `news_cache`，都是公共表 —— **采集层不需要隔离，这是本次改造最大的成本节省点**。

### 1.6 前端

- 技术栈：Vite 5 + React 18 + react-router v6，入口 `frontend/src/main.tsx`，路由集中在 `frontend/src/App.tsx:291-307`。
- token 存在 `localStorage`（`token` / `token_expires`），**无 refresh token、无 auth store**（`packages/api/src/client.ts:11-31`）。
- **三处重复注入 Authorization**，多账户/刷新必须收口：`client.ts:42`、`sse.ts:55-56`、`tradingagents.ts:203-207`。
- 401 处理直接调 `logout()`（`client.ts:69-72`），后者整页硬跳 `window.location.href='/login'`（`client.ts:18`）；`App.tsx:80` 虽然把 `state.from` 塞进了跳转，但 `Login.tsx` 从不读它，所以来源路径实际上丢了。
- 前端**从未调用** `/api/auth/me`（`packages/api/src/auth.ts` 只有 `status/login/setup`），所以登录后前端不知道"我是谁"，也无处展示用户名/绑定状态。
- 登录页 `frontend/src/pages/Login.tsx:23-29` 挂载即查 `/auth/status`，`initialized=false` 时切成"设置密码"模式。
- 设置页是**单页锚点滚动**，不是子路由：`frontend/src/pages/Settings.tsx:662-668` 的跳转胶囊 + 各 `#sec-*` section。绑定入口应加在这里（新增 `#sec-account`），不要新开顶级路由。
- **命名冲突**：`src/components/AccountMenu.tsx` 是登录用户的下拉菜单（`title="账户与设置"`），而后端 `/accounts` 是**券商交易账户**，前端也叫"账户"（`Stocks.tsx:1903,1912,2734`）。改造前必须先把前者改名 `UserMenu`，否则新功能的文案会彻底乱掉。

### 1.7 测试现状

- `tests/conftest.py` 没有任何伪造登录态的 fixture；大多数 API 测试自己 `FastAPI()` + `include_router`，**不带** `protected`（如 `test_pats_api.py:28-41`、`test_avatar_setting.py:30-40`）——认证分支实际零覆盖。
- 有两条架构约束测试必须遵守：`tests/test_architecture_boundaries.py` 禁止 `platform → modules` 反向依赖、禁止跨模块 import 别的模块 `models/repository/api`；`tests/test_module_ownership.py` 锁定实现文件的归属位置。
- 迁移框架：`schema_migrations(version,name,checksum,success,error)`，按 `MIGRATIONS` 元组顺序执行，checksum 是函数源码哈希（改了函数就会重跑），当前最大版本 136（`migrations.py:2379-2416`）。

---

## 2. 先想清楚：真的需要多租户吗？

在投入 40 人日之前，评估另一个选项：

| 方案 | 代码改动 | 隔离强度 | 运维成本 | 适用场景 |
|---|---|---|---|---|
| **D. 每用户一个实例**（多容器 + 各自 data 卷） | ~0 | 物理隔离，最强 | 每用户一份进程/内存/端口，升级要逐个操作 | 2~5 人小圈子、同机部署 |
| **B. 单实例多租户**（本文推荐） | 40~50 人日 | 应用层隔离 | 一份进程，升级一次 | 想做成产品、用户数不确定、要求"注册即用" |

判据：**如果需要"用户自己注册并开始使用"这个体验，D 做不到，只能选 B。** 如果只是自己 + 家人，D 的性价比高得多。本文后续按 B 展开。

---

## 3. 三个"账户"必须先分清

这个项目里"账户"已经有两个含义，新增登录账号后会变成三个，必须在命名上彻底分开，否则代码和 UI 都会长期混乱：

| 概念 | 建议命名 | 现有实体 | 说明 |
|---|---|---|---|
| 登录账号 | `user` / 用户 | **不存在**（要新建） | 绑邮箱/手机/QQ/微信的那个 |
| 第三方身份 | `identity` | 不存在 | 一条 (provider, external_id) → 一个 user |
| 券商交易账户 | `brokerage_account` | `accounts` 表 | 招商证券/华泰证券那种，属于某个 user |
| 模拟盘账户 | `paper_account` | `paper_trading_account` 表 | 属于某个 user |

落地建议：
- 数据库：新表统一 `user_` 前缀；`accounts` 表**保持名字不变**（避免全仓改名），但字段注释与文档里一律称"券商账户"。
- API：新路由挂 `/api/auth/*` 与 `/api/user/*`；`/api/accounts` 不动，避免前端大改。
- 前端：`AccountMenu.tsx` → `UserMenu.tsx`；UI 文案里登录账号叫"账号"，券商账户叫"券商账户"。

---

## 4. 目标数据模型

### 4.1 身份层（新增 5 张表）

```python
# src/platform/persistence/models.py 追加

class User(Base):
    """登录账号。一个 user 可绑定多条 identity。"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=True)   # 可空:纯第三方注册时自动生成
    password_hash = Column(String(255), nullable=True)          # 可空:纯 OAuth 账号可无密码
    display_name = Column(String(64), default="")
    avatar = Column(String(255), default="")                    # 文件名,实体落 data/avatars/{user_id}.{ext}
    role = Column(String(16), default="member")                 # admin / member
    status = Column(String(16), default="active")               # active / disabled / pending
    token_version = Column(Integer, default=0)                  # 递增即吊销该用户全部 token
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class UserIdentity(Base):
    """第三方/验证过的登录身份。(provider, external_id) 全局唯一 => 一个身份只能绑一个账号"""
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_identity_provider_external"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(32), nullable=False)     # email / phone / qq / wechat / wechat_mp
    external_id = Column(String(255), nullable=False) # 邮箱小写归一 / E.164 手机 / openid 或 unionid
    verified_at = Column(DateTime, nullable=True)
    is_primary = Column(Boolean, default=False)
    meta = Column(JSON, default={})                   # 昵称、头像 url、unionid、appid
    created_at = Column(DateTime, server_default=func.now())


class VerificationCode(Base):
    """邮箱/短信验证码。只存哈希,一次性消费,带尝试次数上限"""
    __tablename__ = "verification_codes"
    __table_args__ = (Index("ix_vcode_target_purpose", "target", "purpose", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    target = Column(String(255), nullable=False)   # 归一化后的邮箱/手机
    channel = Column(String(16), nullable=False)   # email / sms
    purpose = Column(String(32), nullable=False)   # register / login / bind / reset
    code_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0)
    consumed_at = Column(DateTime, nullable=True)
    ip = Column(String(64), default="")
    created_at = Column(DateTime, server_default=func.now())


class AuthEvent(Base):
    """身份变更审计:登录/失败/注册/绑定/解绑/改密/吊销"""
    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_event_user_time", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event = Column(String(32), nullable=False)
    provider = Column(String(32), default="")
    ip = Column(String(64), default="")
    user_agent = Column(String(255), default="")
    detail = Column(JSON, default={})
    created_at = Column(DateTime, server_default=func.now())


class UserSetting(Base):
    """用户级设置。全局部署级设置仍留在 app_settings"""
    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_setting"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(64), nullable=False)
    value = Column(Text, default="")
    description = Column(String(255), default="")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```

**为什么 identity 单独建表而不是在 `users` 上加 `email`/`phone`/`qq_openid`/`wx_unionid` 四列**：

1. 微信有 openid（每应用一个）与 unionid（跨应用）两种 ID，同一用户可能有多条；
2. 加第四、第五种登录方式时不用改表；
3. `UNIQUE(provider, external_id)` 一条约束就挡住了"一个微信绑两个账号"这个最典型的越权场景；
4. 未来加"游客账号升级""账号合并"时，identity 行可迁移而不动主体。

### 4.2 配置层

| 表 | 改法 |
|---|---|
| `ai_services` / `ai_models` | **两张都加** `user_id`。虽然 model 能从 service 推出归属，但 `GET /providers/models` 是直接 `db.query(AIModel)` 的（`providers.py:138`），只加在 service 上会漏掉这条路径 |
| `data_sources` | 加 `user_id`；`is_default` 语义按 user 独立 |
| `notify_channels` | 加 `user_id`；`is_default` 按 user 独立 |
| `agent_configs` | 加 `user_id`：**把它变成用户私有表**，注册时按代码里的 `AGENT_SEED_SPECS` 为每个用户种一份 |
| `app_settings` | 只留部署级；用户级迁到 `user_settings` |

`agent_configs` 变成私有表而不是"全局定义 + 用户覆盖表"，是个刻意的取舍：它能把 `automation/api/agents.py`（700+ 行）的改动降到接近零，代价是每个用户多 6 行数据。对自托管应用这个取舍明显划算。

`is_default` 的单例语义改成**每用户唯一**，用 SQLite 支持的部分唯一索引：

```sql
-- 每个用户最多一个默认模型（部分唯一索引，NULL 与 0 不参与约束）
CREATE UNIQUE INDEX uq_ai_model_default_per_user
  ON ai_models(user_id) WHERE is_default = 1;

CREATE UNIQUE INDEX uq_notify_channel_default_per_user
  ON notify_channels(user_id) WHERE is_default = 1;
```

更省事的做法：把「默认」从列上移到 `user_settings.default_ai_model_id`，`is_default` 列只做兼容。推荐后者——它同时解决了"设默认时清全表"这个危险写法。

### 4.3 数据归属总表（72 张表）

**A. 用户私有 —— 51 张，必须加 `user_id`**

| 组 | 表 |
|---|---|
| 持仓 (7) | accounts, positions, stocks, stock_agents, trade_records, dca_plans, dividend_ledger |
| 提醒 (3) | price_alert_rules, price_alert_hits, notify_throttle |
| 模拟盘 (3) | paper_trading_account, paper_trading_positions, paper_trading_trades |
| 助手 (10) | chat_conversations, chat_messages, assistant_context_snapshots, assistant_task_runs, assistant_task_events, assistant_task_steps, assistant_tool_invocations, assistant_tool_approvals, assistant_tool_permissions, assistant_artifacts |
| 令牌 (2) | personal_access_tokens, mcp_call_logs |
| 计划/建议 (6) | operation_plans, stock_suggestions, entry_candidates, entry_candidate_feedback, entry_candidate_outcomes, suggestion_feedback |
| 策略 (5) | strategy_playbooks, strategy_drafts, strategy_revisions, strategy_decisions, backtest_runs |
| 选股 (3) | screener_rules, screener_runs, screener_run_items |
| 分析 (8) | analysis_history, agent_runs, agent_context_runs, agent_prediction_outcomes, stock_context_snapshots, news_topic_snapshots, portfolio_risk_snapshots, agent_configs |
| 配置 (4) | ai_services, ai_models, data_sources, notify_channels（另新增 `user_settings`，不计入 72） |

**B. 全站公共 —— 21 张，保持全局，不加 owner**

daily_bars, daily_bars_qfq, dataset_sync_state, dataset_providers, dividend_events, news_cache, hot_themes, screener_snapshots, screener_concept_members, market_scan_snapshots, market_regime_snapshots, strategy_catalog, strategy_weights, strategy_weight_history, factor_weights, factor_weight_history, strategy_signal_runs, strategy_outcomes, strategy_factor_snapshots, log_entries, schema_migrations

> 公共表分两类：一类是**市场事实**（K 线、新闻、概念成分），全站一份；一类是**部署级策略参数**（`strategy_weights`、`factor_weights`），管理员的算法调参不该被单个用户覆盖。

**C. 待定 —— 3 张，建议先私有，留观察**

`stock_context_snapshots`、`news_topic_snapshots`、`agent_prediction_outcomes`：它们的内容由"市场数据 + 用户配置的模型"共同生成，若模型人人不同则必须私有；若想省 AI 成本可做成带 `(symbol, date, model_label)` 键的公共缓存。**先私有，等有真实数据再考虑共享**。

**唯一约束必须改的清单**（加列后冲突，不改会直接报错或串数据）：

| 表 | 现有约束 | 改为 |
|---|---|---|
| `stocks` | 无（靠应用层去重） | `UNIQUE(user_id, symbol, market)` |
| `agent_configs` | `name UNIQUE` | `UNIQUE(user_id, name)` |
| `app_settings` | `key UNIQUE` | 不动（部署级）；用户级走 `user_settings` |
| `personal_access_tokens` | `token_hash UNIQUE` | 不动（哈希全局唯一是对的），加 `user_id` 列 |
| `notify_throttle` | `UNIQUE(agent_name, stock_symbol)` | `UNIQUE(user_id, agent_name, stock_symbol)` |
| `analysis_history` | `UNIQUE(agent_name, stock_symbol, analysis_date)` | 加 `user_id` 进键 |
| `entry_candidates` | `UNIQUE(stock_symbol, stock_market, snapshot_date)` | 加 `user_id` 进键 |
| `stock_context_snapshots` | `UNIQUE(symbol, market, snapshot_date, ...)` | 加 `user_id` 进键 |
| `portfolio_risk_snapshots` | `UNIQUE(snapshot_date, market)` | 加 `user_id`（并考虑再拆到 account 维度） |

---

## 5. 隔离方案选型

### 5.1 三个候选

| | A. 逐查询显式改造 | **B. 会话级租户作用域（推荐）** | C. 每用户独立 SQLite |
|---|---|---|---|
| 改动量 | 553 处查询逐条加 `.filter(user_id==...)` | 改 2 个会话入口 + 51 张表加列 | 每请求切引擎 + 迁移 ×N |
| 漏一处 | 直接数据泄漏 | 默认被过滤 | 物理隔离 |
| 新代码心智负担 | 高，必须记得写 filter | 低，自动 | 低 |
| 跨用户功能（管理员总览） | 容易 | 需显式 `system_scope()` | 难 |
| 公共数据（K 线全市场） | 不变 | 不变 | 需要 ATTACH 或共享库，麻烦 |
| 迁移/运维 | 一次性 | 一次性 | 每次加用户跑一遍迁移 |
| 结论 | ❌ 不现实 | ✅ 采用 | 备选（>50 用户再考虑） |

### 5.2 B 的实现骨架

核心思路：**租户身份放在 `ContextVar`，ORM 每次执行时读它并自动加 `WHERE user_id = ?`。**作用域没设就访问私有表 → 直接抛错（fail-closed），而不是返回全量数据。

```python
# src/platform/persistence/tenancy.py（新增，属于 platform，不依赖 modules）

from contextvars import ContextVar
from contextlib import contextmanager

SYSTEM_SCOPE = object()
_scope: ContextVar[int | object | None] = ContextVar("panwatch_tenant", default=None)

class TenantScopeMissing(RuntimeError):
    """在无用户作用域时访问了私有表。"""

def current_user_id() -> int | None:
    v = _scope.get()
    return v if isinstance(v, int) else None

def is_system_scope() -> bool:
    return _scope.get() is SYSTEM_SCOPE

@contextmanager
def tenant_scope(user_id: int):
    """后台任务/服务层显式声明当前处理的用户。"""
    token = _scope.set(int(user_id))
    try:
        yield
    finally:
        _scope.reset(token)

@contextmanager
def system_scope():
    """仅限迁移种子数据、管理员总览等确需跨租户的场景。可 grep 审计。"""
    token = _scope.set(SYSTEM_SCOPE)
    try:
        yield
    finally:
        _scope.reset(token)
```

```python
# src/platform/persistence/database.py 里挂监听器

class OwnedMixin:
    """所有用户私有表的基类。"""
    @declared_attr
    def user_id(cls):
        return Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True)

@event.listens_for(Session, "do_orm_execute")
def _enforce_tenant_on_select(state):
    if not state.is_select:
        return
    owned = [m for m in (state.all_mappers or ()) if issubclass(m.class_, OwnedMixin)]
    if not owned:
        return                                  # 公共表：不干预
    uid = current_user_id()
    if uid is None and not is_system_scope():
        raise TenantScopeMissing(
            "访问私有表但当前没有用户作用域；请用 tenant_scope(user_id) 或 system_scope() 包裹"
        )
    if is_system_scope():
        return
    stmt = state.statement
    for mapper in owned:
        stmt = stmt.options(
            with_loader_criteria(mapper.class_, lambda cls: cls.user_id == uid,
                                 include_aliases=True)
        )
    state.statement = stmt

@event.listens_for(Session, "before_flush")
def _stamp_tenant_on_write(session, flush_context, instances):
    uid = current_user_id()
    for obj in session.new:
        if not isinstance(obj, OwnedMixin):
            continue
        if obj.user_id is None:
            if uid is None:
                raise TenantScopeMissing("新建私有表记录但无用户作用域")
            obj.user_id = uid
        elif uid is not None and obj.user_id != uid:
            raise TenantScopeMissing("跨租户写入被拒绝")
    if uid is not None:
        for obj in session.dirty:
            if isinstance(obj, OwnedMixin) and obj.user_id != uid:
                raise TenantScopeMissing("跨租户修改被拒绝")
```

HTTP 侧只需要一个依赖把作用域设起来，`get_db` 与 159 处 `SessionLocal()` 就会自动带上租户：

```python
# 在现有 get_current_user（auth.py:160-186）基础上改造：返回 User 对象
async def get_current_user(credentials=Depends(security), db=Depends(get_session)) -> User:
    ...
    user = load_user_from_jwt(credentials.credentials, db)
    _scope.set(user.id)          # 关键：进入请求就把作用域钉住
    return user

# get_db 保持签名不变，内部拿 scope
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

### 5.3 这个方案的已知边界（必须写测试验证）

1. **自建线程池不传播 ContextVar。** `asyncio.create_task` 会复制上下文，`anyio.to_thread`（FastAPI 同步端点）会复制，但裸 `concurrent.futures.ThreadPoolExecutor.submit()` **不会**。已知使用点：`dataset/sync.py:214`、`screener/snapshot.py:289`、`tradingagents/operations.py:187`、`dataset/api.py:97`。修法：`ctx = copy_context(); pool.submit(ctx.run, fn)`，或显式把 `user_id` 传进去。
2. **`with_loader_criteria` 对"只选列"的语句不保证生效。** `select(Position.cost_price)` 这类不带实体的语句可能拿不到条件。规避：优先 `select(Entity)`；聚合一律 `select(func.count()).select_from(Entity)`。**必须写一条测试专门盯这个**。
3. **批量写不保证被过滤。** 现有 3 处 `query().delete()`、6 处 `query().update()` 请手工加 `user_id` 条件，别赌框架。
4. **原生 SQL 完全绕过。** 业务代码只有 23 处 `text()`，逐个审一遍即可，其中多数打公共表。
5. **fail-closed 会打到启动流程。** `seed_sample_stocks()`（`server.py:281-301`）、`seed_agents()`、`reconcile_data_sources()` 都在 `lifespan` 里裸跑（`server.py:1673-1684`），加了监听器后要么包 `system_scope()`，要么改成"给指定用户种数据"。**建议后者**：注册时按用户种，启动时只做全局初始化。
6. **159 处 `SessionLocal()` 的语义变了。** 在请求里它们是安全的自增，在后台线程里会抛 `TenantScopeMissing`。这**正是想要的效果**，但要有清晰的错误信息，否则线上表现为莫名 500。

---

## 6. 认证与身份绑定设计

### 6.1 注册模式（部署级开关，管理员可配）

`registration_mode` 存 `app_settings`：`open`（任何人可注册）/ `invite`（需要邀请码）/ `closed`（仅管理员建号）。

- 首个用户 = `admin`，由「首次设置」流程创建（沿用现有 `/auth/setup`，但写入 `users` 表）。
- 默认值建议 `closed`，从单用户升级上来的老部署不该突然对外开户；管理员在设置页显式打开。
- 邀请码：`invitation_codes(code_hash, created_by, max_uses, used_count, expires_at)`，一张小表即可。

### 6.2 邮箱（零门槛，第一优先级）

- 发信走 SMTP，配置存 `app_settings`（部署级：`smtp_host/port/user/password/from_name/tls`）。
- 流程：`POST /api/auth/email/send-code` → 6 位码，10 分钟有效，60s 冷却，每目标每小时 5 次，验证码只存 sha256。
- 注册：邮箱 + 验证码 + 密码（可空，允许"验证码即登录"）。
- 绑定：已登录用户 + 目标邮箱验证码 → 写 `user_identities`。
- 未配置 SMTP 时该入口自动隐藏（`GET /api/auth/capabilities` 返回可用登录方式）。

### 6.3 手机号（可插拔 SMS Provider）

- 抽象 `SmsProvider.send(phone, code)`，实现 `aliyun` / `tencent` / `null`。
- 凭据存部署级设置，管理员在设置页填（阿里云 AccessKey + 签名 + 模板 ID）。
- **现实约束**：国内短信必须实名、签名报备、模板审核，且多数要求域名备案或 APP 资质。自托管用户大概率没有 → 这条做成"可选，配了才出现"。
- 手机号一律存 E.164 归一格式，展示时脱敏 `138****8000`。

### 6.4 QQ / 微信（这条要提前对齐预期）

**必须说清的四件事**：

1. **资质门槛**：微信开放平台「网站应用」的微信登录需要企业/个体工商户主体 + 开放平台认证（300 元/年）+ 网站 ICP 备案；QQ 互联同样要备案域名与应用审核。个人主体基本走不通。
2. **每个部署实例都得自己注册应用**：OAuth 的 `redirect_uri` 必须是备案域名，`client_secret` 不能随开源镜像分发。所以**不可能**做一个"所有 PanWatch 实例共用的微信登录"——除非你自建一个中央中转服务，那会打破"数据私有、自托管"的卖点。
3. **openid 的语义**：微信 openid 是按 appid 区分的，同一用户在你的网站应用和公众号里 openid 不同；跨应用识别要用 unionid。所以 `provider` 要区分 `wechat`（网站应用）/ `wechat_mp`（公众号）/ `wechat_mini`（小程序），`external_id` 优先存 unionid。
4. **桌面扫码 vs 移动端**：网站应用的微信登录是二维码扫码，只适合桌面；移动端 PWA 里要跳转微信 App 授权。

**建议的落地方式**（把风险挡在代码之外）：

```python
# src/platform/identity/oauth/base.py
class OAuthProvider(Protocol):
    name: str
    def authorize_url(self, state: str, redirect_uri: str) -> str: ...
    def exchange(self, code: str, redirect_uri: str) -> OAuthProfile: ...  # -> external_id, nickname, avatar
```

- provider 实现放 `src/platform/identity/oauth/`，凭据读部署级设置；**未配置的 provider 在 `/api/auth/capabilities` 里不出现**，前端自然只显示可用的。
- 首期只实现 `wechat`（网站应用）一个，先验证整条 OAuth + 绑定/登录链路；QQ 用同一接口复制即可。
- 前端回调：`/oauth/callback?provider=wechat&code=...&state=...`，由后端换 openid 后**重定向回前端并带一次性 ticket**（不要直接把 JWT 放 URL query，会进浏览器历史和 Referer）。

### 6.5 绑定 / 解绑的安全规则（最容易出漏洞的地方）

| 规则 | 原因 |
|---|---|
| 绑邮箱/手机必须先验证该目标 | 否则可以把别人的邮箱绑到自己账号 |
| 绑定 OAuth 必须在**已登录**状态下发起，且 `state` 与当前 session 绑定 | 防 CSRF 式的身份劫持（攻击者把自己的微信绑到受害者账号） |
| 登录用 OAuth 的 `state` 必须用一次性 cookie 校验 | 防登录 CSRF |
| `UNIQUE(provider, external_id)` | 一个身份不能绑两个账号 |
| 解绑前必须校验"剩余可用登录方式 ≥ 1" | 否则账号变砖 |
| 要解绑最后一种方式时，强制先设密码 | 同上 |
| 绑定的身份已属于别的账号 → **拒绝并提示"该邮箱已绑定其他账号"**，绝不静默合并 | 静默合并是账号接管漏洞的经典形态 |
| 合并账号做成独立的高风险功能：需两侧都验证 + 明确确认 | 同上 |
| 改密/解绑/换绑 → `token_version += 1` 吊销全部 token | 现有实现完全没有吊销能力 |

### 6.6 登录与 token

- JWT payload 改为 `{sub: str(user.id), ver: user.token_version, jti, iat, exp}`，有效期从 30 天降到 **7 天**（自托管场景可配）。
- 校验时：解出 `sub` → 查 `users` → 比对 `ver`（不等即吊销）→ 检查 `status == "active"`。
- 不做 refresh token（前端是纯 Bearer + localStorage，加 refresh 要同时改 cookie/CSRF，收益不抵成本）。以后要上，再单独一期。
- **删掉"空库放行"逻辑**（`auth.py:166-169`）。改为：无任何 user 时，除 `/api/auth/status`、`/api/auth/setup` 外一律 401。前端 `RequireAuth` 增加"未初始化 → 引导到 setup"分支。

### 6.7 PAT 与 MCP

- `personal_access_tokens` 加 `user_id`；创建时写入当前用户；`pats.py` 的列表/吊销加 owner 过滤。
- `pat_auth.py` 校验通过后同样 `_scope.set(user_id)`，这样 ingest（`portfolio/api/ingest.py`）和 MCP 工具会自动落在正确的租户下。
- `mcp_call_logs` 加 `user_id`。
- `trade_ingest.resolve_account` / `resolve_stock` 必须在租户作用域内查（否则按 id 能命中别人的账户）。

---

## 7. 配置隔离改造清单

| 项 | 现在 | 改成 |
|---|---|---|
| AI 服务商 | `ai_services` 全局 | 加 `user_id`；`GET /providers/services` 只返回自己的 |
| 默认模型 | `AIModel.is_default` 全局唯一 + 清全表 | 默认值移入 `user_settings.default_ai_model_id`；`resolve_ai_model(user_id, ...)` 加参数 |
| API Key 脱敏 | `providers.py:63` 明文返回 | 复用 `dataset/api.py:41-46` 的 `_mask` 模式；前端提交掩码时保留原值 |
| 凭据加密 | 明文落库 | 新增 `platform/security/crypto.py`（Fernet，密钥来自 `PANWATCH_SECRET_KEY` 或 `data/secret.key`，首启生成 0600）；加密 `ai_services.api_key`、`data_sources.config` 中的 key/token、OAuth client_secret |
| 数据源 | `data_sources` 全局 | 加 `user_id`；`DATA_SOURCE_SEEDS` 在**用户注册时**种一份（现在在启动时种，`server.py:791`）；`reconcile_data_sources` 按用户跑 |
| 通知渠道 | `notify_channels` 全局 + `is_default` 清全表 | 加 `user_id`；默认渠道改为 `user_settings.default_channel_ids`；`channels.py:54,70` 的 update 加 owner 条件 |
| 用户设置 | `app_settings` 混装 | 部署级留 `app_settings`；`notify_*` 策略、`stock_link_platform`、`panwatch_base_url`、`panwatch_base_url` 迁 `user_settings` |
| 头像 | 单键 + 固定文件名 | `users.avatar` + `data/avatars/{user_id}.{ext}`；旧文件迁给 user 1 |
| `http_proxy` | `apply_proxy_env` 写进程 env（`server.py:59-74`） | **保持部署级全局**。进程级 env 是天然单租户假设，改成按用户代理等于给所有 httpx 调用加参数，收益低风险高。UI 上标注"仅管理员可改" |
| `dataset_providers` | 全局 | 保持全局（行情数据集是部署级基础设施） |

> 注意 `server.py:862` 起的 `reconcile_market_sources` / `reconcile_data_sources` 是"按 provider 对账并删孤儿"的逻辑，多用户后要改成"逐用户对账"，否则会删掉别人的数据源配置。

---

## 8. 业务数据改造

### 8.1 加列与回填（迁移 137+）

```python
def _m137_user_tables(conn):
    conn.execute(text("""CREATE TABLE IF NOT EXISTS users (...)"""))
    conn.execute(text("""CREATE TABLE IF NOT EXISTS user_identities (...)"""))
    # ... verification_codes / auth_events / user_settings

    # 1) 从 app_settings 迁移出 user 1
    username = conn.execute(text("SELECT value FROM app_settings WHERE key='auth_username'")).scalar() or "admin"
    pw = conn.execute(text("SELECT value FROM app_settings WHERE key='auth_password_hash'")).scalar()
    conn.execute(text("INSERT INTO users(username, password_hash, role, display_name) VALUES (:u,:p,'admin',:u)"),
                 {"u": username, "p": pw})
    uid = conn.execute(text("SELECT last_insert_rowid()")).scalar()

    # 2) 51 张私有表加列并回填
    for table in OWNED_TABLES:
        _add_column_if_missing(conn, table, "user_id", "ALTER TABLE ... ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        conn.execute(text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"), {"uid": uid})
```

要点：

- SQLite `ALTER TABLE ADD COLUMN` **不能加 NOT NULL 无默认值的列**，所以流程必须是「加可空列 → 回填 → 建索引 → 重建表加 NOT NULL」或者干脆保持可空 + 应用层兜底。对这个规模，**建议保持列可空但由 `before_flush` 保证写入**，避免 SQLite 重建 51 张表（慢且易错）。
- 回填后必须 `CREATE INDEX ix_<table>_user_id`，否则加了过滤条件会全表扫。
- 唯一约束改动需要重建表（`CREATE TABLE xx__new ... INSERT SELECT ... DROP ... RENAME`），`database.py:560-603` 已有可复用的写法。
- 迁移里涉及 ORM 的部分要包 `system_scope()`；纯 `text()` 的 DDL/DML 不受监听器影响。

### 8.2 `stocks` 双身份怎么解

最小改动、不动外键的方案：

1. `stocks` 加 `user_id`，建 `UNIQUE(user_id, symbol, market)`。
2. `Stock.positions` 的 `cascade="all, delete-orphan"` **改为 `save-update, merge`**（`models.py:112-114`）。删自选不再级联删持仓，改为 API 层显式拦截并提示"该股票仍有持仓"。
3. `trade_ingest.resolve_stock` 加 user 维度：在**当前用户**的自选里找，找不到就在**当前用户**下建一条（现在 `trade_ingest.py:68-79` 还有一条"忽略 market 只按 symbol"的兜底查询，比全局去重更危险）。
4. **应用层去重目前是全局的**：`create_stock` 按 `(symbol, market)` 全表查重并报"股票已存在"（`stocks.py:269-273`），`create_stocks_batch` 也是全表取现有集合（`stocks.py:286-287`），`sort_order` 用 `func.max()` 全表取最大值（`stocks.py:275`）。改造后这三处都必须落在 `user_id` 作用域内，否则 B 永远加不进 A 已经自选的股票，排序号也会互相冲突。
5. 回填历史数据时按 `position.account.user_id` 归属，孤立的 stocks 行归 user 1。

**为什么不把自选拆成单独的 `watchlist_items` 表**：那要动 `positions/price_alert_rules/stock_agents/operation_plans` 四张表的外键和对应查询，改动面比加一列大一个数量级，收益只是"更干净"。

### 8.3 缓存 key 必须加维度（7 处，漏了就是串数据）

| 位置 | 现状 | 后果 |
|---|---|---|
| `portfolio/api/accounts.py:693` `_PORTFOLIO_RESULT_CACHE` | key 是 `_holdings_signature(db)` 全账户聚合 | **A 的持仓分析结果返回给 B** |
| `strategy/gate.py:36,41` `_GATE_CACHE` | key 含 `model_id`/`account_context`，无 user | 部分隔离，需补 user 维度 |
| `platform/dataset/qfq.py:43` `_FUYAO_KEY` | 永久缓存首个 key，无失效钩子 | 换 key/多用户后一直用旧 key |
| `platform/dataset/router.py:24,23` `_instances` / `_fail_until` | 实例 key 含 api_key（尚安全）；冷却按 provider_id | 一人失败让所有人冷却 60s |
| `platform/ai/ai_failover.py:50` `_AI_FAIL_UNTIL` | 按"服务商/模型"标签冷却 | 一人额度耗尽 → 全员模型冷却 |
| `platform/ai/ai_client.py:20` `_SESSION_IDS` | key 为 `base_url|model` | 不同 key 同 base_url+model 撞同一 session |
| `platform/marketdata/marketdata_client.py:58` `_md` | 进程级单例，配置每次查全表 | 所有用户共用一套凭据 |

**改法统一为一句**：key 里加 `user_id`（或对真正全局的对象显式标注 `GLOBAL` 并写注释说明为什么可以共享）。`marketdata_client` 建议改为"按用户取配置，客户端实例按 `(user_id, provider)` 缓存"。

### 8.4 全局唯一语义的 5 处清表

`providers.py:149,169,241` 与 `channels.py:54,70` 的 `db.query(X).update({"is_default": False})` 在多用户下会清掉**所有人的**默认值。改造后：

```python
db.query(NotifyChannel).filter(NotifyChannel.user_id == current_user.id).update({"is_default": False})
```

或按 §7 的建议改用 `user_settings.default_channel_ids`，彻底消掉这个写法。

---

## 9. 后台任务改造

### 9.1 核心改动：`build_context` 加 user 维度

```python
# server.py:1264 现状
def build_context(agent_name: str, stock_agent_id: int | None = None) -> AgentContext:

# 改为
def build_context(agent_name: str, user_id: int, stock_agent_id: int | None = None) -> AgentContext:
    with tenant_scope(user_id):          # 一次作用域覆盖内部所有 SessionLocal
        ...  # load_watchlist / load_portfolio / resolve_ai_model / resolve_notify_channels 全部自动收敛
```

配合 `resolve_ai_model(user_id, ...)`、`resolve_notify_channels(user_id, ...)`、`load_watchlist_for_agent(user_id, ...)`、`load_portfolio_for_agent(user_id, ...)` 全部加首参。

### 9.2 调度器：一个 job 内逐用户循环

保持现有 APScheduler 结构不变（不改成"每用户一份 job"，那会让 job 数量 ×N 且难排查），在 job 内部按用户分组：

```python
async def _run_for_agent(agent_name: str):
    db = SessionLocal()
    try:
        user_ids = [r[0] for r in db.query(StockAgent.user_id)
                    .filter(StockAgent.agent_name == agent_name).distinct()]
    finally:
        db.close()

    for uid in user_ids:
        try:
            with tenant_scope(uid):
                await _run_one(agent_name, uid)
        except Exception:
            logger.exception("user=%s agent=%s 执行失败", uid, agent_name)  # 单用户失败不影响其他人
```

要点：

- **错误隔离**：一个用户的 AI 超时/额度耗尽不能中断整个 job。
- **并发控制**：AI 调用慢，串行 N 个用户可能超过调度间隔 → 用 `asyncio.Semaphore(2~3)` 限流，并加"上一轮未结束则跳过本轮"的锁。
- **成本**：每个用户独立计费。`tradingagents` 预算表要按用户，不然一人烧穿全站。
- **`price_alert_engine`**：一次扫全表 → 改为扫全表但按 user 分组处理，或直接加 `user_id` 条件逐用户扫。
- **`paper_trading_engine`**：`PaperTradingAccount.first()` 必须改成按 user（`models.py:1077` 那张表加 user_id，或直接按 market+user 唯一）。
- **`wealth_scheduler`**：定投/分红入账按账户 owner 循环。
- **`assistant/task_runner`**：`session_factory=SessionLocal` 注入点（`task_runner.py:144`）拿不到请求作用域 → 启动任务时把 `user_id` 一并带进 task 记录，恢复时 `recover_pending`（`bootstrap/application.py:237`）同样按记录的 user 建作用域。

### 9.3 启动期（`lifespan`）

`server.py:1649-1704` 一串初始化里，会碰私有表的要重写：

- `seed_agents()` → 改为 `seed_agents_for_user(user_id)`，注册时调用；启动时只给"尚未有 agent 配置的老用户"补种。
- `seed_sample_stocks()` → 改为注册时给新用户种示例，启动时不跑（或包 `system_scope()` 仅用于老数据）。
- `reconcile_data_sources()` → 按用户循环（见 §7 末尾）。
- `init_auth_from_env()` → 改为创建/更新 user 1，并写 identity 表。
- `dataset_scheduler.start()`、`warmup_async()` 打公共表，不用改。

---

## 10. 前端改造

| 项 | 改动 |
|---|---|
| token 收口 | 把 `client.ts:42`、`sse.ts:55-56`、`tradingagents.ts:203-207` 三处 `Authorization` 注入合并成 `authHeader()`；所有请求走同一个 `request()` |
| auth store | 新增轻量 store（zustand 或 context）存 `{user, token, expiresAt, capabilities}`，替换各组件直读 `localStorage`（`AccountMenu.tsx:153`、`App.tsx:62,106`） |
| 登录后拉身份 | 登录/初始化成功后 `GET /api/auth/me`，把 user、已绑定身份、可用登录方式灌进 store |
| 401 软跳转 | `client.ts:69-72` 调 `logout()` 整页硬跳（`client.ts:18`）→ 换成 router navigate + `state.from`（`App.tsx:80` 已塞入但 `Login.tsx` 从不读取） |
| 登录页 | `Login.tsx` 加"邮箱验证码"tab；`isSetup` 分支保留（对应新的 setup）；新增 `/register` 页 |
| 绑定页 | `Settings.tsx` 新增 `#sec-account` section + `jumpItems` 条目（`:662-668`）：邮箱/手机/微信/QQ 四行，显示已绑定（脱敏）与绑定/解绑按钮；未配置的 provider 灰掉并说明原因 |
| OAuth 回调 | 新增 `/oauth/callback` 路由（不放守卫里），拿到 ticket 后调 `/auth/oauth/exchange` 换 token |
| 命名 | `AccountMenu.tsx` → `UserMenu.tsx`；设置页里"账户"相关文案区分"账号"与"券商账户" |
| 我的页面 | 展示用户名、头像、绑定状态、最近登录记录（`auth_events`） |

---

## 11. 迁移、灰度与回滚

### 11.1 一次性迁移（迁移 137~140）

| 版本 | 内容 |
|---|---|
| 137 | 建 `users/user_identities/verification_codes/auth_events/user_settings`；从 `app_settings` 迁出 user 1（role=admin） |
| 138 | 51 张私有表加 `user_id` 列 + 回填 user 1 + 建索引 |
| 139 | 改唯一约束（重建表）：`stocks`、`agent_configs`、`notify_throttle`、`analysis_history`、`entry_candidates`、`stock_context_snapshots`、`portfolio_risk_snapshots` |
| 140 | 配置迁移：`ai_services/ai_models/data_sources/notify_channels` 归 user 1；`ui_avatar` → `users.avatar`；用户级 `app_settings` 键 → `user_settings` |

迁移前 `database.py:179-196` 已有自动备份逻辑（`has_pending_migrations` 为真时备份），**务必确认在容器里也生效**，并额外提醒用户手动 `docker cp` 一份。

### 11.2 兼容开关

`app_settings.registration_mode = "closed"` + `app_settings.multi_user_enabled = false`。

- `multi_user_enabled=false` 时：注册关闭、`/api/auth/me` 返回唯一用户、行为与今天一致（相当于"多租户能力已就位但只开一个户"）。
- 打开开关后：注册可用、数据隔离生效。
- 这样老用户升级后**业务行为零变化**，出问题只需把开关关掉，不必回滚数据库。

### 11.3 回滚

- 代码回滚 + 保留新表（新列可空、旧代码不读）→ 可安全回退到上一版本，**前提是列可空且旧代码不做 `SELECT *` 的列序依赖**。SQLAlchemy 按列名取，没问题。
- 唯一约束的改动（重建表）是不可逆的，回滚需要恢复备份 → 迁移 139 之前的那次自动备份要留住。

### 11.4 分阶段上线

| 阶段 | 内容 | 可独立上线 |
|---|---|---|
| P0 | 安全止血（见 §12），不动架构 | ✅ |
| P1 | 身份层 + 邮箱注册 + `/auth/me` + 前端登录注册 | ✅（此时仍是单租户数据，但已是真用户体系） |
| P2 | 配置层隔离（AI/数据源/通知） | ✅ |
| P3 | 业务数据隔离（加列 + 作用域 + 缓存） | ✅ |
| P4 | 后台任务逐用户化 | ✅ |
| P5 | OAuth（微信/QQ）+ 手机号 | 取决于资质 |

---

## 12. 安全修补清单（建议 P0 就做，与多账户无关但会放大风险）

| # | 问题 | 证据 | 修法 |
|---|---|---|---|
| 1 | 无盐 sha256 存密码 | `auth.py:78-80` | 换 `argon2-cffi`（纯 Python，无编译负担）或 `bcrypt`；登录成功时顺带升级旧哈希 |
| 2 | 空库匿名放行 | `auth.py:166-169` | 删除该分支，改为仅放行 setup/status |
| 3 | 改密不校验旧密码 | `auth.py:236-249` | 要求 `old_password`；成功后 `token_version += 1` |
| 4 | 登录无限流 | `auth.py:218-233` | 按 IP + 用户名双维度计数（内存或小表），5 次/分钟，指数退避 |
| 5 | API Key 明文返回前端 | `providers.py:63` | 脱敏 + "掩码即保留原值"约定 |
| 6 | 凭据明文入库 | `ai_services.api_key` 等 | Fernet 加密（见 §7） |
| 7 | token 无法吊销 | `auth.py:96-104` | 加 `token_version` |
| 8 | JWT 30 天过长 | `auth.py:22` | 降到 7 天 |
| 9 | 头像全局覆盖 | `api/settings.py:165` | 按 user 命名文件 |
| 10 | `log_entries` 可能记录敏感信息 | — | 它是公共表，只加**可空** `user_id` 供过滤（不进 `OwnedMixin`），并在写日志前脱敏邮箱/手机 |

---

## 13. 工作量估算

按 1 名熟悉该仓库的工程师，单位人日，含自测不含联调等待。

| 阶段 | 内容 | 估算 |
|---|---|---|
| P0 | 安全修补 10 项 | 2~3 |
| P1 | 身份层（5 表 + 注册/登录/绑定/改密/限流 + JWT 改造 + PAT 归属 + 单测） | 6~8 |
| P2 | 配置层（4 表加 owner + `user_settings` + 默认值语义 + 脱敏 + 加密 + 种子/对账按用户） | 4~5 |
| P3 | 数据层（51 表加列 + 7 张唯一约束重建 + 作用域机制 + 7 处缓存 + 7 条隔离测试） | 10~13 |
| P4 | 后台任务（`build_context` 签名 + 6 个业务调度器 + 启动期重写 + 错误隔离/限流；`dataset_scheduler` 打公共表不动） | 5~7 |
| P5 | 前端（token 收口 + store + 登录/注册/绑定页 + 回调 + 改名） | 6~8 |
| P6 | OAuth 微信/QQ + 手机号（provider 抽象 + 2 个实现 + 资质材料） | 5~7（资质审批 2~4 周另计） |
| P7 | 回归（全量 pytest + 前端 vitest + 手工多用户走查） | 4~6 |
| | **合计** | **42~57 人日** |
| | **MVP（P0~P3 + 前端最小改动，不做 OAuth/SMS）** | **22~29 人日** |

---

## 14. 风险与待确认

**技术风险**

1. `with_loader_criteria` 在"只选列"和"批量写"上的边界需要实测确认（§5.3 已列）。先写 7 条隔离测试，再动业务代码。
2. 51 张表加列后，SQLite 写放大与索引体积上升；`daily_bars` 这类大表不动，影响可控。
3. 单进程 + 全局缓存的假设仍成立（本项目是单实例部署）。若将来多进程/多实例，`ContextVar` 方案没问题，但进程级缓存需要外置。

**产品风险**

4. **QQ/微信登录的资质门槛是硬约束**，不是排期能解决的。如果目标是开源分发，建议文案上就写"邮箱注册 + 可选第三方登录（需自备资质）"。
5. 开放注册后需要防滥用：AI 调用是真金白银，必须有每用户配额（建议在 `user_settings` 里加 `ai_daily_budget`，并在 `ai_client` 出口拦一刀）。

**待你确认的问题**

| # | 问题 | 影响 |
|---|---|---|
| 1 | 这是自用（2~5 个号）还是要做成对外产品？ | 决定是否走多租户（§2 的 D 方案） |
| 2 | 注册是开放、邀请制，还是仅管理员建号？ | 决定要不要做邀请码表与前端注册页 |
| 3 | 手机号必须吗？有没有可用的短信服务商资质？ | 决定 SMS Provider 是否进 MVP |
| 4 | QQ/微信有没有企业主体 + 备案域名？ | 决定 OAuth 是本期还是下期 |
| 5 | 老数据（现有持仓/配置）归给哪个账号？ | 默认归 user 1（原账号），可否？ |
| 6 | 是否需要管理员看全站（跨用户总览）？ | 决定 `system_scope()` 的使用面 |
| 7 | 是否接受"配置项按用户独立后，AI Key 每人各配一份"？ | 若想全站共用一把 Key，配置层不该拆，只需隔离持仓 |

---

## 附录 A：改造面速查

| 指标 | 数值 | 含义 |
|---|---|---|
| 数据表 | 72 | 51 私有 / 21 公共 |
| `Depends(get_db)` | 181 | 一次改造全覆盖 |
| `SessionLocal()` | 159（60 文件） | 后台需显式作用域 |
| `.query()` | 553 | 不逐条改，靠拦截 |
| 原生 SQL `text()` | 145（业务 23） | 手工审 23 处 |
| 调度器 | 8 | 全部逐用户化 |
| 进程级单例缓存 | 7 处 | 全部加维度 |
| 全局唯一 `is_default` | 5 处 | 改按用户 |
| 迁移当前版本 | 136 | 新迁移从 137 起 |

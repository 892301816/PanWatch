# PanWatch 多账户改造 · 方案定稿 v1

> 状态：**方案已定稿，未开始编码**
> 定稿日期：2026-11-14
> 前置分析：[multi-account-and-identity-design.md](./multi-account-and-identity-design.md)（现状诊断、隔离方案选型、各改造点行号证据）
> 本文只定范围和做法，不含任何代码改动。

---

## 0. 决策记录

### 0.1 你的四项决策

| # | 决策项 | 结论 | 直接后果 |
|---|---|---|---|
| D1 | 部署形态 | **单实例多租户** | 一个进程内多账户，数据按 `user_id` 隔离；走完整改造 |
| D2 | 账号来源 | **仅管理员建号** | 不做注册页、不做邀请码、不做短信/邮箱验证码注册流 |
| D3 | 本期登录方式 | **邮箱 + 密码** | 不做手机号、不做 QQ/微信登录（推迟二期）；账号即邮箱登录名 |
| D4 | 老数据归属 | **全部归现有账号** | 迁移后现有账号成为管理员（user 1），持仓/配置/AI Key 原样保留 |

### 0.2 由此派生的三项决策（我按最小风险定的，你可否决）

| # | 派生决策 | 理由 |
|---|---|---|
| D5 | **不建 `user_identities` 表**，本期只新增 `users` / `user_settings` / `auth_events` 三张表 | D2+D3 之下没有任何第三方身份可绑。二期加 QQ/微信时再建这张表，它和一期表结构无耦合，**不会返工** |
| D6 | **不做 SMTP、不做自助找回密码**；忘记密码由管理员在用户管理页重置 | 少一个外部依赖、少一套凭据配置，本期可完全内网离线使用。将来要找回密码，加 1 个接口 + 1 组 SMTP 配置即可 |
| D7 | **只允许禁用账号，不提供删除账号** | 误删会 CASCADE 掉该用户全部持仓/交易记录，且 SQLite 无回收站。禁用可随时恢复 |

### 0.3 范围变化带来的影响

- 相比分析文档里的初版，本期**去掉了**：自助注册、邀请码表、邮箱/短信验证码表、OAuth provider 抽象、SMS provider、微信/QQ 回调页、`registration_mode` 设置项。
- 但**没有去掉**多租户改造的主体——51 张表加 owner、553 处查询的隔离、7 处缓存、6 个调度器。这部分是成本大头，与登录方式无关。
- 因此估算从 42~57 人日降到 **34~45 人日**，不是因为少做了核心，而是因为没有资质依赖和外部联调等待。

---

## 1. 本期范围

### 1.1 做什么（In Scope）

| 编号 | 内容 | 用户可感知的结果 |
|---|---|---|
| S1 | 身份与认证重构 | 管理员能建多个账号；每个账号独立邮箱+密码登录；支持改密、登出、token 吊销 |
| S2 | 用户管理 | 管理员可建号、禁用/启用、重置密码、设为管理员 |
| S3 | 配置隔离 | 每个账号各自一套 AI 服务商/API Key/数据源/通知渠道/偏好设置 |
| S4 | 业务数据隔离 | 每个账号的持仓、自选、交易、提醒、模拟盘、AI 对话、策略、选股规则互不可见 |
| S5 | 后台任务逐用户化 | 定时 Agent、提醒、定投、模拟盘等按账号分别执行，互不串数据 |
| S6 | 安全加固 | 密码哈希升级、凭据加密与脱敏、token 可吊销、登录限流 |

### 1.2 不做什么（Out of Scope，本期明确拒绝）

| 项 | 为什么不现在做 | 以后要加的成本 |
|---|---|---|
| 自助注册 | D2 已定仅管理员建号 | +3~4 人日（注册页 + 邀请码或验证码流 + 防刷） |
| 手机号登录/绑定 | 需短信服务商实名与签名报备 | +2~3 人日 + 资质 |
| 微信登录/绑定 | 需企业主体、开放平台认证、备案域名，且每个部署实例要自己注册应用 | +3~4 人日 + 资质 + 2~4 周审批 |
| QQ 登录/绑定 | 同上 | +2~3 人日 + 资质 |
| SMTP 自助找回密码 | D6 | +1~2 人日 |
| 管理员跨用户查看持仓 | 隐私敏感，且需销毁隔离保证 | +2~3 人日 |
| 多实例/多进程部署 | 当前是单进程 SQLite，不涉及 | — |
| 移动端账号切换 | 单账号登录足够 | 视需求 |

> ⚠️ 注意：**原始需求里的"绑定邮箱/QQ/微信/手机"在本期只落地了"邮箱作为登录名"这一层**，第三方绑定整体推迟。如果你需要手机号或微信在本期可用，请现在提出，我把它加回范围并调整估算。

---

## 2. 最终数据模型

### 2.1 新增表（3 张）

```python
# src/platform/persistence/models.py 末尾追加

class User(Base):
    """登录账号。一期一个账号 = 一个邮箱登录名 + 密码。"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(120), unique=True, nullable=False)   # 登录名,即邮箱地址
    display_name = Column(String(64), default="")
    avatar = Column(String(255), default="")                      # 文件落 data/avatars/{id}.{ext}
    password_hash = Column(String(255), nullable=False)           # argon2
    role = Column(String(16), default="member")                   # admin / member
    status = Column(String(16), default="active")                 # active / disabled
    must_change_password = Column(Boolean, default=False)         # 管理员建号/重置后置 1
    token_version = Column(Integer, default=0)                    # 递增即吊销全部 token
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class UserSetting(Base):
    """用户级设置。部署级设置仍留 app_settings。"""
    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_setting"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(64), nullable=False)
    value = Column(Text, default="")
    description = Column(String(255), default="")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AuthEvent(Base):
    """认证审计:登录成功/失败/建号/禁用/重置密码/改密/吊销。"""
    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_event_user_time", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event = Column(String(32), nullable=False)
    actor_user_id = Column(Integer, nullable=True)   # 管理员操作时记录操作者
    ip = Column(String(64), default="")
    detail = Column(JSON, default={})
    created_at = Column(DateTime, server_default=func.now())
```

**`username` 就是邮箱**：这样"邮箱+密码登录"不需要额外字段，也不出现"登录名和邮箱哪一个是权威"的歧义。校验只做基本格式（含 `@` 且有点），不做邮箱真实性验证（D6 没有发信能力）。

**不存在的字段**：`email`、`phone`、`provider`、`openid`。二期引入 `user_identities` 时再加，`users` 表不用动。

### 2.2 加 owner 列的 51 张表

统一用混入基类，列定义一致：

```python
class OwnedMixin:
    @declared_attr
    def user_id(cls):
        return Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=True, index=True)   # 可空:SQLite 加列限制,由应用层保证写入
```

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
| 配置 (4) | ai_services, ai_models, data_sources, notify_channels |

### 2.3 保持全局的 21 张表

`daily_bars`、`daily_bars_qfq`、`dataset_sync_state`、`dataset_providers`、`dividend_events`、`news_cache`、`hot_themes`、`screener_snapshots`、`screener_concept_members`、`market_scan_snapshots`、`market_regime_snapshots`、`strategy_catalog`、`strategy_weights`、`strategy_weight_history`、`factor_weights`、`factor_weight_history`、`strategy_signal_runs`、`strategy_outcomes`、`strategy_factor_snapshots`、`log_entries`、`app_settings`。

> 其中 `log_entries` 加**可空** `user_id` 仅用于过滤，不纳入隔离基类。

### 2.4 必须改的唯一约束（7 张表）

| 表 | 现在 | 改为 |
|---|---|---|
| `stocks` | 无（应用层按 symbol 全局去重） | `UNIQUE(user_id, symbol, market)` |
| `agent_configs` | `name` UNIQUE | `UNIQUE(user_id, name)` |
| `notify_throttle` | `(agent_name, stock_symbol)` | 加 `user_id` 进键 |
| `analysis_history` | `(agent_name, stock_symbol, analysis_date)` | 加 `user_id` 进键 |
| `entry_candidates` | `(stock_symbol, stock_market, snapshot_date)` | 加 `user_id` 进键 |
| `stock_context_snapshots` | `(symbol, market, snapshot_date, ...)` | 加 `user_id` 进键 |
| `portfolio_risk_snapshots` | `(snapshot_date, market)` | 加 `user_id` 进键 |

`app_settings.key` 与 `personal_access_tokens.token_hash` 的全局唯一**保持不变**。

---

## 3. 认证与账号生命周期

### 3.1 账号状态机

```
管理员建号 → status=active, must_change_password=1
   ↓ 首次登录
强制改密 → must_change_password=0
   ↓
正常使用 ←→ status=disabled（管理员禁用，立即生效：token 因 token_version 变化与状态检查双重失效）
```

### 3.2 关键规则

| 规则 | 说明 |
|---|---|
| 首个用户 | 沿用现有 `/api/auth/setup`，但写入 `users` 表并置 `role=admin`；之后该接口永久关闭 |
| 建号 | 管理员填邮箱登录名 + 初始密码（≥8 位）+ 角色；`must_change_password=1` |
| 登录 | `username` + 密码 → JWT；若 `status=disabled` 返回 403；若 `must_change_password=1`，响应里带标记，前端强制跳改密页 |
| 改密 | 必须提供旧密码；成功后 `token_version += 1`（踢掉其他设备） |
| 重置密码 | 管理员操作，不需旧密码；`must_change_password=1` 且 `token_version += 1` |
| 禁用 | `status=disabled` + `token_version += 1`，该用户所有 token 立即失效 |
| 删除 | **不提供**（D7） |
| 登录限流 | 按 IP + 用户名双维度：5 次/分钟，连续失败指数退避（1s→2s→4s，上限 60s） |
| 密码哈希 | `argon2-cffi`；登录成功时若发现旧 sha256 哈希，**就地升级**为 argon2 |
| 密码策略 | 新密码 ≥8 位；不允许与登录名相同；不做复杂度强制（避免用户写便利贴） |

### 3.3 JWT

```json
{ "sub": "12", "ver": 3, "jti": "...", "iat": 1700000000, "exp": 1700604800 }
```

- 有效期 **7 天**（现在 30 天）。
- 校验顺序：签名 → 过期 → 查 `users.id = int(sub)` → `user.status == "active"` → `user.token_version == ver`。
- **删除现有"空库放行"分支**（`auth.py:166-169`）。无任何用户时，仅放行 `/api/auth/status` 与 `/api/auth/setup`，其余一律 401。
- 不做 refresh token。过期即重登，前端已有 401 处理逻辑。

---

## 4. 隔离机制规格

### 4.1 语义定义（这是验收的核心）

| 场景 | 行为 |
|---|---|
| HTTP 请求已登录，访问私有表 | 自动附加 `WHERE user_id = <当前用户>` |
| HTTP 请求已登录，访问公共表 | 不附加条件（`daily_bars` 等全站共享一份） |
| 后台任务，已用 `tenant_scope(uid)` 包裹 | 同上，按 uid 过滤 |
| 后台任务，未包裹却访问私有表 | **抛 `TenantScopeMissing`**（fail-closed），不返回全量数据 |
| 显式 `system_scope()` 内访问私有表 | 不过滤（仅限迁移、启动种子、管理员账号管理） |
| 新建私有表记录未给 `user_id` | 用当前作用域自动填充；无作用域则抛错 |
| 修改/删除别的用户的记录 | 抛 `TenantScopeMissing` |

### 4.2 落地位置

| 文件 | 内容 |
|---|---|
| `src/platform/persistence/tenancy.py`（新增） | `tenant_scope()` / `system_scope()` / `current_user_id()` / 异常类型 |
| `src/platform/persistence/database.py`（改） | `OwnedMixin`；`do_orm_execute` 与 `before_flush` 两个监听器 |
| `src/modules/administration/api/auth.py`（改） | `get_current_user` 返回 `User` 并设置作用域 |
| `src/platform/persistence/models.py`（改） | 3 张新表 + 51 个模型继承 `OwnedMixin` |

### 4.3 必须交付的 7 条隔离测试

| # | 测试 | 断言 |
|---|---|---|
| T1 | 参数化遍历 51 张私有表 | 用户 B 查询返回 0 条；按 A 的记录 id 直查返回 None |
| T2 | 跨租户写 | B 作用域下修改 A 的记录 → 抛 `TenantScopeMissing` |
| T3 | 无作用域读私有表 | 抛 `TenantScopeMissing`，且**不返回任何数据** |
| T4 | 公共表 | 有/无作用域都可读，且不附加过滤 |
| T5 | 只选列的语句（`select(Position.cost_price)`） | 仍然被过滤（这是 `with_loader_criteria` 的已知薄弱点，必须实测） |
| T6 | HTTP 层代表作 | 用 A 的 token 请求 B 的持仓/账户/提醒/对话 id → 404 或空，绝不返回数据 |
| T7 | PAT 租户 | 用 B 的 PAT 调 ingest 指定 A 的 account_id → 拒绝，不写入 |

> T1 用参数化一次性覆盖 51 张表，是这套方案的安全底线；T5/T7 是已知薄弱点。**这 7 条测试先写、先红、再改业务代码。**

---

## 5. 接口清单

### 5.1 认证（`/api/auth`；`status`/`setup`/`login` 公开，其余需登录）

| 方法 | 路径 | 变化 |
|---|---|---|
| GET | `/status` | 返回 `{initialized, multi_user}` |
| POST | `/setup` | 建首个管理员，写入 `users`（现为写 app_settings） |
| POST | `/login` | 返回 `{token, expires_at, must_change_password, user}` |
| GET | `/me` | **新增实际内容**：`{id, username, display_name, avatar, role, must_change_password}` |
| POST | `/change-password` | 加 `old_password` 校验；成功后 `token_version += 1` |
| POST | `/logout` | 可选，前端清 token 即可（保留给"登出所有设备"） |

**删除**：`/api/auth/me` 里恒返回 `"user"/"guest"` 的旧逻辑。

### 5.2 用户管理（`/api/admin/users`，仅 admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `` | 列表（id/username/role/status/last_login_at/是否待改密） |
| POST | `` | 建号：`{username, password, display_name, role}` |
| PATCH | `/{user_id}` | 改 `display_name` / `role` / `status` |
| POST | `/{user_id}/reset-password` | 重置为临时密码并置 `must_change_password` |

不提供 DELETE（D7）。禁止管理员禁用自己、禁止把最后一个 admin 降级。

### 5.3 设置

| 方法 | 路径 | 变化 |
|---|---|---|
| GET/PUT | `/api/settings` | 只返回**当前用户**的可写项 |
| GET/PUT | `/api/admin/settings`（新增） | 部署级设置（`http_proxy`、`jwt_secret` 只读、数据集同步等），仅 admin |
| GET/PUT | `/api/settings/avatar` | 文件改为 `data/avatars/{user_id}.{ext}` |

### 5.4 既有业务接口

**全部保持路径与请求体不变**，靠会话层自动按用户过滤。这是本方案最重要的性质：前端业务页面不需要改接口调用。

### 5.5 PAT / MCP

- `personal_access_tokens` 加 `user_id`；`/api/pats` 按当前用户列出/吊销。
- PAT 校验通过后设置租户作用域，`/mcp` 与 `/api/ingest` 自动落在正确用户下。
- `trade_ingest.resolve_account` / `resolve_stock` 不再需要手工加过滤（作用域已生效），但**必须有 T7 覆盖**。

---

## 6. 配置隔离最终形态

| 项 | 最终做法 |
|---|---|
| AI 服务商/模型 | 两表都加 `user_id`；`/providers/*` 全部按当前用户 |
| 默认模型 | 从 `is_default` 列迁到 `user_settings.default_ai_model_id`，**彻底删掉"清全表"写法**（`providers.py:149,169,241`） |
| 默认通知渠道 | 迁到 `user_settings.default_channel_ids`，删掉 `channels.py:54,70` |
| API Key 展示 | 脱敏返回（复用 `dataset/api.py:41-46` 的 `_mask` 模式）；前端提交掩码即保留原值 |
| 凭据落库 | Fernet 加密 `ai_services.api_key`、`data_sources.config` 中的 key/token、`notify_channels.config` 中的 token；密钥来自 `PANWATCH_SECRET_KEY`，未配置则首次启动生成 `data/secret.key`（0600） |
| 数据源 | 加 `user_id`；`DATA_SOURCE_SEEDS` 在**建号时**为该用户种一份；`reconcile_data_sources` 按用户跑 |
| 用户偏好 | `notify_*`、`stock_link_platform`、`panwatch_base_url`、`ui_avatar` 迁 `user_settings` / `users.avatar` |
| `http_proxy` | **保持部署级全局**，仅 admin 可改。进程级 env 是天然单租户假设，改为每用户代理等于给所有 httpx 调用加参数，不做 |
| `dataset_providers` | 保持全局（行情数据集是部署基础设施） |

---

## 7. 后台任务改造

| 任务 | 改法 |
|---|---|
| Agent 调度（6 个 Agent） | 一个 job 内 `SELECT DISTINCT user_id FROM stock_agents WHERE agent_name=?`，逐个 `tenant_scope(uid)` 执行；单用户异常隔离；`asyncio.Semaphore(2~3)` 限流；上一轮未完则跳过本轮 |
| `build_context` | 签名加 `user_id` 首参，内部一个 `tenant_scope` 覆盖所有会话 |
| `resolve_ai_model` / `resolve_notify_channels` | 加 `user_id`，按该用户的默认值解析 |
| 价格提醒 | 按用户分组扫描 |
| 模拟盘 | `PaperTradingAccount.first()` 改为按当前用户取 |
| 定投/分红 | 按账户 owner 循环 |
| 助手任务恢复 | `assistant_task_runs` 记录 `user_id`，`recover_pending` 按记录的用户建作用域 |
| 数据集同步 | 公共表，**不改** |
| 启动期 | `seed_agents` / `seed_data_sources` 改为建号时按用户执行；`seed_sample_stocks` 同理；启动只做全局初始化 |

> 关键约束：`concurrent.futures.ThreadPoolExecutor` 不传播 `ContextVar`。已知 4 处自建线程池（`dataset/sync.py:214`、`screener/snapshot.py:289`、`tradingagents/operations.py:187`、`dataset/api.py:97`）必须改成 `copy_context().run(...)` 或显式传 `user_id`。

---

## 8. 前端改造

| 项 | 改动 | 说明 |
|---|---|---|
| token 收口 | 合并 `client.ts:42`、`sse.ts:55-56`、`tradingagents.ts:203-207` 三处注入为单一 `authHeader()` | 为将来 refresh 留单点 |
| auth store | 新增轻量 store（zustand 或 context）存 `{user, token, expiresAt}` | 替代组件直读 `localStorage` |
| 登录后拉身份 | 登录成功调 `/api/auth/me` | 现在前端从不调用 |
| 强制改密 | `must_change_password=1` 时路由拦到 `/change-password`，不可跳过 | |
| 401 软跳转 | 换成 router navigate + 保留 `state.from`（`App.tsx:80` 已塞但 `Login.tsx` 从不读） | |
| 用户管理页 | 新增 `/admin/users`（仅 admin 可见入口）：列表、建号、禁用/启用、重置密码 | 对应 §5.2 |
| 我的账号 | `/settings` 新增 `#sec-account`：显示登录名、角色、最近登录、改密 | 不做绑定相关 UI |
| 命名 | `AccountMenu.tsx` → `UserMenu.tsx`；UI 文案区分"账号"（登录）与"券商账户"（`/accounts`） | 避免第三个"账户"概念混淆 |
| 不做的页面 | 注册页、找回密码页、第三方授权回调页 | D2/D6 之下不存在 |

---

## 9. 迁移与回滚

### 9.1 迁移脚本

> **前置条件**：当前工作区有未提交的在建改动（策略中心那条线，含迁移 136 `strategy_workflow`、`models.py` 新增 66 行、`server.py` 改动）。开工前应先提交或合并这批改动；本次迁移从 **137** 起编号。若那批改动之后又引入新迁移，本节的 137~140 相应顺延（版本号只是占位，内容不变）。

| 版本 | 内容 |
|---|---|
| 137 | 建 `users` / `user_settings` / `auth_events`；从 `app_settings` 的 `auth_username`/`auth_password_hash` 迁出 user 1，`role=admin`；**保留旧 sha256 哈希原样写入** `password_hash`（首次登录自动升级为 argon2） |
| 138 | 51 张表加 `user_id`（可空）+ 回填 user 1 + 建 `ix_<table>_user_id` 索引 |
| 139 | 7 张表唯一约束重建（`CREATE TABLE xx__new ... INSERT SELECT ... DROP ... RENAME`，复用 `database.py:560-603` 的写法） |
| 140 | 配置归户：`ai_services/ai_models/data_sources/notify_channels` 的 `user_id` 回填 user 1；`ui_avatar` → `users.avatar`（旧文件改名为 `1.{ext}`）；用户级 `app_settings` 键搬 `user_settings`；`is_default` → `user_settings` 默认值项 |

### 9.2 灰度开关

`app_settings.multi_user_enabled`（默认 `false`）：

- `false`：注册/建号入口关闭，只允许 user 1 使用，**行为与今天完全一致**（隔离代码已就位但不产生可感知差异）。
- `true`：管理员可建号，多租户正式生效。

出问题时把开关关掉即可，不必回滚数据库。

### 9.3 回滚

- 代码回滚可安全执行（新列可空、旧代码按列名取值不依赖列序）。
- 迁移 139 的约束重建不可逆 → **必须保留 139 之前那次自动备份**（`database.py:179-196` 在 `has_pending_migrations()` 为真时会自动备份，需确认容器内也生效）；同时提示用户手动 `docker cp` 一份 `data/panwatch.db`。

---

## 10. 任务分解与验收标准

按可独立验收的粒度拆，每项都可单独合并。

| 阶段 | 任务 | 交付物 | DoD（完成判据） |
|---|---|---|---|
| P0 | 安全加固 | 密码哈希换 argon2 + 旧哈希自动升级；删空库放行；改密校验旧密码；登录限流；`token_version` 吊销 | 新增单测覆盖 5 项；老账号能正常登录且哈希被升级 |
| P1a | 身份层-数据 | 3 张新表 + `users` 迁移 + `/auth/login|me|setup|change-password` | 单用户模式行为不变；`/auth/me` 返回真实信息 |
| P1b | 身份层-管理 | `/api/admin/users` 四个接口 + 建号时种数据源/Agent 配置 | 能建第二个账号并登录；不能禁用自己/降级最后一个 admin |
| P1c | PAT 归属 | `personal_access_tokens` + `mcp_call_logs` 加 `user_id`；PAT 校验设作用域 | MCP 调用按 PAT 归属到正确用户 |
| P2 | 配置隔离 | 4 张配置表加 owner；`user_settings`；默认值迁出 `is_default`；Key 加密与脱敏 | A 的 AI 服务/数据源/通知渠道对 B 不可见；列表接口返回掩码 |
| P3a | 隔离机制 | `tenancy.py` + 两个监听器 + `OwnedMixin`（51 模型） | T1~T5 通过 |
| P3b | 业务隔离 | 唯一约束重建 + 缓存加维度（7 处）+ 应用层全局去重改用户级（`stocks` 三处） | T1~T6 通过；A/B 各自能加同一只自选股 |
| P4 | 后台任务 | `build_context(user_id)` + 6 个调度器 + 启动期重写 + 线程池上下文传播 | 两账号各挂一只股票，定时任务各推各的；一方 AI 失败不影响另一方 |
| P5 | 前端 | token 收口 + store + `/auth/me` + 强制改密 + 用户管理页 + 账号设置 + 改名 | 多账号切换登录正常；admin 能完成建号→对方登录→改密全流程 |
| P6 | 回归 | 全量 pytest + 前端 vitest + 手工多用户走查清单 | 见 §10.1 |

### 10.1 整体验收清单（人工走查）

1. 老数据完整：升级后 User 1 能看到原有持仓、自选、AI 配置、通知渠道、历史分析。
2. 建号隔离：新建 User 2，登录后看到的是空持仓、空自选、**空 AI 配置**（不是老用户那套）。
3. 交叉不可见：User 2 无法通过任何接口读到 User 1 的账户/持仓/对话/提醒（含直接猜 id）。
4. 配置独立：User 2 配自己的 AI Key 后，User 1 的配置不受影响；改默认模型只影响自己。
5. 调度独立：两账号各挂一只股票，定时分析各自生成各自的记录、推送到各自渠道。
6. 禁用生效：禁用 User 2 后，其已登录 token 立即 401。
7. 无泄漏开关：`multi_user_enabled=false` 时行为与升级前一致。

---

## 11. 变更控制：二期怎么做才不返工

| 二期需求 | 需要新增 | 是否需要改一期表 |
|---|---|---|
| 手机号登录 | `user_identities` 表 + SMS Provider + `/auth/phone/*` | 否（`users` 不动） |
| 微信/QQ 登录 | `user_identities` 表 + OAuth provider + 回调页 | 否 |
| 自助注册 | `invitation_codes` 或验证码表 + `/auth/register` + 防刷 | 否 |
| 邮箱验证/找回密码 | `verification_codes` 表 + SMTP 配置 + `/auth/forgot` | 否 |
| 管理员总览 | 只读接口 + `system_scope()` | 否 |

一期所有表都不需要为二期预留字段，这是 §2.1 刻意不做 `email`/`openid` 列的原因。

---

## 12. 风险登记

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 51 表加列后漏加 `OwnedMixin` | 该表数据全站可见 | 用脚本比对 `models.py` 的表清单与 `OwnedMixin` 子类清单，加一条架构测试断言二者差集为空 |
| R2 | `with_loader_criteria` 对只选列语句漏过滤 | 静默泄漏数值 | T5 专门覆盖；聚合查询统一 `select_from(Entity)` |
| R3 | 自建线程池丢失 `ContextVar` | 后台任务报 `TenantScopeMissing` 或错租户 | 4 处已知点逐一改造 + 日志告警 |
| R4 | 迁移 139 重建表失败 | 升级卡住 | 迁移前强制备份；先在真实库副本上演练一遍 |
| R5 | 缓存 key 漏维度 | 串数据（最严重的是 `accounts.py:693` 的组合分析缓存） | 7 处逐一到齐；缓存命中率可观测 |
| R6 | 定时任务串行变慢 | 用户多了错过执行窗口 | 信号量限流 + 上一轮未完成则跳过 + 记录跳过次数 |
| R7 | 老用户忘记自己密码且无 SMTP | 只能管理员重置 | 管理员就是部署者本人，可接受；文档写明 |

---

## 13. 排期估算

| 阶段 | 人日 |
|---|---|
| P0 安全加固 | 2~3 |
| P1 身份层（a/b/c） | 5~6 |
| P2 配置隔离 | 4~5 |
| P3 隔离机制与业务隔离 | 10~13 |
| P4 后台任务 | 5~7 |
| P5 前端 | 4~5 |
| P6 回归 | 4~6 |
| **合计** | **34~45** |

建议顺序即 P0 → P1 → P2 → P3 → P4 → P5 → P6，但 **P3a 的 7 条测试要提前到 P1 期间就写好**（用假用户跑），这样 P3b 才能在一天内验证完 51 张表。

---

## 14. 开工前的最后确认项

| # | 待确认 | 默认值 |
|---|---|---|
| 0 | 工作区当前在建的策略中心改动先提交/合并，再开多账户分支（否则迁移号与 `models.py` 会互相打架） | 需要你确认 |
| 1 | 部署级 `http_proxy` 保持全局，不按用户区分 | 按默认执行 |
| 2 | 管理员**不能**查看其他用户的持仓 | 按默认执行 |
| 3 | 不提供删除账号，只能禁用 | 按默认执行 |
| 4 | 新密码最短 8 位 | 按默认执行 |
| 5 | JWT 有效期 7 天 | 按默认执行 |

以上若无疑义，本定稿即为施工依据。**下一步（等你确认后才开始）：先写 §4.3 的 7 条隔离测试，再动业务代码。**

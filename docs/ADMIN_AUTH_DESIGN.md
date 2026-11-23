# 后台管理员账号、密码登录与会话（V1 设计补充）

> 版本：0.1 · 日期：2026-11-23 · 状态：**草案，待 Kelvin 批准**。批准之前，INVENTORY-TASK-001～003 不得开始写代码（CLAUDE.md「碰个人数据的任务先出设计」）。
> 依据：[V1_MINIMAL_DESIGN](V1_MINIMAL_DESIGN.md) 第 2 节（登录限流用库表计数，不用 Redis）、第 10 节（V1 只有管理员一个角色；登录页：密码登录、限流、记录异常尝试）、第 12 节（独立账号、密码登录、带限流；变更记操作者）；
> [REQUIREMENTS](REQUIREMENTS.md) 第 5 节「安全」（独立账号、不与他人共用、登录限流、记录异常尝试、支持立即停用、首期不做双重验证）。
> 取值与做法尽量沿用 acuven-shop 已获批、已上线的管理员登录（同样是标准库 scrypt、按来源与用户名组合锁定、服务端会话），不同处在第 7 节列出。
> 本文是 V1_MINIMAL_DESIGN 的补充，只管后台登录；与它冲突时以 V1_MINIMAL_DESIGN 为准。设计有改动时，改动部分的批准作废、须重新批准。

---

## 1. 范围

| 做 | 不做（本文） |
| --- | --- |
| 管理员账号（可以有多个，每人一个，同一个角色）、密码哈希、服务器上的建号与改密命令、停用与恢复 | 网页注册、网页改密或找回密码、邮件或短信、双重验证（REQUIREMENTS 第 5 节：列为后续增强） |
| 密码登录、服务端会话、退出、取当前会话 | 多角色与权限分级（V-06：只有管理员） |
| 按「来源 + 提交的用户名」的登录失败锁定，每次登录尝试记一条 | 业务变更的审计记录（数量、配额、映射、连接的操作者与原因由各业务任务写；它们从本文第 5.4 节的依赖拿到操作者） |
| 之后所有后台接口共用的「须为已登录管理员」依赖与 CSRF 校验 | 登录页前端（随「后台前端骨架」任务做）；登录尝试的查看页与过期清理（随之后的待处理、定时作业任务做，见第 6 节） |

## 2. 个人数据

- 账号只存用户名，不存姓名、电话、邮箱。用户名是登录标识，建议用不含真实姓名的短名。
- 登录尝试**不存来源地址原文、不存提交的用户名原文**，只存二者拼接后的 SHA-256（`throttle_key`，用于计数）。
  IPv4 地址空间小，这个摘要是假名化不是匿名化：知道用户名的人能穷举出来源地址。所以它和账号表一样只在库里，不进日志、不进接口响应。
- 密码只存 scrypt 哈希；会话令牌只存 SHA-256；CSRF 令牌不入库（第 4.4 节）。三者的原文都不进日志、异常消息、命令输出。

## 3. 数据模型（INVENTORY-TASK-001）

沿用 `app/db.py` 的 `Base` 与命名约定；时间一律是不带时区的 UTC（`DateTime`，Python 侧用 `datetime.now(UTC).replace(tzinfo=None)`）；指向账号的外键一律 `ON DELETE RESTRICT`。

**`admin_accounts`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | Integer | 主键，自增 |
| `username` | String(32) | 非空；唯一 `uq_admin_accounts_username`；检查 `ck_admin_accounts_username_length`：`LENGTH(username) >= 3 AND LENGTH(username) <= 32`。字符集规则在规则层（4.1） |
| `password_hash` | String(255) | 非空 |
| `created_at` | DateTime | 非空 |
| `password_updated_at` | DateTime | 非空 |
| `disabled_at` | DateTime | 可空；非空 = 已停用 |

**`admin_sessions`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | Integer | 主键，自增 |
| `admin_account_id` | Integer | 非空；外键 → `admin_accounts.id`（RESTRICT）；普通索引 |
| `token_hash` | String(64) | 非空；唯一 `uq_admin_sessions_token_hash`；检查 `ck_admin_sessions_token_hash_length`：`LENGTH(token_hash) = 64` |
| `created_at` | DateTime | 非空 |
| `expires_at` | DateTime | 非空；检查 `ck_admin_sessions_expires_after_created`：`expires_at > created_at` |
| `revoked_at` | DateTime | 可空；非空 = 已撤销 |

**`admin_login_attempts`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BigInteger | 主键，自增 |
| `occurred_at` | DateTime | 非空 |
| `throttle_key` | String(64) | 非空；检查 `ck_admin_login_attempts_throttle_key_length`：`LENGTH(throttle_key) = 64`；与 `occurred_at` 组成普通复合索引（列顺序 `throttle_key`、`occurred_at`），供 4.2 的计数 |
| `admin_account_id` | Integer | 可空（提交的用户名没有对应账号时为空）；外键 → `admin_accounts.id`（RESTRICT） |
| `outcome` | String(20) | 非空；检查 `ck_admin_login_attempts_outcome_known`：`outcome IN ('success', 'bad_password', 'unknown_user', 'disabled', 'locked')` |

- 迁移是本仓库的第一条：`alembic/versions/0001_admin_auth.py`，revision `"0001"`，`down_revision = None`；upgrade 建三张表、逐个写出上面的约束与索引名（与模型生成的名字一致，CI 的 `alembic check` 比对）；downgrade 按依赖反序删表。
- 模型放 `app/models/admin_auth.py`，`app/models/__init__.py` 导出 `AdminAccount`、`AdminSession`、`AdminLoginAttempt`；`alembic/env.py` 加一行 `import app.models`，让 `Base.metadata` 里有这三张表（env.py 顶部注释已要求这样做）。
- 集成测试直接用 `Base.metadata.create_all` / `drop_all` 在 CI 的一次性库上建删表（迁移本身由 CI 的迁移往返与 `alembic check` 覆盖），测试结束删表，不影响之后的迁移步骤。

**约束的例子**（集成测试逐条对真 MySQL 验证）：

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| D1 | 已有账号 `kelvin` | 再插入用户名 `kelvin` 的账号 | 被 `uq_admin_accounts_username` 拒绝 |
| D2 | 空表 | 插入用户名 `ab`（2 个字符）的账号 | 被 `ck_admin_accounts_username_length` 拒绝；`abc` 可以 |
| D3 | 账号 1 存在 | 插入 `token_hash` 为 63 个字符的会话 | 被 `ck_admin_sessions_token_hash_length` 拒绝 |
| D4 | 账号 1 存在 | 插入 `expires_at` 等于 `created_at` 的会话 | 被 `ck_admin_sessions_expires_after_created` 拒绝 |
| D5 | 空表 | 插入 `outcome` 为 `ok` 的登录尝试 | 被 `ck_admin_login_attempts_outcome_known` 拒绝；五个合法值都可以 |
| D6 | 账号 1 有一条会话或一条登录尝试 | 删除账号 1 | 被外键拒绝（停用账号用 `disabled_at`，不删行） |
| D7 | 空表 | 插入 `admin_account_id` 为空的登录尝试 | 可以（用户名不存在的尝试） |

## 4. 规则（INVENTORY-TASK-002）

### 4.1 用户名与密码

- **用户名**：建号时按 `^[a-z0-9][a-z0-9._-]{2,31}$` 校验（3–32 个字符，小写字母、数字、`.`、`_`、`-`，首字符是字母或数字）；命令行输入先转小写再校验。登录时提交的用户名同样先转小写再查。
- **密码**：长度 12–128 个字符（Python `len`），不做组成规则（不强制大小写、数字、符号）；转小写后不得等于用户名。不查常见弱口令表（单人或几人使用，靠长度与锁定）。
- **哈希**：标准库 `hashlib.scrypt`，参数 N=2^14、r=8、p=5，16 字节随机盐，64 字节摘要；存成自描述字符串
  `scrypt$n=16384,r=8,p=5$<盐 Base64url>$<摘要 Base64url>`（不带填充，不超过 255 个字符）。校验按串里记下的参数重算、`hmac.compare_digest` 比较；
  串格式不合法、参数越界（N 不是 2 的幂或大于 2^16、r 或 p 大于 16）、计算失败时返回不通过，不抛异常。
- **假哈希**：固定的 `DUMMY_PASSWORD_HASH`（参数同上、盐与摘要全零字节），对任何密码都不通过。用户名不存在时照样对它校验一次，让两种失败的耗时相近。

| 编号 | 输入 | 结果 |
| --- | --- | --- |
| P1 | 用户名 `kelvin`，密码 11 个字符 | 拒绝（过短）；12 个字符可以；129 个字符拒绝 |
| P2 | 用户名 `warehouse.lead`（14 个字符），密码 `Warehouse.Lead` | 拒绝（转小写等于用户名）；密码 `warehouse.lead2` 可以 |
| P3 | 同一密码哈希两次 | 两个串不同（盐不同），各自都能校验通过 |
| P4 | 校验 `DUMMY_PASSWORD_HASH`、空串、`scrypt$n=3,r=8,p=5$…`（N 不是 2 的幂） | 都返回不通过，不抛异常 |

### 4.2 登录失败锁定

- `throttle_key = SHA-256 十六进制( 来源 + "\n" + 转小写后的提交用户名 )`，来源的取法见 5.3。
- **失败**指 `outcome` 为 `bad_password`、`unknown_user`、`disabled` 的尝试。`locked` 不算失败（被锁时继续尝试不会延长锁定），`success` 不清零。
- 一次登录开始时，数同一 `throttle_key`、`occurred_at > 现在 − 60 分钟`（严格大于）的失败条数；**满 10 条即拒绝**：不查账号、不校验密码，写一条 `locked`，回答「已锁定」。
- 锁定随最早的那条失败滑出 60 分钟窗口而自动解除，没有人工解锁命令；解除后照常校验密码。
- 只锁「来源 + 用户名」的组合：从别处的来源对同一用户名的尝试不受影响，攻击者无法从自己的地址把管理员锁在外面。
- 并发：两个请求同时读到 9 条失败，都会去校验密码，最多多出几条失败；不为此加锁（单人后台，可接受）。

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| L1 | 来源 A、用户名 `kelvin` 在 10:00:00～10:09:00 每分钟失败一次，共 10 条 | 10:15 来源 A 用正确密码登录 | 拒绝，`locked`；不建会话；失败仍是 10 条 |
| L2 | 同 L1 | 11:00:00 来源 A 用正确密码登录 | 10:00:00 那条不再满足 `> 10:00:00`，失败只剩 9 条 → 校验密码，成功，建会话，写 `success` |
| L3 | 同 L1 | 10:15 来源 B 用正确密码登录 `kelvin` | 来源 B 的组合没有失败 → 成功 |
| L4 | 来源 A 对不存在的用户名 `ghost` 失败 10 次 | 第 11 次来源 A 提交 `ghost` | `locked`；与 `kelvin` 已存在时的表现一致，不泄露用户名是否存在 |
| L5 | 来源 A、`kelvin` 有 9 条失败、1 条 `success` | 再失败一次 | 失败满 10 条（`success` 不清零），下一次即锁定 |

### 4.3 一次登录（`authenticate`）

输入：来源、提交的用户名、提交的密码、当前时间。在**一个数据库事务**里按顺序：

1. 算 `throttle_key`；按 4.2 数失败，满 10 条 → 写 `locked`（`admin_account_id` 为空），提交，返回「已锁定」。
2. 按转小写后的用户名查账号。没有 → 对 `DUMMY_PASSWORD_HASH` 校验一次，写 `unknown_user`（`admin_account_id` 为空），提交，返回「凭据无效」。
3. 校验密码。不通过 → 写 `bad_password`（带账号 id），提交，返回「凭据无效」。
4. 账号已停用 → 写 `disabled`（带账号 id），提交，返回「凭据无效」（与密码错误不可区分；先校验密码再看停用，耗时一致）。
5. 建会话（4.4），写 `success`（带账号 id），提交，返回账号与会话令牌原文。

每次调用恰好写一条登录尝试。

### 4.4 会话与 CSRF

- **令牌**：`secrets.token_urlsafe(32)`；库里只存它的 SHA-256 十六进制（`token_hash`）；原文只在登录成功时返回一次。
- **有效期**：自登录起 **12 小时**绝对到期，使用中不延长。
- **解析**（`resolve_session`）：按 `token_hash` 找会话；`revoked_at` 为空、`expires_at > 现在`、账号 `disabled_at` 为空，三条都成立才有效，否则当作没有会话。
- **撤销**：退出时把该会话的 `revoked_at` 设为现在；已撤销的再撤销不改原值。重设密码、停用账号时撤销该账号**全部**未撤销的会话（与改账号在同一事务）。
- **CSRF 令牌**：`HMAC-SHA256(key=会话令牌原文的 UTF-8 字节, msg=b"inventory-admin-csrf")` 的十六进制，**不入库**，随时可由 cookie 里的会话令牌重算；校验用 `hmac.compare_digest`。浏览器脚本读不到 HttpOnly 的会话 cookie，只能从接口响应拿到 CSRF 令牌。

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| S1 | 08:00 登录成功 | 19:59 解析 | 有效；20:00 解析无效（`expires_at = 20:00`，要求严格大于） |
| S2 | 会话有效 | 退出后解析 | 无效；再退出一次，`revoked_at` 不变 |
| S3 | 账号有两条有效会话 | 命令重设密码 | 两条的 `revoked_at` 都被设上；新密码能登录，旧密码 `bad_password` |
| S4 | 账号有一条有效会话 | 命令停用账号 | 会话被撤销；之后正确密码登录写 `disabled`；恢复后可以重新登录，被撤销的旧会话不复活 |

### 4.5 服务器上的账号命令

`python -m app.admin <子命令> <用户名>`，在服务器上的 API 容器里运行；数据库连接串取自 `INVENTORY_DATABASE_URL`（`app/config.py` 的 `Settings`），未设置时报错退出（退出码 2）。

| 子命令 | 做什么 |
| --- | --- |
| `create-account` | 校验用户名（4.1）；用户名已存在则报错。密码用 `getpass` 不回显地输入两次，不一致或不合 4.1 则报错。写入账号，`created_at` 与 `password_updated_at` 为现在 |
| `reset-password` | 账号必须存在；同样两次输入新密码；更新哈希与 `password_updated_at`，撤销该账号全部会话 |
| `disable-account` | 账号必须存在；已停用则提示后正常退出、不改值；否则设 `disabled_at` 为现在并撤销全部会话 |
| `enable-account` | 账号必须存在；把 `disabled_at` 清空；不恢复已撤销的会话 |

- 密码只经交互输入，不接受命令行参数或环境变量。成功退出码 0，业务错误（用户名不合法、已存在、不存在、密码不合规、两次不一致）退出码 1，错误消息不含密码。
- 命令输出只有用户名与结果，不打印哈希。

## 5. 接口（INVENTORY-TASK-003）

### 5.1 路径与响应

| 方法与路径 | 需要 | 成功 | 失败 |
| --- | --- | --- | --- |
| `POST /api/v1/admin/session` | 请求体 `{"username": <1–64 个字符>, "password": <1–256 个字符>}` | 200 `{"username", "expires_at", "csrf_token"}`，并设会话 cookie | 401 `{"detail": "invalid_credentials"}`（用户名不存在、密码错误、账号停用三种不可区分）；429 `{"detail": "login_locked"}`；请求体不合法 422（框架默认） |
| `GET /api/v1/admin/session` | 有效会话 cookie | 200 `{"username", "expires_at", "csrf_token"}` | 401 `{"detail": "not_authenticated"}` |
| `DELETE /api/v1/admin/session` | 有效会话 cookie ＋ 请求头 `X-CSRF-Token` | 204，撤销会话并清除 cookie | 401 `not_authenticated`；403 `{"detail": "csrf_failed"}` |

- `expires_at` 用 ISO 8601 UTC，带 `Z` 后缀，精确到秒。
- 三个接口的响应都带 `Cache-Control: no-store`。
- 未配置数据库（`database_url` 为空）时三个接口都回答 503 `{"detail": "database_not_configured"}`，与 `/readyz` 一致。
- 已登录时再登录：照常建新会话，旧会话不动（到期或退出时失效）。

### 5.2 会话 cookie

名字 `inventory_admin_session`；属性 `HttpOnly`、`Secure`、`SameSite=Strict`、`Path=/`、`Max-Age=43200`（12 小时，与 4.4 一致）。退出时用同名同属性、`Max-Age=0` 清除。
TLS 在边缘反向代理终止，浏览器看到的是 HTTPS，所以 `Secure` 不需要开关；测试客户端用 `https://` 的 base URL。

### 5.3 来源地址

沿用 acuven-shop 的取法：请求头 `X-Real-IP` **恰好一个**、且是合法的 IPv4 或 IPv6 地址时用它（规范化后的文本）；缺失、出现多次、不合法时退回连接的对端地址；再没有时用 `unknown`。
不用 `X-Forwarded-For`（访客能在前面追加伪造的地址）。上线前由运营者确认边缘反向代理给本站的 vhost **覆盖设置** `X-Real-IP $remote_addr`（共享基建其他 vhost 已是这样）；
没设置时所有人共享代理的地址，锁定只会更严、不会放开，但别处的来源也会被一起锁住。

### 5.4 供之后后台接口用的依赖

- `require_admin`：解析会话 cookie（4.4），无效时 401 `not_authenticated`；有效时返回 `AdminPrincipal(account_id, username, session_id)`，业务任务用它记操作者。
- `require_admin_csrf`：先做 `require_admin`，再校验 `X-CSRF-Token`，不符时 403 `csrf_failed`。之后所有改数据的后台接口（POST、PUT、PATCH、DELETE）都用它。

| 编号 | 操作 | 结果 |
| --- | --- | --- |
| A1 | 正确用户名密码登录 | 200，响应有三个字段，`Set-Cookie` 带 5.2 的全部属性；库里多一条 `success` 与一条会话 |
| A2 | 密码错误、用户名不存在、账号停用各登录一次 | 三次都是 401 `invalid_credentials`，响应体相同 |
| A3 | `X-Real-IP: 203.0.113.7` 失败 10 次后第 11 次 | 429 `login_locked`；换 `X-Real-IP: 203.0.113.8` 用正确密码登录 200 |
| A4 | 登录后不带 `X-CSRF-Token` 调 `DELETE`；再带正确的调一次 | 第一次 403 `csrf_failed`、会话仍有效；第二次 204，之后 `GET` 为 401 |

## 6. 留给之后任务的事

- 登录尝试的查看（后台「待处理」或审计页显示最近的异常尝试）与过期清理（建议保留 90 天，随定时作业表一起做）。
- 严重告警：本文不推送登录锁定（V1_MINIMAL_DESIGN 第 11 节的严重异常清单里没有它）。
- 登录页前端：随「后台前端骨架与 CI 前端检查」做，调用 5.1 的接口。

## 7. 与 acuven-shop 的不同（要 Kelvin 确认）

| 项 | acuven-shop | 本文 | 理由 |
| --- | --- | --- | --- |
| 账号数 | 至多一个（单例槽） | 可以多个，同一角色 | REQUIREMENTS 第 5 节要求独立账号、不与他人共用，审计记实际个人账号 |
| 会话有效期 | 30 天 | 12 小时，不续期 | 这个后台能改真实店铺的库存与配额 |
| 失败计数 | Redis 固定窗口 | 库表 `admin_login_attempts`，60 分钟滑动窗口 | V1_MINIMAL_DESIGN 第 2 节：不用 Redis，登录限流用库表计数 |
| 登录记录 | 审计表只记已有账号的尝试 | 每次尝试一条，用户名不存在的也记（不存用户名原文） | V1_MINIMAL_DESIGN 第 10 节「记录异常尝试」 |
| 停用 | 无 | `disable-account` / `enable-account` | REQUIREMENTS 第 5 节「支持立即停用账号」 |
| CSRF 令牌 | 会话响应里给 | 同样在响应里给，由会话令牌 HMAC 导出、不入库 | 少一列，页面刷新后 `GET` 能重新拿到 |

## 8. 修订记录

- **0.1**（2026-11-23）：草案。

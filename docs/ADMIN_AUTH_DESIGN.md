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

沿用 `app/db.py` 的 `Base` 与命名约定；指向账号的外键一律 `ON DELETE RESTRICT`。

**时间**：一律是不带时区的 UTC、**精确到整秒**（`DateTime`，即 MySQL 的 `DATETIME`，不带小数秒）。规则层只从一处取当前时间：
`datetime.now(UTC).replace(tzinfo=None, microsecond=0)`（截断，不四舍五入）；测试与调用方传入的时间也必须是不带时区（`tzinfo` 为 `None`）的整秒，带时区或带小数秒时报 `ValueError`。
由此存进库的时间与算出来的时间完全相同，4.2 的窗口、4.4 的到期与 5.1 的 `expires_at` 不会差一秒。

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

- **用户名**：合法用户名是 `^[a-z0-9][a-z0-9._-]{2,31}$`（3–32 个 ASCII 字符，小写字母、数字、`.`、`_`、`-`，首字符是字母或数字）。
  命令行输入与登录时提交的用户名都先用 Python 的 `str.lower()` 转小写，得到「规范用户名」。建号时规范用户名不合法即拒绝。
  **登录时规范用户名不合法就不查库**，直接按「用户名不存在」处理（4.3 第 2 步）。这样查库用的永远是纯 ASCII 小写串，MySQL 默认排序规则的大小写与重音不敏感（例如 `kélvin` 会匹配到 `kelvin`）用不上；
  能匹配到同一账号的输入，规范用户名必然相同，`throttle_key`（4.2）也就相同，换写法拿不到新的尝试次数。
- **密码**：长度 12–128 个字符（Python `len`，按码点计），不做组成规则（不强制大小写、数字、符号）；转小写后不得等于规范用户名。不查常见弱口令表（单人或几人使用，靠长度与锁定）。
  送进 scrypt 的是密码的 UTF-8 字节，不做 Unicode 规范化；含无法按 UTF-8 编码的字符（孤立代理项）时，建号与改密拒绝，登录校验返回不通过。
- **哈希**：标准库 `hashlib.scrypt`，参数 N=2^14、r=8、p=5，16 字节随机盐，64 字节摘要，`maxmem = 128 × 16 × 2^16 + 2^20` 字节（显式给出，接受的参数上限也能算）；存成自描述字符串
  `scrypt$n=16384,r=8,p=5$<盐 Base64url>$<摘要 Base64url>`（不带填充，不超过 255 个字符）。校验按串里记下的参数重算、`hmac.compare_digest` 比较；
  串格式不合法、参数越界（N 不是大于 1 的 2 的幂或大于 2^16、r 或 p 大于 16）、盐不是 16 字节或摘要不是 64 字节、计算失败时返回不通过，不抛异常。
- **假哈希**：固定的 `DUMMY_PASSWORD_HASH`（参数同上、盐与摘要全零字节），对任何密码都不通过。用户名不存在时照样对它校验一次，让两种失败的耗时相近。

| 编号 | 输入 | 结果 |
| --- | --- | --- |
| P1 | 用户名 `kelvin`，密码 11 个字符 | 拒绝（过短）；12 个字符可以；129 个字符拒绝 |
| P2 | 用户名 `warehouse.lead`（14 个字符），密码 `Warehouse.Lead` | 拒绝（转小写等于用户名）；密码 `warehouse.lead2` 可以 |
| P3 | 同一密码哈希两次 | 两个串不同（盐不同），各自都能校验通过 |
| P4 | 校验 `DUMMY_PASSWORD_HASH`、空串、`scrypt$n=3,r=8,p=5$…`（N 不是 2 的幂） | 都返回不通过，不抛异常 |

### 4.2 登录失败锁定

- `throttle_key = SHA-256 十六进制( UTF-8( 来源 + "\n" + 规范用户名 ) )`，规范用户名见 4.1（合法与否都用它算），来源的取法见 5.3。
  提交的用户名含无法按 UTF-8 编码的字符时，按 `errors="replace"` 编码后再算（只用于计数，不查库）。
- **失败**指 `outcome` 为 `bad_password`、`unknown_user`、`disabled` 的尝试。`locked` 不算失败（被锁时继续尝试不会延长锁定），`success` 不清零。
- 一次登录开始时，数同一 `throttle_key`、`现在 − 60 分钟 < occurred_at <= 现在` 的失败条数（下界严格大于，上界含等于；时间在「现在」之后的记录不算）；**满 10 条即拒绝**：不查账号、不校验密码，写一条 `locked`，回答「已锁定」。
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
| L6 | 来源 A、`kelvin` 有 10 条失败 | 来源 A 提交 `KELVIN`、`kélvin` | `KELVIN` 的规范用户名是 `kelvin`，同一个 key → `locked`；`kélvin` 不合法、不查库，算作另一个 key 的 `unknown_user`，永远登录不进 `kelvin` |

### 4.3 一次登录（`authenticate`）

`authenticate(db, *, source, username, password, now) -> LoginResult`：`db` 是调用方给的 SQLAlchemy `Session`，本函数在里面完成并**提交**一个事务。按顺序：

1. 算 `throttle_key`；按 4.2 数失败，满 10 条 → 写 `locked`（`admin_account_id` 为空），提交，返回「已锁定」。
2. 规范用户名不合法（4.1），或按它查不到账号 → 对 `DUMMY_PASSWORD_HASH` 校验一次，写 `unknown_user`（`admin_account_id` 为空），提交，返回「凭据无效」。
   查账号用 `SELECT … FOR UPDATE`，**锁住账号行到本事务提交**（见 4.4 的「并发」）。
3. 校验密码。不通过 → 写 `bad_password`（带账号 id），提交，返回「凭据无效」。
4. 账号已停用 → 写 `disabled`（带账号 id），提交，返回「凭据无效」（与密码错误不可区分；先校验密码再看停用，耗时一致）。
5. 建会话（4.4），写 `success`（带账号 id），提交，返回账号与会话令牌原文。

每次调用恰好写一条登录尝试。

`LoginResult` 是不可变的数据类：`outcome`（`"success"`、`"invalid_credentials"`、`"locked"` 之一）；成功时另有 `account_id`、`username`、`session_id`、`token`（会话令牌原文）、`expires_at`，失败时这几项为 `None`。
它的 `repr` 不含 `token`（字段声明 `repr=False`）。数据库出错（`SQLAlchemyError`）时回滚并原样抛出，由调用方处理。

### 4.4 会话与 CSRF

- **令牌**：`secrets.token_urlsafe(32)`；库里只存它的 SHA-256 十六进制（`token_hash`）；原文只在登录成功时返回一次。
- **有效期**：自登录起 **12 小时**绝对到期，使用中不延长。
- **解析**：`resolve_session(db, *, token, now) -> ActiveSession | None`。按 `token_hash` 找会话；`revoked_at` 为空、`expires_at > 现在`、账号 `disabled_at` 为空，三条都成立才返回
  `ActiveSession(session_id, account_id, username, expires_at)`（不可变数据类），否则返回 `None`。`token` 为空串或不是字符串时直接返回 `None`。只读，不提交。
- **撤销**：`revoke_session(db, *, session_id, now) -> None`，把该会话的 `revoked_at` 设为现在并提交；已撤销的再撤销不改原值；没有这个 `session_id` 时什么都不做、不报错。重设密码、停用账号时撤销该账号**全部**未撤销的会话（与改账号在同一事务）。
- **并发**：登录（4.3）与重设密码、停用、启用（4.5）都先对账号行 `SELECT … FOR UPDATE`，在同一事务里改完才提交，所以对同一账号是串行的：
  停用先提交时，排在后面的登录看到 `disabled_at` 已设，写 `disabled`；登录先提交时，它建的会话已在库里，随后的停用把它一起撤销。重设密码同理（后到的登录用新哈希校验）。
  不会出现「撤销全部会话之后又冒出一条有效会话」。
- **CSRF 令牌**：`csrf_token_for(token) -> str`，`HMAC-SHA256(key=会话令牌原文的 UTF-8 字节, msg=b"inventory-admin-csrf")` 的十六进制，**不入库**，随时可由 cookie 里的会话令牌重算；
  `csrf_token_matches(token, presented) -> bool` 用 `hmac.compare_digest` 比较，`presented` 为空或不是字符串时为 `False`。浏览器脚本读不到 HttpOnly 的会话 cookie，只能从接口响应拿到 CSRF 令牌。
- 以上函数与 `authenticate` 都在 `app/services/admin_auth.py`，接口层（第 5 节）只调用它们，不另写查询。

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
| `create-account` | 先校验用户名（4.1）并查库，已存在则报错——都在提示输入密码**之前**。密码用 `getpass` 不回显地输入两次，不一致或不合 4.1 则报错。写入账号，`created_at` 与 `password_updated_at` 为现在 |
| `reset-password` | 先查账号必须存在，再同样两次输入新密码；更新哈希与 `password_updated_at`，撤销该账号全部会话 |
| `disable-account` | 账号必须存在；已停用则输出「已是停用状态」、退出码 0、不改值；否则设 `disabled_at` 为现在并撤销全部会话 |
| `enable-account` | 账号必须存在；未停用则输出「已是启用状态」、退出码 0、不改值；否则把 `disabled_at` 清空；不恢复已撤销的会话 |

- **账号变更的服务函数**（在 `app/services/admin_auth.py`，各自完成并提交一个事务，时间由参数传入，测试的 S3、S4 直接调它们）：
  - `create_account(db, *, username, password, now) -> int`（返回账号 id）
  - `reset_password(db, *, username, password, now) -> None`
  - `disable_account(db, *, username, now) -> bool`（`False` = 原本已停用，未改）
  - `enable_account(db, *, username) -> bool`（`False` = 原本未停用，未改）
  - 四个函数都先按 4.1 把 `username` 转成规范用户名，不合法即抛 `invalid_username`、不查库；查库只用规范用户名。
  - 业务错误抛 `AccountError`，带 `code`：`invalid_username`、`already_exists`、`not_found`、`weak_password`（消息不含密码）。`create_account` 撞上唯一约束的 `IntegrityError` 时回滚并转成 `already_exists`。
  - 命令只负责解析参数、提示输入密码、取当前时间（第 3 节的取法）、调这些函数、把结果与错误转成输出和退出码；命令开始前的「用户名合法、存在与否」检查用同一模块里的只读查询，不另写规则。
- 入口是 `main(argv: list[str] | None = None) -> int`，`if __name__ == "__main__": sys.exit(main())`；测试直接调 `main([...])`，替换 `getpass.getpass` 提供输入。
- 密码只经交互输入，不接受命令行参数或环境变量。
- **退出码**：0 成功（含上面两种「已是……状态」）；1 业务错误或取消——用户名不合法、已存在（含并发建号撞上唯一约束的 `IntegrityError`）、不存在、密码不合规、两次不一致、输入密码时遇到 EOF 或 Ctrl+C（输出「已取消」，什么都不写）；
  2 运行环境错误——没设 `INVENTORY_DATABASE_URL`、连不上数据库、表不存在或其他 `SQLAlchemyError`（输出「数据库错误」加异常类名，不输出连接串）。参数不对由 `argparse` 处理（它自己的退出码 2）。
- 消息写到标准错误，成功结果写到标准输出；输出只有用户名与结果，不打印密码或哈希。

## 5. 接口（INVENTORY-TASK-003）

### 5.1 路径与响应

| 方法与路径 | 需要 | 成功 | 失败 |
| --- | --- | --- | --- |
| `POST /api/v1/admin/session` | 请求体 `{"username": <1–64 个字符>, "password": <1–256 个字符>}` | 200 `{"username", "expires_at", "csrf_token"}`，并设会话 cookie | 401 `{"detail": "invalid_credentials"}`（用户名不存在、密码错误、账号停用三种不可区分）；429 `{"detail": "login_locked"}`；请求体不合法 422 `{"detail": "invalid_request"}`（见下） |
| `GET /api/v1/admin/session` | 有效会话 cookie | 200 `{"username", "expires_at", "csrf_token"}` | 401 `{"detail": "not_authenticated"}` |
| `DELETE /api/v1/admin/session` | 有效会话 cookie ＋ 请求头 `X-CSRF-Token` | 204，撤销会话并清除 cookie | 401 `not_authenticated`；403 `{"detail": "csrf_failed"}` |

- `expires_at` 用 ISO 8601 UTC，形如 `2026-11-23T20:00:00Z`（库里本就是整秒，见第 3 节，原样格式化，不再取整）。
- **登录请求体不用框架默认的 422**：FastAPI 默认的校验错误会把提交的值（含密码）放进响应的 `input` 字段。登录接口自己读请求体（`await request.body()` 后 `json.loads`）并逐项检查：
  不是 JSON 对象、缺 `username` 或 `password`、不是字符串、长度不在上表范围内，一律 422 `{"detail": "invalid_request"}`，不回显任何提交内容。多出来的字段忽略。
- 三个接口的**所有**响应（含 401、403、422、429、503）都带 `Cache-Control: no-store`。
- 未配置数据库（`database_url` 为空）时三个接口都回答 503 `{"detail": "database_not_configured"}`；处理中数据库出错（`SQLAlchemyError`）时回答 503 `{"detail": "database_unreachable"}`，与 `/readyz` 的两个原因一致，响应不含异常内容。
- 每个请求用一个 SQLAlchemy `Session`（由 `create_app` 里已有的 engine 建 `sessionmaker`），请求结束关闭。
- 已登录时再登录：照常建新会话，旧会话不动（到期或退出时失效）。

### 5.2 会话 cookie

名字 `inventory_admin_session`；属性 `HttpOnly`、`Secure`、`SameSite=Strict`、`Path=/`、`Max-Age=43200`（12 小时，与 4.4 一致）。退出时用同名同属性、`Max-Age=0` 清除。
TLS 在边缘反向代理终止，浏览器看到的是 HTTPS，所以 `Secure` 不需要开关；测试客户端用 `https://` 的 base URL。

### 5.3 来源地址

沿用 acuven-shop 的取法：请求头 `X-Real-IP` **恰好一个**、且是合法的 IPv4 或 IPv6 地址时用它（规范化后的文本）；缺失、出现多次、不合法时退回连接的对端地址；再没有时用 `unknown`。
不用 `X-Forwarded-For`（访客能在前面追加伪造的地址）。

按来源锁定**依赖一个部署前提**：边缘反向代理给本站的 vhost 用 `proxy_set_header X-Real-IP $remote_addr` **覆盖**这个头（共享基建其他 vhost 已是这样）。三种情形：

| 代理的做法 | 结果 |
| --- | --- |
| 覆盖设置（要求的做法） | 来源是访客的真实地址，锁定按设计工作 |
| 不设置这个头 | 所有访客共用代理的地址：锁定更严，但别处的来源也会被一起锁住 |
| 把访客自己送来的头原样转进来 | **锁定可被绕过**：攻击者每次换一个头的值就是一个新的 key |

- 所以「本站 vhost 覆盖设置 `X-Real-IP`」是上线前提，由运营者在开通子域名时确认，并从外网用一个伪造的 `X-Real-IP` 头试登录失败几次、在库里看到 key 没有随头变化（之后的上线检查记录）。
- 本服务不发布端口，只挂在共享的代理网络上；同一网络里的其他容器能绕过代理直接带任意头访问。那些都是 Acuven 自己的服务，这条残余风险接受（要 Kelvin 确认，第 7 节）。
- 本仓库的代码无法判断代理有没有覆盖这个头，所以不做「只信任某个代理地址」的配置。

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
| 来源头的残余风险 | 同样依赖代理覆盖 `X-Real-IP` | 同左；另把它写成上线前提，并接受共享代理网络里其他容器能直接带头访问 | 5.3 |

## 8. 修订记录

- **0.1**（2026-11-23）：草案。同日按三个任务的设计预审补齐：时间截断到整秒；登录时规范用户名不合法即不查库（避开 MySQL 排序规则的大小写与重音不敏感）；
  密码的编码与 scrypt 的 `maxmem`；账号命令的退出码与先校验后提示；服务函数的签名与返回值（含账号变更的服务函数）；登录与账号变更对账号行加锁，避免撤销全部会话后又冒出新会话；登录请求体不回显提交值的 422；数据库出错时的 503；来源头的部署前提与残余风险；`now` 不得带时区、失败计数的窗口上界、账号变更先规范化用户名。

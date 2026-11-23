# Core 客户端、命令发件箱与进程内调度（V1 设计补充，W1）

> 版本：0.1 · 日期：2026-11-23 · 状态：**草案，待 Kelvin 批准**。批准之前，INVENTORY-TASK-005～008 不得开始写代码（CLAUDE.md：幂等、状态机走设计闸门）。
> 依据：[V1_MINIMAL_DESIGN](V1_MINIMAL_DESIGN.md) 0.3 第 2 节（进程内调度加作业表，不用 Redis、Celery）、第 3.2 节（引用与操作者头）、第 3.3 节（接口与两个凭据）、第 3.5 节（发件箱、结果不明与 Core 不可用）、第 15 节 W1；
> `acuven_stock_core/docs/CONTRACT_V1.md` 0.21（下称「Core 契约」，`§` 指它的章节）：§7.1 两层去重、§8.1 协议约定、§8.3 错误码、§8.4 兼容规则、§10 鉴权与凭据。
> 本文只做通用的地基：怎么把一个写命令可靠地送到 Core、怎么读 Core、怎么定时做事。**具体哪个业务动作发什么命令**（预留、确认销售、收货……）由之后的工作包（W3、W4）登记成「命令种类」并写各自的处理函数，本文只定它们要遵守的接口。
> 与 V1_MINIMAL_DESIGN 冲突时以它为准（本文对它的一处收窄列在第 7 节，要 Kelvin 确认）；设计有改动时，改动部分的批准作废、须重新批准。

分四个任务实现：

| 任务 | 层 | 本文章节 |
| --- | --- | --- |
| INVENTORY-TASK-005 | 数据：发件箱、顺序锁、作业三张表，迁移 0002 | 第 2 节 |
| INVENTORY-TASK-006 | Core HTTP 客户端与响应分类（不碰数据库） | 第 3 节 |
| INVENTORY-TASK-007 | 发件箱服务：入队、顺序、发送、重试、结果回调、取消与恢复 | 第 4 节 |
| INVENTORY-TASK-008 | 进程内调度器与应用启动接线 | 第 5 节 |

部署侧（Core 生产实例的内部网络、两个令牌文件、打开调度器的环境变量）不是 Worker 任务，在第 6 节列为上线步骤。

---

## 1. 原则

1. **先落库，再发出。**每个 Core 写命令先在 Inventory 库里写一行发件箱记录（和触发它的业务变化同一个事务），带一个**入队时就定死**的幂等键与请求体，然后才由后台发出。进程崩溃、Core 不可用都不会丢命令，也不会换键重发。
2. **可能已经发出的命令，只能同键重发。**超时、连接中断、500 与其他 5xx 的结果都是「未知」（§8.3）。Core 把幂等记录与命令写入放在同一个事务提交（§7.1），所以同键重发要么原样重放已生效的结果，要么第一次执行，不会生效两次。**绝不把未知当成功或失败**，也不在 Inventory 一侧补造数量（V1 设计 3.5）。可能发出过的命令也不能取消（4.6）。
3. **业务拒绝是确定结果。**取锁之后的 4xx（缺货、SKU 停用等）已被 Core 保存，同键只会重放（§7.1）；交给登记该命令种类的业务处理函数决定下一步。要再试只能由业务层**入队一条新命令**，带新幂等键（§7.1 最后一条）。
4. **配置与凭据问题不消耗命令；Inventory 自己的 bug 要停下来。**401、403 发生在 Core 取锁之前、不保存（§7.1、§8.1 检查顺序第 4、5 步），同键重发永远安全：命令保持排队，发件箱整体暂停一段时间再试，由 W8 告警（唯一的例外是 SKU 合并执行时锁内复核池的 403，它会被保存，§2.8；V1 不做 SKU 合并，V1 设计 3.3）。其他调用方错误（400、405、413、幂等或业务引用冲突等）说明 Inventory 有 bug：命令停在 `failed`，同一订单后面的命令也不再发出，等人处理；修好之后由运维把它**同键**放回队列（4.7）。
5. **同一顺序键内，按入队的先后发出。**同一个「顺序键」（订单引用）下，先入队的没有成功，后入队的就不发。本层只保证「入队顺序」，**不保证业务上的先后**：确认销售要带预留 ID，只能在预留成功后由处理函数入队，在那之前同一订单入队的其他命令会排在它前面。业务层（W4）必须按订单状态决定什么时候入队下一步，不能指望发件箱替它排业务顺序（4.2）。

## 2. 数据模型（INVENTORY-TASK-005）

沿用 `app/db.py` 的 `Base`、命名约定与 `hide_parameters`；时间一律是不带时区、精确到整秒的 UTC（同 [ADMIN_AUTH_DESIGN](ADMIN_AUTH_DESIGN.md) 第 3 节，取当前时间用其中的 `utc_now()`）。

**排序规则**：三张表都用 `utf8mb4_bin`（模型的 `__table_args__` 写 `mysql_collate`，迁移的 `create_table` 同样写）。MySQL 8 默认的 `utf8mb4_0900_ai_ci` 不区分大小写，会让检查约束接受 `post`、`/V1/x`、`QUEUED`，也会让幂等键、顺序键的比较忽略大小写；Core 的幂等键与引用都区分大小写。

**`core_outbox_commands`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BigInteger | 主键，自增；同一顺序键内的先后（第 4.1 节保证它就是入队与提交的先后） |
| `kind` | String(40) | 非空；命令种类名（如之后的 `reservation.create`），格式在入队时校验（4.1） |
| `ordering_key` | String(128) | 可空；同一键内按 `id` 顺序发送；不要求顺序的命令为空 |
| `method` | String(6) | 非空；检查 `ck_core_outbox_commands_method_known`：`method IN ('POST', 'PATCH')` |
| `path` | String(255) | 非空；Core 的路径；检查 `ck_core_outbox_commands_path_v1`：`path LIKE '/v1/%'` |
| `body` | JSON | 可空（没有请求体的命令，如 `late-payment-review/close`）；没有请求体存 SQL `NULL`，不存 JSON 的 `null`（列用 `JSON(none_as_null=True)`，`response_body` 同） |
| `actor_ref` | String(128) | 可空 |
| `actor_role` | String(20) | 可空；检查 `ck_core_outbox_commands_actor_pair`：`(actor_ref IS NULL) = (actor_role IS NULL)`（§8.1 两个头要么都带、要么都不带） |
| `idempotency_key` | String(128) | 非空；唯一 `uq_core_outbox_commands_idempotency_key` |
| `status` | String(12) | 非空；检查 `ck_core_outbox_commands_status_known`：`status IN ('queued', 'succeeded', 'rejected', 'failed', 'cancelled')` |
| `attempts` | Integer | 非空，默认 0（模型的列写 `default=0, server_default="0"` 两个，迁移写 `server_default="0"`）；检查 `ck_core_outbox_commands_attempts_nonnegative`：`attempts >= 0` |
| `handler_failures` | Integer | 非空，默认 0（写法同 `attempts`）；处理函数失败的次数（4.3）；检查 `ck_core_outbox_commands_handler_failures_nonnegative`：`handler_failures >= 0` |
| `next_attempt_at` | DateTime | 非空；`queued` 时最早什么时候再发 |
| `lease_until` | DateTime | 可空；有发送者正在处理时非空 |
| `last_claimed_at` | DateTime | 可空；**第一次**被认领的时间，设了就不再改、不再清。非空 = 可能已经发出过（4.6 取消的依据） |
| `resolved_at` | DateTime | 可空；业务层确认已处理完这条被拒或失败的命令、不再挡住同一顺序键的时间（4.2）；检查 `ck_core_outbox_commands_resolved_only_final`：`resolved_at IS NULL OR status IN ('rejected', 'failed')` |
| `last_outcome` | String(16) | 可空；最近一次发送的分类（3.3 除 `not_configured` 外的六类之一） |
| `first_unknown_at` | DateTime | 可空；第一次得到「未知」的时间，之后不清（供 W8 判断积压与告警） |
| `http_status` | SmallInteger | 可空；最近一次得到的 HTTP 状态码 |
| `error_code` | String(64) | 可空；最近一次响应的错误码（4.3） |
| `last_handler_error` | String(255) | 可空；最近一次处理函数失败的异常类名（不存消息） |
| `response_body` | JSON | 可空；**最终**结果（成功或确定的拒绝）的响应体，供业务处理函数与排查；未知的中间响应不存 |
| `core_request_id` | String(128) | 可空；最近一次响应的请求 ID（3.3） |
| `created_at` | DateTime | 非空 |
| `finished_at` | DateTime | 可空；进入 `succeeded`、`rejected`、`failed`、`cancelled` 的时间 |

索引：`ix_core_outbox_commands_status_next_attempt_at`（`status`、`next_attempt_at`），`ix_core_outbox_commands_ordering_key_id`（`ordering_key`、`id`）。

**`core_outbox_ordering_locks`**（只用来让同一顺序键的入队串行，4.1）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `ordering_key` | String(128) | 主键 |

行只增不删（每个订单一行，V1 的量很小）。

**`scheduled_jobs`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `name` | String(64) | 主键；作业名（如 `core_outbox.drain`） |
| `next_run_at` | DateTime | 非空 |
| `lease_owner` | String(64) | 可空；持有租约的进程标识（第 5 节） |
| `lease_until` | DateTime | 可空 |
| `last_started_at` | DateTime | 可空 |
| `last_finished_at` | DateTime | 可空 |
| `last_status` | String(12) | 可空；检查 `ck_scheduled_jobs_last_status_known`：`last_status IS NULL OR last_status IN ('ok', 'error')` |
| `last_error` | String(255) | 可空；只存异常的类名，不存消息（消息可能含数据） |

- 迁移：`alembic/versions/20261123_0002_core_outbox_and_jobs.py`，revision `"0002"`，`down_revision = "0001"`；upgrade 建三张表并逐个写出约束与索引名，downgrade 删三张表。
- 模型：`app/models/core_outbox.py`（`CoreOutboxCommand`、`CoreOutboxOrderingLock`）、`app/models/scheduled_job.py`（`ScheduledJob`），`app/models/__init__.py` 导出。
- 集成测试照 INVENTORY-TASK-001 的做法：用 alembic 的 upgrade 按迁移建表（检查约束与排序规则只有对迁移建出的表测才算数），跑完例子后 downgrade 到 base；没有测试库时跳过。
- **上线前提**同 ADMIN_AUTH_DESIGN 第 3 节：MySQL ≥ 8.0.16（生产是 8.0.46）。
- 发件箱表**不存个人资料**：命令体里只有 Core 允许的引用、SKU ID、件数与时间（Core 契约 §10 第 6 条），`actor_ref` 是管理员的不透明账号 ID。

| 编号 | 操作 | 结果 |
| --- | --- | --- |
| D1 | 插入两条 `idempotency_key` 相同的命令 | 第二条被 `uq_core_outbox_commands_idempotency_key` 拒绝 |
| D2 | 插入 `actor_ref` 有值、`actor_role` 为空的命令 | 被 `ck_core_outbox_commands_actor_pair` 拒绝；两者都空、都有值都可以 |
| D3 | 插入 `status` 为 `sending`、`method` 为 `GET`、`path` 为 `/v2/x`、`attempts` 为 −1、`handler_failures` 为 −1 的命令（各一条） | 分别被对应的检查约束拒绝 |
| D4 | 插入 `last_status` 为 `running` 的作业 | 被 `ck_scheduled_jobs_last_status_known` 拒绝 |
| D5 | 插入 `status = queued` 且 `resolved_at` 有值的命令 | 被 `ck_core_outbox_commands_resolved_only_final` 拒绝；`status = rejected` 时可以 |
| D6 | 插入 `status` 为 `QUEUED`（大写）的命令；插入两条 `idempotency_key` 只差大小写的命令 | 前者被 `ck_core_outbox_commands_status_known` 拒绝；后者两条都能插入（`utf8mb4_bin` 区分大小写） |

## 3. Core HTTP 客户端（INVENTORY-TASK-006）

### 3.1 配置

`app/config.py` 的 `Settings` 新增（环境变量前缀不变，都可以不设）：

| 字段 | 环境变量 | 含义 |
| --- | --- | --- |
| `core_base_url` | `INVENTORY_CORE_BASE_URL` | Core 的根地址；空 = 未配置 |
| `core_token_file` | `INVENTORY_CORE_TOKEN_FILE` | 业务凭据令牌所在文件的路径（Docker secrets 文件）；空 = 未配置 |
| `core_ops_token_file` | `INVENTORY_CORE_OPS_TOKEN_FILE` | 运维凭据（Inventory 每日对账那一个，Core 契约 §10 第 2 条）令牌文件；空 = 未配置 |

- **地址合格**：先要求整串只含可见 ASCII（0x21–0x7E，不含空格、制表符、回车换行——`urlsplit` 会悄悄去掉这些字符，之后 `http.client` 却会报错），再用 `urllib.parse.urlsplit` 解析，scheme 是 `http` 或 `https`，有主机名，端口能解析（访问 `.port` 不抛 `ValueError`），没有用户信息、查询串与片段，路径为空。不合格按「未配置」处理。
- **令牌每次请求时读文件**，去掉首尾空白，不缓存：Core 轮换令牌时有最长 7 天的重叠期（§10 第 4 条），换文件即可生效，不用重启。
- **令牌合格**：去掉首尾空白后是 1–512 个 `[A-Za-z0-9._~+/=-]` 字符（RFC 6750 的 b64token；Core 的令牌是 `csk_` 加 43 个 base64url 字符）。文件不存在、读不了、为空或内容不合格，都按「未配置」处理；进程日志只记是哪一个文件（业务或运维）出了什么问题，**不记内容**。这样含换行或非 ASCII 字符的令牌不会进到 HTTP 库里、再随异常消息带出来。
- 令牌只出现在 `Authorization` 头里；不进日志、异常消息、`repr`、数据库。
- 仓库里不写真实地址：示例值放 `.env.example`（本任务不改它，由第 6 节的上线步骤补）。

### 3.2 发请求

`app/core_client/http.py`，只用标准库（`urllib.request`、`urllib.parse`、`json`），**不加依赖**：

```
DEFAULT_TIMEOUT_SECONDS = 15
send(settings, *, method, path, body=None, idempotency_key=None, actor=None, credential="business", request_id=None, transport=None, timeout=DEFAULT_TIMEOUT_SECONDS) -> CoreResponse
encode_body(body) -> bytes
check_configured(settings, credential="business") -> bool
```

- **判定顺序**：先做下面的校验与请求体编码（不合格报 `ValueError`，是调用方 bug，不管配没配置）；再看配置（`check_configured` 为假就返回 `not_configured`，不发请求）；最后才发出。
- **校验**：
  - `credential` 只能是 `business` 或 `ops`；
  - `method` 只能是 `GET`、`POST`、`PATCH`；`GET` 不能带幂等键或请求体，`POST`、`PATCH` 必须带幂等键；
  - `path` 整串匹配 `/v1/[A-Za-z0-9._~:/?&=%,+-]{0,250}`（只有 ASCII，不含空格与控制字符；调用方负责对路径参数与查询值做百分号编码）；
  - `Idempotency-Key` 1–128 个 0x21–0x7E 字符；`request_id` 1–128 个 `[A-Za-z0-9._:-]` 字符；
  - `actor` 为 `Actor(ref, role)` 时，`ref` 1–128 个 `[A-Za-z0-9._:-]` 字符，`role` 只能是 `stock_operator`、`stock_admin`、`catalog_admin`（§10 第 3 条）。
- **`encode_body(body)`**：`body` 必须是 `dict`（JSON 对象），整棵树里只允许 `dict`（键必须是 `str`）、`list`、`str`、`int`、`bool`、`None`（`tuple`、`float`、非字符串的键等一律拒绝）；`json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")`。下面任一情况报 `ValueError`：
  - 不是 `dict`；
  - 含 `float`（Core 拒绝带小数点或指数的数，§7.1；件数与 ID 都是整数或字符串）；
  - 含 `NaN`、`Infinity`；
  - 有不能编码的对象（把 `TypeError` 转成 `ValueError`）；
  - 任何列表超过 100 个元素（Core 的批量上限，§8.1 第 7 步）；
  - 编码后超过 128 KiB（§8.1 第 3 步）。
- **`check_configured(settings, credential)`**：只判定「配置是否可用」（3.1 的地址与对应令牌文件），不发请求、**任何输入都不抛异常**（`credential` 不是 `business`、`ops` 时返回假）。发件箱（4.3 第 1 步）用它。`send` 内部用同一个私有函数一次读出「地址与令牌」（读不到或不合格即 `not_configured`），**只读一次令牌文件**、用读到的那个值发请求，不会出现检查时合格、发送时又去读一次的情况。
- **`validate_request(*, method, path, body, idempotency_key, actor, request_id, credential)`**：公开的校验函数，做上面「校验」与 `encode_body` 的全部检查，不合格报 `ValueError`。`send` 与发件箱入队（4.1）都调它，不各写一套。
- **请求头**：
  - `Authorization: Bearer <令牌>`；
  - `Content-Type: application/json`（有请求体时）；
  - `Idempotency-Key`（写请求）；
  - `X-Request-ID`：调用方给的，不给就生成 `inv_` 加 26 个 `[A-Za-z0-9]` 随机字符；
  - `X-Actor-Ref`、`X-Actor-Role`：`actor` 不为空时两个都带，为空时都不带；
  - `User-Agent: acuven-inventory/<版本>`（§8.4 要求带名字与版本）；`<版本>` 是 `settings.git_sha`，不符合 1–64 个 `[A-Za-z0-9._-]` 字符时用 `unknown`。
- **transport**：可替换的发送函数（测试注入假的，生产用 urllib），签名 `(method, url, headers, body_bytes, timeout) -> (status, headers, body_bytes)`；请求头传 `dict[str, str]`，返回的响应头是 `dict[str, str]`、键一律小写（`send` 按小写查找）。拿不到 HTTP 响应时以异常抛出。
- **生产 transport**：用 `urllib.request.build_opener(urllib.request.ProxyHandler({}), <不跟随重定向的处理器>)` 建一次 opener，`opener.open(request, timeout=timeout)`。
  - `ProxyHandler({})` **不用**环境变量里的 `HTTP_PROXY` 等代理（Core 只在内部网络，走代理只会把令牌交给代理）；
  - 不跟随重定向的处理器是 `HTTPRedirectHandler` 的子类、`redirect_request` 返回 `None`，3xx 因此以 `HTTPError` 出来；
  - `urllib.error.HTTPError` 当作**正常响应**返回（取它的状态码、头与正文）；
  - 超时只有一个值（标准库里它同时管连接与每次读）；默认 15 秒，测试传小值。
- **异常**：`send` 把 `urllib.error.URLError`、`TimeoutError`（含 `socket.timeout`）、`ConnectionError`、`http.client.HTTPException` 及其他 `OSError`（`ssl.SSLError` 是它的子类）归为 `unknown`；**不要 `import ssl`**（Worker 沙箱关闭 Win32k 时 `_ssl` 加载失败；`urllib.request` 自己对 `ssl` 是可选导入）。别的异常（编程错误）原样抛出。
- 只返回分类后的结果，不抛业务异常。

### 3.3 响应分类

`CoreResponse` 是不可变数据类：`outcome`（下面七类之一）、`http_status`（拿不到为 `None`）、`data`（成功时响应体的 `data`）、`error_code`、`error_details`、`retry_after`（秒，整数，没有为 `None`）、`request_id`、`replayed`、`body`（解析后的整个响应体，拿不到为 `None`）。

- 响应头：生产 transport 对同名的头只取第一个值；值为空串的头当作没有。
- 正文里的字段类型不对时当作没有：`error.code` 不是字符串（按「没有错误码」处理）、`error.details` 不是对象（`error_details` 为 `None`）、`request_id` 不是字符串。
- `request_id`：取响应头 `x-request-id`；没有这个头时取响应体的 `request_id`；都没有为 `None`（Core 两处平时相同；幂等重放时响应体里是首次的，头里是本次的，§7.1）。
- `replayed`：响应头 `idempotency-replayed` 的值去掉首尾空白后、不分大小写等于 `true`。
- 响应体按 UTF-8 解码、`json.loads` 解析，`parse_constant` 遇到 `NaN`、`Infinity` 时报错：这样的正文按「不是合法 JSON」处理。
- `Retry-After` 只认非负整数秒（全是数字），截到 0–600；HTTP 日期格式或其他写法当作没有（`None`）。

| 分类 | 判定（按顺序，先命中先算） | 调用方该做的 |
| --- | --- | --- |
| `not_configured` | 没发出：`check_configured` 为假 | 不算一次尝试 |
| `unknown` | 超时、连接被拒或中断、TLS 错误；状态码小于 200 或大于 599；500、502、504 与其他 5xx（503 另见下）；503 但错误码不是 `UNAVAILABLE`；2xx 但正文不是合法 JSON、不是对象，或 `data` 不是 JSON 对象（缺失、`null`、数组都算） | **结果未知**：同键重发 |
| `retry` | 503 且 `code` 为 `UNAVAILABLE`（确定未生效）；409 `REQUEST_IN_PROGRESS`；429 `RATE_LIMITED` | 同键重发，`Retry-After` 有就照它 |
| `auth_error` | 401；403 | 凭据或权限问题，Core 取锁之前就拒了、不保存：同键长退避重发，告警 |
| `succeeded` | 2xx 且正文的 `data` 是 JSON 对象 | 完成 |
| `caller_error` | 400；405；413；3xx；409 的 `IDEMPOTENCY_CONFLICT`、`BUSINESS_REF_CONFLICT`、`ORDER_REFERENCE_CONFLICT`；422 `INVALID_REQUEST`；404 `NOT_FOUND`（路由不存在）；4xx 但正文不是合法 JSON 或没有字符串 `error.code`（例如框架默认的 404 正文——Core 的错误总带错误体，没有就说明请求没到 Core 的业务代码） | Inventory 的 bug：停下，等人 |
| `rejected` | 其余 4xx（含 404 的各种 `*_NOT_FOUND`、409 的业务码、422 的其他码，以及**认不出的错误码**——§8.4 要求当不可重试处理） | 确定的业务拒绝：交业务处理函数 |

| 编号 | Core 的回答 | 分类 |
| --- | --- | --- |
| C0 | 404，正文 `{"detail": "Not Found"}`（没有 `error.code`） | `caller_error` |
| C1 | 201，`{"data": {...}, "request_id": "r1"}`，响应头 `X-Request-ID: r1` | `succeeded`，`data` 原样，`request_id = r1` |
| C2 | 201，响应头 `Idempotency-Replayed: true`、`X-Request-ID: r2`，正文 `request_id` 为 `r1` | `succeeded`，`replayed` 为真，`request_id = r2` |
| C3 | 409 `INSUFFICIENT_STOCK`，`details.lines` | `rejected`，`error_details` 带回 lines |
| C4 | 409 `REQUEST_IN_PROGRESS`，`Retry-After: 1` | `retry`，`retry_after` = 1 |
| C5 | 503 `UNAVAILABLE` | `retry` |
| C6 | 500 `INTERNAL_ERROR`；502；读超时；连接被拒 | 四个都是 `unknown` |
| C7 | 409 `IDEMPOTENCY_CONFLICT`；422 `INVALID_REQUEST` | 两个都是 `caller_error` |
| C7b | 401 `UNAUTHORIZED`；403 `FORBIDDEN` | 两个都是 `auth_error` |
| C8 | 409 `SOME_FUTURE_CODE`（客户端不认识） | `rejected` |
| C9 | 令牌文件不存在；令牌文件内容**中间**含换行 | 两个都是 `not_configured`，没有发出任何请求（假 transport 没被调用），异常与日志里没有令牌内容；令牌文件只是**结尾**带换行时照常发出 |
| C10 | 200 但正文是 `<html>`；200 但正文含 `NaN` | 两个都是 `unknown` |
| C11 | 请求体含 `1.5`；`path` 含空格；请求体的键是整数 | 发出前 `ValueError`，没调 transport |
| C12 | `core_base_url` 中间含制表符 | `not_configured`（`check_configured` 为假），没调 transport |

### 3.4 读接口

同一个 `send`，`method="GET"`、不带幂等键；分类相同（`rejected` 对读接口一般是 404）。读失败的处理由调用方定：读不到 Core 的量时，**不能当作 0**（例如 W3 的库存总览显示「Core 暂不可用」）。

## 4. 发件箱服务（INVENTORY-TASK-007）

`app/services/core_outbox.py`。所有接受 `now` 或 `now_fn` 的公开函数都按 ADMIN_AUTH_DESIGN 第 3 节校验时间：必须是不带时区的整秒，否则 `ValueError`（小数秒会被 MySQL 四舍五入，租约值就对不上了）。

### 4.1 入队

`enqueue(db, *, kind, method, path, body, ordering_key=None, actor=None, now) -> CoreOutboxCommand`：

- **校验**（不合格报 `ValueError`，什么都不写）：`kind` 符合 `[a-z][a-z0-9_.]{0,39}` 且已登记了处理函数（4.5）；`method` 是 `POST` 或 `PATCH`；`path`、`actor`、`body` 用 3.2 的公开函数 `validate_request` 校验（`body` 可以为 `None`）；`ordering_key` 为 `None` 或 1–128 个 `[A-Za-z0-9._:-]` 字符（与 Core 引用同一字符集）。3.2 发出前的校验因此在入队时全做一遍，能入队的命令发送时不会因校验失败。
- **同一顺序键串行**：`ordering_key` 不为空时，先执行 `INSERT INTO core_outbox_ordering_locks (ordering_key) VALUES (:k) ON DUPLICATE KEY UPDATE ordering_key = ordering_key`（不用 MySQL 8.0.20 起弃用的 `VALUES()` 写法）。这一句对该行加**排他锁**（第一次插入时新行带排他锁；重复键时 `ON DUPLICATE KEY UPDATE` 也加排他锁，不会出现共享锁再升级），一直持有到调用方的事务结束。持锁者提交的情况下不会死锁；持锁者**回滚**且同时有两个以上等待者时，InnoDB 可能判其中一个死锁，按 4.4 整笔重试。同一顺序键的两个入队事务因此串行：后一个要等前一个提交或回滚之后才能插入命令行，所以它拿到的 `id` 一定更大、且前一个已经提交——**同一顺序键内 `id` 的先后就是提交的先后**，认领时不会因为看不到未提交的更小 `id` 而越过它。
- **不提交**：只把行加进调用方的事务（`db.add` 加 `flush` 拿到 `id`），由调用方和它的业务变化一起提交——这就是「先落库」。
- `idempotency_key = "inv:" + kind + ":" + secrets.token_hex(16)`，入队时生成、存进行里、之后永不改；最长 4 + 40 + 1 + 32 = 77 个字符。
- `status = queued`、`attempts = 0`、`handler_failures = 0`、`next_attempt_at = now`、`created_at = now`，其余可空列为空。
- **重试一条被拒的命令 = 入队一条新命令**（新键），并把旧的那条标为已处理（4.2 的 `resolve`）。本层不提供「把 rejected 改回 queued」。

### 4.2 能不能发、解除阻塞

一条 `queued` 命令**可发**，当且仅当：

1. `next_attempt_at <= 现在`；
2. 没有有效租约（`lease_until` 为空或 `<= 现在`）；
3. `ordering_key` 为空，或同一 `ordering_key` 下 `id` 更小的命令**全部**「不挡路」：`succeeded`、`cancelled`，或 `rejected`/`failed` 且 `resolved_at` 非空。

前面有未处理的 `rejected` 或 `failed`（`resolved_at` 为空）时，后面的命令一直不发（「被阻塞」），直到业务层处理完并调用：

`resolve(db, *, command_id, now) -> bool`：用**条件更新**——`UPDATE core_outbox_commands SET resolved_at = :now WHERE id = :id AND status IN ('rejected', 'failed') AND resolved_at IS NULL`——按影响行数返回 `True`/`False`。**不提交**。

**条件更新的写法**（`resolve`、`cancel`、`requeue` 与 4.3 的短事务都一样）：执行 SQLAlchemy Core 的 `update()` 语句，ORM 写法则必须加 `execution_options(synchronize_session=False)`，只看 `rowcount`。不能用默认的会话同步：它会拿会话里已加载的旧属性在 Python 里套 `WHERE`，数据库没更新、内存里的对象却被改了。需要最新值时用 `populate_existing()` 重新读。

业务层的标准做法（给 W4 的约定）：在**同一个事务**里，先 `cancel` 被阻塞的后续命令，再入队新的命令（新 `id` 更大、新幂等键），再 `resolve` 那条被拒的，一起提交。**任何一条 `cancel` 返回 `False`**（那条后续命令已被认领过、可能已发出），整个事务回滚、不 `resolve`，交给人处理。三步必须在同一个事务里：分开提交的话，`resolve` 生效之后、`cancel` 之前，被阻塞的旧后续命令可能先被发出。`failed` 命令同理，但它是 Inventory 的 bug，通常要先修好，再由运维 `requeue`（4.7）。

**业务先后不归本层管**（原则 5）：确认销售的请求体要带预留成功后才知道的 `reservation_id`，只能由预留的 `on_succeeded` 入队；在预留发出到成功之间入队的同订单命令（例如发货）会排在确认销售前面。W4 的事件管线必须按订单状态决定什么时候入队下一步（例如订单还没确认销售时，发货事件留在收件箱里等），这条写进 W4 的设计。

### 4.3 发一条（`process_one`）

`process_one(session_factory, settings, *, now_fn, transport=None) -> int | None`（返回处理过的命令 id，没有可发的返回 `None`）：

1. **先看配置与暂停**：`check_configured(settings, "business")` 为假，或本进程的**凭据暂停**还没到期（见下面 `auth_error` 一行），就**什么都不认领**，直接返回 `None`（`drain` 随之停止）。未配置或暂停期间没有任何命令被认领，也就没有命令的 `last_claimed_at` 因此被设上。凭据暂停是模块级变量 `_auth_paused_until`（每个进程各自一份，不进数据库），另有只给测试用的 `reset_auth_pause()`。
2. **认领**（短事务，`now = now_fn()`）：一条查询同时满足 4.2 的三个条件，被阻塞的命令根本不会被选中，不会占住名额：
   `SELECT c.* FROM core_outbox_commands c WHERE c.status = 'queued' AND c.next_attempt_at <= :now AND (c.lease_until IS NULL OR c.lease_until <= :now) AND NOT EXISTS (SELECT 1 FROM core_outbox_commands p WHERE p.ordering_key = c.ordering_key AND p.id < c.id AND NOT (p.status IN ('succeeded', 'cancelled') OR (p.status IN ('rejected', 'failed') AND p.resolved_at IS NOT NULL))) ORDER BY c.next_attempt_at, c.id LIMIT 1 FOR UPDATE OF c SKIP LOCKED`
   （`ordering_key` 为空时 `p.ordering_key = c.ordering_key` 不成立，子查询为空，自然可发）。选中就设 `lease_until = now + 60 秒`，`last_claimed_at` 为空时设为 `now`（同时在内存里记下「这次认领前它为空」），提交；没有就返回 `None`。
3. **发出**（事务外，不持有数据库连接）：调 3.2 的 `send`，用这条命令存着的 `method`、`path`、`body`、`idempotency_key`、`actor`，`request_id = "inv_cmd_<id>_<attempts+1>"`。
4. **记结果**（新事务，`now = now_fn()` **重新取**，发送可能花了十几秒）：用 `SELECT … FOR UPDATE` 加 `populate_existing()` 读这一行，确认仍是 `queued` 且 `lease_until` 是第 2 步设的值；不是就放弃（不改任何东西，`process_one` 返回这条命令的 id，`drain` 继续）。按分类：

| 分类 | `attempts` | `status` | 其他 |
| --- | --- | --- | --- |
| `not_configured`（第 1 步之后、发出之前令牌文件恰好被换掉） | 不变 | `queued` | `next_attempt_at = now + 60 秒`；`last_outcome`、`http_status`、`core_request_id`、`error_code` 都不改；清租约 |
| `retry` | +1 | `queued` | `next_attempt_at = now + max(退避, retry_after)` |
| `unknown` | +1 | `queued` | 同上；`first_unknown_at` 为空时设为 `now` |
| `auth_error` | +1 | `queued` | `next_attempt_at = now + 600 秒`；不调处理函数；**本进程暂停认领 600 秒**（`_auth_paused_until = now + 600 秒`），`drain` 随之停止，凭据出错时不会在几分钟内把整个积压各发一遍、刷满 Core 的安全日志 |
| `succeeded` | +1 | `succeeded` | `response_body`、`finished_at`；调处理函数的 `on_succeeded` |
| `rejected` | +1 | `rejected` | `response_body`、`finished_at`；调 `on_rejected` |
| `caller_error` | +1 | `failed` | `finished_at`；**不调**处理函数（不是业务结果） |

   - **恢复可取消**：分类是 `not_configured` 或 `auth_error`，并且「这次认领前 `last_claimed_at` 为空」时，把 `last_claimed_at` 改回空。这是安全的：`not_configured` 没有发出；401、403 在 Core 取锁之前被拒、不保存，确定没生效；租约核对已排除别的发送者。之前被认领过的（`last_claimed_at` 本来就有值）不改，因为更早那次可能已发出。
   - 除 `not_configured` 那一行外，每种情况都写 `last_outcome`、`http_status`、`core_request_id`、`error_code`（本次响应的错误码，没有错误码——成功、超时等——时写 `NULL`，所以成功后不会留下旧的 `REQUEST_IN_PROGRESS`），清掉 `lease_until`。`error_code` 超过 64 个字符、`core_request_id` 超过 128 个字符时截断后存。
   - 处理函数与状态更新在**同一个事务**里提交。处理函数抛异常，或要调处理函数时（`succeeded`、`rejected`）这条命令的 `kind` 已不在登记表里：回滚这个事务，再**另开一个短事务**，用条件更新 `… WHERE id = :id AND status = 'queued' AND lease_until = :认领时的租约值`：`handler_failures + 1`、`last_handler_error = 异常类名`（种类未登记时写 `UnregisteredKind`）、`next_attempt_at = now + 退避(handler_failures)`、清租约，其他不改；影响 0 行（租约已到期被别人接手）就什么都不改，只记日志；记进程日志（命令 id、`kind`、异常类名）；`process_one` 不抛出，返回这条命令的 id。到时间后会同键重发，Core 重放同一结果，处理函数再执行一次，所以**处理函数必须能在「首次执行失败回滚」后安全地再执行**。`handler_failures > 0` 且仍 `queued` 的命令由 W8 告警（它们的 `attempts`、`first_unknown_at` 不会变，靠这一列才看得见）。
   - `send` 抛出任何异常（3.2 归类之外的，只可能是编程错误）：记 `status = failed`、`attempts` +1（可能已发出）、`last_outcome = caller_error`、`error_code = CLIENT_EXCEPTION`、`finished_at`；`http_status`、`core_request_id` 不改；不调处理函数，清租约；记进程日志（异常类名）。它可能已发出，所以修好之后只能 `requeue`（4.7）同键重发，不能当成没发过。
   - 记结果的事务（或上面那个短事务）因为别的原因失败（数据库错误等）：回滚，记进程日志（命令 id 与异常类名），不抛出，返回这条命令的 id；租约到期后会同键重发。
   - `retry_after` 为 `None` 时按 0 算。
   - 第 4 步重新取的 `now_fn()` 不合格（带时区或小数秒，编程错误）：按「记结果的事务失败」处理——不写任何东西，记进程日志，返回这条命令的 id；租约到期后同键重发。第 2 步取的时间不合格则直接报 `ValueError`（什么都还没认领）。
5. **退避**：`退避(n) = min(5 × 2^(n−1), 600)` 秒（`n` 是加一之后的 `attempts` 或 `handler_failures`）：5、10、20、40……封顶 10 分钟。没有随机抖动（单进程、单发送者，不需要；也让测试确定）。
6. 一直重试，不设上限：可能已发出的命令永远不能自己放弃（原则 2）。积压多久要告警由 W8 根据 `first_unknown_at`、`attempts`、`handler_failures`、`last_outcome = auth_error` 以及「仍 `queued` 且 `created_at` 早于某个时间」判断。

`drain(session_factory, settings, *, now_fn, max_commands=50, max_seconds=120, stop_event=None, transport=None) -> int`：循环 `process_one`（把 `now_fn`、`transport` 原样传下去），直到它返回 `None`、处理满 `max_commands` 条、从开始算起超过 `max_seconds` 秒（按 `now_fn()` 算，在每条之间检查），或 `stop_event` 被设置；返回处理的条数。供调度器调用（第 5 节）。

### 4.4 租约与并发

- 部署换容器时新旧进程可能短暂同时运行；认领用 `FOR UPDATE … SKIP LOCKED` 加租约，同一条命令同时只有一个发送者。
- 租约 60 秒，比一次请求的超时（15 秒）长得多。发送者在发出后崩溃：租约到期后别的发送者同键重发，Core 重放或执行一次（原则 2）。
- 记结果时租约值对不上（已被别人接手）就放弃：Core 那边同键，最终只有一个结果生效。
- **加锁顺序**：业务事务先锁自己的业务行（如订单行），再碰发件箱（`enqueue` 的顺序锁、`cancel`、`resolve`）；处理函数在记结果的事务里会先持有命令行的锁、再锁业务行——两者方向相反，InnoDB 可能判死锁、牺牲其中一个。牺牲记结果一方时，算作处理函数失败（4.3 第 4 步：`handler_failures` 加一、退避后同键重发，W8 看得见）；牺牲业务一方时，调用 `enqueue`、`cancel`、`resolve` 的业务代码要对死锁（MySQL 错误 1213）整笔重试。处理函数里调用的 `enqueue` 碰上顺序锁的死锁（4.1）也走前一种。处理函数里**不对别的命令行加锁**，也不调 `cancel`、`resolve`。

### 4.5 命令种类与处理函数

```
register_kind(kind, *, on_succeeded, on_rejected)
unregister_kind(kind)
```

- 在模块级登记表里登记；同名重复登记报 `ValueError`。业务模块（W3、W4）在 import 时登记自己的种类。`unregister_kind` 只给测试用（不存在时什么都不做）：测试在 `setUp`/`tearDown` 里登记、移除自己的种类。
- `on_succeeded(db, command)`、`on_rejected(db, command)`：在 4.3 第 4 步的事务里调用，可以改 Inventory 的业务表，也可以 `enqueue` 后续命令（例如预留成功后入队确认销售），**不提交**（由发件箱提交）、**不调 Core**。`command.response_body` 里是 Core 的响应体。
- 处理函数要容忍响应体里没有 `/v1` 后来新增的字段：Core 重放旧幂等记录时不会补上它们（§7.1）。
- 本层的测试用假的种类（例如 `test.echo`）验证调用时机与事务边界，不登记任何真实种类。
- **给 W3、W4 的接口提示**：V1 设计 7.3 要求一个 SKU 盘点前它的命令发件箱已清空。发件箱本身不按 SKU 索引（一条命令可以涉及多个 SKU，请求体是 JSON），W3、W4 要在自己的业务表里记下「哪条命令涉及哪些 SKU」（例如订单行到命令的对照），按那张表判断，不去扫请求体。

### 4.6 取消

`cancel(db, *, command_id, now) -> bool`：用**条件更新**——`UPDATE core_outbox_commands SET status = 'cancelled', finished_at = :now WHERE id = :id AND status = 'queued' AND last_claimed_at IS NULL`——按影响行数返回 `True`/`False`。**不提交**（与业务层的其他变化一起提交）。

- 只有**从未被认领过**的命令可取消。命令一旦被认领就可能已经发出（发出后进程崩溃、记结果的事务回滚、得到未知），这时它可能已在 Core 生效，必须先同键重发拿到确定结果。`last_claimed_at` 在认领事务里设上、之后不清，所以不论后来发生什么都认得出「可能发出过」；未配置时根本不认领（4.3 第 1 步），所以积压在未配置期间的命令仍可取消。
- 用条件更新、不先读再写：业务事务早先可能已把这条命令读进 SQLAlchemy 的会话，读到的旧值（`last_claimed_at` 仍为空）不能作为判断依据。条件更新以数据库里的当前行为准；行正被认领事务锁着时，更新会等它提交，再按提交后的值判断。

### 4.7 运维恢复

`requeue(db, *, command_id, now) -> bool`：条件更新 `UPDATE … SET status = 'queued', next_attempt_at = :now, finished_at = NULL WHERE id = :id AND status = 'failed' AND resolved_at IS NULL`，按影响行数返回。**幂等键、请求体都不变**：修好后同键重发是安全的（原则 2）。
- 已 `resolve` 的命令不能 `requeue`：`resolve` 是业务层「这条已处理完、已被替代」的承诺，放回去会用旧键再发、打乱顺序，还会重新挡住后面的命令。
- `rejected` 不能 `requeue`（确定的业务拒绝同键只会重放，§7.1）。
- `requeue` 只对**不在请求内容里的错误**有用：`CLIENT_EXCEPTION`、3xx、构造请求头之类的客户端代码或配置问题。请求体或路径本身错了（400、413、422 `INVALID_REQUEST`、409 的各类冲突），同键重发只会得到同样的结果（Core 已保存的冲突也只会重放）——这种要走 `resolve` 加入队新命令。不提交。V1 由运维在服务器上调用（之后 W8 的待处理页面可以加按钮）。

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| O1 | 空 | 在一个事务里改业务表并 `enqueue`，然后回滚 | 发件箱里没有这条（入队不自己提交） |
| O2 | 订单 A 有命令 1（queued）、2（queued） | `process_one` | 发 1，不发 2；1 成功后再 `process_one` 才发 2 |
| O3 | 订单 A 的命令 1 `rejected`，命令 2 `queued` | `process_one` | 2 不发（被阻塞）；订单 B 的命令照常发 |
| O3b | 接 O3：同一事务里 `cancel(2)`、入队新命令 3、`resolve(1)` 并提交 | `process_one` | 发 3（1 已处理、2 已取消，都不挡路）；`resolve` 对 `queued` 或已 `resolve` 过的命令返回 `False` |
| O4 | 命令得到 C6 的读超时 | 记结果 | `queued`、`attempts = 1`、`next_attempt_at = 记结果时的 now + 5 秒`、`first_unknown_at` 为记结果时的 now；第二次再超时，`+10 秒` |
| O5 | O4 之后 Core 恢复，同键重发得到 C2（重放 201） | 记结果 | `succeeded`，`on_succeeded` 只被调用这一次 |
| O6 | 得到 C4（`Retry-After: 1`），`attempts` 加一后为 4 | 记结果 | `next_attempt_at = now + 40 秒`（退避 40 大于 1） |
| O7 | 得到 C3 | 记结果 | `rejected`，`on_rejected` 被调用，`error_code = INSUFFICIENT_STOCK` |
| O8 | 得到 C7 | 记结果 | `failed`，两个处理函数都没被调用；同订单后面的命令被阻塞；`requeue` 之后同键再发 |
| O8b | 一条从未被认领过的命令得到 C7b（401） | 记结果 | 仍 `queued`，`last_outcome = auth_error`，`next_attempt_at = now + 600 秒`，`last_claimed_at` 改回空（之后可以 `cancel`）；本进程 600 秒内 `process_one` 都返回 `None`；同订单后面的命令照样被它挡着（它没完成） |
| O8c | O8 的命令已被业务层 `resolve` | `requeue` | 返回 `False`、不改；未 `resolve` 的 `failed` 命令返回 `True`，`resolved_at` 仍为空 |
| O9 | 未配置令牌文件，有一条 `queued` 命令 | `process_one` | 返回 `None`，没发请求；命令的 `attempts`、`next_attempt_at`、`last_claimed_at` 都不变（可以取消） |
| O10 | `on_succeeded` 抛 `RuntimeError` | 记结果 | 事务回滚：命令仍 `queued`、业务表没变；另记 `handler_failures = 1`、`last_handler_error = RuntimeError`、`next_attempt_at = now + 5 秒`；`process_one` 返回 id、不抛出 |
| O10b | 同 O10，但短事务执行前这条命令的租约已到期、被另一个发送者认领（租约值变了） | 短事务 | 影响 0 行，`handler_failures` 不变，对方的租约不被清掉 |
| O11 | 命令 1 正被发送者甲租着 | 发送者乙 `process_one` | 乙认领不到 1（`SKIP LOCKED` 或租约未到期） |
| O12 | 三条 `queued` 命令：甲得到过未知（`attempts = 1`）；乙被认领后记结果回滚（O10，`attempts = 0`，`last_claimed_at` 有值）；丙从未被认领 | `cancel` | 甲、乙返回 `False`、不改；丙返回 `True` |
| O12b | 业务会话先读出命令丙（`last_claimed_at` 为空），随后另一个会话认领了丙并提交 | 业务会话 `cancel(丙)` | 返回 `False`、不改（条件更新以库里当前值为准） |
| O13 | 订单 A 的命令 1 `rejected`，命令 2～25 `queued`（被阻塞，`next_attempt_at` 都更早）；订单 B 的命令 26 `queued` | `process_one` | 发 26（被阻塞的命令不进认领结果） |
| O14 | 某条命令的种类在记结果前被移出登记表（测试里模拟） | 记结果（`succeeded`） | 回滚，命令仍 `queued`，`handler_failures = 1`、`last_handler_error = UnregisteredKind`；`process_one` 返回 id、不抛异常 |
| O15 | 两个事务并发给同一顺序键入队：甲先拿到顺序锁、插入后暂不提交；乙随后入队 | 乙 | 乙在顺序锁上等到甲提交才插入，乙的 `id` 大于甲的；甲回滚时乙照常插入 |
| O16 | `enqueue` 的 `body` 含 `1.5`、或 `ordering_key` 为 129 个字符、或 `now` 带小数秒 | `enqueue` | `ValueError`，什么都没写 |

## 5. 进程内调度器（INVENTORY-TASK-008）

`app/scheduler.py`。

### 5.1 作业

```
register_job(name, *, interval_seconds, run)
unregister_job(name)
run_once(session_factory, settings, *, owner, now_fn, jobs=None, stop_event=None)
```

- `run(session_factory, settings, now_fn, stop_event)`：同步函数；`interval_seconds` 是两次开始之间的最短间隔。长作业要在 `stop_event` 被设置时尽快返回。
- 同名重复登记报 `ValueError`；`unregister_job` 只给测试用（不存在时什么都不做）。
- `core_outbox.drain`（每 5 秒，调 4.3 的 `drain`，`max_commands = 50`、`max_seconds = 120`，把 `stop_event` 传下去）在 `app/scheduler.py` 模块底部登记，import 即登记。之后的工作包在各自模块里登记自己的作业（补拉、对账、清理过期登录尝试等），由 `app/scheduler.py` 统一 import。
- `run_once` 跑一轮：`jobs` 为 `None` 时跑全部已登记的作业；为列表时只跑列表里的（空列表即什么都不跑）；列表里有没登记的名字报 `ValueError`。**每处理一个作业都重新调一次 `now_fn()`** 作为这个作业的「现在」。测试用它指定 `owner`（模拟两个调度器）、时间与只跑某个作业。线程每次醒来就调一次 `run_once`。

### 5.2 运行

- 一个后台线程（`threading.Thread`，`daemon=True`），每 2 秒醒一次，调 `run_once`。对每个作业：
  1. 确保 `scheduled_jobs` 里有它的行（没有就插入，`next_run_at = now`；撞主键就忽略）；
  2. **抢租约**（单独的短事务，**提交后**才执行作业，这样持有者崩溃时别人看得到租约、到期可以接手）：`UPDATE scheduled_jobs SET lease_owner = :me, lease_until = :now + 300 秒, last_started_at = :now WHERE name = :name AND next_run_at <= :now AND (lease_until IS NULL OR lease_until <= :now)`，影响行数为 1 才算抢到；
  3. 抢到就执行 `run`；结束后（不论成败）`UPDATE … SET next_run_at = :now + interval, lease_owner = NULL, lease_until = NULL, last_finished_at = :now_fn(), last_status, last_error WHERE name = :name AND lease_owner = :me`（`:now` 是第 2 步用的值）。成功时 `last_status = ok`、`last_error = NULL`；
  4. `run` 抛异常：`last_status = error`、`last_error = 异常类名`，写一条进程日志（只有作业名与异常类名），继续下一个作业；
  5. 第 1–3 步本身的数据库错误（插行、抢租约、记结果失败）：这一个作业本轮放弃，记进程日志（作业名与异常类名），接着处理下一个作业；`run_once` 不抛出。没能记结果的租约 300 秒后到期，由下一轮接手。
- `me` = 主机名加进程号加启动时的随机串，截到 64 个字符。
- **接线**：`create_app` 的 lifespan 里，`session_factory` 用 INVENTORY-TASK-004 建好的 `app.state.sessionmaker`（ADMIN_AUTH_DESIGN 第 5.1 节），`now_fn` 用 `utc_now`（ADMIN_AUTH_DESIGN 第 3 节），不另建一套。
- **只在 `settings.scheduler_enabled` 为真时启动**（`INVENTORY_SCHEDULER_ENABLED`，默认假）：测试与命令行不会意外启动后台线程。生产在第 6 节的上线步骤里打开。未配置数据库时不启动（记一条进程日志）。
- **停止**：lifespan 结束时设停止事件，最多等线程 8 秒（`docker stop` 默认 10 秒后强制结束容器）。作业通过 `stop_event` 尽快返回；没来得及结束的发送由租约与同键重发兜底（4.4），不会重复生效。
- 作业在同一个线程里依次执行：Core 不可用时一次 `drain` 最长跑到 `max_seconds`（120 秒）加最后一次请求的超时，期间其他作业要等。之后登记的作业必须能容忍这样的延迟（在各自的设计里说明）。

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| J1 | 作业 `x` 间隔 10 秒，`next_run_at = 10:00:00` | 10:00:01 跑一轮 | 执行 `x`；`next_run_at = 10:00:11`，`last_status = ok`、`last_error` 为空 |
| J2 | 同 J1 | 两个调度器（不同 `owner`）同时在 10:00:01 抢租约 | 只有一个执行 |
| J3 | `x` 抛 `RuntimeError("secret")` | 跑一轮 | `last_status = error`、`last_error = RuntimeError`（不含 `secret`）；下一轮照常 |
| J4 | 持有者崩溃，`lease_until = 10:05:01` | 10:05:02 另一个调度器 | 能抢到并执行 |
| J5 | `scheduler_enabled` 为假 | `create_app` | 没有后台线程 |
| J6 | `run_once(…, jobs=["nope"])` | — | `ValueError` |

### 5.3 `/readyz`

不变：只看数据库。调度器的状态（各作业的 `last_status`、`last_finished_at`）由 W8 的运维页面与告警读 `scheduled_jobs` 表，不进健康检查（以免一个作业失败让部署判定失败）。

## 6. 上线步骤（运营者做，不是 Worker 任务）

Core 生产实例就绪、签发了凭据之后（Core 契约 §10 第 2 条：一个业务凭据、一个给 Inventory 对账的运维凭据）：

1. 令牌放到服务器上 Inventory 应用目录旁的 secrets 文件（权限 600，属主为容器用户 10001），不进仓库、不进 CI。
2. 改 `docker-compose.yml`：把两个令牌文件作为 Docker secrets 挂进 `inventory_api`；加 Core 生产实例的内部网络；环境变量加 `INVENTORY_CORE_BASE_URL`、两个 `*_TOKEN_FILE`、`INVENTORY_SCHEDULER_ENABLED=true`；同步 `.env.example` 的示例值。这是一次部署配置变更，单独开 PR，由 Kelvin 合并。
3. 部署后核对：`scheduled_jobs` 里 `core_outbox.drain` 的 `last_status = ok`；用运维凭据读一次 `GET /v1/ops/invariants` 成功。

在那之前：发件箱里的命令停在 `queued`、从未被认领，不会丢；调度器关着时没有任何东西在发。

## 7. 不做、偏离与残余风险（要 Kelvin 确认）

| 项 | 决定 | 理由 / 代价 |
| --- | --- | --- |
| 结果未知时先按业务引用查询 | **不先查**，直接同键重发。这**收窄了 V1 设计第 15 节 W1**（「结果不明时查询」）与 Core 契约 §8.3 的建议做法（「先按业务引用查询，再同键重试」） | Core 把幂等记录与命令写入同事务提交（§7.1），同键重发本身就给出确定结果；先查要为每个命令种类写一套查询，收益只是排查信息。查询留给 W8 对账 |
| 重试上限 | 不设 | 可能已发出的命令放弃就可能少报或错报；靠 W8 告警 |
| HTTP 库 | 标准库 `urllib` | 不加依赖（Worker 不能装依赖，沙箱也要能跑单元测试）；同步阻塞在后台线程里，不影响请求处理 |
| 换 Core 凭据（不是轮换令牌） | **V1 不换凭据，只轮换令牌**（`credential_id` 不变，幂等作用域不变，§10 第 4 条） | 换凭据会改变幂等作用域，可能已发出的命令换凭据重发可能生效两次。要换时，先要一个「只发已认领过的命令」的开关，把它们补交完——这个开关与步骤留给 W8 |
| 401、403 不消耗命令 | 同键、600 秒后重发，期间本进程暂停认领，W8 告警 | Core 取锁之前就拒了、不保存（§7.1），同键重发安全；暂停让凭据出错时每 10 分钟只有一条请求；代价是凭据出错期间所有命令都卡在队里，等人修好。暂停只在进程内存里，换容器后新进程会先试一次 |
| 单进程发送 | 是 | V1 只有一个 API 容器；租约与 `SKIP LOCKED` 保证换容器时不会重复发送 |
| 调度器租约 300 秒 | 是 | `drain` 有 `max_seconds = 120` 上限，正常远小于 300 秒；万一超过，两个 `drain` 重叠也是安全的：每条命令由发件箱自己的租约与 `SKIP LOCKED` 保证只被一个发送者处理（4.4）。之后登记的作业必须同样能容忍重叠执行，做不到的自己加锁或分批 |
| 业务先后 | 本层只保证同一顺序键内的入队顺序 | 见原则 5 与 4.2；W4 按订单状态决定何时入队下一步 |

## 8. 修订记录

- **0.1**（2026-11-23）：草案。同日经四轮设计预审与一轮独立评审修订，主要改动：
  - 发件箱的顺序：认领用一条查询排除被阻塞的命令（原写法会被阻塞命令占满而卡住）；同一顺序键的入队用顺序锁表串行，保证 `id` 先后就是提交先后；原则 5 收窄为「入队顺序」，业务先后交给 W4。
  - 阻塞与恢复：被拒或失败的命令可由业务层 `resolve`；`failed` 可由运维同键 `requeue`；401、403 不消耗命令，长退避重发。
  - 取消：只允许从未被认领的命令（只写一次的 `last_claimed_at`），未配置时不认领；`cancel`、`resolve` 用条件更新，不依赖会话里的旧值。
  - 处理函数失败另记 `handler_failures` 并退避，W8 看得见。
  - 客户端：502 按 Core 契约归「未知」；地址用 `urlsplit` 校验；令牌格式先校验再用；请求体拒绝浮点数、超长数组与超过 128 KiB；路径字符白名单；不用环境代理、不跟随重定向；不 `import ssl`；超时可覆盖；`request_id`、`replayed` 的取法；正文含 `NaN` 按非法 JSON。
  - 记结果时重新取时间；`now` 必须是不带时区的整秒；`error_code` 每次覆盖、超长截断。
  - 调度器：`run_once` 入口与每作业取时间；租约先提交再执行；停止事件与 8 秒等待；`drain` 的总时长上限；复用 `app.state.sessionmaker` 与 `utc_now`。
  - 三张表都用 `utf8mb4_bin`；JSON 空值存 SQL `NULL`。
  - 第五轮预审与独立评审第 2 轮：`requeue` 不碰已 `resolve` 的命令并写明适用范围；处理函数失败的短事务改为核对租约的条件更新；`auth_error` 让本进程暂停认领 600 秒，首次发送就得到 401、403 或未配置的命令恢复可取消；顺序锁改用不弃用的写法、收窄「不会死锁」的说法；条件更新不用 ORM 会话同步；地址先查可见 ASCII；公开 `validate_request`，`send` 只读一次令牌；`encode_body` 只接受六种类型与字符串键；`User-Agent` 版本的格式；响应里类型不对的字段与重复、空的响应头；记结果时 `now_fn` 不合格的处理；默认值在模型上两种都写。

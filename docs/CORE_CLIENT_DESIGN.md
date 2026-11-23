# Core 客户端、命令发件箱与进程内调度（V1 设计补充，W1）

> 版本：0.1 · 日期：2026-11-23 · 状态：**草案，待 Kelvin 批准**。批准之前，INVENTORY-TASK-005～008 不得开始写代码（CLAUDE.md：幂等、状态机走设计闸门）。
> 依据：[V1_MINIMAL_DESIGN](V1_MINIMAL_DESIGN.md) 0.3 第 2 节（进程内调度加作业表，不用 Redis、Celery）、第 3.2 节（引用与操作者头）、第 3.3 节（接口与两个凭据）、第 3.5 节（发件箱、结果不明与 Core 不可用）；
> `acuven_stock_core/docs/CONTRACT_V1.md` 0.21（下称「Core 契约」，`§` 指它的章节）：§7.1 两层去重、§8.1 协议约定、§8.3 错误码、§8.4 兼容规则、§10 鉴权与凭据。
> 本文只做通用的地基：怎么把一个写命令可靠地送到 Core、怎么读 Core、怎么定时做事。**具体哪个业务动作发什么命令**（预留、确认销售、收货……）由之后的工作包（W3、W4）登记成「命令种类」并写各自的处理函数，本文只定它们要遵守的接口。
> 与 V1_MINIMAL_DESIGN 冲突时以它为准；设计有改动时，改动部分的批准作废、须重新批准。

分四个任务实现：

| 任务 | 层 | 本文章节 |
| --- | --- | --- |
| INVENTORY-TASK-005 | 数据：发件箱与作业表、迁移 0002 | 第 2 节 |
| INVENTORY-TASK-006 | Core HTTP 客户端与响应分类（不碰数据库） | 第 3 节 |
| INVENTORY-TASK-007 | 发件箱服务：入队、顺序、发送、重试、结果回调 | 第 4 节 |
| INVENTORY-TASK-008 | 进程内调度器与应用启动接线 | 第 5 节 |

部署侧（Core 生产实例的内部网络、两个令牌文件、打开调度器的环境变量）不是 Worker 任务，在第 6 节列为上线步骤。

---

## 1. 原则

1. **先落库，再发出。**每个 Core 写命令先在 Inventory 库里写一行发件箱记录（和触发它的业务变化同一个事务），带一个**入队时就定死**的幂等键，然后才由后台发出。进程崩溃、Core 不可用都不会丢命令，也不会换键重发。
2. **结果不明就同键重发，不猜。**超时、连接中断、500 与其他 5xx 的结果都是「未知」（§8.3 最后两行）。Core 把幂等记录与命令写入放在同一个事务提交（§7.1），所以同键重发要么原样重放已生效的结果，要么第一次执行，不会生效两次。**绝不把未知当成功或失败**，也不在 Inventory 一侧补造数量（V1 设计 3.5）。
3. **业务拒绝是确定结果。**取锁之后的 4xx（缺货、SKU 停用等）已被 Core 保存，同键只会重放（§7.1）；交给登记该命令种类的业务处理函数决定下一步（例如进待处理）。要再试只能由业务层**入队一条新命令**，带新幂等键（§7.1 最后一条）。
4. **调用方错误要停下来。**400、401、403、405、413、幂等或业务引用冲突说明 Inventory 自己有 bug 或凭据有问题：不重试，这条命令停在 `failed`，同一订单后面的命令也不再发出，等人处理（告警由 W8 做）。
5. **同一订单的命令按顺序发。**先预留、后确认销售、再发货：同一个「顺序键」（订单引用）下，前一条没有成功，后一条就不发。

## 2. 数据模型（INVENTORY-TASK-005）

沿用 `app/db.py` 的 `Base`、命名约定与 `hide_parameters`；时间一律是不带时区、精确到整秒的 UTC（同 [ADMIN_AUTH_DESIGN](ADMIN_AUTH_DESIGN.md) 第 3 节，取当前时间用其中的 `utc_now()`）。

**`core_outbox_commands`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BigInteger | 主键，自增；也是同一顺序键内的先后 |
| `kind` | String(40) | 非空；命令种类名（如之后的 `reservation.create`），由登记它的业务模块定，本层不限值 |
| `ordering_key` | String(128) | 可空；同一键内按 `id` 顺序发送；不要求顺序的命令为空 |
| `method` | String(6) | 非空；检查 `ck_core_outbox_commands_method_known`：`method IN ('POST', 'PATCH')` |
| `path` | String(255) | 非空；Core 的路径，以 `/v1/` 开头（检查 `ck_core_outbox_commands_path_v1`：`path LIKE '/v1/%'`） |
| `body` | JSON | 可空（没有请求体的命令，如 `late-payment-review/close`）；没有请求体存 SQL `NULL`，不存 JSON 的 `null`（列用 `JSON(none_as_null=True)`，`response_body` 同） |
| `actor_ref` | String(128) | 可空 |
| `actor_role` | String(20) | 可空；检查 `ck_core_outbox_commands_actor_pair`：`(actor_ref IS NULL) = (actor_role IS NULL)`（§8.1 两个头要么都带、要么都不带） |
| `idempotency_key` | String(128) | 非空；唯一 `uq_core_outbox_commands_idempotency_key` |
| `status` | String(12) | 非空；检查 `ck_core_outbox_commands_status_known`：`status IN ('queued', 'succeeded', 'rejected', 'failed', 'cancelled')` |
| `attempts` | Integer | 非空，默认 0（模型默认值与迁移的 `server_default='0'` 都写）；检查 `ck_core_outbox_commands_attempts_nonnegative`：`attempts >= 0` |
| `next_attempt_at` | DateTime | 非空；`queued` 时最早什么时候再发 |
| `lease_until` | DateTime | 可空；有发送者正在处理时非空（第 4.3 节） |
| `last_outcome` | String(16) | 可空；最近一次发送的分类（第 3.3 节的六类之一）或 `not_configured` |
| `first_unknown_at` | DateTime | 可空；第一次得到「未知」的时间，成功或确定结果后保留不清（供 W8 判断积压与告警） |
| `http_status` | SmallInteger | 可空；最近一次得到的 HTTP 状态码 |
| `error_code` | String(64) | 可空；最近一次 Core 错误体里的 `code` |
| `response_body` | JSON | 可空；**最终**结果（成功或确定的拒绝）的响应体，供业务处理函数与排查；未知的中间响应不存 |
| `core_request_id` | String(128) | 可空；最近一次响应的 `X-Request-ID` |
| `created_at` | DateTime | 非空 |
| `finished_at` | DateTime | 可空；进入 `succeeded`、`rejected`、`failed`、`cancelled` 的时间 |

索引：`ix_core_outbox_commands_status_next_attempt_at`（`status`、`next_attempt_at`），`ix_core_outbox_commands_ordering_key_id`（`ordering_key`、`id`）。

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

- 迁移：`alembic/versions/20261123_0002_core_outbox_and_jobs.py`，revision `"0002"`，`down_revision = "0001"`；upgrade 建两张表并逐个写出约束与索引名，downgrade 删两张表。
- 模型：`app/models/core_outbox.py`（`CoreOutboxCommand`）、`app/models/scheduled_job.py`（`ScheduledJob`），`app/models/__init__.py` 导出。
- 集成测试照 INVENTORY-TASK-001 的做法：用 alembic 的 upgrade 按迁移建表（检查约束只有对迁移建出的表测才算数），跑完例子后 downgrade 到 base；没有测试库时跳过。
- 发件箱表**不存个人资料**：命令体里只有 Core 允许的引用、SKU ID、件数与时间（Core 契约 §10 第 6 条），`actor_ref` 是管理员的不透明账号 ID。

| 编号 | 操作 | 结果 |
| --- | --- | --- |
| D1 | 插入两条 `idempotency_key` 相同的命令 | 第二条被 `uq_core_outbox_commands_idempotency_key` 拒绝 |
| D2 | 插入 `actor_ref` 有值、`actor_role` 为空的命令 | 被 `ck_core_outbox_commands_actor_pair` 拒绝；两者都空、都有值都可以 |
| D3 | 插入 `status` 为 `sending`、`method` 为 `GET`、`path` 为 `/v2/x`、`attempts` 为 −1 的命令（各一条） | 分别被对应的检查约束拒绝 |
| D4 | 插入 `last_status` 为 `running` 的作业 | 被 `ck_scheduled_jobs_last_status_known` 拒绝 |

## 3. Core HTTP 客户端（INVENTORY-TASK-006）

### 3.1 配置

`app/config.py` 的 `Settings` 新增（环境变量前缀不变，都可以不设）：

| 字段 | 环境变量 | 含义 |
| --- | --- | --- |
| `core_base_url` | `INVENTORY_CORE_BASE_URL` | Core 的根地址（`http://` 或 `https://` 开头，不以 `/` 结尾）；空 = 未配置 |
| `core_token_file` | `INVENTORY_CORE_TOKEN_FILE` | 业务凭据令牌所在文件的路径（Docker secrets 文件）；空 = 未配置 |
| `core_ops_token_file` | `INVENTORY_CORE_OPS_TOKEN_FILE` | 运维凭据（每日对账那一个，Core 契约 §10 第 2 条）令牌文件；空 = 未配置 |

- 令牌**每次请求时读文件**（去掉首尾空白），不缓存：Core 轮换令牌时有最长 7 天的重叠期（§10 第 4 条），换文件即可生效，不用重启。文件不存在、不可读或为空，按「未配置」处理。
- 令牌只出现在 `Authorization` 头里；不进日志、异常消息、`repr`、数据库。
- 仓库里不写真实地址：示例值放 `.env.example`（本任务不改它，由第 6 节的上线步骤补）。

### 3.2 发请求

`app/core_client/http.py`，只用标准库（`urllib.request`、`json`），**不加依赖**：

```
send(settings, *, method, path, body=None, idempotency_key=None, actor=None, credential="business", request_id=None, transport=None) -> CoreResponse
```

- `credential` 是 `"business"` 或 `"ops"`，决定读哪个令牌文件；`transport` 是可替换的发送函数（测试注入假的，生产用 urllib），签名 `(method, url, headers, body_bytes, timeout) -> (status, headers, body_bytes)`，网络错误、超时以异常抛出。
- 请求头：
  - `Authorization: Bearer <令牌>`；
  - `Content-Type: application/json`（有请求体时）；请求体按 `json.dumps(body, ensure_ascii=False, separators=(",", ":"))` 编码为 UTF-8；
  - `Idempotency-Key`：写请求（POST、PATCH）必带，格式 1–128 个 0x21–0x7E 字符；
  - `X-Request-ID`：调用方给的，不给就生成 `inv_` 加 26 个字符的随机串（`[A-Za-z0-9]`）；格式 1–128 个 `[A-Za-z0-9._:-]`；
  - `X-Actor-Ref`、`X-Actor-Role`：`actor` 为 `Actor(ref, role)` 时两个都带，`role` 只能是 `stock_operator`、`stock_admin`、`catalog_admin`（§10 第 3 条），否则 `ValueError`；`actor` 为空时都不带；
  - `User-Agent: acuven-inventory/<settings.git_sha>`（§8.4 要求带名字与版本）。
- 超时：一个值 15 秒，传给 `urllib.request.urlopen` 的 `timeout`（标准库里它同时管连接与每次读，不分开设）；transport 的 `timeout` 参数就是这个数。不自动跟随重定向（3xx 归 `caller_error`，见下表）。
- 生产用的 transport 把 `urllib.error.HTTPError` 当作**正常响应**返回（取它的状态码、头与正文），只有拿不到 HTTP 响应时才抛异常。
- `send` 把这些异常归为 `unknown`：`urllib.error.URLError`、`TimeoutError`（含 `socket.timeout`）、`ConnectionError`、`ssl.SSLError`、`http.client.HTTPException` 及其他 `OSError`。别的异常（编程错误）原样抛出。
- 发出前的校验（不合格报 `ValueError`，是调用方 bug）：`method` 只能是 `GET`、`POST`、`PATCH`；`GET` 不能带幂等键或请求体，`POST`、`PATCH` 必须带幂等键；`path` 以 `/v1/` 开头；`X-Actor-Ref` 1–128 个 `[A-Za-z0-9._:-]` 字符。
- `core_base_url` 不以 `http://` 或 `https://` 开头、或以 `/` 结尾时，按「未配置」处理（`not_configured`），不发请求。
- 只返回分类后的结果，不抛业务异常；网络层异常也在这里分类。

### 3.3 响应分类

`CoreResponse` 是不可变数据类：`outcome`（下面六类之一）、`http_status`（拿不到为 `None`）、`data`（成功时响应体的 `data`）、`error_code`、`error_details`、`retry_after`（秒，整数，没有为 `None`）、`request_id`、`replayed`（响应头 `Idempotency-Replayed: true`）、`body`（解析后的整个响应体，拿不到为 `None`）。

| 分类 | 判定（按顺序，先命中先算） | 调用方该做的 |
| --- | --- | --- |
| `not_configured` | 没发出：`core_base_url` 为空或所需令牌文件读不到 | 不算一次尝试，过一会儿再看 |
| `unknown` | 超时、连接被拒或中断、TLS 错误；HTTP 500；502、503 以外的其他 5xx 与 504；2xx 但响应体不是合法 JSON 或没有 `data`；503 但错误码不是 `UNAVAILABLE` | **结果未知**：同键重发 |
| `retry` | 503 且 `code` 为 `UNAVAILABLE`（确定未生效）；502（反向代理未连上 Core，未生效）；409 `REQUEST_IN_PROGRESS`；429 `RATE_LIMITED` | 确定没生效或还在处理：同键重发，`Retry-After` 有就照它 |
| `succeeded` | 2xx 且响应体有 `data` | 完成 |
| `caller_error` | 400；401；403；405；413；409 的 `IDEMPOTENCY_CONFLICT`、`BUSINESS_REF_CONFLICT`、`ORDER_REFERENCE_CONFLICT`；422 `INVALID_REQUEST`；404 的 `NOT_FOUND`（路由不存在）；3xx | Inventory 的 bug 或凭据、配置问题：停下，等人 |
| `rejected` | 其余 4xx（含 404 的各种 `*_NOT_FOUND`、409 的业务码、422 的其他码，以及**认不出的错误码**——§8.4 要求当不可重试处理） | 确定的业务拒绝：交业务处理函数 |

- 502 归 `retry` 的理由：Core 不挂公网，经内部网络直连；502 只会来自中间的代理或网关，请求没到 Core。若将来部署形态变了（Core 前面有会转发后失败的代理），这一条要重新评估。
- 4xx 的响应体不是合法 JSON，或是 JSON 但没有 `error.code` 字符串（例如框架默认的 404 正文）：一律 `caller_error`——Core 的错误总带错误体，没有就说明请求没到 Core 的业务代码（路径写错、中间层拦截）。
- `Retry-After` 只认非负整数秒（全是数字），截到 0–600；HTTP 日期格式或其他写法当作没有（`None`）。

| 编号 | Core 的回答 | 分类 |
| --- | --- | --- |
| C0 | 404，正文 `{"detail": "Not Found"}`（没有 `error.code`） | `caller_error` |
| C1 | 201，`{"data": {...}, "request_id": "r1"}` | `succeeded`，`data` 原样 |
| C2 | 201，响应头 `Idempotency-Replayed: true` | `succeeded`，`replayed` 为真 |
| C3 | 409 `INSUFFICIENT_STOCK`，`details.lines` | `rejected`，`error_details` 带回 lines |
| C4 | 409 `REQUEST_IN_PROGRESS`，`Retry-After: 1` | `retry`，`retry_after` = 1 |
| C5 | 503 `UNAVAILABLE` | `retry` |
| C6 | 500 `INTERNAL_ERROR`；读超时；连接被拒 | 三个都是 `unknown` |
| C7 | 409 `IDEMPOTENCY_CONFLICT`；401 `UNAUTHORIZED` | 两个都是 `caller_error` |
| C8 | 409 `SOME_FUTURE_CODE`（客户端不认识） | `rejected` |
| C9 | 令牌文件不存在 | `not_configured`，没有发出任何请求（假 transport 没被调用） |
| C10 | 200 但响应体是 `<html>` | `unknown` |

### 3.4 读接口

同一个 `send`，`method="GET"`、不带幂等键；分类相同（`rejected` 对读接口一般是 404）。读失败的处理由调用方定：读不到 Core 的量时，**不能当作 0**（例如 W3 的库存总览显示「Core 暂不可用」）。

## 4. 发件箱服务（INVENTORY-TASK-007）

`app/services/core_outbox.py`。

### 4.1 入队

`enqueue(db, *, kind, method, path, body, ordering_key=None, actor=None, now) -> CoreOutboxCommand`：

- **不提交**：只把行加进调用方的事务（`db.add` 加 `flush` 拿到 `id`），由调用方和它的业务变化一起提交——这就是「先落库」。
- `idempotency_key = "inv:" + kind + ":" + 32 个十六进制字符的随机串`（`secrets.token_hex(16)`），入队时生成、存进行里、之后永不改；长度不超过 128（`kind` 最长 40）。
- `status = queued`、`attempts = 0`、`next_attempt_at = now`、`created_at = now`。
- 检查（不合格报 `ValueError`，什么都不写）：`kind` 符合 `[a-z][a-z0-9_.]{0,39}` 且已经登记了处理函数（4.5）；`method` 是 `POST` 或 `PATCH`；`path` 以 `/v1/` 开头且不超过 255 个字符；`actor` 的两个值都符合 3.2 的格式；`body` 能按 3.2 的方式编码成 JSON。也就是说 3.2 发出前的全部校验在入队时就做一遍，能入队的命令发送时不会因校验失败。
- **重试一条被拒的命令 = 入队一条新命令**（新键）。本层不提供「把 rejected 改回 queued」。

### 4.2 能不能发

一条 `queued` 命令**可发**，当且仅当：

1. `next_attempt_at <= 现在`；
2. 没有有效租约（`lease_until` 为空或 `<= 现在`）；
3. `ordering_key` 为空，或同一 `ordering_key` 下 `id` 更小的命令**全部**是 `succeeded` 或 `cancelled`。

前面有 `rejected` 或 `failed` 的命令时，后面的命令一直不发（「被阻塞」），直到业务层处理：把阻塞的那条之后的命令 `cancel`（4.6）或入队新的命令补上（新命令 `id` 更大，排在被阻塞的命令后面，所以业务层要先取消后面的再重排——这是业务层的事，W4 设计里定）。

### 4.3 发一条（`process_one`）

`process_one(session_factory, settings, *, now, transport=None) -> int | None`（返回处理的命令 id，没有可发的返回 `None`）：

1. **认领**（短事务）：一条查询同时满足 4.2 的三个条件，被阻塞的命令根本不会被选中，不会占住名额：
   `SELECT c.* FROM core_outbox_commands c WHERE c.status='queued' AND c.next_attempt_at <= :now AND (c.lease_until IS NULL OR c.lease_until <= :now) AND NOT EXISTS (SELECT 1 FROM core_outbox_commands p WHERE p.ordering_key = c.ordering_key AND p.id < c.id AND p.status NOT IN ('succeeded','cancelled')) ORDER BY c.next_attempt_at, c.id LIMIT 1 FOR UPDATE OF c SKIP LOCKED`
   （`ordering_key` 为空时 `p.ordering_key = c.ordering_key` 不成立，子查询为空，自然可发）。选中就设 `lease_until = now + 60 秒`，提交；没有就返回 `None`。
2. **发出**（事务外，不持有数据库连接）：调 3.2 的 `send`，用这条命令存着的 `method`、`path`、`body`、`idempotency_key`、`actor`，`request_id = "inv_cmd_<id>_<attempts+1>"`。
3. **记结果**（新事务，先 `SELECT … FOR UPDATE` 这一行，确认仍是 `queued` 且租约是自己设的那个值；不是就放弃，不改任何东西）：

| 分类 | `attempts` | `status` | 其他 |
| --- | --- | --- | --- |
| `not_configured` | 不变 | `queued` | `next_attempt_at = now + 60 秒`；`last_outcome = not_configured` |
| `retry` | +1 | `queued` | `next_attempt_at = now + max(退避, retry_after)` |
| `unknown` | +1 | `queued` | 同上；`first_unknown_at` 为空时设为 `now` |
| `succeeded` | +1 | `succeeded` | `response_body`、`finished_at`；调处理函数的 `on_succeeded` |
| `rejected` | +1 | `rejected` | `response_body`、`error_code`、`finished_at`；调 `on_rejected` |
| `caller_error` | +1 | `failed` | `error_code`、`finished_at`；**不调**处理函数（不是业务结果） |

   每种情况都写 `last_outcome`、`http_status`、`core_request_id`，清掉 `lease_until`。处理函数和状态更新在**同一个事务**里提交。
   - 处理函数抛异常，或这条命令的 `kind` 已不在登记表里：整个事务回滚，命令保持原样（`lease_until` 仍是认领时的值，到期后会再发——Core 同键重放同一结果，处理函数再执行一次，所以处理函数必须能在「首次执行失败回滚」后安全地再执行）。`process_one` 记一条进程日志（命令 id、`kind`、异常类名），**不再抛出**，返回这条命令的 id；`drain` 继续处理下一条。
   - `send` 抛出任何异常（3.2 归类之外的，只可能是编程错误）：记 `status = failed`、`last_outcome = caller_error`、`error_code = CLIENT_EXCEPTION`、`finished_at`，不调处理函数，清租约；记进程日志（异常类名）。
   - `retry_after` 为 `None` 时按 0 算。
4. **退避**：`退避 = min(5 × 2^(attempts−1), 600)` 秒（`attempts` 是加一之后的值）：5、10、20、40……封顶 10 分钟。没有随机抖动（单进程、单发送者，不需要；也让测试确定）。
5. 一直重试，不设上限：结果未知的命令永远不能自己放弃（原则 2）。积压多久要告警由 W8 根据 `first_unknown_at`、`attempts` 判断。

`drain(session_factory, settings, *, now_fn, max_commands=50)`：循环 `process_one` 直到返回 `None` 或处理满 `max_commands` 条，供调度器调用（第 5 节）。每条用 `now_fn()` 取一次当前时间。

### 4.4 租约与并发

- 部署换容器时新旧进程可能短暂同时运行；认领用 `FOR UPDATE SKIP LOCKED` 加租约，同一条命令同时只有一个发送者。
- 租约 60 秒，比一次请求的超时（15 秒）长得多。发送者在发出后崩溃：租约到期后别的发送者同键重发，Core 重放或执行一次（原则 2）。
- 记结果时租约值对不上（已被别人接手）就放弃：Core 那边同键，最终只有一个结果生效。

### 4.5 命令种类与处理函数

```
register_kind(kind, *, on_succeeded, on_rejected)
```

- 在模块级登记表里登记；同名重复登记报 `ValueError`。业务模块（W3、W4）在 import 时登记自己的种类。
- 另有 `unregister_kind(kind)`（不存在时什么都不做），只给测试用：测试在 `setUp`/`tearDown` 里登记、移除自己的种类，避免重复登记。
- `on_succeeded(db, command)`、`on_rejected(db, command)`：在 4.3 第 3 步的事务里调用，可以改 Inventory 的业务表（例如 W4 里确认销售成功后 `pending −q`），**不提交**（由发件箱提交）。`command.response_body` 里是 Core 的响应体。
- 处理函数里**不调 Core**（需要接着发的命令就入队新的一条）。
- 本层的测试用假的种类（例如 `test.echo`）验证调用时机与事务边界，不登记任何真实种类。

### 4.6 取消

`cancel(db, *, command_id, now) -> bool`：只有**确定从未发出过**的命令可取消：`status = queued`、`attempts = 0` 且 `lease_until IS NULL`。满足就设 `status = cancelled`、`finished_at = now`，返回 `True`；否则返回 `False`、不改。**不提交**（与业务层的其他变化一起提交）。
- 为什么看 `lease_until`：认领时设的租约在认领事务里提交。发出后进程崩溃、或记结果的事务回滚（处理函数抛异常），`attempts` 仍是 0，但 `lease_until` 留着认领时的值，所以仍判为「可能已发出」，不能取消——它可能已在 Core 生效，必须先同键重发拿到确定结果。
- 只有 `not_configured`（没发出）会清掉租约而 `attempts` 仍为 0，这种命令可以取消。

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| O1 | 空 | 在一个事务里改业务表并 `enqueue`，然后回滚 | 发件箱里没有这条（入队不自己提交） |
| O2 | 订单 A 有命令 1（queued）、2（queued） | `process_one` | 发 1，不发 2；1 成功后再 `process_one` 才发 2 |
| O3 | 订单 A 的命令 1 `rejected`，命令 2 `queued` | `process_one` | 2 不发（被阻塞）；订单 B 的命令照常发 |
| O4 | 命令得到 C6 的读超时 | 记结果 | `queued`、`attempts = 1`、`next_attempt_at = now + 5 秒`、`first_unknown_at = now`；第二次再超时，`+10 秒` |
| O5 | O4 之后 Core 恢复，同键重发得到 C2（重放 201） | 记结果 | `succeeded`，`on_succeeded` 只被调用这一次 |
| O6 | 得到 C4（`Retry-After: 1`），`attempts` 加一后为 4 | 记结果 | `next_attempt_at = now + 40 秒`（退避 40 大于 1） |
| O7 | 得到 C3 | 记结果 | `rejected`，`on_rejected` 被调用，`error_code = INSUFFICIENT_STOCK` |
| O8 | 得到 C7 | 记结果 | `failed`，两个处理函数都没被调用；同订单后面的命令被阻塞 |
| O9 | 未配置令牌文件 | `process_one` | 没发请求；`attempts` 仍为 0，`next_attempt_at = now + 60 秒` |
| O10 | `on_succeeded` 抛异常 | 记结果 | 整个事务回滚：命令仍 `queued`、业务表没变；租约到期后重发 |
| O11 | 命令 1 正被发送者甲租着 | 发送者乙 `process_one` | 乙认领不到 1（`SKIP LOCKED` 或租约未到期） |
| O12 | 命令 `queued`，`attempts = 1`（得到过未知）；另一条 `attempts = 0` 但 O10 回滚后 `lease_until` 仍有值 | `cancel` | 两条都返回 `False`，不改；从未认领过的第三条返回 `True` |
| O13 | 订单 A 的命令 1 `rejected`，命令 2～25 `queued`（被阻塞，`next_attempt_at` 都更早）；订单 B 的命令 26 `queued` | `process_one` | 发 26（被阻塞的命令不进认领结果） |
| O14 | 某条命令的种类在记结果前被移出登记表（测试里模拟） | 记结果 | 回滚，命令仍 `queued`，`process_one` 返回 id、不抛异常 |

## 5. 进程内调度器（INVENTORY-TASK-008）

`app/scheduler.py`。

### 5.1 作业

```
register_job(name, *, interval_seconds, run)
```

- `run(session_factory, settings, now_fn)`：同步函数；`interval_seconds` 是两次开始之间的最短间隔。
- 同名重复登记报 `ValueError`；另有只给测试用的 `unregister_job(name)`。
- `core_outbox.drain`（每 5 秒，调 4.3 的 `drain`）在 `app/scheduler.py` 模块底部登记，import 即登记。之后的工作包在各自模块里登记自己的作业（补拉、对账、清理过期登录尝试等），由 `app/scheduler.py` 统一 import。
- 跑一轮的入口：`run_once(session_factory, settings, *, owner, now_fn, jobs=None)`，`jobs` 为作业名列表，为空表示全部已登记的作业；测试用它指定 `owner`（模拟两个调度器）、时间与只跑某个作业。线程每次醒来就调一次 `run_once`。

### 5.2 运行

- 一个后台线程（`threading.Thread`，`daemon=True`），每 2 秒醒一次：对每个已登记的作业，
  1. 确保 `scheduled_jobs` 里有它的行（没有就插入，`next_run_at = now`；撞主键就忽略）；
  2. **抢租约**：`UPDATE scheduled_jobs SET lease_owner=:me, lease_until=:now+300, last_started_at=:now WHERE name=:name AND next_run_at <= :now AND (lease_until IS NULL OR lease_until <= :now)`，影响行数为 1 才算抢到；
  3. 抢到就执行 `run`；结束后（不论成败）`UPDATE … SET next_run_at = :开始时间 + interval, lease_owner=NULL, lease_until=NULL, last_finished_at=:now_fn(), last_status, last_error WHERE name=:name AND lease_owner=:me`（开始时间是第 2 步用的 `now`）。
  4. `run` 抛异常：记 `last_status = error`、`last_error = 异常类名`，写一条进程日志（只有作业名与异常类名），线程继续。
  5. 第 1–3 步本身的数据库错误（插行、抢租约、记结果失败）：这一个作业本轮放弃，记进程日志（作业名与异常类名），接着处理下一个作业；`run_once` 不抛出。没能记结果的租约 300 秒后到期，由下一轮接手。
- `me` = 主机名加进程号加启动时的随机串，截到 64 个字符。
- 停止：`create_app` 用 FastAPI 的 lifespan 启动与停止线程；停止时设事件、等线程最多 20 秒。
- **只在 `settings.scheduler_enabled` 为真时启动**（`INVENTORY_SCHEDULER_ENABLED`，默认假）：测试与命令行不会意外启动后台线程。生产在第 6 节的上线步骤里打开。
- 未配置数据库时不启动（记一条进程日志）。

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| J1 | 作业 `x` 间隔 10 秒，`next_run_at = 10:00:00` | 10:00:01 跑一轮 | 执行 `x`；`next_run_at = 10:00:11`，`last_status = ok` |
| J2 | 同 J1 | 两个调度器同时在 10:00:01 抢租约 | 只有一个执行 |
| J3 | `x` 抛 `RuntimeError("secret")` | 跑一轮 | `last_status = error`、`last_error = RuntimeError`（不含 `secret`）；下一轮照常 |
| J4 | 持有者崩溃，`lease_until = 10:05:01` | 10:05:02 另一个调度器 | 能抢到并执行 |
| J5 | `scheduler_enabled` 为假 | `create_app` | 没有后台线程 |

### 5.3 `/readyz`

不变：只看数据库。调度器的状态（各作业的 `last_status`、`last_finished_at`）由 W8 的运维页面与告警读 `scheduled_jobs` 表，不进健康检查（以免一个作业失败让部署判定失败）。

## 6. 上线步骤（运营者做，不是 Worker 任务）

Core 生产实例就绪、签发了凭据之后（Core 契约 §10 第 2 条：一个业务凭据、一个给 Inventory 对账的运维凭据）：

1. 令牌放到服务器上 Inventory 应用目录旁的 secrets 文件（权限 600，属主为容器用户 10001），不进仓库、不进 CI。
2. 改 `docker-compose.yml`：把两个令牌文件作为 Docker secrets 挂进 `inventory_api`；加 Core 生产实例的内部网络；环境变量加 `INVENTORY_CORE_BASE_URL`、两个 `*_TOKEN_FILE`、`INVENTORY_SCHEDULER_ENABLED=true`；同步 `.env.example` 的示例值。这是一次部署配置变更，单独开 PR，由 Kelvin 合并。
3. 部署后核对：`scheduled_jobs` 里 `core_outbox.drain` 的 `last_status = ok`；用运维凭据读一次 `GET /v1/ops/invariants` 成功。

在那之前：发件箱里的命令停在 `queued`、`last_outcome = not_configured`，不会丢；调度器关着时没有任何东西在发。

## 7. 不做与残余风险（要 Kelvin 确认）

| 项 | 决定 | 理由 / 代价 |
| --- | --- | --- |
| 结果未知时先按业务引用查询 | **不先查**，直接同键重发 | Core 把幂等记录与命令写入同事务提交（§7.1），同键重发本身就给出确定结果；先查要为每个命令种类写一套查询，收益只是排查信息。查询留给 W8 对账 |
| 重试上限 | 不设 | 未知的命令放弃就可能少报或错报；靠 W8 按 `first_unknown_at` 告警 |
| HTTP 库 | 标准库 `urllib` | 不加依赖（Worker 不能装依赖，沙箱也要能跑单元测试）；同步阻塞在后台线程里，不影响请求处理 |
| 换 Core 凭据（不是轮换令牌） | 运营步骤：先等发件箱里所有 `first_unknown_at` 非空且仍 `queued` 的命令拿到确定结果，再换 | 新凭据的 `credential_id` 不同，幂等作用域随之改变（§10 第 4 条）；未知命令换凭据重发可能生效两次（业务引用这一层会挡住同一事实，但不同内容会 409） |
| 502 当作确定未生效 | 是 | 见 3.3；部署形态变了要重议 |
| 单进程发送 | 是 | V1 只有一个 API 容器；租约与 `SKIP LOCKED` 保证换容器时不会重复发送 |
| 调度器租约 300 秒 | 是 | 作业跑超过 5 分钟时可能被另一个进程同时执行；V1 的作业（发件箱一批 50 条）远小于此；长作业（对账）在它们自己的设计里分批 |

## 8. 修订记录

- **0.1**（2026-11-23）：草案。同日按设计预审补齐：JSON 空值存 SQL `NULL`、`attempts` 的库侧默认值；超时只有一个值、哪些异常归未知、发出前的校验、没有错误体的 4xx、`Retry-After` 的解析；认领改为一条查询排除被阻塞的命令（原写法会被阻塞命令占满而卡住整个发件箱）；取消改为只允许确定从未发出的命令；入队时就做发出前的全部校验；处理函数异常与 `send` 异常的处理；命令种类与作业的登记、测试用的移除与 `run_once` 入口；调度器自身数据库错误的处理。

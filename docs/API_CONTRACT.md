# 网店库存 API v1 契约

> 版本：1.0 设计草案 · 日期：2026-10-07。用于 Acuven Shop 与后续自建网店接入；尚无已实现接口。编码前须按模板完成设计批准及预审。本文细化 DEVELOPMENT 第 8 节，数量语义以 STOCK_RULES 为准。

[接入规范](INTEGRATION_STANDARD.md)规定连接类型、向导、能力与安全；[接入指南](INTEGRATION_GUIDE.md)给完整调用流程；[OpenAPI 3.1.1 设计契约](contracts/inventory-v1.openapi.json)是机器可读格式。API 版本为 `1.0.0-draft.2`，不与文档版本混用。只有实现后的契约、双端和真实店铺验收通过才算接通。

## 1. 协议和服务身份

- JSON/UTF-8、HTTPS；UTC 时间采用 RFC 3339，例如 `2026-10-07T03:00:00Z`。所有返回时间和过期判断以服务端为准。
- `Authorization: Bearer <service credential>` 由部署环境配置，每个连接、环境独立；不发给浏览器或大模型。建议随机生成至少 32 字节的不透明凭据，服务端存不可逆摘要及凭据 ID，密钥材料不进 Git。令牌加密与此不同：平台访问令牌需要后端读取，采用部署密钥加密或密钥管理引用。
- 身份绑定 `connection_id`、固定 instance_id、环境和 scopes：`connection:read`、`mapping:read`、`availability:read`、`availability:status`、`reservation:write`、`reservation:read`、`event:read`；事件写入按动作拆成 `event:sale:write / event:cancel:write / event:dispatch:write / event:return:write`，不发通用 event:write。Chatbot 仅授予 `availability:status`，使用独立身份，不能复用 Shop 库存写入身份。
- 请求中不接收 `connection_id`、tenant、环境或角色覆盖字段。所有 ID 查询同时限定当前身份的连接；其他连接的对象统一返回 404，避免暴露是否存在。
- `X-Request-ID` 为每次 HTTP 尝试的关联 ID；返回原样合法 ID。`Idempotency-Key` 为每个写操作的稳定键，重试不变；同一业务动作不同操作仍用不同键。
- v1 请求对象禁止未知字段；拒绝金额、姓名、电话、地址、付款凭据等额外字段。引用限 1–128 个 ASCII 字符 `[A-Za-z0-9._:-]`，必须由调用系统生成不含个人资料的内部标识。数量为 1–2147483647 的整数，拒绝布尔、浮点、0、负数和超界值；超出实际库存仍按数量规则校验。
- 单次查询/写入最多 100 行、请求体最多 128 KiB（首批规模的技术限制，后续版本可调整）。重复 `line_ref`、空行集、同一行映射到不同 SKU 均为无效请求。同 SKU 不同行可合并计算容量，但保留各行关联。
- 管理 API、OAuth 回调和平台 webhook 是独立入口，不能共用此服务写入凭据。服务凭据可轮换、停用；新旧重叠期需明确到期并审计，撤销后所有操作立即拒绝，不因 draining 放行无效凭据。

### 1.1 统一响应

成功响应使用 `{ "data": {...}, "request_id": "..." }`；错误使用如下结构。HTTP 重试不能改变业务引用。

```json
{
  "error": {
    "code": "INSUFFICIENT_STOCK",
    "message": "One or more items cannot be reserved",
    "retryable": false,
    "details": {"lines": [{"line_ref": "line-1", "sku_id": "sku-001"}]}
  },
  "request_id": "req-001"
}
```

面向顾客的 Shop 响应使用自己受控的中文/英文文案，不直接转发内部错误对象。API 信息只能包含调用连接的字段；日志不记录 Authorization、原始敏感请求或 OAuth code。

### 1.2 连接和映射发现

`GET /internal/v1/connection` 需 connection:read，返回身份绑定的实例/连接、环境、profile、生命周期、契约版本、实际 scopes、mapping_revision、逐项 capability 状态和 effective_limits；不返回服务秘密或平台令牌。示例见[连接响应](contracts/examples/connection.example.json)。客户端先核对环境及能力，服务端每次操作仍检查权限；返回 ID 不能用于切换调用身份。

`GET /internal/v1/mappings?limit=100` 需 mapping:read，只返回本连接已确认映射。支持 limit 1–100、opaque cursor；可按 external_product_id + external_variant_id 一起筛选，两者缺一为 422。返回 mapping_id、外部商品/规格 ID、可空卖家 SKU、中心 sku_id/code、mapping_version、mapping_revision 和 next_cursor；空页合法。示例见[映射响应](contracts/examples/mappings.example.json)。

游标绑定连接、筛选条件、映射版本和固定排序；其他连接游标按无效请求拒绝，版本变化返回 409 MAPPING_REVISION_CHANGED，接入方重新分页并核对映射，不续读混合版本。规格映射创建/确认只允许管理端，不让网店服务为任意中心 SKU 自行授权。

instance_id 固定于部署，connection_id 固定于店铺；连接的归属/环境不能普通编辑。draft/testing 只读/隔离测试，不可真实预留或自动发布；active 仍检查每项能力，draining/closed 边界见第 6 节。scope 有效但能力未核实返回 409 CAPABILITY_NOT_AVAILABLE；生命周期未开放返回 409 CONNECTION_NOT_ACTIVE，不进入故障收款兜底。

## 2. 可售查询

`GET /internal/v1/availability?sku_id=sku-001&sku_id=sku-002`，重复参数表示批量，最多 100 个不同 SKU。

Shop 服务身份响应：

```json
{
  "data": {
    "items": [{
      "sku_id": "sku-001",
      "availability_state": "in_stock",
      "available_quantity": 4,
      "stock_version": 12,
      "as_of": "2026-10-07T03:00:00Z"
    }]
  },
  "request_id": "req-001"
}
```

- `in_stock` / `out_of_stock`：可校验的本连接数量大于 0 / 等于 0；`needs_review`：SKU 或连接差异阻止新预留；`unknown`：目前无法确认状态。
- `needs_review` 与 `unknown` 时 `available_quantity=null`，不得制造 0 或可售数。HTTP 200 的逐项状态也须在 Shop 服务端校验；已知业务异常不能进入继续收款例外。
- 无法确认版本的 unknown/review 项 stock_version 可为 null，不能编造最新版本；as_of 表示本次状态所依据的观察时间，不能把旧缓存伪报成刚核对的库存。
- 未映射/无权 SKU 统一 404 `SKU_NOT_FOUND`；批量含此类 SKU 时整批返回错误，不泄露其他连接映射。SKU 规格展示信息由 Shop 自己的目录或受限映射查询提供，不通过此数量接口返回其他店铺资料。
- Chatbot 身份使用 `GET /internal/v1/availability-status`，同样的查询参数，响应每项仅包含 `sku_id`、`availability_state`、`as_of`，不含数量、余额版本或任何其他连接字段。其身份调用数量端点收到 403。
- 查询只是快照；结账仍须预留成功。版本供核对和审计，不让浏览器凭旧版本绕过服务端容量检查。

## 3. 整单预留、查询与释放

### 3.1 创建

`POST /internal/v1/reservations`：

```json
{
  "order_ref": "shop-order-001",
  "lines": [
    {"line_ref": "line-1", "sku_id": "sku-001", "quantity": 2},
    {"line_ref": "line-2", "sku_id": "sku-002", "quantity": 1}
  ]
}
```

201 成功响应：

```json
{
  "data": {
    "reservation_id": "res-001",
    "order_ref": "shop-order-001",
    "state": "active",
    "created_at": "2026-10-07T03:00:00Z",
    "expires_at": "2026-10-07T03:30:00Z",
    "lines": [
      {"line_ref": "line-1", "sku_id": "sku-001", "quantity": 2},
      {"line_ref": "line-2", "sku_id": "sku-002", "quantity": 1}
    ]
  },
  "request_id": "req-002"
}
```

校验全部映射、连接生命周期、SKU 状态和容量，由 Core 在稳定租户/池/地点/SKU 顺序下锁余额、行和已生效额度；全成功或全失败。一笔 `connection + order_ref` 只有一个预留，换键也不能重复创建。同订单同内容复用原预留；不同内容返回 409 `ORDER_REFERENCE_CONFLICT`。过期旧单不能原地重新申请或延长，Shop 创建新的订单引用。

创建写操作重试复用原 HTTP 结果，包括原到期时间，额外返回 `Idempotency-Replayed: true`；重放结果可能是历史 active 状态，调用方必须查询最新状态再决定进入付款页。有效期限为首次成功后 30 分钟，客户端不能指定 TTL。Shop 自己在本地事务执行最多 3 笔未付款占用的限制，Inventory 不接收顾客身份。

### 3.2 查最新结果

- `GET /internal/v1/reservations/{reservation_id}`：200 返回第 3.1 节字段及当前 `active / committed / released / expired` 状态，处理到期检查后再返回，不仅依赖定时任务。
- `GET /internal/v1/reservations?order_ref=shop-order-001`：按稳定订单引用查唯一预留，供创建响应丢失时查询；不存在为 404 `RESERVATION_NOT_FOUND`。缺少 query 参数为 422。
- 404 查询不能证明一次超时写入永远不会到达；客户端重试同键，并以数据库唯一引用防重复。作废订单后需持久化不再重试创建的状态，清理迟到请求的补偿任务须查询并释放对应预留。

### 3.3 释放

`POST /internal/v1/reservations/res-001/release`：

```json
{"reason": "payment_failed"}
```

原因枚举 `payment_failed / customer_cancelled / replaced_checkout / admin_verified`。200 返回最新预留对象。active 释放全部行；已经 released/expired 是安全无操作，不增加份额；committed 为 409 `STATE_CONFLICT`，应通过销售取消事件。超过到期时间的 active 在锁内先转 expired，不复活为 released。释放规则详见 STOCK_RULES 第 3 节。

## 4. 库存事实事件

`POST /internal/v1/inventory-events`，202 代表事实已持久接收，**不代表库存确认成功**：

```json
{
  "event_ref": "sale-order-001-v1",
  "order_ref": "shop-order-001",
  "action": "sale_confirmed",
  "occurred_at": "2026-10-07T03:05:00Z",
  "reservation_id": "res-001",
  "lines": [
    {"line_ref": "line-1", "sku_id": "sku-001", "quantity": 2},
    {"line_ref": "line-2", "sku_id": "sku-002", "quantity": 1}
  ]
}
```

```json
{
  "data": {"event_id": "evt-001", "event_ref": "sale-order-001-v1", "state": "received"},
  "request_id": "req-003"
}
```

| action | 要求 |
| --- | --- |
| `sale_confirmed` | 有 active 预留时行集、SKU、数量及 order_ref 必须完全一致，一次转为 committed。无预留或引用已过期预留时，保存真实销售事实并按数量规则入账/核对；不能把超卖事实隐藏成普通失败。 |
| `sale_cancelled` | 仅取消未实际出库部分；每行数量不超过剩余已确认需求，不自动执行退款。 |
| `dispatched` | 仅已确认、未取消、未出库数量，记录实际交运事实；不能以打印面单代替出库。 |
| `return_received` | 已验收实际收回，每行额外必填 `disposition=sellable / unavailable`；累计返库不超过累计出库。不允许同一行在一次事件里重复，分两种验收结果则发两个稳定引用事件。 |

`reservation_id` 仅在 sale_confirmed 可选，其他动作不接收；它始终属于同一连接和订单。不认识的 action 返回 422。首次销售确认以连接订单行为唯一初始数量，换 event_ref 重报相同事实复用结果，数量不同进入 `needs_review`，不新增第二次占用；首期不通过增加确认事件修改已付款订单行数量。

每种 action 分别检查动作 scope；拥有销售确认权限不等于可以取消、出库或返库。检查当前连接对象及数量链后才接收；库存管理员的实物/配额调整不通过此事件 API 开放。受理权限只能证明来源有权提交，实物验收与履约证据仍由原业务系统负责。

已登记订单行的后续事件按原行的固定 SKU 和连接归属授权，不按当前映射重新归到其他实物 SKU。新订单确认需要有效映射；存量映射不再生效时仍不能丢弃已有合法行的取消/出库/退货事实，实际映射迁移遵守专门核对流程。

已知语法错误、无权引用在接收前拒绝。无效/待核对 SKU 映射的来源事件可先持久保存为 `needs_review`，但不应用到任意 SKU；未映射中心 SKU 的市场事件通过适配器 inbox 和外部规格引用核对，不伪造此 API 的 sku_id。

正常市场销售本来没有 Shop 预留，经过适配器核验且份额/映射正常时可以 processed；真实 Shop 没有有效预留的付款确认属于故障/迟到路径，数量事实即使足够也保留 needs_review，待人工核对后结案。两类来源由可信连接能力区分，不能让请求正文自称市场来源绕过核对。

### 4.1 查询及处理结果

- `GET /internal/v1/inventory-events/{event_id}` 或 `GET /internal/v1/inventory-events?event_ref=sale-order-001-v1`，只查询当前连接的唯一事件。
- 状态：`received`（待处理）、`processed`（数量动作全部完成且此次核对正常）、`needs_review`（需补前序/人工核对）、`failed`（技术处理失败待重试）。未知结果不能当 processed。
- 所有查询返回 `event_id / event_ref / state / received_at / processed_at / effects_applied / reason_code / reconciliation_ref`。未完成时间为 null；`effects_applied` 表示全部数量动作已应用，或未应用，不允许混合部分应用。
- 已应用但出现超卖等差异可以是 `effects_applied=true` 且 `state=needs_review`；Shop 保持内部待库存核对，不能把“需求已登记”说成库存足够。人工结案后仍有可查的审计与差异历史。
- 乱序事件为 `effects_applied=false`，待前序补齐后按原 event_id 自动再处理；需人工决定的差异不得用定时重试绕过。
- 技术失败在事务提交前回滚数量；重试同一 event_id。若提交后的通知失败，库存动作仍已完成，只重试通知，不再次应用流水。

## 5. 幂等、重试与故障继续收款

Inventory 保存 wrapper 请求/投递记录，Core 保存数量操作幂等与规范化摘要/首次确定结果，作用域含可信 tenant/pool/连接/operation/key；数量结果与账内预留/流水同事务提交。响应丢失从 Core 查原业务引用，不让 wrapper 单库记录替代数量确认。行按 line_ref 排序规范化，同内容不同 JSON 属性顺序不应冲突。不同请求内容重用键为 409。重放复用原业务数据和 HTTP 状态，但 request_id 对应当前请求。写操作还用业务引用唯一键，防止换键导致重复。幂等去重依据随真实库存历史长期保留，不随 Redis 清理或备份周期删除。

| 结果 | Shop 处理 |
| --- | --- |
| 400/422 `INVALID_REQUEST` | 修正协议/字段，不能继续收款兜底。 |
| 401/403 `UNAUTHORIZED / FORBIDDEN` | 修复身份、scope、环境，不能当普通连接故障。 |
| 404 `SKU_NOT_FOUND / RESERVATION_NOT_FOUND / EVENT_NOT_FOUND` | 检查自身映射/引用；超时查询未找到时仍需按同键重试规则核对。 |
| 409 `INSUFFICIENT_STOCK` | 整单不进入付款页，顾客调整购物车。 |
| 409 `SKU_REVIEW_REQUIRED / MAPPING_REVIEW_REQUIRED / CONNECTION_DRAINING / CONNECTION_CLOSED` | 不申请新库存；人工核对/交接，不作为故障继续收款。 |
| 409 `CONNECTION_NOT_ACTIVE / CAPABILITY_NOT_AVAILABLE / MAPPING_REVISION_CHANGED` | 完成接入验证/能力核实或重新读映射；不作为网络故障继续收款。 |
| 413 `REQUEST_TOO_LARGE` | 按限制调整请求，不能以拆成逐行预留破坏整单原子性。 |
| 409 `STATE_CONFLICT / RESERVATION_EXPIRED / ORDER_REFERENCE_CONFLICT / IDEMPOTENCY_CONFLICT` | 查询最新状态，修正调用；不得新键反复占用。 |
| 429 `RATE_LIMITED` | 遵循 Retry-After，同键退避；不启动继续收款例外。 |
| 5xx、网络超时 | 优先按原引用查询、同键重试；持续不可用且无法确认结果时才进入已批准的故障收款路径。明确响应中的鉴权/业务失败不能被5xx包装掩盖。 |

写接口默认短重试建议最多 3 次、指数退避带随机抖动，具体连接/总超时在联调时配置和记录；不让浏览器无限等待。异步重试持久化、重试预算用尽进入待办，不丢弃事实。服务端尚在处理同幂等键时可返回 409 `REQUEST_IN_PROGRESS` 加 Retry-After，客户端等待同一操作，不能当业务失败另造订单。

Shop 的故障收款流程：本地保存稳定订单引用、未取得预留的状态和后续库存事件；付款由真实支付确认，随后本地事务记录 `inventory_check_pending` 及 outbox。如果预留创建可能成功但响应丢失，恢复后先按 order_ref 查询，在有效时沿用，不额外占用。预留过期则按迟到销售事实处理；对账可能返回需求已应用但不足，人工处理。Shop 未恢复持久存储时不能凭浏览器付款返回页补造可靠订单。

这段持续收款例外仅适用于用户已选定的 Acuven Shop。后续网店需在接入记录中明确自己的库存故障销售政策，不能因使用相同 API 默认继承该风险选择。

本地 `outbox pending → sent → awaiting_result → done / needs_review` 是交付进度，不等同顾客订单状态。202 后查事件状态；收到 `processed` 才认为正常库存动作完成。需要人工核对的订单保留核对标记。Inventory 恢复后补交顺序按同订单行前序关系执行，不凭到达时间强扣库存。

## 6. 连接生命周期与版本

- active 可按能力新预留与接收事实；draining 拒绝新预留，只接受切换前已登记的引用；closed 只允许授权身份查历史，拒绝写入。凭据无效时一律拒绝。
- 进入 draining 前 Shop 须停止新结账并交付本地尚未送达的稳定订单行引用清单；未预留的故障订单也须登记到切换清单，否则不能利用存量接口创建新需求。清单仅最小标识，不含顾客资料。
- 能力状态须明确 availability、reserve、release、events、stock_write、reconcile 是否可用；缺少全链路能力不宣称自动同步。
- v1 不删除/改名既有字段、枚举或语义；新增可选响应字段须通知调用方。新必填字段、数量语义或不兼容鉴权变更使用新版本并安排迁移。

## 7. 接口验收矩阵

| 场景 | 应证明 |
| --- | --- |
| 两项一项缺货 | 409，全无预留，无可用支付页。 |
| 两个并发结账抢最后 1 件 | 一个成功，另一个缺货；MySQL 行锁下验证。 |
| 创建响应丢失/同键重试/换键同单 | 一份预留、原 TTL，能按 order_ref 查到。 |
| 相同键不同数量 | 409，无新流水。 |
| 付款确认与到期同时运行 | 锁内唯一结果，不同时释放并再次占用；迟到事实可查。 |
| 202 后异步处理失败 | Shop 不误报库存确认，可查未应用状态及待办。 |
| 超卖真实事实 | 已确认需求保留、O 非负、needs_review 与缺口可查。 |
| 部分退货重复或超过出库 | 重试不重复，超量不返库；不可售退货不增加可售量。 |
| 相同订单号在两个连接 | 分别去重，互相不可查询或修改。 |
| 顾客/AI 或 Demo 调真实写入 | 拒绝，不返回完整数量或生产记录。 |
| draining 的旧单与新单 | 只允许切换清单/已登记的旧单事实，新单拒绝。 |
| 临时故障收款后恢复 | 本地 outbox 未丢、原预留核对、事件只应用一次，库存不足转人工。 |

本轮已保存 `docs/contracts/inventory-v1.openapi.json` 设计契约与 examples；当前没有实现服务、模拟服务或 SDK。正式 CI 须校验 OpenAPI/JSON Schema、实际响应、动作 scope 的应用代码及双端联调；HTTP bearer 在 OpenAPI 中不使用 OAuth scopes，应用动作权限由 x-required-scopes / x-required-scopes-by-action 注解并由服务端实施。文档或 schema 通过不等于授权、并发或真实同步已经验证。

## 8. 系统群核心和可信库存上下文

此 v1 是 Inventory 多入口产品的服务接口包装，不是单卖 Shop/POS/ERP 的基础账 API。wrapper 将数量命令按原幂等/事实引用送至 Core，结果和 stock_version 取权威账/已生效政策，不能先在 Inventory 私有库占用再在 Core 扣第二次。Core 返回未知或未完成时保留对应状态，不将 wrapper 202 当账已确认。

sku_id 表示目录稳定规格 ID，sku_code/基本名称/条码经目录读取；映射权威在 Inventory。连接身份绑定 tenant_ref、inventory_pool_id、location_id、目录/账实例引用及已购共享能力；这些上下文只返回本网店字段，不接受请求正文自选 tenant/pool/location。每个对象/幂等键/队列/缓存均限定该范围，跨池/跨租户混合批次整批拒绝。

连接 metadata 增补该上下文与共享模式。V1 一池/地点，响应含 location_id 不代表多仓/POS 已实现。单模块基础调用、ERP 入库和 POS 设备额度要在 Core 另订契约，不能把采购收货造作 Inventory 销售入口。旧 draft.1 尚未发布，本次设计版本为 draft.2；实现和消费者 CI 都要重新验证，不能沿用旧单库事务模型。

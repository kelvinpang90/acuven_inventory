# 自建网店接入指南

> 版本：1.0 草案 · 日期：2026-10-07。示例仅说明设计协议，当前 API 尚未实现/部署；不能向真实店铺直接执行。Acuven Shop 首个接入，其他网店后端沿用同一库存生命周期。

## 1. 接入前准备

由管理员创建本环境专属连接、确认外部规格与 Acuven SKU 映射、分配份额并发放最小权限服务凭据。网店保存凭据于后端环境/密钥配置，不能在前端构建变量或对话中使用。库存实际数量/预留由共用 Core 管理；指南是已购买 Inventory 的多入口接口包装，不适用于未购 Inventory 的单模块基础账接入。连接 metadata 的租户/池/地点和 Core 引用必须与目标部署匹配，调用方不能在正文切换归属。最低需要：

| 配置/能力 | 准备 |
| --- | --- |
| `INVENTORY_BASE_URL` | 本部署已验证 HTTPS 域名，末尾不含 API 路径。指南不提供可误用的生产默认地址。 |
| `INVENTORY_SERVICE_CREDENTIAL` | 本连接环境的秘密，示例不写实值。 |
| 本地商品规格 | 外部稳定变体 ID 映射到 Inventory sku_id，管理员确认后读取；不按名称自动猜。 |
| 本地订单/行/动作引用 | 永久稳定、不含顾客资料；重试复用，不跟随页面刷新重新生成。 |
| 本地持久 outbox | 本地订单变化与待发送事实同事务保存，异步重试并查询 Inventory 结果。 |
| 行为声明 | 声明 reserve_api，正常预留、30 分钟 TTL；明确本网店自己的库存故障销售政策。 |

密钥发放、现货调整、配额和映射确认都是管理员操作；网店服务 API 不提供这些写权限。该协议不负责网店价格、支付、顾客验证、资金退款或物流商调用。

## 2. 第一次连接

以下 bash/curl 片段供接入方参考；在 Windows 可转成同义 HTTP 客户端，不把测试占位命令当生产运维命令。环境变量由部署注入，不输出其值；HTTP 客户端不开 verbose，不将 Authorization 写入日志。

```bash
curl --fail-with-body "$INVENTORY_BASE_URL/internal/v1/connection" \
  -H "Authorization: Bearer $INVENTORY_SERVICE_CREDENTIAL" \
  -H "X-Request-ID: setup-001"
```

响应显示本网店 connection_id、环境、profile、生命周期、契约版本、已授予 scopes 和 capability 状态。核对确为期望实例/环境；不把输出 connection_id 再放进写请求以切换连接。未经批准的写能力不调用。

```bash
curl --fail-with-body "$INVENTORY_BASE_URL/internal/v1/mappings?limit=100" \
  -H "Authorization: Bearer $INVENTORY_SERVICE_CREDENTIAL" \
  -H "X-Request-ID: setup-002"
```

仅返回本网店已确认规格 ID、卖家 SKU（可空）、中心 sku_id/code、映射 ID/版本，按 next_cursor 分页。保存到自己的规格记录；映射缺失联系管理员，不新造 sku_id。游标绑定当前连接及筛选参数，不能跨连接复用。

## 3. 正常下单流程

1. 商品页由网店后端取本店状态；公开页面只传有货/售罄等，不回传准确件数。展示可缓存，结账容量不可用缓存代替。
2. 顾客提交订单，网店服务端校验商品/数量及每人最多 3 笔未付款限制，先生成稳定本地 order_ref/line_ref 和预留操作键。
3. 整单 POST reservations；所有行都成功才创建可付款入口。失败按错误类型处理；已知无货不进入付款。
4. 保存 reservation_id/expires_at；刷新页面不延长，付款期限按 Inventory 返回时间。创建结果未知先按 order_ref 查原预留，再同键重试。
5. 支付由网店自己的已验签回调/权威查询确认，本地事务保存付款事实和 sale_confirmed outbox，关联原预留。
6. 发库存事件得到 202 后查结果；processed 才认为正常完成，needs_review 保留人工核对，不能由“已发送”推断库存足够。

预留请求文件示例：

```json
{
  "order_ref": "web-order-001",
  "lines": [{"line_ref": "line-001", "sku_id": "sku-001", "quantity": 1}]
}
```

```bash
curl --fail-with-body "$INVENTORY_BASE_URL/internal/v1/reservations" \
  -H "Authorization: Bearer $INVENTORY_SERVICE_CREDENTIAL" \
  -H "X-Request-ID: checkout-001" \
  -H "Idempotency-Key: reserve-web-order-001" \
  -H "Content-Type: application/json" \
  --data-binary @reservation.json
```

reservation.json 由网店后端根据已校验订单生成，不接收顾客浏览器直接指定授权对象。`Idempotency-Key` 标识业务操作；每次 HTTP 尝试的 X-Request-ID 不同。

## 4. 后续库存事实

| 业务事实 | 库存调用 | 网店需要确认 |
| --- | --- | --- |
| 付款成功 | sale_confirmed | 实际付款状态和订单行；不能凭付款返回页判断。 |
| 待付款失败/放弃 | release | 原预留未转销售；已付款不走 release。 |
| 已确认取消、未出库 | sale_cancelled | 原系统已经阻止发货并确认取消；申请/退款审核尚未完成不冒充已取消。具体 Shop 取消审批选择仍待确认。 |
| 实际交运 | dispatched | 已确认未取消的数量；仅打印物流单不算。 |
| 实际退货收到并验收 | return_received | 验收数量及 disposition，可售/不可售分开；退款本身不产生返库。 |

付款事实示例，reservation_id 为前一步返回值：

```json
{
  "event_ref": "sale-web-order-001-v1",
  "order_ref": "web-order-001",
  "action": "sale_confirmed",
  "occurred_at": "2026-10-07T03:05:00Z",
  "reservation_id": "res-001",
  "lines": [{"line_ref": "line-001", "sku_id": "sku-001", "quantity": 1}]
}
```

POST `/internal/v1/inventory-events` 使用稳定事件键，查询 `/internal/v1/inventory-events?event_ref=sale-web-order-001-v1`。不发送姓名、手机号、地址、金额、物流单号或支付凭据。取消、出库、返库每次实际动作各有自己的稳定 event_ref；只有引用订单行不足以区分多次部分返库。

## 5. 对接方常见错误

| 情况 | 正确处理 |
| --- | --- |
| 超时不知道预留是否成功 | 查原 order_ref；同键重试，不能另生成订单/键。 |
| 201 重放仍写 active | 是原操作响应，查询最新状态，不认为原预留还有效。 |
| 202 事件受理 | 查 effects_applied/state，不能立刻标库存完成。 |
| 同 SKU 同订单多行 | 不同行引用，Inventory 整单合并容量校验；不逐行预留留下半单。 |
| 401/403 | 修权限/环境，不进入故障收款兜底。 |
| 409 缺货/待核对/换内容同键 | 按业务处理，不伪装网络失败。 |
| 429 | 按 Retry-After 退避并保留 outbox，不换 key 绕限额。 |
| 预留已过期但后来付款 | 提交原订单销售事实，留待核对，不复活旧预留。 |
| Inventory 长期不可用 | 启动该网店经评审的故障策略；Acuven Shop 的持续收款选择不自动适用于其他网店。 |
| 主动停用连接 | 原网店停止新接入，登记未送达存量引用；旧事件核对完成再撤销写凭据。 |

## 6. 接入验收与移交

先在隔离 Demo 验证 A/B 两连接对象权限、状态只读、重复和乱序、最后一件并发、响应丢失、过期/迟到、部分返库上限及限流。在实际 MySQL 验证事务，再做双方部署与环境核对。

接入记录包含网店服务 commit、契约版本、连接/环境非秘密标识、能力状态、映射/份额确认、故障销售政策、维护负责人、测试报告与缺项。平台未批准能力不在记录中填已通过。

首次联调必需通过正常整个库存生命周期及故障路径，然后管理员完成期初数量与旧占用核对，启用已确认规格。未来新网店只增加接入方客户端/配置或对应适配器，若需要改库存公式，应先作为库存业务变化单独设计。

详细规则见 [API 契约](API_CONTRACT.md)、[接入规范](INTEGRATION_STANDARD.md)及[机器可读契约](contracts/inventory-v1.openapi.json)。

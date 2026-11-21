# 跨项目交付与 OpenClaw 任务计划

> 版本：1.0 设计草案 · 日期：2026-10-07。这里只列任务和交接标准，未创建任务契约、仓库或控制面登记，也不代表任务已 ready。文件范围是建仓后的建议精确路径，登记前按真实代码核实。

## 1. 交付顺序

| 阶段 | 可以并行推进 | 依赖与完成证据 |
| --- | --- | --- |
| 准入 | Shopee 更正后审核复核；TikTok 新公司资格官方核实；支付认证与各方式能力核验 | 台账有实际状态与证据，不以账号创建代表通过；实际联系官方另需用户授权。 |
| 库存基础 | 本项目骨架、数量规则、流水、人工配额、Demo | 不依赖购物平台获批；需要模板身份/远端设置及库存状态设计闸门。 |
| Shop 对接 | Shop 真实交易设计、双部署、库存客户端/outbox、支付、售后 | 在 Shop 项目独立计划和批准；Inventory v1 契约与正常/故障路径联调。 |
| 真实运营 | 初始盘点、人工 Shopee 份额、Inventory、正式支付、人工寄件 | 支付满足卡/FPX/明确 TNG 选项和退款验收；Inventory 首次通过闸门。短信延期可暂缓 AI 订单操作。 |
| 平台自动接入 | 哪家 scope 获批先做哪家，逐项核验能力 | 每 SKU 核对后启用，无强制多日只读期；权限不足仅开放已验证功能。 |
| 客服 | 真实库存只读、人工售后入口、模式隔离先做；本人订单动作后做 | 共用号码隔离验收，登录/短信/订单归属完成后才开启 AI 订单动作。 |

不设固定总完成日期，按 Core → Inventory V1 → 真实网店 → 获权平台的依赖和质量证据推进，审批不作可控工期承诺。Demo 可独立上线；真实 Shop 必须满足首次上线闸门。若时间不足，先暂缓自动平台或短信依赖功能，不删减真实收款与库存的必需验收。

## 2. 候选工作包重排：Core 前置

旧 001–025 草案没有登记到 .platform；其中私有商品/余额/预留迁移方案已失效。本轮只更新计划，不授权执行；以下职责在 Core 契约与迁移设计冻结后再拆为精确单层任务并分配编号。Core 项目任务不得登记到 Inventory 的文件白名单。

| 工作包 | 所属项目/建议路径 | 前置/验收 |
| --- | --- | --- |
| 租户/目录/库存池及数量契约 | 拟议 Core 项目；Inventory 文档 contracts/consumer 接口 | 稳定目录 ID、唯一账、池隔离、单模块基本额度及 Inventory 政策原子执行，设计批准后建仓。 |
| 商品身份和基础账实现/迁移 | 拟议 Core 项目 | ERP/Shop 旧结构语义、数量幂等与预留、恢复；本项目不建 stock_balances 或私有 SKU 主档。 |
| Inventory 版本健康检查/最小骨架 | app/main.py、app/api/health.py、tests/test_health.py | 模板/独立代码与身份事实；健康 SHA，尚不称库存能力已可用。 |
| Core 客户端/响应未知核对 | app/services/core_client.py、tests/contracts/test_core_client.py | 固定上下文/版本、稳定原操作，结果不明查 Core，不能自己补造第二占用。 |
| 连接/目录映射与政策模型 | app/persistence/models/connections.py、mappings.py、policies.py | tenant/pool/location/Core refs，映射到目录规格，目标/账内生效政策分开。 |
| 服务/本地与门户身份 | app/api/service_auth.py、admin_auth.py | 租户/池/对象/动作、已购能力和限时支持；门户失联不免鉴权。 |
| 可售与原子预留包装 API | app/api/availability.py、reservations.py | Core 原子事务证据，返回 Core 版本、原预留 TTL，整单成功/失败。 |
| 平台/网店事实持久接收与送账 | app/api/inventory_events.py、app/services/fact_delivery.py | Inbox/业务来源去重及 Core 回执，202 不称账已应用；同事实入口切换不重扣。 |
| 份额政策、转移和平台发布 | app/domain/allocations.py、publishing.py、app/jobs/publish_stock.py | Core 已生效额度，Q/T 单份约束、旧目标废弃、未知结果不增加；平台限流夹具。 |
| 管理端盘点/收货/目录入口 | app/api/admin_stock.py、admin_catalog.py | 调 Core 对应权限 API，个人审计；不让本项目直接更改核心表。 |
| 人工 Shopee 与异常核对 | app/services/manual_reconciliation.py、frontend 相关页面 | 原人工份额/日核对规则是 Acuven 配置，映射/已知旧事件一致，合法租户范围。 |
| 连接器协议与独立平台适配器 | app/integrations/base.py、mock.py；平台实现另拆 | 权限/版本/口径核验、逐 SKU 结果，未获权不能 ready 真实适配器。 |
| 连接向导和接入发现 | app/api/connection.py、mappings.py、frontend Connections 页面 | 六步预览/启用、池/目录核心上下文可见，测试按钮不写真实库存。 |
| 联合恢复/Demo/线索交接 | Inventory + Core + Shop 各自作业/运维 | 联合 generation/业务水位，真实 CRM 线索不被模拟重置删除；不在库存库存联系人。 |
| 单卖/组合/SaaS 契约验收 | tests/contracts、tests/integration | 一账一身份、跨租户/跨池拒绝、两个入口共池需授权、单模块不依赖门户/ERP。 |

每个代码任务一个层、≤12 精确文件、≤8 验收、预计大改先拆。共享注册、模型加载、文案和测试夹具必须列白名单；.platform 由登记流程维护。首先满足模板/设计/Core 依赖再登记 1–3 个任务，不复用旧余额迁移任务的 allowed_change_paths。

## 3. CI、设计和部署证据

- 建议必需 CI job：`backend-checks`（静态和纯领域）、`mysql-integration`（临时 MySQL、迁移与并发）、`frontend-checks`（双语界面构建及关键交互）、`contract-checks`（OpenAPI 和协议夹具）。名字在 project.yaml 与 GitInventory 必需检查中必须一致，不能要求尚不存在的 job。
- contract-checks 应使用经选型的 OpenAPI 3.1/JSON Schema 2020-12 验证器，并检查实际动作权限；设计文件格式检查不能证明鉴权生效。此会话未安装验证器或构建 CI，具体工具/版本在建仓时固定。
- Worker 只使用已批准的固定安全命令。FastAPI TestClient、asyncio、Node 与 MySQL 测试只放 CI，不写进任务 allowed_commands；允许纯逻辑检查仍须实测沙箱兼容性。
- 状态/迁移任务分别引用 STOCK_RULES、API_CONTRACT 和具体迁移设计；每个任务 ≤8 条验收，包含失败、恢复及前后数据，预审未通过不能 ready。
- D1–D5：默认分支 push、部署精确 DEPLOY_SHA、健康结果包含 SHA、新版失败/回滚仍判失败、不取消正在部署、25 分钟内结束。迁移与恢复步骤必须在 OPERATIONS 设计基础上细化，不能把数据库备份恢复放进自动部署回滚。
- GitInventory 远端归属、可见性、实际部署域名、资源容量尚未落实到本仓库；建仓阶段再确认所需信息，不把示例路径或域名当已部署证据。

## 4. Shop 仓库的独立改造包

现有 Shop 是纯 Demo：模拟付款/发货/退款、每日库存重置、演示订单手机号查询及认领等行为不能直接进入真实站。下面需在 `E:\projects\acuven-shop` 自己的文档与任务契约落实；本次仅在 Inventory 记录交接要求，没有修改 Shop。

| 包 | 要改的职责 | 验收 |
| --- | --- | --- |
| S1 真实交易设计 | 订单/支付/售后状态、真实身份、数据保留、地址改动后的运费、故障收款例外 | 与现有 Demo 列出环境行为矩阵，经营者批准后才写代码。 |
| S2 双部署 | 同代码独立 DB/域名/账号；服务端固定模式；生产禁止模拟支付与演示重置 | 启动配置冲突即失败，UI 不能切模式；真实密钥不进入 Demo。 |
| S3 Inventory 客户端 | v1 身份、商品映射、原子预留、30 分钟期限、3 笔名额 | 重复点击复用旧单，整单缺货阻止付款，已知业务失败不作故障。 |
| S4 本地 outbox 与恢复 | 稳定行/动作引用、持久事件、202 后查结果、迟到付款及响应丢失 | 正常取消/出库/返库和故障补交不重复；保留近期已完成发送事实供 PITR 恢复重放，不只保留 pending。 |
| S5 支付与退款 | 最终服务商、卡/FPX/TNG、验签、同 Stripe 多业务隔离、人工审核全额/部分退款 | 实际审核/方式启用、付款与退款和提现证据；AI 无资金权限。 |
| S6 商品、物流与政策 | 3C 资料核对、MY 邮编/重量报价、人工交运/录单、售后承诺、政策发布 | 保留报价快照，支付前可履约；真实人工核验与时限队列；未定政策显式关闭发布闸门。 |
| S7 身份与 AI 订单接口 | 短信服务、原手机认领、一次性聊天绑定、订单归属、服务端动作检查 | 演示验证码/仅订单号电话查询不能访问真实订单；短信延期则受控关闭认领和 AI 动作，保留人工入口。 |

Shop 的真实商品库存不再以本地 variant.stock 或每日重置作为权威。允许本地状态缓存用于展示，但库存确认由 Inventory 决定，临时故障按已选流程记录待核对；不能让缓存数量和 Inventory 各扣一份。

## 5. AI 与 WhatsApp 网关隔离包

2026-10-07 只读核对发现：Chatbot 的 `backend/app/tools/registry.py` 使用进程全局 `_tools_enabled` 控制所有 bot；WhatsApp 网关 `app/demos_registry.py` 目前仅登记 ai_chatbot 下游。现有配置不足以证明共用号码的真实店与 Demo 已隔离。

推荐同一客服代码库增加独立真实店部署，与现有行业 Demo 部署分开；同一个 WhatsApp 号码由网关明确路由到两种服务。这样 Demo 导演台的开关不会作用于真实店进程，仍需在真实服务禁用演示故障注入入口。

| 仓库 | 改造包 | 必需约束 |
| --- | --- | --- |
| ai_chatbot_demo | 独立真实零售 bot 配置及受限 Inventory/Shop 客户端 | 明确 `real_acuven / demo` 来源上下文，真实服务仅允许库存状态、本人订单查询、结账、符合条件地址/取消、退款申请、转人工工具。 |
| ai_chatbot_demo | 工具权限和演示控制隔离 | 不直接复用 ERP 订单、客户创建、e-Invoice、credit note 等工具作为真实 Shop 写入；目录里存在工具不等于当前会话允许调用。Demo 开关只作用 Demo，真实配置不能开启导演台/模拟数据。 |
| whatsapp_gateway | 默认真实、显式演示入口和会话持久路由 | 路由模式由网关可信状态生成；专用 Demo 链接/菜单只发起受控模式选择，不信任聊天文本直接指定任意下游或连接。 |
| 两仓库 | 切换、绑定与人工接管 | 切换清空/隔离上下文，真实订单授权撤销；网关验签后转发受认证内部消息；下游拒绝来源未验证的模式字段。人工台显示模式。 |

登录绑定设计：Shop 可信浏览器签发短期一次性凭证，绑定顾客、目标真实聊天会话及当前模式；兑换后服务端保存受限授权，可撤销/过期。大模型不能持有服务凭据或顾客登录密码，也不能只凭 WhatsApp 号码认领订单。Demo 转真实必须重新登录绑定，旧授权不能回流 Demo。

首期真实只读/转人工可以先上线；SMS 和所有身份边界通过前，真实订单动作默认关闭。测试须证明：关闭全部 Demo 工具仍能回答真实库存；伪造模式或切换后旧凭证无效；Demo 工具永远不能访问真实 Shop、Inventory 或顾客资料。

## 6. 跨仓库联调与启用顺序

1. 在隔离环境部署 Inventory v1 与 Shop 客户端，运行同版本 fixture 和端到端库存生命周期。
2. 先启用库存客户端和持久 outbox，验证响应丢失、到期竞态与重放；再启用真实支付路径。首次上线不能以“故障继续收款”跳过 Inventory 验收。
3. 商品映射、实盘、旧占用和人工 Shopee 配额核对后开放真实结账；未获批平台仍标人工/待开通。
4. WhatsApp 默认真实服务与 Demo 路由隔离后开放只读/人工客服；身份闸门通过后单独开启 AI 订单动作。
5. 平台适配器获批后核对事件语义和绝对写库存能力；当天可启用全部已确认 SKU，按规格保存结果，未知项保持待核对。

| 联调证据 | 所属方 |
| --- | --- |
| Schema/错误码/身份兼容报告，Inventory 与 Shop 精确 commit | 两项目 |
| 原子预留、付款、取消、出库、可售/不可售退货数量复算 | Inventory + Shop |
| 临时 Inventory 故障继续收款、持久补交、已确认缺货不兜底收款 | Shop + Inventory |
| Demo 重置、生产隔离、WhatsApp 模式隔离 | 三业务仓库 + 网关 |
| 支付审批与实际方式、退款和提现；人工寄件/政策 | Shop 运营者 |
| RPO/RTO 演练及恢复后含已完成事件的重放 | Inventory + Shop 运维 |

每份联调记录只保留脱敏引用、时间、版本、结果、缺项和负责人，不附密钥、银行卡、顾客资料或完整官网证件。任何真实平台和支付未通过，保持明确依赖状态；Demo 成功不能替代真实闸门。

## 7. 系统群路线与 ERP 后续工作

现行数量归属见[共用核心决定](decisions/SHARED_COMMERCE_CORE.md)。ERP 收货/自身业务经同一账写入；Inventory 管多入口政策/平台适配，Core 变化/结果供 ERP，不再开发 Inventory 私有账再复制 ERP 的方向。Acuven ERP 日渠道经营汇总从原交易源生成，汇总不再次扣库存，金额与票据不进库存事件。

路线为现有 Shop Demo → Core 契约/迁移与实现 → Inventory V1 → 真实 Shop → 获权 Shopee/TikTok → ERP 真实经营 → CRM/最小门户 → 后续 POS/AI 工作台。无固定总完成日期，优先保证完整与验收。此排序不修改 Shop 现行 planning-v1 或控制面授权。

单模块基本目录/数量契约是 Core 的职责；POS 设备级离线与多地点另专项，未购 Inventory 的不同入口不得共享同一池。外部客户 SaaS/专属/自托管、支持访问与退出导出是明确产品目标，阶段验收不等于现在已完成。

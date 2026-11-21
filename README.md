# acuven_inventory

**Acuven Inventory** 是可独立交付的多销售入口库存控制产品：在随附共用商品目录和基础库存账上管理渠道映射、安全库存/份额政策、预留协调、平台发布和对账。第一试点为 Acuven 单池/单地点、真实 Shop 与获权平台；完整订单、资金和顾客资料在原业务系统。

2026-11-21 从项目模板起步：后端服务骨架（健康检查、就绪检查、两个平台授权回调的固定地址，授权尚未实现）、CI、`deploy.yml`、`.platform/`；还没有业务功能，也还没有编号任务。
OpenClaw 项目 ID 为 `acuven_inventory`，任务编号 `INVENTORY-TASK-NNN`，尚未在控制面登记。项目规则见 [CLAUDE.md](CLAUDE.md)（与 `AGENTS.md` 同一内容），任务计划见 [docs/TODO.md](docs/TODO.md)。

- **[最小 V1 设计：只接自营 Shopee 与 TikTok Shop](docs/V1_MINIMAL_DESIGN.md)**：0.2 已于 2026-11-21 经 Kelvin 批准。这是当前优先（acuven_hub `DECISIONS.md` I-08），范围上对下面的需求文档做了删减，删减与改动列在它的第 14 节，待定事项列在第 16 节
- [需求文档](docs/REQUIREMENTS.md)
- [开发与集成设计](docs/DEVELOPMENT.md)
- [库存数量与状态规则](docs/STOCK_RULES.md)
- [网店库存 API v1 契约](docs/API_CONTRACT.md)
- [网店与平台接入规范](docs/INTEGRATION_STANDARD.md)
- [AI 解耦与后续 CRM/ERP 集成边界](docs/SYSTEM_DECOUPLING.md)
- [现行：共用商品目录和基础库存账](docs/decisions/SHARED_COMMERCE_CORE.md)
- [历史：Inventory 与 ERP 的库存方向](docs/decisions/ERP_INVENTORY_AUTHORITY.md)
- [与 Acuven Hub 的架构对齐及变更矩阵](docs/HUB_ARCHITECTURE_ALIGNMENT.md)
- [自建网店接入指南](docs/INTEGRATION_GUIDE.md)
- [机器可读 OpenAPI 设计契约](docs/contracts/inventory-v1.openapi.json)
- [跨项目交付与 OpenClaw 任务计划](docs/DELIVERY_PLAN.md)
- [界面、Demo 重置与恢复操作设计](docs/OPERATIONS.md)
- [需求与交付流程复核](docs/REVIEW_2026-10-07.md)
- 平台开发者接入台账、TikTok 拒审核对、支付商户申请台账：含经营信息，放在 acuven_hub 私有仓库的 `inventory/` 下，不进本公开仓库

当前活跃设计为 1.0 架构对齐草案；数量模型迁至拟议 Core，旧候选任务重排，接口设计为 draft.2。共用核心与 Inventory 应用均未实现，现行批准/代码任务未改变；独立 SaaS/专属/自托管和组合门户的权限/迁移仍须验证。

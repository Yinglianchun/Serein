# 归线历史恢复与明确重建

## 先恢复，不猜补 Track

Track 的当前延续卡会随新的窗口继续更新；`scope` 表示最后使用窗口，不能充当历史归属记录。恢复旧批次不会放宽窗口过滤到未来窗口，也不会编造同名 Track 卡。

日间 Router 现在在同一个事务内保存 normalized `routing_result`、归线缓存及 `pipeline_route_provenance`（每条缓存的来源批次和对应 assignment）。结算写入缓存时同样记录来源。原来的 Router 请求、输出与 ordinal 继续保留。

恢复顺序为：本批快照 → 本批 Router job → 缓存来源批次中的已完成 job/快照 → 旧版已有的有界卡片校验。旧版没有 provenance 时，只在同一 source/window scope 内查与本批原话 ID 实际相交的历史批次；最多 64 个来源批次，每个最多 256 个 Router job。达到上限或结果存在歧义会停下，不会任选一个。

历史回放不调用模型。它核对来源契约、原话编号/顺序/内容/角色/时间、当时的 ordinal、完整 assignment 和 bridge 两端；有下游任务时还核对冻结 component、predecessor 和 ownership。来源日间批次可能比当前结算批次大，因此按已完成 job 的输入块回放，不能使用看过未来原话的日终卡片。没有完整可信材料时保持 `needs_repair`，不会把缺失静默当成功。

恢复结果写入当前批次快照，使用 `preserve_newer` 保护全局较新的延续卡。已经完成且相容的 Curator/Writer 结果继续复用。没有逐条保存的旧 provenance 不会在初始化时被猜测填充。

## 界面出口

“重新校验并继续”先尝试上述历史恢复。无法恢复时，“作废本批计划并重新归线”会再次明确确认：

- 旧批次置为 `superseded_repair`，原冻结输入、job、模型输出与尝试记录全部保留。
- 仅弃用本批涉及且尚未处理的归线缓存；弃用前把缓存和 provenance 原值写入旧批次审计记录。
- 新计划使用新批次/job ID，保留原始消息与边界，不会把旧 Curator/Writer 输出套到新归线上。此次重建跳过失效缓存及其旧历史入口。
- 不删除或改写已发布的 Event，不把原话标成 processed/skip。已经有结算收据、原话已被处理或原文内容变动时，拒绝重建。
- 保留队列顺序；新 Track 编号避开当前卡片和被弃用缓存引用的编号。重复提交旧批次 ID 会被拒绝。

HTTP：`POST /v1/pipeline/rebuild`，请求包含 `batch_id` 和 `confirm="REBUILD_PIPELINE_BATCH"`。浏览器经现有同源鉴权代理 `/__serein/pipeline/rebuild`，后端 bearer token 不交给浏览器。MCP 使用 `pipeline_rebuild(batch_id, confirm)`。两者共享 pipeline lease；已有 queued/running 任务时返回 busy。重建本身不调用模型，完成后通过“继续整理”启动新计划；界面确认按钮会接着提交继续请求。

## 验证范围

合成回归覆盖跨窗卡片迁移、旧版无 provenance、来源卡片确实缺行、较大日间批次的早期输入块恢复、旧任务复用、源内容/ordinal/bridge 不匹配、歧义来源、事务回滚、确认重建、旧记录保留、鉴权与 lease 冲突。没有连接真实部署或读取真实对话。本改动不加入并发，不改变 Event Writer 的 1000/1500 契约。

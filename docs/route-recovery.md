# 归线历史恢复与明确重建

## 先恢复，不猜补 Track

同一聊天归线时，上一条回复的完成时间可能晚于下一条请求的接收时间。本批先前分段已成功归线的原话，按原话 ID 判断是否早于当前段首。跨会话材料和从旧批次载入的历史卡仍要核对来源时间，以免补录较早聊天时带入后来的内容。旧压缩批次中途首次出现的候选卡按当段边界校验；若其锚点落在本批起点之后，还需要有与该 Track 相符的独立归线回执。

Track 的当前延续卡会随新的窗口继续更新；`scope` 表示最后使用窗口，不能充当历史归属记录。恢复旧批次不会放宽窗口过滤到未来窗口，也不会编造同名 Track 卡。

日间 Router 现在在同一个事务内保存 normalized `routing_result`、归线缓存及 `pipeline_route_provenance`（每条缓存的来源批次和对应 assignment）。结算写入缓存时同样记录来源。原来的 Router 请求、输出与 ordinal 继续保留。

恢复顺序为：本批快照 → 本批 Router job → 缓存来源批次中的已完成 job/快照 → 旧版已有的有界卡片校验。旧版没有 provenance 时，只在同一 source/window scope 内查与本批原话 ID 实际相交的历史批次；最多 64 个来源批次，每个最多 256 个 Router job。达到上限或结果存在歧义会停下，不会任选一个。

历史回放不调用模型。它核对来源契约、原话编号/顺序/内容/角色/时间、当时的 ordinal、完整 assignment 和 bridge 两端；有下游任务时还核对冻结 component、predecessor 和 ownership。来源日间批次可能比当前结算批次大，因此按已完成 job 的输入块回放，不能使用看过未来原话的日终卡片。没有完整可信材料时保持 `needs_repair`，不会把缺失静默当成功。

完成批的压缩快照保留 Router 初始 `tracks`、`recent`、`next_track_ordinal` 和候选筛选 `input_policy`。已知旧版压缩快照缺失这些字段时，从已保存的 Router 请求恢复初始状态，并继续核对后续状态、原话哈希和时间边界；候选 Track 暂时隐藏后重新出现，也必须符合之前的更新。此次旧版兼容只用于已完成的压缩批，未完成批仍保留代码升级审查，已有待修复计划可能需要明确重建一次。

已发布的归线来源（`routed`、`done`、`superseded_input_budget`）若保留完整初始状态和逐请求 `routing_frame`，即使 `runtime_revision` 与当前代码不同，也会按保存的请求逐帧核对；仍要求相同数据契约和 scope，原话、来源版本、前序卡片、ordinal、assignment 及时间范围必须全部通过校验。这不会重新调用旧任务，也不会把旧记录的版本号改成当前值。缺少完整初始状态的旧压缩批仍仅兼容已知版本，未完成任务仍保留升级审查。

`route producer has an incompatible frozen contract` 指向的是归线来源批次，未必是面板上的当前批次。错误现在包含 producer ID、状态、保存的 contract/runtime、当前 contract/runtime，以及保存和预期 scope。排查升级问题时应将它们与当前批次的 ID、`rebuild_of`、contract/runtime 一起核对。明确重建本来就会将新计划冻结到当前 contract/runtime；新计划引用旧 producer 不能证明新计划继承了旧版本号。不要直接改历史记录的版本号或仅追加版本白名单来消除报错。

恢复结果写入当前批次快照。历史恢复只会补齐数据库里真正缺失的 Track 卡，绝不覆盖任何已经存在的全局卡片；因此即使旧卡缺少 `recent_source_message_ids`，也不会被历史状态倒灌。普通当前归线仍使用 `preserve_newer`。已经完成且相容的 Curator/Writer 结果继续复用。没有逐条保存的旧 provenance 不会在初始化时被猜测填充。

## 界面出口

升级前已处于 `needs_repair` 的批次，如果冻结版本确实与当前版本不同，也会补上 `runtime_contract` 提示，并保留原失败结果；不会清空失败次数或迁移旧计划。冻结版本未变化的归线证据问题仍按原原因处理，不一律标为版本变化。把 `needs_repair` 误送到“重试暂停批次”接口时，会明确提示使用重校验或明确重建入口。

“重新校验并继续”先尝试上述历史恢复。无法恢复时，“作废本批计划并重新归线”会再次明确确认：

- 旧批次置为 `superseded_repair`，原冻结输入、job、模型输出与尝试记录全部保留。
- 仅弃用本批涉及且尚未处理的归线缓存；弃用前把缓存和 provenance 原值写入旧批次审计记录。
- 新计划使用新批次/job ID，保留原始消息与边界，不会把旧 Curator/Writer 输出套到新归线上。此次重建跳过失效缓存及其旧历史入口。
- 若历史 Track 载入时无法验证来源帧（例如 `No bounded historical pre-routing Track frame`），此次确认从本批首条原话开始一个新的归线历史段，不带入旧卡片；新计划和旧批审计记录 `recovery_downgrade`。新归线成功发布后，该聊天后续窗口也忽略截断点之前的归线历史，避免下批再次遇到同一缺口。其他聊天的历史不变，旧卡片、原话、job 和 Event 均保留；不会把未来卡片的正文重新挂到早期来源上。
- 不删除或改写已发布的 Event，不把原话标成 processed/skip。已经有结算收据、原话已被处理或原文内容变动时，拒绝重建。
- 保留队列顺序；新 Track 编号避开当前卡片和被弃用缓存引用的编号。重复提交旧批次 ID 会被拒绝。

HTTP：`POST /v1/pipeline/rebuild`，请求包含 `batch_id` 和 `confirm="REBUILD_PIPELINE_BATCH"`。浏览器经现有同源鉴权代理 `/__serein/pipeline/rebuild`，后端 bearer token 不交给浏览器。MCP 使用 `pipeline_rebuild(batch_id, confirm)`。两者共享 pipeline lease；已有 queued/running 任务时返回 busy。重建本身不调用模型，完成后通过“继续整理”启动新计划；界面确认按钮会接着提交继续请求。

## 验证范围

合成回归覆盖跨窗卡片迁移、旧版无 provenance、来源卡片确实缺行、较大日间批次的早期输入块恢复、旧任务复用、源内容/ordinal/bridge 不匹配、歧义来源、事务回滚、确认重建、旧记录保留、鉴权与 lease 冲突。没有连接真实部署或读取真实对话。本改动不加入并发，不改变 Event Writer 的 500 字软预算与 1500 字异常阈值。

升级回放另使用 `180c94a` 的实际代码生成合成数据库：`93eb2d7` 读取其中已发布的归线会重现 incompatible frozen contract；修复后可以核对旧归线，明确重建的新计划使用当前版本，旧 producer 记录保持原样，重建批次及后续窗口均可结算。此验证仍不代表已核对反馈者实例中的具体 producer 记录。

## 与模型失败暂停的区别

needs_repair 表示无法证明旧归线可以安全恢复，需要先校验或明确重建，不自动跳过或猜补。paused_failure 表示文字 job 累计失败三次：点“重试此批次”保留冻结材料和成功结果，只重新开放未完成步骤的预算。其他聊天可继续，同一聊天后续等待恢复；绕行不适用于 needs_repair。

HTTP 使用 `POST /v1/pipeline/retry-batch` 和 `{batch_id}`，随后调用继续接口。与重建共用 pipeline 执行锁，已有 queued/running 任务时返回 busy。

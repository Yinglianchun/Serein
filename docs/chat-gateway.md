# Chat gateway

已有自己模型调用与工具循环的宿主可改走独立的 [Hook 接入示例](hook-integration.md)：只向 Serein 取前情，在实际模型请求成功后由宿主登记交付。Hook 不会替宿主调用聊天模型或归档对话。

Upstream configuration uses the `gateway.upstreams` structure: one connection
per upstream, multiple model IDs, and optional public aliases mapped to real upstream names.
The public implementation aggregates the configured model lists for `/v1/models` and resolves
each chat request independently. Duplicate public aliases are rejected instead of silently
choosing the first upstream. This configuration does not provide key rotation or failover.

## Context and delivery

- Recall uses actual current user text after removing external attachments, workspace blocks,
  leading `proxy_sender` and `【系统提示…】`, and recognized external-context sections.
  A pure attachment or a tool continuation does not start a new memory query.
- Operit rewriting is enabled by default and configurable. Stable context remains near the
  leading system instructions; current activity and recalled memory enter the current user
  message. Multimodal and tool protocol messages retain the original conservative handling.
- A tool continuation can reuse the exact prepared prefix. The source prefix and model/tools
  contract must match; snapshots expire after one hour and are cleared after a final answer.
  Missing reasoning fields are restored only for matching assistant tool calls in that window.
- Prompt cache keys and retention settings preserve caller-supplied values. Native Anthropic
  mode supports automatic cache control or explicit breakpoints on system, tools and an earlier
  assistant message. The current user turn receives no explicit breakpoint. Token estimates
  and breakpoint distances are heuristic estimates.
- Model, identity, Operit and memory setting changes separate snapshot namespaces. There is no
  cross-window result cache. The proxy records successful upstream deliveries; selection and
  incomplete/failed streams do not become successful delivery receipts.
- The public host uses a five-response recent-card window. The core still checks
  cooldown after selecting the final cards and never substitutes a third-place candidate.

## Worldbook and raw dialogue

完整的 `<worldbook>…</worldbook>` 是客户端背景设定，不是用户本轮原话。
召回查询和新增原话归档都会排除整个区块（包括多个 entry、多段、多行、大小写与带属性的标签），
保留区块外的对话文字和图片附件。归档在生成本轮去重标识之前清理世界书，直接 raw ingest
也执行同样的过滤；只有世界书而没有正文或图片的当前用户消息不会创建新的原话或冒用历史轮次。
工具续接仍沿用原有轮次。世界书之外的独立 `<entry>` 和未闭合的标签不按完整世界书删除。

此过滤只作用于原话/查询投影，不删除转发给聊天模型的世界书，也不依赖 Operit 重写开关。
升级不会批量改写已有原话、Event、归线缓存或已冻结的整理任务。已污染的历史材料和由其生成的
Event 需要另行核查，不能仅靠更新代码或点击“继续整理”保证自动清理。

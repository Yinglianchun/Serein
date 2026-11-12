# 扩展接口

`Contributions(tools, prompt_hooks, jobs)` 由显式工厂装配。关闭的扩展不加载工厂；HTTP `/v1/extensions/{tool}` 与 MCP 共用扩展函数。开窗续接默认使用聊天 `/resume` 指令；MCP 入口独立开启后注册只读 `resume` 工具，可与聊天指令同时使用。关闭指令入口会停用该指令及其快照续带；关闭总开关则停用两个入口。后台任务由同一生命周期启动和取消；索引任务是有可写索引时的核心任务。

这里没有插件市场或自动发现机制。自定义工厂由 Python 宿主传给 `Application(extension_factories=...)`；现成内置工厂由 TOML 选择。

## Event 正文修订

可写部署提供 `edit_event(event_id, expected_updated_at, title=None, content=None)`。
先用 `read_memory` 读取目标 Event 及其 `updated_at`，需要核对原话时设置 `with_evidence=true`。
只传要改的标题或正文，省略字段沿用原值。工具只修订活动 Event，不修改原话、来源绑定或手动浮现开关；不能用于 Scene、事实条目、归档或已删除记录。
成功修订会保留旧版并返回新的 `event_id` 与 `updated_at`；后续读取和编辑使用新 ID。
旧 ID、过期时间或并发修订返回 `conflict`；内容完全相同返回 `unchanged`。
结果只返回修订回执，需要正文或原话时再读取。独立的人工修订不受自动摘要 Agent 开关控制。

## Event 分工流水线

在设置的“自动摘要管理”页选择执行方式和三个角色 `track_router`、`event_curator`、`event_writer` 的模型。API 模式需要三个模型配置完整；Agent 模式领取和提交任务，不调用这些模型；旧版混合配置中未选模型的阶段等待 Agent。在“功能”页开启自动摘要后，后台白天归线，默认北京时间 03:00 结算 Event。配置页“继续整理”可立即处理最近已完成的对话。

外部 Agent 使用前，先在“自动摘要管理”页开启「自动摘要 Agent 工具」并保存，默认关闭。此开关统一控制 MCP 和 HTTP 扩展的 `pipeline_next`、`pipeline_submit`、`pipeline_rebuild`；关闭后缓存的工具调用也被拒绝，已有任务和结果保留。自动摘要自身的启停、配置页整理和修复操作仍由各自入口控制。

HTTP `POST /v1/extensions/pipeline_next` 接受 `{"include_recent":true}`，返回下一份任务或本批结果。任务含 `job_id`、`role` 和冻结 `request`，其中 `prompt` 为最新完整提示、`rules` 为对应 AGENTS.md。提交到 `POST /v1/extensions/pipeline_submit`，body 为 `{"job_id":"...","output":{...}}`，之后再调用 next。MCP 同名工具使用同样参数。重试相同结果幂等；不同结果不能覆盖已接收阶段。

Agent 交付的原话只在 `request.prompt` 中出现一次；`messages`、`component` 等旁侧字段仅提供编号和必要结构，不是服务端完整校验快照。完整原话、来源绑定及旧材料回执保存在服务端，提交时仍按这些材料校验。MCP `pipeline_next` 明确只返回一份紧凑 JSON 文本（图片另走 image content），不附同内容的 `structuredContent`；HTTP 仍返回 JSON 对象。外部 runner 应解析文本 JSON，完整读取 `rules` 和 `prompt`，不要从精简后的旁侧字段重建原话。

网页失败恢复使用 `POST /v1/pipeline/recovery-preview` 查看指定 `batch_id` 的数量、时间范围及可选 `max_input_chars` 下的拆分结果；此接口不修改批次或调用模型。`POST /v1/pipeline/skip-batch` 要求 `confirm: "SKIP_PIPELINE_BATCH"`；`POST /v1/pipeline/rebatch` 要求 `confirm: "REBATCH_PIPELINE_BATCH"` 与整数 `max_input_chars`（2000–100000）。只操作同一聊天最早的待处理批次，经现有 pipeline 锁执行；原话、旧请求和模型输出保留，不改已有 Event。重新分批不修改全局设置，使用完整问答并保留待回答的尾巴；成功后网页通过现有 `/v1/pipeline/next` 继续后台任务。

设置中的“自动摘要管理”集中提供执行方式、阶段模型、输入参数和 Agent 开关。新批次的候选旧 Event 固定先给 Curator 标题、正文和来源编号，不再提供“减小输入”开关；旧配置中保存的关闭值也按此处理。需要核对具体旧主张时，可返回 `{"context_request":{"event_id":"本轮候选编号","reason":"missing_prior_claim"}}`；也可使用现有 Track 补读。两种方式共用一次机会，按完整对话单元选取，最多六个、序列化三万字符；未提供的单元会在回执中列明，不能推断或视为 omit。旧正文不能当逐字原话引用。

设置页的“配置 Agent 整理 Event”弹窗提供 MCP 配置示例。使用发行包内 `scripts/event_agent_mcp.py` 连接已运行的 HTTP 后端；它只暴露领取、提交两个工具，不启动另一份后台任务。配置 `SEREIN_AGENT_URL` 与 `SEREIN_AGENT_TOKEN`，并把 Python 和脚本路径替换成实际安装位置。Agent 完整读取 prompt，完成对应角色，提交 JSON，再领下一阶段；不是打开弹窗就自动运行 Agent。

三份角色文件在 `src/serein/resources/agents/`，随 wheel 打包。旧的 `segment_events` 单阶段协议已经退役，不再接受 `extensions.event_pipeline.command` 启动另一套切分器。显式启用该扩展与设置页模型选择都运行同一份最新流水线。已有旧配置请删除 command，使用上述 API/MCP 接入。

## Narrative runner

设置页可以直接为 Writer 选择已配置的模型，省去外部 runner。两种入口均在运行前读取服务端身份配置，并由宿主复查输出 schema 与保存 fingerprint。

叙事 API Writer 对 DeepSeek 官方 `api.deepseek.com` 的 OpenAI / Chat Completions 接口使用 `json_object`，在提示中提供完整 schema，并在后端校验返回结构；其他 OpenAI 兼容上游仍使用 `json_schema`，不会按模型名称猜测中转能力或失败后自动降级重试。宿主的证据充分性、自检、材料及保存 fingerprint 校验保持不变。HTTP 400/422 表示请求被拒绝，需要检查输出格式、输入及接口兼容性；404 才归为模型或接口不可用，不回传上游原始错误正文。

**仍不建议使用 DeepSeek 作为 Writer。** JSON 模式兼容仅解决请求格式问题，不代表叙事质量或严格遵守写作契约得到保证；请优先选择已验证能稳定遵守 schema 和写作规则的模型。这项兼容不改变 Event Writer 或其他模型任务。

设置页“配置 Agent 撰写叙事卷”弹窗提供配置步骤。使用外部 runner 时，关闭“启用 API Writer”，在前端 Node 服务环境设置 `SEREIN_WRITER_ENABLED=1`、`SEREIN_WRITER_MODEL`，并在修改这些进程环境后重启该 Node 服务。

`SEREIN_WRITER_COMMAND` 为 JSON 字符串数组，例如 `["python","/absolute/path/writer.py"]`。runner 接收 `task=narrative_preview`、显式 `model`、`prompt`、冻结 `materials` 与 `output_schema`，返回严格符合 schema 的 JSON。证据不足必须返回 `evidence_sufficient=false`、空 body 和问题列表。Writer 只读取本次绑定的文字材料，不加载原消息图片；文字不足时不能猜测图片内容。

角色文件在 `web/codex_agents/`；目录名保留导入接口，runner 不依赖 Codex。`update` 使用原正文和新增材料，`rewrite` 使用全部绑定材料；移除材料要求 rewrite。服务端保存仍复查材料 fingerprint 和版本，预览不发布。

## 收藏预算

HTTP 与网关内部 resume 正文预算以字符计，不冒充模型 token。默认每页 16000，允许 1000..64000。返回集合总数与当页 ID，完整清单随分页读取；每页最多 50 项，标题显示上限 240 字符并标记是否完整；正文跨页有 offset 和 complete 标记。集合变化要求重启分页，删除和不可读项不返回正文。宿主应读取完整集合后按自身上下文容量处理，不能把第一页面冒充全部收藏。

MCP 不再注册 resume 工具；聊天 `/resume` 指令直接加载内部全部分页，不受 MCP 返回长度限制。内部与 HTTP 的 resume 接口保留。Arc 材料 MCP 读取有独立的4000 UTF-16单位返回上限及游标，详见[功能约定](public-feature-contracts.md)。

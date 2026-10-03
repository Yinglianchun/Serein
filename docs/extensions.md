# 扩展接口

`Contributions(tools, prompt_hooks, jobs)` 由显式工厂装配。关闭的扩展不加载工厂；HTTP `/v1/extensions/{tool}` 与 MCP 共用扩展函数。开窗续接默认使用聊天 `/resume` 指令；选择 MCP 模式后注册只读 `resume` 工具，同时停用该聊天指令及其快照续带。后台任务由同一生命周期启动和取消；索引任务是有可写索引时的核心任务。

这里没有插件市场或自动发现机制。自定义工厂由 Python 宿主传给 `Application(extension_factories=...)`；现成内置工厂由 TOML 选择。

## Event 分工流水线

在“配置”页选择执行方式和三个角色 `track_router`、`event_curator`、`event_writer` 的模型。API 模式需要三个模型配置完整；Agent 模式领取和提交任务，不调用这些模型；旧版混合配置中未选模型的阶段等待 Agent。在“功能”页开启自动摘要后，后台白天归线，默认北京时间 03:00 结算 Event。配置页“继续整理”可立即处理最近已完成的对话。

HTTP `POST /v1/extensions/pipeline_next` 接受 `{"include_recent":true}`，返回下一份任务或本批结果。任务含 `job_id`、`role` 和冻结 `request`，其中 `prompt` 为最新完整提示、`rules` 为对应 AGENTS.md。提交到 `POST /v1/extensions/pipeline_submit`，body 为 `{"job_id":"...","output":{...}}`，之后再调用 next。MCP 同名工具使用同样参数。重试相同结果幂等；不同结果不能覆盖已接收阶段。

设置页的“配置 Agent 整理 Event”弹窗提供 MCP 配置示例。使用发行包内 `scripts/event_agent_mcp.py` 连接已运行的 HTTP 后端；它只暴露领取、提交两个工具，不启动另一份后台任务。配置 `SEREIN_AGENT_URL` 与 `SEREIN_AGENT_TOKEN`，并把 Python 和脚本路径替换成实际安装位置。Agent 完整读取 prompt，完成对应角色，提交 JSON，再领下一阶段；不是打开弹窗就自动运行 Agent。

三份角色文件在 `src/serein/resources/agents/`，随 wheel 打包。旧的 `segment_events` 单阶段协议已经退役，不再接受 `extensions.event_pipeline.command` 启动另一套切分器。显式启用该扩展与设置页模型选择都运行同一份最新流水线。已有旧配置请删除 command，使用上述 API/MCP 接入。

## Narrative runner

设置页可以直接为 Writer 选择已配置的模型，省去外部 runner。两种入口均在运行前读取服务端身份配置，并由宿主复查输出 schema 与保存 fingerprint。

设置页“配置 Agent 撰写叙事卷”弹窗提供配置步骤。使用外部 runner 时，关闭“启用 API Writer”，在前端 Node 服务环境设置 `SEREIN_WRITER_ENABLED=1`、`SEREIN_WRITER_MODEL`，并在修改这些进程环境后重启该 Node 服务。

`SEREIN_WRITER_COMMAND` 为 JSON 字符串数组，例如 `["python","/absolute/path/writer.py"]`。runner 接收 `task=narrative_preview`、显式 `model`、`prompt`、冻结 `materials` 与 `output_schema`，返回严格符合 schema 的 JSON。证据不足必须返回 `evidence_sufficient=false`、空 body 和问题列表。Writer 只读取本次绑定的文字材料，不加载原消息图片；文字不足时不能猜测图片内容。

角色文件在 `web/codex_agents/`；目录名保留导入接口，runner 不依赖 Codex。`update` 使用原正文和新增材料，`rewrite` 使用全部绑定材料；移除材料要求 rewrite。服务端保存仍复查材料 fingerprint 和版本，预览不发布。

## 收藏预算

HTTP 与网关内部 resume 正文预算以字符计，不冒充模型 token。默认每页 16000，允许 1000..64000。返回集合总数与当页 ID，完整清单随分页读取；每页最多 50 项，标题显示上限 240 字符并标记是否完整；正文跨页有 offset 和 complete 标记。集合变化要求重启分页，删除和不可读项不返回正文。宿主应读取完整集合后按自身上下文容量处理，不能把第一页面冒充全部收藏。

MCP 不再注册 resume 工具；聊天 `/resume` 指令直接加载内部全部分页，不受 MCP 返回长度限制。内部与 HTTP 的 resume 接口保留。Arc 材料 MCP 读取有独立的4000 UTF-16单位返回上限及游标，详见[功能约定](public-feature-contracts.md)。

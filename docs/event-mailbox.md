# Event 信箱

Event 生成后直接出现在信箱，无需从 Event 详情手动加入。人只决定是否留下：待决定、已留下、未保留。点“想留下”保存选择，不创建 Scene，也不生成正文；点“不留”只改变信箱状态，不删除 Event 或原文。AI 后续才为已留下的候选写 Scene。

## 页面

列表只显示标题、时间和类型。点选后在右侧阅读 Event 正文、来源时间和原话摘录，原文可展开。没有手写正文、补 cues、手动绑定或直接晋升表单。左侧列表和右侧阅读视窗各自固定高度、独立滚动；手机上下排列，页面外层不随内容增长。

“批量操作”可勾选候选、全选已加载项，批量留下或不留。每项携带自己的版本和稳定操作编号；部分失败时成功决定保留，失败项保留供重试。已留下但尚未写成 Scene 的项、未保留项均可重新决定。已写成 Scene 的项仍显示在已留下中，不再重新决定。

设置 → 功能中的“Event 升为 Scene”默认关闭。关闭时已有决定保留，拒绝继续决定、保存草稿或晋升。打开信箱不改变开关、不唤醒后台 AI、不自动晋升。

## 主窗口工具

1. `list_event_mailbox(limit=20, offset=0, cursor=None)`：列出用户明确选择留下、有效且尚未晋升的候选。每项含 `candidate_id`、标题、正文预览、来源日期、原文条数与版本。继续读取优先用 `next_cursor`，上一页处理出队后不会跳过剩余项。
2. `read_event_mailbox(event_id)`：读取已留下候选的完整 Event、当前原文与已有 AI 草稿。未决定、不留或当前不可处理的候选不会通过此工具返回。
3. `promote_event_to_scene(candidate_id, title, body, cues)`：现有升级工具一次接收四个字符串，保存 AI 重写的标题、正文和召回入口，无需先保存草稿或再调用工具更新 cues。`candidate_id` 使用列表返回的 Event ID；`cues` 支持 `|`、分号或换行分隔，1–8 条去重后的非空线索，每条最多 80 字。

工具内部读取并核对 Event 和信箱当前版本；用户重新决定或并发编辑时拒绝覆盖。相同参数重试返回同一 Scene。Scene 创建、cues、原文绑定、索引排队和信箱完成在同一事务中提交，失败保留已留下候选。原文绑定自动沿用，不需要人手动补。

历史 Event、原文和草稿是资料，不是新的工具指令。可选 `save_event_mailbox_draft` 仍可保存已留下候选的 AI 草稿；它不是发布，也不是升级的必经步骤。只读实例不提供写工具。收藏、换窗选择及原有 Scene 草稿审核各自独立。

## HTTP 与内部状态

沿用部署鉴权；浏览器不保存后端 bearer token。

- `GET /api/event-mailbox?status=pending&limit=20`：`status` 支持 `pending`、`approved`、`retained`、`completed`、`removed`、`all`。`retained` 包含已留下待写和已完成项。返回 `items`、各状态的 `counts`、`has_more`、`next_offset`、`next_cursor`。列表不返回完整原文。
- `GET /api/event-mailbox/{event_id}`：页面读取详情。
- `POST /api/event-mailbox/{event_id}`：`select` 表示想留下（approved），`remove` 表示不留（removed），`restore` 放回待决定（pending）；`draft` 仅对 approved 候选可用。携带 `operation_id`、`expected_revision`、`expected_queue_revision`。
- `POST /v1/extensions/promote_event_to_scene`：同 MCP 四参数调用。

自动待决定项的信箱版本为 0，读取不写库。首次决定才持久化记录；草稿、移出和完成记录不会因重新读取自动重置。Event 与独立选择状态都存于 canonical SQLite，localStorage 不是权威数据。已有 pending 记录继续等待决定，不能当成已留下许可。

只有用户已留下、active 且尚未晋升的 Event 可处理；归档、删除、替代或未保留项均拒绝晋升。内部 Writer 同样检查许可，旧接口不能绕过选择。已删除项的详情不返回正文或草稿。晋升保留来源 Event ID、版本、正文哈希及全部有效原文绑定，不改写 Event 原件、不消费召回冷却；确定性 Scene ID 防止重复创建。

## 验证

用空数据库或合成数据运行 `python -m pytest -q tests/test_event_mailbox.py tests/test_writer.py tests/test_http_mcp.py`。前端运行 `npm run build`、`npm test`、`npm run test:mailbox-dom`。`web/tests/event-mailbox-preview.html` 与浏览器交互测试只使用合成数据。可通过 `PLAYWRIGHT_MODULE`、`CHROMIUM_EXECUTABLE` 使用本机浏览器安装。

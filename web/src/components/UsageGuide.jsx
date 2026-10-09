import { useEffect, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const tabs = [["start", "开始"], ["features", "日记与功能"], ["favorites", "收藏"], ["connect", "接入"], ["source", "原话"], ["install", "一键安装"], ["migration", "迁移"], ["passages", "长文分段"], ["events", "自动 Event"]];
const settings = { appearance: "外观", features: "功能", models: "模型", configuration: "配置", imports: "对话导入", migration: "旧库迁移" };
const hookSnippet = `\`\`\`python
from examples.hook_host import SereinHook

hook = SereinHook.from_env()
turn = hook.prepare("chat-001", [{"role": "user", "content": "上次读书会定在什么时候？"}])
reply = existing_model_call(turn.messages)  # 换成宿主现有的模型调用
hook.record_success(turn)  # 仅在模型完整成功后登记
\`\`\``;

function SettingsLink({ tab, children, onOpen }) {
  return <a href="#settings" className="settings-link" onClick={event => { event.preventDefault(); onOpen?.(tab); }}>{children || settings[tab]}</a>;
}

function installationMarkdown(location) {
  const root = location?.root || "";
  const windows = /^[A-Za-z]:[\\/]/.test(root);
  const quote = windows ? `'${root.replaceAll("'", "''")}'` : `'${root.replaceAll("'", "'\\''")}'`;
  const enter = root ? windows ? `Set-Location -LiteralPath ${quote}` : `cd -- ${quote}` : "# 先进入解压后的 Serein 发行目录";
  const launch = windows ? "powershell -ExecutionPolicy Bypass -File .\\scripts\\one_click.ps1" : "bash scripts/one_click.sh";
  return `### 在安装主机上打开菜单

${root ? `当前页面读取到的${location.installed ? "安装" : "源码预览"}目录：\n\n\`${root}\`\n\n${location.installed ? "命令已替换成该发行目录。" : "正式安装后，这里会显示实际发行目录。"}` : "尚未读到发行目录。先在终端进入解压后的目录，再运行对应平台的脚本。"}

\`\`\`${windows ? "powershell" : "sh"}
${enter}
${launch}
\`\`\`

首次运行会尝试注册 \`se\`；新开终端后进入此目录输入 \`se\`，即可打开同一管理菜单。多个实例以**当前目录**为准。找不到 \`se\` 时继续用上方脚本。

| 菜单 | 常用操作 |
| --- | --- |
| **0** | 设置页面用户名 / 密码 |
| **1** | 全新安装、重部署或旧库迁移 |
| **2** | 分别重启 Gateway 或记忆库 |
| **3** | 补齐、重建、清理派生向量 |
| **4** | 启停服务、查看状态和最近日志 |
| **5** | 配置本机、局域网 / 公网或 HTTPS 入口 |
| **6** | 更换 Gateway Key，并同步更新客户端 |
| **7** | Docker 网页旧库路径：授权一个只读来源目录 |

\`q\` 退出主菜单，子菜单用 \`r\` 返回；退出菜单不等于停服。**部署失败保持服务停止，不会自动回退**。修复当前步骤后，在同一目录重试。`;
}

export function UsageGuide({ onOpenSettingsTab, initialPage }) {
  const [location, setLocation] = useState(null);
  const hookExamplePath = location?.root
    ? `${location.root.replace(/[\\/]+$/, "")}${/^[A-Za-z]:/.test(location.root) ? "\\" : "/"}examples${/^[A-Za-z]:/.test(location.root) ? "\\" : "/"}hook_host.py`
    : "examples/hook_host.py";
  const codexExamplePath = location?.root
    ? `${location.root.replace(/[\\/]+$/, "")}${/^[A-Za-z]:/.test(location.root) ? "\\" : "/"}examples${/^[A-Za-z]:/.test(location.root) ? "\\" : "/"}codex-continuity-packet`
    : "examples/codex-continuity-packet";
  const [active, setActive] = useState(() => Math.max(0, tabs.findIndex(([key]) => key === initialPage)));
  const pages = useRef(null);
  const snapTimer = useRef(null);
  const targetPage = useRef(null);
  useLayoutEffect(() => {
    if (pages.current) pages.current.scrollTo({ left: pages.current.clientWidth * active, behavior: "instant" });
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/__serein/install-location", { signal: controller.signal })
      .then(response => response.ok ? response.json() : null)
      .then(value => { if (value?.root) setLocation(value); }).catch(() => {});
    return () => { controller.abort(); window.clearTimeout(snapTimer.current); };
  }, []);
  const page = index => ({ role: "tabpanel", id: `usage-content-${tabs[index][0]}`,
    "aria-labelledby": `usage-tab-${tabs[index][0]}`, "aria-hidden": active !== index, inert: active !== index });
  function show(index) {
    setActive(index);
    const element = pages.current;
    if (!element) return;
    window.clearTimeout(snapTimer.current);
    targetPage.current = index;
    const left = element.clientWidth * index;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    element.style.scrollSnapType = "none";
    element.scrollTo({ left, behavior: reduced ? "instant" : "smooth" });
    snapTimer.current = window.setTimeout(() => {
      element.scrollTo({ left, behavior: "instant" });
      element.style.scrollSnapType = "";
      setActive(index);
      targetPage.current = null;
    }, reduced ? 0 : 480);
  }
  function keyDown(event, index) {
    const moves = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault(); show(moves[event.key]);
    document.getElementById(`usage-tab-${tabs[moves[event.key]][0]}`)?.focus();
  }
  return <div className="usage-guide">
    <p className="usage-guide__intro"><span className="usage-marker">从安装到读回原话</span>，左右滑动或选择标签。灰色虚线文字会带你到相应配置页。</p>
    <div className="settings-tabs usage-guide__tabs" role="tablist" aria-label="使用说明分类">
      {tabs.map(([key, label], index) => <button key={key} type="button" role="tab" id={`usage-tab-${key}`}
        aria-controls={`usage-content-${key}`} aria-selected={active === index} tabIndex={active === index ? 0 : -1}
        onClick={() => show(index)} onKeyDown={event => keyDown(event, index)}>{label}</button>)}
    </div>
    <div className="usage-guide__pages" ref={pages} onScroll={event => {
      if (targetPage.current !== null) return;
      const element = event.currentTarget;
      setActive(Math.min(tabs.length - 1, Math.max(0, Math.round(element.scrollLeft / Math.max(1, element.clientWidth)))));
    }}>
    <section className="settings-group usage-guide__page" {...page(0)}>
      <div className="settings-group__heading"><h3>从这里开始</h3><p>部署、连接模型和迁移旧记忆，都可以在这里找到说明。</p></div>
      <ol>
        <li>在<SettingsLink tab="appearance" onOpen={onOpenSettingsTab}>外观设置</SettingsLink>填写你和 AI 的名字。</li>
        <li>在<SettingsLink tab="models" onOpen={onOpenSettingsTab}>模型</SettingsLink>添加上游地址、密钥和模型名，再到<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置</SettingsLink>为打标、嵌入、重排等任务选择模型。通过 HTTP 地址访问网页时，也可以添加上游和模型；旧版按钮无反应时，升级并刷新页面后重试。</li>
        <li>首次配置或更换 Embedding 后，先保存，再点击“建立 / 补齐检索索引”，完成后开启自动记忆。日常存记忆不用每次按；重复点击仍会重新计算路由向量并扫描索引，有模型调用和服务器开销。详见“长文分段”页的按钮说明。</li>
        <li>在<SettingsLink tab="features" onOpen={onOpenSettingsTab}>功能</SettingsLink>按需开启各项能力；“日记与功能”页说明哪些内容只保存、哪些会进入候选，以及哪些会明确带入聊天。当前日期时间默认使用 Asia/Shanghai，也可另选时区。聊天记录与 Operit 备份在<SettingsLink tab="imports" onOpen={onOpenSettingsTab}>对话导入</SettingsLink>处理；Ombre 旧库到<SettingsLink tab="migration" onOpen={onOpenSettingsTab}>旧库迁移</SettingsLink>。</li>
      </ol>
      <h4>主窗口保存 Scene</h4>
      <p><code>write_scene</code> 只必填正文 <code>content</code> 和检索线索 <code>cues</code>（1–8 条，每条最多 80 字符）。标题、日期和主域可选；编号由程序生成，无须填写 operation_id。默认不绑原话，明确要求引用时才传 evidence_refs。响应丢失时先读回确认，避免重复新建。</p>
      <p><code>edit_scene</code> 修改已有 Scene：先用 <code>read_memory</code> 读取，传 <code>scene_id</code>、读回的 <code>expected_updated_at</code> 和要改的 title、content 或 cues。未传字段和已有证据保留。状态使用 <code>set_scene_status</code>，批注使用 <code>annotate</code>。日记用独立的 read_diary、write_diary、revise_diary、comment_diary、delete_diary；升级后刷新工具列表。</p>
      <p>需要审核时仍可用 <code>propose_memory</code>，在 draft 中填写 title、body_md、cues、date，接受候选后才正式保存。Event 由原话整理流程生成；叙事卷正文走 <code>narrative_volume</code>，开启“主模型读写叙事卷”后，由主模型读取材料、自己写正文、预览并保存，不额外调用 Writer 模型。</p>
      <h4>把 Event 写成自己的 Scene</h4>
      <p>在功能页开启“Event 升为 Scene”并保存后，主窗口可以用 <code>read_memory</code> 读取摘要和绑定原话，自己改好标题、正文，再调用 <code>promote_event_to_scene</code>，传 Event ID、当前版本和改好的文字。工具保留 Event 原件及原话绑定，生成关联 Scene；原 Event 不再自动浮现，也不进入叙事卷修订箱。这一步不会自动执行。关闭开关会停用工具，已有内容保留。</p>
      <h4>Arc 的修订提醒</h4>
      <p>修订箱每天凌晨四点后由程序检查已有 Arc 的关联材料是否比卷的发布时间更新，不用为修订箱选模型。旧版模型生成的待处理成卷候选会在下次扫描时退出队列，历史内容保留。要开启新主题，在叙事卷页输入主题手动找材料；这一步才使用<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置页</SettingsLink>里的“叙事卷找材料”模型。</p>
    </section>
    <section className="settings-group usage-guide__page" {...page(1)}>
      <div className="settings-group__heading"><h3>日记、暗房与可选功能</h3><p>这些内容的保存、候选和注入边界彼此独立。</p></div>
      <h4>日记与暗房</h4>
      <p>日记在侧栏单独阅读，也可用 <code>read_diary</code>、<code>write_diary</code>、<code>revise_diary</code>、<code>comment_diary</code> 和 <code>delete_diary</code> 管理。它不参加普通 Scene／Event 自动召回，也不能收藏；需要时由网页或工具明确读取。日记可以作为叙事卷材料，近期没有新 Event／Scene 时也可能成为梦境材料。</p>
      <p>新建日记时把 <code>unlock_at</code> 设为未来时间，就会成为暗房日记。到期前正文不可读，也不能修订、评论或删除；解锁后按普通日记使用。暗房是 Serein 接口层的锁定约束，不等于对数据库备份加密。</p>
      <h4>换窗：窗影与 /resume</h4>
      <p>“窗影”让当前主模型在换窗前写下“我眼中的你”“我眼中的自己”和“这一窗发生的事”；它不生成 Scene，也不进入普通向量召回。“开窗续接”是另一个开关，可以勾选最新窗影、最近 Event、收藏 Scene、自选 Event／Scene，以及最近或尚未整理的原话。</p>
      <p>新窗口使用独立的 <code>X-Serein-Window-ID</code>，在经过 Serein 网关的聊天中发送 <code>/resume</code> 后，只把勾选内容明确带入；不会整库注入，也不会额外运行语义召回。仅连接 MCP 不处理这条指令。</p>
      <h4>备忘、梦境、心绪与防撤退</h4>
      <p>备忘是“留给未来的话”，可按日期、轮次、早晚和次数限制在符合条件的聊天里带入；它不是主动通知，也不写成 Scene／Event。梦境在<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置页</SettingsLink>选择模型，可设置每日概率和“梦境 · 主模型 Prompt”；凌晨四点后按日尝试，优先读取最近 48 小时新建的最多五条 Event／Scene，没有时才读新日记。</p>
      <p>心绪和防撤退共用所选模型，但开关独立。心绪按窗口保存状态和变化，只在心绪页展示，不注入后续聊天；防撤退在完整回复后异步判断，命中后按六轮且至少十分钟的冷却，在下一轮带入一次提示。</p>
      <h4>联想、收藏工具与原话查阅</h4>
      <p>联想沿已确认的 Scene 关系最多补一条候选，仍参加同一次重排并通过最终门槛，不会直接注入。页面收藏不依赖工具开关；开启“收藏工具”后，AI 才能读取、收藏或取消收藏 Event／Scene。开启“原话查阅”后，AI 才能按文字、日期、说话者搜索原话，再按 ID 读取全文和同会话前后文；显式读取不等于已经注入聊天。</p>
      <h4>其余独立开关</h4>
      <p>当前日期时间只附在当轮用户消息后，不进入原话档案或记忆流水线；“写入时找前情”在 Scene 保存成功后最多返回一条旧 Scene 提示，不建关系或加入 Arc；“关系提案自动通过”仍执行正文、证据和端点校验。“Event 升为 Scene”及“主模型读写叙事卷”保留各自的读取、预览和版本检查。关闭任一功能会停止后续调用或注入，已有内容仍保留。</p>
    </section>
    <section className="settings-group usage-guide__page" {...page(2)}>
      <div className="settings-group__heading"><h3>收藏的记忆</h3></div>
      <p>Event 和 Scene 详情都可点击“收藏”，在各自的“舍不得丢的”视图查看，归档的收藏也会保留。取消收藏不删除记忆。</p>
      <p>开启“收藏工具”后，客户端刷新工具列表即可使用 <code>read_favorites</code>。默认读取两类收藏的全文，每页 5 条；<code>kind</code> 可填 <code>event</code> 或 <code>scene</code>，用 <code>limit</code> 和 <code>offset</code> 分页。默认不包含归档收藏，传 include_archived 可读取，<code>with_evidence</code> 可附原文证据。读取不影响自动召回和冷却。</p>
      <p>AI 可在 <code>write_scene</code> 中传 <code>favorite: true</code> 随正文一起收藏；旧记忆用 <code>set_memory_state</code>，传记忆 ID、当前版本和 <code>favorite</code>，无需重写正文。<code>true</code> 收藏、<code>false</code> 取消，不传保持原样。需要实例允许写入；关闭“收藏工具”会禁止 AI 修改收藏，但页面仍可使用。</p>
    </section>
    <section className="settings-group usage-guide__page" {...page(3)}>
      <div className="settings-group__heading"><h3>连接 MCP 与聊天 API</h3></div>
      <p>以下是连接地址模板，请把“公网IP”“网关端口”或“你的域名”替换成自己的实际值。公网 IP 使用安装主机的公网地址，端口使用安装时配置的网关端口；域名示例适用于已配置 HTTPS 的入口。</p>
      <h4>聊天 API · OpenAI 兼容</h4>
      <p>接口类型选 OpenAI 兼容，<strong>Base URL</strong> 填 <code>http://公网IP:网关端口/v1</code>，使用域名时填 <code>https://你的域名/v1</code>；<strong>API Key</strong> 填完整的 Gateway Key，密钥输入框不用加 <code>Bearer </code> 前缀。聊天客户端可拉取已配置的全部上游模型。</p>
      <p>手动发请求时，模型列表为 <code>GET http://公网IP:网关端口/v1/models</code>，聊天为 <code>POST http://公网IP:网关端口/v1/chat/completions</code>；请求头为 <code>Authorization: Bearer &lt;Gateway Key&gt;</code>，把占位文字及尖括号替换成实际 Key。客户端 Base URL 通常只填到 <code>/v1</code>。</p>
      <h4>MCP · Streamable HTTP</h4>
      <p><strong>服务器 URL</strong> 填 <code>https://你的域名/serein/mcp</code>，<strong>传输类型</strong> 选 <code>Streamable HTTP</code>。支持 OAuth 的客户端把身份验证选为 <strong>OAuth</strong>；浏览器打开 Serein 授权页后，手动输入 Gateway Key 并确认。OAuth 要求 HTTPS 域名，同机 <code>localhost</code> 例外；不要把 Key 写进 URL。</p>
      <p>不支持 OAuth、但可添加请求头的客户端仍可使用静态方式：名称 <code>Authorization</code>，值 <code>Bearer &lt;Gateway Key&gt;</code>；专门的 Bearer Token 输入框只填完整 Key。<code>http://公网IP:网关端口/serein/mcp</code> 也只能使用静态方式。</p>
      <p>旧地址 <code>http://公网IP:网关端口/mcp</code> 仍兼容。到<SettingsLink tab="features" onOpen={onOpenSettingsTab}>功能设置</SettingsLink>开启可选工具并保存后，刷新客户端工具列表。</p>
      <p>仅当客户端和 Serein 服务在同一台机器上时，才把“公网IP”换成 <code>127.0.0.1</code>。同一局域网内的其他设备可填安装主机的局域网 IP；手机连接电脑时不能填 <code>127.0.0.1</code>，那会指向手机自身。</p>
      <h4>Gateway Key 在哪里</h4>
      <p>MCP 和聊天 API 共用安装时生成的 Gateway Key。安装完成的终端输出和安装目录下的 <code>deploy/connection-guide.txt</code> 都有；也可读取 <code>{location?.root ? `${location.root.replace(/[\\/]$/, "")}${/^[A-Za-z]:/.test(location.root) ? "\\" : "/"}deploy${/^[A-Za-z]:/.test(location.root) ? "\\" : "/"}secrets${/^[A-Za-z]:/.test(location.root) ? "\\" : "/"}api-token` : "deploy/secrets/api-token"}</code> 的完整内容。本页不展示实际密钥。</p>
      <p>网页登录用安装时设置的用户名、密码；模型厂商的 API Key 填在<SettingsLink tab="models" onOpen={onOpenSettingsTab}>模型页</SettingsLink>。主菜单 <code>6</code> 会更换 Gateway Key，旧静态 Key 和已经发放的 OAuth 凭据随即失效；OAuth 客户端需要重新授权。密钥和连接说明请勿公开。</p>
      <h4>聊天窗口与开窗续接</h4>
      <p>在客户端添加请求头：名称填 <code>X-Serein-Window-ID</code>，值填当前会话的独立标识，例如 <code>chat-001</code>。同一会话保持不变，新建会话换一个值；若客户端支持会话 ID 变量，可使用它。</p>
      <p>不填请求头也能使用，会统一进入默认会话 <code>main</code>，共用提醒轮次和召回冷却，无法据此识别新窗口。固定写一个值也不会自动区分窗口。启用开窗续接后，可在侧栏“换窗”页选择带入最近 1–50 条原话；“最近原话”和“尚未整理的原话”只能开启一个，打开一个会自动关闭另一个。选择命令方式后，在聊天中发送 <code>/resume</code>，也可以在指令后写上想继续聊的话，Serein 会读取完整的接续资料。</p>
      <p>两个续接入口在功能设置独立开关，可同时开启；内容选择在侧栏“换窗”页。默认开启网关指令入口，发送 <code>/resume</code> 时聊天需经过本实例网关；MCP 入口开启后提供只读 <code>resume</code> 工具。已通过指令读取本轮资料时，无须再次调用工具。两个入口均关闭时仍可预览和复制，关闭开窗续接总开关则停用整个功能。勾选后预览会自动更新，无须先保存；点击“保存选择”才更新下次续接的内容。让客户端读到 <code>has_more: false</code> 才算读完；换窗页同样读完全部页面后才能复制完整资料。读取不会调用模型或记录注入。</p>
      <h4>自建前后端：预装到 Codex 新窗口</h4>
      <p>如果聊天界面、后端和换窗动作都由你自己管理，可让后端请求 Serein 的结构化续接资料，再通过 Codex App Server 新建 thread，预装最新窗影、Scene、Event 和所选原话。示例目录：<code>{codexExamplePath}</code>。</p>
      <p>原话保持原来的 user / assistant 角色，正文前会带上可供 Serein 精确读回的原文 ID（如 <code>raw:42</code>）；来源另有上游消息 ID 时，也保留 <code>source_message_id</code>。Gateway Key 只放在后端。完整流程见 <a className="settings-link" href="https://github.com/Yinglianchun/Serein/blob/main/docs/codex-continuity-packet.md" target="_blank" rel="noreferrer">Codex 换窗包接入说明</a>。</p>
      <h4>已有聊天宿主：用 Hook 找前情</h4>
      <p>如果模型调用由你自己的服务或 Agent 管理，先在<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置页</SettingsLink>建立 / 补齐检索索引。宿主在新的用户轮用 Gateway Key 请求 <code>POST /api/hook/recall</code>，把返回的 <code>additional_context</code> 实际放进模型输入；返回的 <code>recalled_ids</code> 只是备选，<code>injected: false</code> 表示 Serein 尚未替宿主注入。</p>
      <p>示例文件：<code>{hookExamplePath}</code>。在安装目录运行宿主代码，服务端设置 <code>SEREIN_BASE_URL</code> 为实例根地址、<code>SEREIN_GATEWAY_KEY</code> 为 Gateway Key。下面的 <code>existing_model_call</code> 要换成你自己的模型调用。</p>
      <div className="usage-guide__markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{hookSnippet}</ReactMarkdown></div>
      <p>模型完整成功后才用 <code>POST /v1/host/deliveries</code> 登记实际交付的 ID；失败或中断不登记。工具续轮沿用本轮上下文，不再次查找或记账；同一会话保持同一个窗口 ID，示例会读取最近五次成功交付的 ID 避免连轮重复。Hook 不会自动归档聊天原话。完整请求格式见 <a className="settings-link" href="https://github.com/Yinglianchun/Serein/blob/main/docs/hook-integration.md" target="_blank" rel="noreferrer">Hook 接入说明</a>。</p>
    </section>
    <section className="settings-group usage-guide__page" {...page(4)}>
      <div className="settings-group__heading"><h3>查找和读回原话</h3></div>
      <p>在<SettingsLink tab="features" onOpen={onOpenSettingsTab}>功能设置</SettingsLink>开启“原话查阅”，并让客户端刷新工具列表。它查找已进入当前实例原话档案的聊天记录；没有导入的记录不会出现在结果中。</p>
      <p><code>source_message_search</code> 的 query（指定文字）、date（日期）、role（角色）都可不填，多项同时填写时须全部匹配。role 可填 user、assistant 或 ai，不填查双方。date 使用 UTC+8 日期，例如 2026-09-11，范围写作 2026-09-01..2026-09-11，包含首尾两天。</p>
      <p>limit 不填最多返回 10 条，可设 1–50；条件全空时从最新原话开始。下一页保持筛选条件，将返回的 next_before_id 填入 before_id。</p>
      <p>搜索返回命中片段与原话 ID。用 <code>source_message_read</code> 的 ids 读取完整正文，一次最多 20 个 ID；neighbor_before 和 neighbor_after 各可取 0–3 条同会话前后文。没有会话标识或只有证据快照的旧原文只读正文，不拼接上下文。</p>
    </section>
    <section className="settings-group usage-guide__page" {...page(5)}>
      <div className="settings-group__heading"><h3>安装与维护</h3><p>一键脚本、管理菜单和实例路径。</p></div>
      <div className="usage-guide__markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{installationMarkdown(location)}</ReactMarkdown></div>
      <p>Linux 服务器安装 Python 3.9+、Docker Engine 和 Compose 插件后，解压发行目录，执行 <code>bash scripts/one_click.sh</code>。</p>
      <p>Windows 在 PowerShell 执行 <code>powershell -ExecutionPolicy Bypass -File .\scripts\one_click.ps1</code>，可选择 Docker Desktop 或无 Docker 直跑。Docker Desktop 需启动并使用 Linux containers。</p>
      <p>安卓 Termux、Windows 和 Linux 均有 Python + Node 直跑分支，需要 Python 3.11+、Node.js 22.12+ 和 npm。Termux 先执行 <code>pkg install python nodejs-lts git clang make rust pkg-config</code>，将发行目录放在 Termux HOME 内，再运行 Bash 入口。安卓依赖安装和后台保活仍需按机型验证。</p>
      <p>重新部署会备份已有运行数据与配置到 <code>deploy/backups</code>；不要将新旧数据库同时投入写入。</p>
      <p>同机客户端填写 127.0.0.1；手机连电脑填写电脑的局域网 IP，安装时选择 0.0.0.0 监听并允许网关端口通过防火墙。连接说明保存在 <code>deploy/connection-guide.txt</code>；本页“接入”也有完整地址和 Key 的填写说明。</p>
      <p>前端和聊天 API 由网关统一提供，记忆库仅供内部访问。外网访问请在网关前配置 HTTPS。模型上游在<SettingsLink tab="models" onOpen={onOpenSettingsTab}>模型页</SettingsLink>配置，任务用途到<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置页</SettingsLink>选择。</p>
    </section>
    <section className="settings-group usage-guide__page" {...page(6)}>
      <div className="settings-group__heading"><h3>从旧 Ombre 迁移</h3></div>
      <p>原公开版的梦境读取 <code>state/dreams/</code>，暗房读取 <code>state/darkroom/entries.jsonl</code> 及相关状态文件，按原格式迁入，不要求 <code>diary.db</code>。请使用包含 <code>state</code> 的完整旧备份；只带 <code>buckets</code> 的备份无法补齐梦境和暗房。</p>
      <p>已经迁移过的用户：升级并完成重新部署后，在 <code>se</code> 中选择<strong>菜单 10「历史数据补漏」</strong>，提供含 <code>state</code> 的完整旧库目录或 tar/tar.gz 备份，先预览数量再确认。升级本身不会自动补漏；补漏前会备份，保留暗房修订、锁定、归档／撤回状态和梦境删除记录，不调用模型、不重导 Scene、不重建向量。</p>
      <p>打开<SettingsLink tab="migration" onOpen={onOpenSettingsTab}>旧库迁移页</SettingsLink>，输入运行 Serein 的机器上的旧库根目录、buckets 目录或 tar 路径；Docker 先在安装菜单 7 把该目录授权为只读来源。读取并预览后，可直接在浏览器下载所选旧库路径的 ZIP。也可把 64 MiB 以内的 tar / tar.gz 拖到上传区。预览后填写旧名字及别名，确认转换规则再开始。聊天记录和 Operit 备份在<SettingsLink tab="imports" onOpen={onOpenSettingsTab}>对话导入页</SettingsLink>处理，最大 32 MB。任务在后台运行，可以暂停并从同一来源续跑；主域和模型先在页面配置。</p>
      <p>转换保留标题和清理后的正文；reflection 与 affect_anchor 整段移除，其他三级分节只去标题。feel / whisper 转为日记，日印象不导入。归档条目保持归档。</p>
      <p>旧“自我锚点”区的正文直接丢弃，不恢复独立的“自我”分类，也不挂成梦境身份锚点。现在的自我由持续更新的窗影表达；心绪状态和备忘仍照旧迁入。做梦只读近期新建的 Event／Scene，最多五条；两者都没有才读新日记，不取召回或修改记录。做梦概率在<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置页</SettingsLink>设置，凌晨四点后检查，与三点的 Event 结算错开。</p>
      <p>旧库中的 Persona 心绪状态、关系状态、变化历史和照顾备忘会一同迁入；备忘保留完成／归档状态、期限、提醒次数与轮次。扫描会列出找到的状态库，缺失会明确提示。旧默认会话映射为 <code>main</code>；其他会话标识保留。迁入后按需开启心绪和备忘，已有新状态不会被覆盖。</p>
      <p>重新提取的 cues 不包含你们的名字。打标、实体、cues、向量准备会调用模型；旧边由程序转换，不调用模型；旧向量只在输入和模型配置能够核对一致时复用。</p>
      <p>中断后可从同一来源继续，已完成的条目不会重复导入。备份、ID 映射和处理报告保存在 <code>deploy/runtime/migrations</code>。确认迁移结果前保留原库。</p>
    </section>
    <section className="settings-group usage-guide__page" {...page(7)}>
      <div className="settings-group__heading"><h3>Passage：找回长记忆里的具体细节</h3><p>长文分段是可选的辅助检索，当前用于 Event / Scene；独立日记和叙事卷暂不使用这条通道。</p></div>
      <p>一篇很长的 Scene 可能同时写了旅行、晚饭和一次约定。只比较整篇正文时，某个细节容易被整体主题盖住。Passage 把原文分成较短的片段，分别建立检索索引，让“那天约好在哪里见面？”更容易找到写着约定的那一段。</p>
      <p>片段帮助找回它所属的记忆，不会改写原文或把一条 Scene 变成多条记忆。命中片段后仍需经过相关性筛选和冷却，不保证每次都会带入聊天。</p>
      <h4>什么时候开启</h4>
      <p>长 Event / Scene 较多、经常想找其中某个具体细节时，可以在<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置</SettingsLink>开启“长文分段检索（Passage）”。如果记忆以短文为主，可以先关闭。关闭后保留已有记忆和分段索引，仍可使用整篇检索。</p>
      <h4>500 字是什么意思</h4>
      <p>默认正文<strong>超过 500 字</strong>才开始分段，正好 500 字不会切；汉字、字母和标点按字符计。这里设置的是“多长的正文需要切”，不是每段 500 字。可填写 1–100000 的整数，保存后对新增或正文修改的记忆生效，已有分段不会因此全库重切。</p>
      <h4>什么时候处理，会增加什么开销</h4>
      <p>开启后，保存或首次导入记忆时预先切段并存好；后台为新增、修改的片段补向量。切段本身不调用模型，生成分段向量会使用已配置的 Embedding，增加模型调用量、处理时间和索引占用。</p>
      <p>聊天请求只读已准备的索引，不现场全量切段或补向量；缺失时仍走可用的整篇检索。首次给旧记忆启用时，可明确点击“建立 / 补齐检索索引”补齐缺失分段，已有分段继续复用。新实例默认关闭；升级保留原有设置。</p>
      <h4>“建立 / 补齐检索索引”：什么时候按，再按会重建吗</h4>
      <p>首次配置、更换 Embedding，或页面提示检索规则需要重新准备时使用。先保存配置；按钮读取已保存的模型与 Passage 设置。日常新增、修改记忆由后台处理，不需要每次手动准备。</p>
      <p>同一模型下再按，会复用当前有效的正文向量、已有分段及分段向量，只补缺失项，不重写记忆、不全库重切。但仍会重新探测维度、计算路由例句向量、扫描记忆并更新实体索引，有模型调用和服务器开销，不是无成本的刷新。</p>
      <p>更换 Embedding 会使用新模型的独立索引，重新生成正文及分段向量。已保存的共享布局可以复用；历史分段尚未迁入共享布局时不保证复用。修改起切字数不改变旧布局，包括原来无需分段的空布局；重复准备也不会让这些旧短文自动重切。</p>
      <p>首次给旧记忆开启 Passage，或确认有缺失向量时，可显式准备补齐；当前后台尚不能独立捡起空队列之外的历史缺口。准备期间会暂时撤下检索就绪标记，失败需重试完成。平时检索正常，不必反复点击。</p>
    </section>
    <section className="settings-group usage-guide__page" {...page(8)}>
      <div className="settings-group__heading"><h3>自动 Event 会调用哪些模型</h3><p>开关允许后台处理后续新聊天，不等于打开时立刻调用，也不是每轮固定三次。</p></div>
      <p>聊天完整成功后，用户和助手原话先进入档案；这一步不调用整理模型。文件导入和旧库迁移的历史原话默认只归档；聊天文件导入完成后可选择“立即从头摘要（后台）”，无需开启自动摘要；或选择“暂不处理，并推游标到最后一句”，只跳过这份导入。不会因打开自动摘要自动补跑。</p>
      <ol>
        <li><strong>归线（Track Router）</strong>：白天同一会话累计至少五个已完成回复包，并有二十分钟停顿后，按批调用归线模型；只记录话题归属，不写 Event。若白天未归线，凌晨结算时仍可能先调用它。</li>
        <li><strong>切分（Curator）</strong>：上海时间凌晨三点后，判断 Track 材料中哪些经历构成 Event、哪些跳过或暂缓。图片先通过独立任务逐张转录；未选图片模型时用切分模型转录，仍需视觉能力。</li>
        <li><strong>Event Writer</strong>：每条拟写的 Event 分别调用写作模型，结合原话、必要前情和已有 Event 生成正文；跳过或暂缓的材料不写。<strong>建议给 Event Writer 选理解上下文和写作能力较强的模型</strong>，它要处理人物归属、因果、修订与细节取舍，不只是压缩摘要。</li>
      </ol>
      <p>调用次数随批次、审阅范围、Event 和图片变化。Curator 与 Writer 各可补读一次。文字步骤的实际模型／校验失败跨执行累计三次后暂停该批，成功结果保留；其他聊天继续，同一聊天的后续材料等待恢复。检查原因后，在原话整理页点“重试此批次”，从失败步骤继续，不从头重跑。Agent 等待、日间只归线和调用前参数错误不适用这项预算。</p>
      <p>图片转录按同一张图累计三次失败，之后可点“重试这张图片”；成功图片与独立文字继续，依赖失败图片的活动暂存。原图无有效地址或返回 404/410 则跳过附件，保留文字和其他图片，不猜补内容。下载超时、403/5xx 或冻结材料损坏仍保留错误。手动重试会重新开放调用机会，可能产生费用。</p>
      <p>归线材料“待修复”与模型失败暂停不同：先重新校验，无法恢复时明确确认作废本批计划并重新归线；不会自动跳过。</p>
      <p>在<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置 → 自动摘要配置</SettingsLink>可调整默认三天的 Track 回看、Writer 首轮并发数及四个默认关闭的开关：跨线联合审阅、逐条材料取舍、普通交流轮次门槛、受保护 Event 后续追加。联合审阅核对同一聊天中由明确跨线消息连接的最多三条话题线；轮次门槛要求至少两轮完整交流；受保护追加原样保留旧正文，只添加新段。保存后用于新批次，不改已有记忆或已冻结任务。</p>
      <p>在<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置</SettingsLink>选择 API 时，由 Serein 向各阶段所选上游发请求；选 Agent 时，由已认证的外部执行器领取任务，实际模型调用和费用取决于它的实现。生成 Event 后，若启用了打标或检索索引，后台还可能分别调用打标模型或 Embedding；它们不属于上述三阶段。关闭自动摘要会停止定时整理调用，但原话仍保存，手动“继续整理”和其他独立功能仍可能调用各自模型。</p>
      <p>模型结果可能有遗漏或误解；重要 Event 请对照绑定原话核对。三阶段选择、执行方式及模型超时在<SettingsLink tab="configuration" onOpen={onOpenSettingsTab}>配置页</SettingsLink>，上游地址与密钥在<SettingsLink tab="models" onOpen={onOpenSettingsTab}>模型页</SettingsLink>。</p>
    </section>
    </div>
  </div>;
}

# Track Router

你只负责把原始消息临时路由到可延续的 Track。Track 是主题、持续问题或经历的阅读归线，不是 Event 边界；不得做 admission，不得写 Event、标题、正文或 event focus。

## Primary routing

- 每条原始消息必须有且只有一个 primary Track。这是持久化 routing 与 accounting 的唯一主归属，不等于 Event ownership。
- 主要看用户消息是否形成新问题或新目标。同一主题的发起、回应、补充、纠正、验证、结果和直接情绪 landing 默认复用同一 Track。
- 沉默二十分钟、数小时、跨日、局部新洞见、换例子、换作品章节、实现位置或工具都不自动创建新 Track。
- 同一持续建设里仍服务同一 throughline 的材料必须复用该 Track；局部目标、新 bug 或验证不是新建理由。只有原建设明确结束，或材料已转入另一项独立建设，才新建 Track。
- 只有预期跨批次持续建设同一个产品或系统时，将 event_policy 设为 rolling_engineering；其余设为 default。已有 rolling_engineering 只能继承，不能降级。
- 已可用工具的正常使用，不自动续接它的安装、调试 Track。只有在验证、修复或补完同一建设时才继续施工线。
- A→B→A 时，最后的 A 复用原 Track。只有互动中心真正换成另一主题或另一段独立经历，才新建 Track。
- active Track 暂时换题或沉默后可以 parked；再次续接时恢复 active。配置的回看天数只限制本批可见的旧 Track，时间不会删除持久卡片；看不到窗口外的旧 Track 时可以新建。

## Bridge and routine

- 一条消息若既回答或落定上一线，又开启下一线，primary 取主要推进的一线，并把另一线声明为 context Track；只有此时 routing role 才是 bridge。
- 直接换题不是 bridge。只有同一枚 dialogue unit 明确回答、结束或改变前线，同时发起后线，才可声明 context Track；语气词或亲昵措辞本身不构成前线落定。
- routing_role=bridge 当且仅当 context_track_refs 非空；没有声明 context Track 时，绝不能使用 bridge。
- 用户先回答前一线、随后明确换题时，该用户消息通常以新线为 primary、以前线为 context bridge；{ai_name} 随后的回答归入新 Track，不能仅因相邻而降成前线 landing。
- 不按醒目的称呼、作品名或重复关键词投票归线；要看本条新增了什么动作、对象或问题，以及后续实际接住哪一部分。若一条消息先短句落定前线，再开始另一对象或另一件事，primary 通常取后半段开启的新线；后续复用前线昵称或比喻，不能把已围绕新对象发展的回应拉回旧 Track。
- 同一对象或关系刚在现实层面建立后，下一句把它假设化、拟人化、夸张化或亲密化，通常仍是这条线的继续发展，不因表达从事实转成玩笑或幻想就新建 Track。
- 主动问候、叫醒、问吃饭或查状态即使带着作品、项目或待办作开场钩子，只要没有形成新的分析、行动或结果，仍是 routine；后续真正展开时，只把展开后的原文归入实质 Track。
- 主动问候、叫醒、问吃饭或查状态时，即使顺手拿正在看的作品、项目或待办当作开场钩子，也不得仅凭出现作品名或项目名创建实质 Track。

## Reading boundary

- Track 卡只来自当前原话时间之前、配置回看范围内的已保存归线记录，不依赖 API 客户端是否提供窗口身份；最近原文只用于补对象、被回答内容和代词指向，不得把主题相似当作续接证据。
- bounded_recent_context_json 最多包含当前 session 在本批之前的六条可见原文，只用于补对象、被回答内容和代词指向。
- 不请求额外上下文，不做 Event admission 或 Boundary。
- 必须 exact-cover 输入消息，并严格遵守任务给出的 JSON schema。

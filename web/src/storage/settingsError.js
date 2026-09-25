const VALIDATION_FALLBACK = "配置未保存，请检查填写内容后重试。";
const UNKNOWN_VALIDATION = "此项设置未通过校验，请检查取值或格式";
const MAX_VISIBLE_ERRORS = 5;

// Never echo arbitrary loc segments: dictionary keys can contain submitted data.
const FIELD_LABELS = {
  pipeline: "自动摘要", recall: "记忆检索", upstreams: "上游", upstream: "聊天与功能",
  models: "模型", assignments: "功能模型", features: "功能开关", identity: "身份",
  dream: "梦境", resume: "接续记忆", clock: "时间", tagging: "主域配置",
  max_prompt_chars: "完整提示词字符上限", max_input_chars: "原话批次字符上限",
  timeout_seconds: "模型读取超时（秒）", event_writer_concurrency: "Event Writer 首轮并发数",
  track_lookback_days: "归线 Track 回看天数", execution_mode: "执行方式", auto_enabled: "自动整理",
  joint_review_enabled: "跨线联合审阅", material_review_enabled: "逐条材料取舍",
  round_gate_enabled: "普通交流轮次门槛", append_protected_enabled: "受保护 Event 后续追加",
  direct_threshold: "直接召回阈值", body_candidate_threshold: "正文候选扩展门槛",
  cue_candidate_threshold: "线索候选扩展门槛", passages_enabled: "长文分段检索",
  passage_min_chars: "长文起切字数", base_url: "接口地址", api_key: "API 密钥",
  api_key_env: "密钥环境变量名", model: "模型名称", upstream_model: "上游模型名称",
  name: "名称", label: "显示名称", id: "标识", dimension: "向量维度", protocol: "接口格式",
  prompt_cache: "提示词缓存策略", prompt_cache_retention: "缓存保留时间",
  default_model: "默认模型", query_instruction: "查询指令", document_instruction: "文档指令",
  anthropic_version: "Anthropic API 版本", anthropic_beta: "Anthropic Beta 选项",
  daily_probability: "每日做梦概率", main_prompt: "主模型 Prompt",
  user_name: "用户名称", ai_name: "AI 名称", user_description: "用户介绍", ai_description: "AI 介绍",
  meeting_date: "相遇日期", timezone: "时区", domains: "主域列表", key: "主域标识",
  description: "描述", policy: "主域策略", expected_version: "设置版本",
};

const CONSTRAINT_MESSAGES = {
  less_than_equal: ["le", "不能大于 ", ""],
  less_than: ["lt", "必须小于 ", ""],
  greater_than_equal: ["ge", "不能小于 ", ""],
  greater_than: ["gt", "必须大于 ", ""],
  multiple_of: ["multiple_of", "必须为 ", " 的倍数"],
  string_too_short: ["min_length", "至少需要 ", " 个字符"],
  string_too_long: ["max_length", "不能超过 ", " 个字符"],
  too_short: ["min_length", "至少需要 ", " 项"],
  too_long: ["max_length", "不能超过 ", " 项"],
};

const TYPE_MESSAGES = {
  missing: "此项为必填",
  int_type: "请填写整数", int_parsing: "请填写整数", int_from_float: "请填写整数",
  float_type: "请填写有效数字", float_parsing: "请填写有效数字",
  finite_number: "请填写有限的有效数字",
  bool_type: "开关值无效，请重新选择", bool_parsing: "开关值无效，请重新选择",
  string_type: "请填写文本", list_type: "应为列表", dict_type: "应为设置对象",
  literal_error: "选项无效，请重新选择", enum: "选项无效，请重新选择",
  string_pattern_mismatch: "格式不正确，请检查填写内容",
  extra_forbidden: "包含当前版本不支持的字段，请刷新后重试",
  json_invalid: "请求内容不是有效的 JSON，请刷新后重试",
};

// Match only fixed validator messages from api/settings.py, never arbitrary msg/ctx.error.
const VALIDATOR_MESSAGES = {
  "Use an HTTP(S) API base URL without credentials, query or fragment": "请填写不含账号密码、查询参数或片段的 HTTP(S) 接口地址",
  "Each upstream needs a model list or default_model": "请至少配置一个模型或默认模型",
  "Upstream names must be unique": "上游名称不能重复",
  "Upstream IDs must be unique": "上游标识不能重复",
  "Model IDs must be unique": "模型标识不能重复",
  "Explicit Anthropic cache breakpoints require the Messages format": "显式 Anthropic 缓存需要 Messages 接口格式",
  "Prompt cache key requires the Chat Completions format": "提示词缓存键需要 Chat Completions 接口格式",
  "Cache retention does not match the selected cache strategy": "缓存保留时间与所选缓存策略不匹配",
  "Use a calendar date in YYYY-MM-DD format": "请使用 YYYY-MM-DD 格式的有效日期",
  "Unknown time zone": "时区名称无效",
  "Unknown optional feature": "包含当前版本不支持的功能开关，请刷新后重试",
  "Unknown task assignment": "包含当前版本不支持的功能模型分配，请刷新后重试",
  "Domain keys must be unique": "主域标识不能重复",
  "Invalid memory ID": "记忆标识无效",
  "Recall settings cannot be null": "召回设置不能为空",
};

function validationLocation(loc) {
  if (!Array.isArray(loc)) return "";
  const path = loc[0] === "body" ? loc.slice(1) : loc;
  return path.map(part => {
    if (Number.isSafeInteger(part) && part >= 0) return `第 ${part + 1} 项`;
    return typeof part === "string" && Object.hasOwn(FIELD_LABELS, part) ? FIELD_LABELS[part] : "其他字段";
  }).join(" / ");
}

function validationReason(issue) {
  const type = typeof issue.type === "string" ? issue.type : "";
  if (Object.hasOwn(CONSTRAINT_MESSAGES, type)) {
    const [key, before, after] = CONSTRAINT_MESSAGES[type];
    const limit = issue.ctx?.[key];
    // input, expected literals, regexes and error objects can all contain secrets.
    return Number.isFinite(limit) ? `${before}${limit}${after}` : UNKNOWN_VALIDATION;
  }
  if (Object.hasOwn(TYPE_MESSAGES, type)) return TYPE_MESSAGES[type];
  if (type === "value_error" && typeof issue.msg === "string") {
    const message = issue.msg.replace(/^Value error, /, "");
    if (Object.hasOwn(VALIDATOR_MESSAGES, message)) return VALIDATOR_MESSAGES[message];
  }
  return UNKNOWN_VALIDATION;
}

function validationDetails(detail) {
  if (!Array.isArray(detail)) return "";
  const messages = [];
  for (const issue of detail) {
    if (!issue || typeof issue !== "object" || Array.isArray(issue)) continue;
    if (!(typeof issue.type === "string" && issue.type.trim())
      && !(typeof issue.msg === "string" && issue.msg.trim())) continue;
    const location = validationLocation(issue.loc);
    const reason = validationReason(issue);
    messages.push(location ? `${location}：${reason}` : reason);
  }
  const unique = [...new Set(messages)];
  if (!unique.length) return "";
  const visible = unique.slice(0, MAX_VISIBLE_ERRORS);
  if (unique.length > MAX_VISIBLE_ERRORS) visible.push(`另有 ${unique.length - MAX_VISIBLE_ERRORS} 项校验错误，请修正后重试`);
  return `配置未保存：${visible.join("；")}。`;
}

export async function settingsResponseError(response) {
  const payload = await response.json().catch(() => null);
  const detail = typeof payload?.detail === "string" ? payload.detail.trim() : "";
  if (response.status === 409) return new Error(detail || "设置已更新，请刷新后重试。");
  if ([400, 422].includes(response.status)) {
    return new Error(detail || validationDetails(payload?.detail) || VALIDATION_FALLBACK);
  }
  return new Error(detail || "设置服务暂不可用，请检查后端连接后重试。");
}

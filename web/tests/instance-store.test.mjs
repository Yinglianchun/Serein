import {test} from "node:test";
import assert from "node:assert/strict";
import {settingsResponseError} from "../src/storage/settingsError.js";

function failedResponse(status, payload, jsonError = null) {
  return {
    ok: false,
    status,
    json: async () => {
      if (jsonError) throw jsonError;
      return payload;
    },
  };
}

test("settings save shows safe backend validation details", async () => {
  for (const [status, detail] of [
    [400, "API 自动摘要需要为三个阶段选择模型"],
    [422, "开启聊天图片转录前，请先选择“图片转录”模型"],
    [503, "设置服务正在重启，请稍后重试"],
  ]) {
    const error = await settingsResponseError(failedResponse(status, {detail}));
    assert.equal(error.message, detail);
  }
});

test("settings save never dumps malformed validation or backend error payloads", async () => {
  const validation = await settingsResponseError(failedResponse(422, {
    detail: [{loc: ["body", "upstreams", 0, "api_key"], input: "provider-secret"}],
  }));
  assert.equal(validation.message, "配置未保存，请检查填写内容后重试。");
  assert.doesNotMatch(validation.message, /provider-secret|api_key/);

  const unavailable = await settingsResponseError(failedResponse(502, {detail: {token: "backend-secret"}}));
  assert.equal(unavailable.message, "设置服务暂不可用，请检查后端连接后重试。");
  assert.doesNotMatch(unavailable.message, /backend-secret/);
});

test("settings conflict keeps its refresh semantics", async () => {
  const fallback = await settingsResponseError(failedResponse(409, {}));
  assert.equal(fallback.message, "设置已更新，请刷新后重试。");
  const detailed = await settingsResponseError(failedResponse(409, {detail: "设置已在其他页面更新，请刷新后重试"}));
  assert.equal(detailed.message, "设置已在其他页面更新，请刷新后重试");
});

test("settings save identifies the pipeline limit instead of blaming models", async () => {
  const error = await settingsResponseError(new Response(JSON.stringify({
    detail: [{
      type: "less_than_equal",
      loc: ["body", "pipeline", "max_prompt_chars"],
      msg: "Input should be less than or equal to 200000",
      input: 1000000,
      ctx: {le: 200000},
    }],
  }), {status: 422}));
  assert.equal(error.message, "配置未保存：自动摘要 / 完整提示词字符上限：不能大于 200000。");
  assert.doesNotMatch(error.message, /核对模型|接口地址|1000000/);
});

test("settings validation uses the responding backend's limit, not a fixed ceiling", async () => {
  for (const status of [400, 422]) {
    const error = await settingsResponseError(failedResponse(status, {
      detail: [{type: "less_than_equal", loc: ["body", "pipeline", "max_prompt_chars"], ctx: {le: 4000000}}],
    }));
    assert.equal(error.message, "配置未保存：自动摘要 / 完整提示词字符上限：不能大于 4000000。");
  }
});

test("settings validation formats numeric and length constraints", async () => {
  for (const [type, ctx, expected] of [
    ["greater_than_equal", {ge: 8000}, "不能小于 8000"],
    ["greater_than", {gt: 0}, "必须大于 0"],
    ["less_than", {lt: 1}, "必须小于 1"],
    ["less_than_equal", {le: 0.65}, "不能大于 0.65"],
    ["string_too_short", {min_length: 1}, "至少需要 1 个字符"],
    ["string_too_long", {max_length: 200}, "不能超过 200 个字符"],
    ["too_short", {min_length: 1}, "至少需要 1 项"],
    ["too_long", {max_length: 500}, "不能超过 500 项"],
    ["multiple_of", {multiple_of: 2}, "必须为 2 的倍数"],
  ]) {
    const error = await settingsResponseError(failedResponse(422, {detail: [{type, ctx}]}));
    assert.equal(error.message, `配置未保存：${expected}。`);
  }
});

test("settings validation handles common field types without echoing inputs", async () => {
  for (const [type, expected] of [
    ["missing", "此项为必填"],
    ["int_type", "请填写整数"],
    ["int_parsing", "请填写整数"],
    ["int_from_float", "请填写整数"],
    ["float_type", "请填写有效数字"],
    ["float_parsing", "请填写有效数字"],
    ["finite_number", "请填写有限的有效数字"],
    ["bool_type", "开关值无效，请重新选择"],
    ["bool_parsing", "开关值无效，请重新选择"],
    ["string_type", "请填写文本"],
    ["list_type", "应为列表"],
    ["dict_type", "应为设置对象"],
    ["literal_error", "选项无效，请重新选择"],
    ["enum", "选项无效，请重新选择"],
    ["string_pattern_mismatch", "格式不正确，请检查填写内容"],
    ["extra_forbidden", "包含当前版本不支持的字段，请刷新后重试"],
    ["json_invalid", "请求内容不是有效的 JSON，请刷新后重试"],
  ]) {
    const error = await settingsResponseError(failedResponse(422, {
      detail: [{type, msg: "provider-secret", input: "provider-secret", ctx: {expected: "provider-secret"}}],
    }));
    assert.equal(error.message, `配置未保存：${expected}。`);
  }
});

test("settings validation identifies nested upstream and model rows with one-based indexes", async () => {
  const error = await settingsResponseError(failedResponse(422, {
    detail: [{type: "missing", loc: ["body", "upstreams", 0, "models", 1, "upstream_model"]}],
  }));
  assert.equal(error.message, "配置未保存：上游 / 第 1 项 / 模型 / 第 2 项 / 上游模型名称：此项为必填。");
});

test("settings validation reports more than one invalid setting and removes duplicates", async () => {
  const prompt = {type: "greater_than_equal", loc: ["body", "pipeline", "max_prompt_chars"], ctx: {ge: 8000}};
  const recall = {type: "less_than_equal", loc: ["body", "recall", "direct_threshold"], ctx: {le: 1}};
  const error = await settingsResponseError(failedResponse(422, {detail: [prompt, null, recall, prompt]}));
  assert.equal(error.message, "配置未保存：自动摘要 / 完整提示词字符上限：不能小于 8000；记忆检索 / 直接召回阈值：不能大于 1。");
});

test("settings validation limits the visible error list without silently dropping the rest", async () => {
  const detail = Array.from({length: 7}, (_, index) => ({type: "missing", loc: ["body", "models", index, "model"]}));
  const error = await settingsResponseError(failedResponse(422, {detail}));
  assert.match(error.message, /第 1 项.*第 5 项/);
  assert.doesNotMatch(error.message, /第 6 项|第 7 项/);
  assert.match(error.message, /另有 2 项校验错误，请修正后重试。$/);
});

test("settings validation uses only safe labels and finite numeric constraint metadata", async () => {
  const error = await settingsResponseError(failedResponse(422, {
    detail: [{
      type: "string_too_long", loc: ["body", "upstreams", 0, "api_key"],
      msg: "Rejected provider-secret", input: "provider-secret",
      ctx: {max_length: 4000, error: "backend-secret"}, url: "https://example.invalid/provider-secret",
    }],
  }));
  assert.equal(error.message, "配置未保存：上游 / 第 1 项 / API 密钥：不能超过 4000 个字符。");
  assert.doesNotMatch(error.message, /provider-secret|backend-secret|example.invalid/);

  for (const le of ["provider-secret", {token: "provider-secret"}, null, Infinity, NaN]) {
    const invalid = await settingsResponseError(failedResponse(422, {
      detail: [{type: "less_than_equal", loc: ["body", "pipeline", "max_prompt_chars"], ctx: {le}}],
    }));
    assert.equal(invalid.message, "配置未保存：自动摘要 / 完整提示词字符上限：此项设置未通过校验，请检查取值或格式。");
  }
});

test("settings validation never echoes arbitrary dictionary keys or custom validator messages", async () => {
  const error = await settingsResponseError(failedResponse(422, {
    detail: [{type: "value_error", loc: ["body", "upstreams", 0, "provider-secret"],
      msg: "Value error, rejected provider-secret", input: {api_key: "provider-secret"}, ctx: {error: "provider-secret"}}],
  }));
  assert.equal(error.message, "配置未保存：上游 / 第 1 项 / 其他字段：此项设置未通过校验，请检查取值或格式。");
  assert.doesNotMatch(error.message, /provider-secret|api_key/);
});

test("settings validation explains recognized safe custom validators", async () => {
  for (const [msg, expected] of [
    ["Value error, Upstream names must be unique", "上游名称不能重复"],
    ["Value error, Each upstream needs a model list or default_model", "请至少配置一个模型或默认模型"],
    ["Value error, Use an HTTP(S) API base URL without credentials, query or fragment", "请填写不含账号密码、查询参数或片段的 HTTP(S) 接口地址"],
  ]) {
    const error = await settingsResponseError(failedResponse(422, {detail: [{type: "value_error", msg}]}));
    assert.equal(error.message, `配置未保存：${expected}。`);
  }
});

test("settings validation ignores inherited object properties", async () => {
  for (const value of ["toString", "constructor", "__proto__"]) {
    const error = await settingsResponseError(failedResponse(422, {
      detail: [{type: value, loc: ["body", value], msg: value}],
    }));
    assert.equal(error.message, "配置未保存：其他字段：此项设置未通过校验，请检查取值或格式。");
  }
});

test("settings validation tolerates unknown errors and missing or malformed locations", async () => {
  for (const loc of [undefined, null, "provider-secret", [], ["body"]]) {
    const error = await settingsResponseError(failedResponse(422, {
      detail: [{loc, msg: "unrecognized provider-secret"}],
    }));
    assert.equal(error.message, "配置未保存：此项设置未通过校验，请检查取值或格式。");
  }
});

test("settings validation falls back neutrally for empty, malformed and non-JSON responses", async () => {
  for (const status of [400, 422]) {
    for (const payload of [undefined, null, [], "html", {}, {detail: "  "}, {detail: {}}, {detail: []},
      {detail: [null, false, [], "provider-secret", {}, {type: 3, msg: {}}, {msg: " "}]}]) {
      const error = await settingsResponseError(failedResponse(status, payload));
      assert.equal(error.message, "配置未保存，请检查填写内容后重试。");
    }
    const error = await settingsResponseError(failedResponse(status, null, new SyntaxError("invalid JSON")));
    assert.equal(error.message, "配置未保存，请检查填写内容后重试。");
  }
});

test("non-validation responses retain conflict and service fallback behavior", async () => {
  for (const [status, expected] of [[409, "设置已更新，请刷新后重试。"], [502, "设置服务暂不可用，请检查后端连接后重试。"]]) {
    for (const payload of [{detail: [{type: "missing", loc: ["body", "models"]}]}, null]) {
      const error = await settingsResponseError(failedResponse(status, payload));
      assert.equal(error.message, expected);
    }
    const error = await settingsResponseError(failedResponse(status, null, new SyntaxError("invalid JSON")));
    assert.equal(error.message, expected);
  }
  const detailed = await settingsResponseError(failedResponse(422, {detail: "  请先选择模型  "}));
  assert.equal(detailed.message, "请先选择模型");
});

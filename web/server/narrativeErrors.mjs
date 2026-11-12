import { sereinBackendTimedOut } from './sereinBackend.mjs';

export function narrativePreviewFailure(error) {
  const text = String(error?.message || '');
  const publicReasons = {
    narrative_identity_unavailable: '无法读取实例姓名与 Writer 配置，请稍后重试。',
    narrative_writer_disabled_main_model_authoring: '已启用主模型叙事工具，请通过主模型书写，或在设置中切换到网页 Writer。',
    narrative_writer_disabled: '网页 Writer 尚未启用，请在设置中启用并配置书写模型。',
    narrative_writer_command_not_configured: '没有配置本地 Writer 命令，请检查 SEREIN_WRITER_COMMAND。',
    narrative_writer_model_not_configured: '没有选择 Writer 模型，请在设置中选择书写模型。',
    narrative_writer_auth_failed: 'Writer 的模型凭据失效，请在设置中检查对应上游的密钥。',
    narrative_writer_rate_limited: 'Writer 的模型额度不足或触发限流，请检查额度后重试。',
    narrative_writer_model_unavailable: '找不到所选 Writer 模型或接口，请检查模型与上游地址。',
    narrative_writer_request_rejected: '上游拒绝 Writer 请求，请检查输出格式、输入与接口兼容性。',
    narrative_writer_upstream_unavailable: 'Writer 上游服务暂不可用，请稍后重试。',
    narrative_writer_invalid_output: 'Writer 返回的正文格式或自检结果不符合要求，请重新书写。',
    narrative_writer_input_too_large: '材料总量超过模型容量，请减少材料后重试。',
    narrative_upstream_failed: 'Writer 上游请求失败，请检查所选模型与上游服务。',
    narrative_runner_failed: '本地 Writer 进程中断或输出超过限制，请检查 runner 日志后重试。',
  };
  if (publicReasons[text]) return { statusCode: 502, payload: {
    status: 'error', reason: text, message: publicReasons[text],
    ...(Number.isInteger(error?.upstreamStatus) ? { upstream_status: error.upstreamStatus } : {}),
    writes_performed: [],
  } };
  let reason = 'narrative_preview_failed';
  let message = '书写服务暂不可用，请稍后重试。';
  let statusCode = 502;
  if (text === 'narrative_writer_timeout') {
    statusCode = 504; reason = 'narrative_writer_timeout';
    message = '叙事卷生成超过 5 分钟，请重新书写。';
  } else if (sereinBackendTimedOut(error)) {
    statusCode = 504; reason = 'narrative_input_timeout';
    message = '读取材料或模型设置超时，尚未完成书写，请稍后重试。';
  } else if (/role_not_installed|role_rules_empty/.test(text)) {
    reason = 'narrative_writer_not_installed'; message = '服务器缺少 Writer 规则或输出格式文件，需要修复安装。';
  } else if (/model_settings_unavailable|invalid_narrative_writer_model/.test(text)) {
    reason = 'narrative_writer_settings_invalid'; message = '无法读取有效的 Writer 模型配置，请检查设置中的书写模型。';
  } else if (error?.code === 'ENOENT') {
    reason = error.syscall === 'open' ? 'narrative_writer_not_installed' : 'narrative_writer_cli_missing';
    message = error.syscall === 'open' ? '服务器缺少 Writer 规则或输出格式文件，需要修复安装。'
      : '没有找到配置的 Writer 程序，请检查 runner 的安装路径。';
  } else if (/401|403|unauthorized|authentication|not logged in|login required|token.*expired/i.test(text)) {
    reason = 'narrative_writer_auth_failed'; message = 'Writer 的模型登录或凭据已失效，需要在服务器重新登录。';
  } else if (/429|rate.?limit|quota|usage limit/i.test(text)) {
    reason = 'narrative_writer_rate_limited'; message = 'Writer 的模型额度不足或触发限流，请检查额度后重试。';
  } else if (/context.*(length|window)|too many tokens|input.*too (long|large)/i.test(text)) {
    reason = 'narrative_writer_input_too_large'; message = '材料总量超过模型容量，请减少材料后重试。';
  } else if (/unexpected argument|unrecognized option|unknown (flag|option)/i.test(text)) {
    reason = 'narrative_writer_cli_incompatible'; message = '服务器 Codex 版本不支持 Writer 启动参数，需要更新程序。';
  } else if (error instanceof SyntaxError || /narrative_writer_(result|issues|review|sufficient|insufficient)/.test(text)) {
    reason = 'narrative_writer_invalid_output'; message = 'Writer 返回的正文格式或自检结果不符合要求，请重新书写。';
  } else if (/narrative_writer_codex_/.test(text)) {
    reason = 'narrative_writer_process_failed'; message = 'Writer 执行中断，未得到可保存正文，请重试；持续失败需检查服务器日志。';
  } else if (/backend_not_configured|dashboard_bridge_not_configured/.test(text)) {
    reason = 'narrative_backend_not_configured'; message = '服务器没有配置记忆服务连接，需要修复服务配置。';
  }
  return { statusCode, payload: { status: 'error', reason, message, writes_performed: [] } };
}

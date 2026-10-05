const skipLabels = {
  empty_body: '正文为空',
  requires_passage_index: '超过整篇向量字符上限',
  requires_token_passages: '超过模型 token 上限',
  changed_during_embedding: '生成期间正文已变更',
};

const count = value => Number.isInteger(value) && value >= 0 ? value : null;
const displayCount = value => count(value) ?? '未知';

function skippedText(skipped, changed = 0) {
  if (!skipped || typeof skipped !== 'object') return '未知';
  const entries = Object.entries(skipped).filter(([, value]) => count(value) > 0);
  if (count(changed) > 0) entries.push(['changed_during_embedding', changed]);
  return entries.length
    ? entries.map(([reason, value]) => `${skipLabels[reason] || reason} ${value}`).join('、')
    : '0';
}

function bodyCoverage(coverage) {
  if (!coverage || !coverage.event || !coverage.scene) return '整篇向量覆盖情况未知。';
  // The backend uses sparse counters for an empty collection.
  return [['event', 'Event'], ['scene', 'Scene']].map(([kind, label]) => {
    const row = coverage[kind];
    const readable = Object.keys(row).length === 0 ? 0 : count(row.readable);
    if (readable === null || (readable > 0 && (count(row.covered) === null || count(row.missing) === null))) {
      return `${label} 整篇向量覆盖情况未知。`;
    }
    return `${label} 整篇向量：已覆盖 ${readable ? row.covered : 0} / ${readable} 条可读记忆，缺失 ${readable ? row.missing : 0} 条。`;
  }).join(' ');
}

export function memoryPreparationSummary(result = {}) {
  const whole = result.vectors || {};
  const passages = result.passages || {};
  const parts = [
    '本次索引准备请求已完成。',
    `整篇向量：计划新增 ${displayCount(whole.planned)}，实际新增 ${displayCount(whole.embedded)}，跳过 ${skippedText(whole.skipped)}。`,
    bodyCoverage(whole.coverage),
  ];
  if (passages.status === 'disabled') {
    parts.push('长文分段检索未开启，本次未补充分段向量。超过整篇上限而被跳过的记忆，不能靠整篇向量匹配；如需检索长文细节，可自行开启并保存 Passage 后再次补齐索引。');
  } else if (result.passages) {
    parts.push(`分段向量：计划新增 ${displayCount(passages.planned)}，实际新增 ${displayCount(passages.embedded)}，跳过 ${skippedText(passages.skipped || (count(passages.changed) !== null ? {} : null), passages.changed)}。`);
    const coverage = passages.coverage;
    if (coverage && count(coverage.passages) !== null && count(coverage.covered) !== null && count(coverage.missing) !== null) {
      parts.push(`已有分段向量：已覆盖 ${coverage.covered} / ${coverage.passages} 段，缺失 ${coverage.missing} 段。此比例只统计已有分段，不代表所有长记忆均已分段。`);
    } else parts.push('分段向量覆盖情况未知。');
  } else parts.push('分段检索状态和向量覆盖情况未知。');
  parts.push('实际新增为 0 可能是复用了已有向量，也可能是跳过了内容；请以覆盖和跳过情况为准。向量覆盖不保证每次都会召回，召回规则仍然生效。');
  return parts.join(' ');
}

export function memoryAvailabilitySummary(config) {
  return config?.memory_ready
    ? '检索配置与路由检查通过；这不代表每条记忆已有向量。建立 / 补齐索引后，请查看覆盖和跳过情况。'
    : '检索配置尚未通过检查。选择并保存 embedding 和 reranker 后，点击下方建立 / 补齐检索索引；准备失败后需重试。';
}

export async function prepareMemoryIndex({fetchImpl = fetch, loadSettings}) {
  let message;
  try {
    const response = await fetchImpl('/__serein/settings/prepare-memory', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    if (!response.ok) throw new Error('prepare_failed');
    message = memoryPreparationSummary(await response.json());
  } catch {
    // A failed request has no reliable per-item failure count. Keep already
    // committed batches, but never describe the operation as fully prepared.
    message = '索引准备失败或结果未能确认，不能确认本次计划、成功和失败条数。已完成的向量批次可能已保留；请检查 embedding 模型、接口、密钥和服务日志后重试。准备期间检索就绪标记可能已撤下，需重试完成。';
  }
  try {
    return {config: await loadSettings(), message};
  } catch {
    return {config: null, message: `${message} 最新配置读取失败，请刷新页面确认检索状态；上面的结果仅代表本次请求。`};
  }
}

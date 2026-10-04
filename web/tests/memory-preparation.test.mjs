import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {memoryAvailabilitySummary, memoryPreparationSummary, prepareMemoryIndex} from '../src/memoryPreparation.js';

const receipt = (overrides = {}) => ({
  status: 'ready', dimension: 1024,
  vectors: {planned: 2, embedded: 2, skipped: {}, coverage: {
    event: {readable: 5, covered: 5, missing: 0}, scene: {},
  }},
  passages: {status: 'disabled', planned: 0, embedded: 0},
  ...overrides,
});

test('separates newly embedded vectors from full existing coverage', () => {
  const text = memoryPreparationSummary(receipt());
  assert.match(text, /计划新增 2，实际新增 2/);
  assert.match(text, /Event 整篇向量：已覆盖 5 \/ 5 条可读记忆，缺失 0/);
  assert.match(text, /Scene 整篇向量：已覆盖 0 \/ 0/);
  assert.doesNotMatch(text, /已准备好|检索已就绪|维度 1024/);
});

test('long body and token skips remain visible with disabled Passage', () => {
  const text = memoryPreparationSummary(receipt({vectors: {
    planned: 0, embedded: 0,
    skipped: {requires_passage_index: 3, requires_token_passages: 2, empty_body: 1},
    coverage: {event: {readable: 6, covered: 0, missing: 6}, scene: {}},
  }}));
  assert.match(text, /超过整篇向量字符上限 3/);
  assert.match(text, /超过模型 token 上限 2/);
  assert.match(text, /正文为空 1/);
  assert.match(text, /已覆盖 0 \/ 6 条可读记忆，缺失 6/);
  assert.match(text, /长文分段检索未开启/);
  assert.match(text, /自行开启并保存 Passage/);
});

test('successful repeated fill with zero new vectors is not called empty or failed', () => {
  const data = receipt();
  data.vectors.planned = data.vectors.embedded = 0;
  const text = memoryPreparationSummary(data);
  assert.match(text, /实际新增 0/);
  assert.match(text, /已覆盖 5 \/ 5/);
  assert.match(text, /可能是复用了已有向量/);
});

test('reports changed bodies and partially covered passages independently', () => {
  const data = receipt({passages: {planned: 4, embedded: 3, changed: 1,
    coverage: {passages: 8, covered: 7, missing: 1, owners: 2}}});
  data.vectors.skipped = {changed_during_embedding: 1};
  const text = memoryPreparationSummary(data);
  assert.match(text, /跳过 生成期间正文已变更 1/);
  assert.match(text, /分段向量：计划新增 4，实际新增 3/);
  assert.match(text, /已覆盖 7 \/ 8 段，缺失 1/);
  assert.match(text, /不代表所有长记忆均已分段/);
});

test('dimension and status alone never imply vector coverage', () => {
  const text = memoryPreparationSummary({status: 'ready', dimension: 1024});
  assert.match(text, /计划新增 未知，实际新增 未知，跳过 未知/);
  assert.match(text, /整篇向量覆盖情况未知/);
  assert.match(text, /分段检索状态和向量覆盖情况未知/);
  assert.doesNotMatch(text, /已准备好|检索已就绪/);
});

test('incomplete per-kind counts are unknown rather than zero', () => {
  const data = receipt();
  data.vectors.coverage.event = {readable: 3};
  assert.match(memoryPreparationSummary(data), /Event 整篇向量覆盖情况未知/);
});

test('route readiness help explicitly distinguishes config from vector coverage', () => {
  assert.match(memoryAvailabilitySummary({memory_ready: true}), /不代表每条记忆已有向量/);
  assert.match(memoryAvailabilitySummary({memory_ready: false}), /尚未通过检查/);
});

test('request uses saved settings without enabling Passage or changing limits', async () => {
  const config = {memory_ready: true, recall: {passages_enabled: false}};
  const result = await prepareMemoryIndex({
    fetchImpl: async (url, options) => {
      assert.equal(url, '/__serein/settings/prepare-memory');
      assert.equal(options.method, 'POST');
      assert.equal(options.body, '{}');
      return {ok: true, json: async () => receipt()};
    }, loadSettings: async () => config,
  });
  assert.equal(result.config, config);
  assert.equal(config.recall.passages_enabled, false);
  assert.match(result.message, /实际新增 2/);
});

for (const failure of ['http', 'network', 'json']) {
  test(`${failure} failure refreshes readiness without inventing item failure counts`, async () => {
    let refreshes = 0;
    const result = await prepareMemoryIndex({fetchImpl: async () => {
      if (failure === 'network') throw new Error('private upstream detail');
      return {ok: failure !== 'http', json: async () => {throw new Error('invalid json');}};
    }, loadSettings: async () => {refreshes++; return {memory_ready: false};}});
    assert.equal(refreshes, 1);
    assert.equal(result.config.memory_ready, false);
    assert.match(result.message, /准备失败或结果未能确认/);
    assert.match(result.message, /不能确认本次计划、成功和失败条数/);
    assert.match(result.message, /已完成的向量批次可能已保留/);
    assert.doesNotMatch(result.message, /private upstream detail|已准备好/);
  });
}

test('refresh failure preserves successful coverage receipt', async () => {
  const result = await prepareMemoryIndex({
    fetchImpl: async () => ({ok: true, json: async () => receipt()}),
    loadSettings: async () => {throw new Error('offline');},
  });
  assert.equal(result.config, null);
  assert.match(result.message, /已覆盖 5 \/ 5/);
  assert.match(result.message, /最新配置读取失败/);
});

test('component wires summaries and clears stale readiness during preparation', async () => {
  const source = await readFile(new URL('../src/components/ModelSettings.jsx', import.meta.url), 'utf8');
  assert.match(source, /memoryAvailabilitySummary\(config\)/);
  assert.match(source, /prepareMemoryIndex\(\{loadSettings:instanceSettings\}\)/);
  assert.match(source, /setConfig\(current=>\(\{\.\.\.current,memory_ready:false\}\)\)/);
  assert.match(source, /if\(result.config\)setConfig\(result.config\)/);
  assert.doesNotMatch(source, /记忆检索已准备好|检索已就绪。/);
});

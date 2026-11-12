import { test } from 'node:test';
import assert from 'node:assert/strict';
import { narrativeErrorMessage, narrativeRequest } from '../src/storage/narrativeErrors.js';
import { narrativePreviewFailure } from '../server/narrativeErrors.mjs';
import { loadNarrativeRolls } from '../src/storage/narrativeStore.js';
import { runNarrativeCodexTask } from '../server/narrativeCodexRunner.mjs';
import { fileURLToPath } from 'node:url';

test('storage errors and per-source causes stay distinct', () => {
  assert.match(narrativeErrorMessage({ detail: 'Memory storage unavailable' }, 503, '保存叙事线'), /保存叙事线失败.*记忆存储/);
  assert.match(narrativeErrorMessage({ reason: 'narrative_revision_changed' }, 409, '保存正文'), /已有新修改/);
  const message = narrativeErrorMessage({ reason: 'bound_material_unavailable', errors: [
    { source_type: 'diary', source_id: 7, reason: 'locked_or_unavailable' },
    { source_type: 'event', source_id: 'event_1', reason: 'source_snapshot_mismatch' },
  ] }, 409, '书写正文');
  assert.match(message, /日记 7：未解锁/);
  assert.match(message, /Event event_1：绑定原话/);
});

test('writer failures expose safe, specific causes without stderr', () => {
  const cases = [
    [new Error('narrative_writer_timeout'), 'narrative_writer_timeout'],
    [Object.assign(new Error('timeout'), { name: 'TimeoutError' }), 'narrative_input_timeout'],
    [new Error('narrative_writer_codex_1:401 secret-token'), 'narrative_writer_auth_failed'],
    [new Error('narrative_writer_codex_1:429 secret-token'), 'narrative_writer_rate_limited'],
    [new Error('narrative_writer_role_not_installed'), 'narrative_writer_not_installed'],
    [new Error('invalid_narrative_writer_model'), 'narrative_writer_settings_invalid'],
    [new SyntaxError('secret-token'), 'narrative_writer_invalid_output'],
    [Object.assign(new Error('secret-token'), { code: 'ENOENT' }), 'narrative_writer_cli_missing'],
    [new Error('narrative_writer_codex_2:unexpected argument secret-token'), 'narrative_writer_cli_incompatible'],
    ...['narrative_identity_unavailable', 'narrative_writer_disabled_main_model_authoring',
      'narrative_writer_disabled', 'narrative_writer_command_not_configured',
      'narrative_writer_model_not_configured', 'narrative_runner_failed'].map(reason => [new Error(reason), reason]),
  ];
  for (const [error, reason] of cases) {
    const result = narrativePreviewFailure(error);
    assert.equal(result.payload.reason, reason);
    assert(!JSON.stringify(result).includes('secret-token'));
  }
});

test('public API Writer preserves safe upstream categories through preview errors', async () => {
  for (const [status, reason] of [[401, 'narrative_writer_auth_failed'],
    [429, 'narrative_writer_rate_limited'], [413, 'narrative_writer_input_too_large'],
    [400, 'narrative_writer_request_rejected'], [422, 'narrative_writer_request_rejected'],
    [404, 'narrative_writer_model_unavailable']]) {
    const backend = async path => path === '/v1/settings'
      ? { ok: true, payload: { identity: { user_name: 'Nori', ai_name: 'Atlas' },
          upstream: { writer_enabled: true }, models: [{ id: 'writer', model: 'synthetic' }], assignments: { writer: 'writer' } } }
      : { ok: false, status: 502, payload: { reason, upstream_status: status, message: 'synthetic-private-provider-body' } };
    await assert.rejects(runNarrativeCodexTask({ mode: 'rewrite', title: 'Synthetic', materials: {},
      roleDir: fileURLToPath(new URL('../codex_agents/narrative_writer/', import.meta.url)) }, { backend }), error => {
      const result = narrativePreviewFailure(error);
      assert.equal(result.payload.reason, reason);
      assert.equal(result.payload.upstream_status, status);
      assert(!JSON.stringify(result).includes('synthetic-private-provider-body'));
      return true;
    });
  }
});

test('empty live shelf stays empty; offline and invalid responses remain visible', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ status: 'ok', items: [] }));
    assert.deepEqual(await loadNarrativeRolls(), []);
    globalThis.fetch = async () => new Response('<html>error</html>', { status: 504 });
    await assert.rejects(loadNarrativeRolls(), /读取书架失败.*HTTP 504/);
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Memory storage unavailable' }), { status: 503 });
    await assert.rejects(narrativeRequest('/test', {}, '保存叙事线'), /记忆存储暂不可用/);
    globalThis.fetch = async () => { throw new TypeError('failed fetch'); };
    await assert.rejects(loadNarrativeRolls(), /网络中断.*尚未确认结果/);
  } finally { globalThis.fetch = original; }
});

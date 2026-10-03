import test from 'node:test';
import assert from 'node:assert/strict';
import {readDomainPolicyDraft, saveDomainPolicyDraft, clearDomainPolicyDraft, hasDomainPolicyDraft, domainPolicyDraftVersion} from '../src/storage/basementStore.js';

const data = new Map();
globalThis.window = {localStorage:{getItem:key=>data.get(key) ?? null, setItem:(key,value)=>data.set(key,value), removeItem:key=>data.delete(key)}};
const baseline = [{key:'life', label:'生活', description:'新描述', policy:'normal'}];

test('Event and Scene retain separate drafts and saving one preserves the other version', () => {
  data.clear();
  saveDomainPolicyDraft([{...baseline[0], policy:'excluded'}], 'event', 3);
  saveDomainPolicyDraft([{...baseline[0], policy:'explicit_only'}], 'scene', 3);
  assert.equal(readDomainPolicyDraft(baseline, 'event')[0].policy, 'excluded');
  assert.equal(readDomainPolicyDraft(baseline, 'scene')[0].policy, 'explicit_only');
  clearDomainPolicyDraft(baseline, 'event');
  assert.equal(hasDomainPolicyDraft('event'), false);
  assert.equal(hasDomainPolicyDraft('scene'), true);
  assert.equal(domainPolicyDraftVersion('scene'), 3);
  assert.equal(readDomainPolicyDraft(baseline, 'scene')[0].description, '新描述');
});

test('a legacy shared draft migrates to both once and cannot resurrect after save', () => {
  data.clear();
  saveDomainPolicyDraft([{key:'life', policy:'excluded'}]);
  assert.equal(readDomainPolicyDraft(baseline, 'event')[0].policy, 'excluded');
  clearDomainPolicyDraft(baseline, 'event');
  assert.equal(readDomainPolicyDraft(baseline, 'scene')[0].policy, 'excluded');
  assert.equal(hasDomainPolicyDraft('event'), false);
  assert.equal(data.has('serein.basement.domain-policy-draft.v1'), false);
});

test('stale versions survive refresh; removed keys and invalid policies do not override the catalog', () => {
  data.clear();
  saveDomainPolicyDraft([{key:'gone',policy:'excluded'},{key:'life',policy:'invalid'}], 'event', 1);
  assert.deepEqual(readDomainPolicyDraft(baseline, 'event'), baseline);
  assert.equal(domainPolicyDraftVersion('event'), 1);
});

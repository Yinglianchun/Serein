// Synthetic browser/DOM fixture. Every fetch is intercepted; no backend calls.
export const fixture = {
  requests: [], fail: false, hold: false, pending: [],
  scenes: [false, null, true].map((manual, index) => ({
    source_id: `scene_synthetic_${index}`, title: `Synthetic archive ${index}`,
    date: '2026-10-10', content: 'Exact synthetic authored body', author: 'Fixture',
    status: '已沉底', storage_status: 'archived', type: 'archived', active: false,
    scene_status: 'archived', status_consistent: true, updated_at: `version-${index}`,
    scene_cues: ['synthetic'], annotations: [], manual,
  })),
};
window.fetch = async (input, options = {}) => {
  const path = String(input);
  const reply = (data, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => data});
  if (path === '/__serein/live/memory-scenes') return reply({status:'ok',scenes:fixture.scenes,edges:[]});
  if (path.startsWith('/__serein/personal')) return reply({items:[],has_more:false});
  if (path === '/__serein/settings') return reply({identity:{user_name:'User',ai_name:'Fixture'},features:{favorites:false}});
  if (path.startsWith('/__serein/live/fact-events')) return reply({items:[],count:0});
  if (path === '/__serein/memory/set-scene-status') {
    const body = JSON.parse(options.body);
    fixture.requests.push(body);
    if (fixture.hold) await new Promise(resolve => {fixture.pending.push(resolve);});
    if (fixture.fail) return reply({status:'conflict',message:'Synthetic version conflict'},409);
    const scene = fixture.scenes.find(row => row.source_id === body.sceneId);
    if (!scene || scene.updated_at !== body.expectedUpdatedAt) return reply({status:'conflict'},409);
    scene.updated_at += '-next';
    scene.storage_status = scene.scene_status = body.status;
    scene.status = body.status === 'archived' ? '已沉底' : '可浮现';
    if (body.restoreSurface === true) scene.manual = true;
    return reply({status:'updated',updated_at:scene.updated_at});
  }
  throw new Error(`Unexpected synthetic request: ${path}`);
};
if (globalThis !== window) globalThis.fetch = (...args) => window.fetch(...args);

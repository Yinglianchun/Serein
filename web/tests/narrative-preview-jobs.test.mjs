import { test } from "node:test";
import assert from "node:assert/strict";
import { Readable } from "node:stream";
import config from "../vite.config.mjs";
import { buildNarrativePreviewFingerprint } from "../server/narrativeMaterialPreview.mjs";
import { createNarrativePreviewJobs } from "../server/narrativePreviewJobs.mjs";
import { previewNarrativeRoll } from "../src/storage/narrativeStore.js";

test("preview jobs return results, cap concurrent work, and expire", async (t) => {
  t.mock.method(console, "error", () => {});
  let now = 0;
  const jobs = createNarrativePreviewJobs({ now: () => now, ttlMs: 600_000 });
  const pending = () => new Promise(() => {});
  const ids = [jobs.start(pending), jobs.start(pending), jobs.start(pending)];
  assert.equal(ids.every(Boolean), true);
  assert.equal(jobs.start(pending), null);

  const timedOutJobs = createNarrativePreviewJobs({ now: () => now });
  const successId = timedOutJobs.start(async () => ({ statusCode: 200, payload: { status: "ok", body: "预览正文" } }));
  const timedOutId = timedOutJobs.start(async () => { throw new Error("narrative_writer_timeout"); });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(timedOutJobs.get(successId).result.payload.body, "预览正文");
  assert.equal(timedOutJobs.get(timedOutId).result.statusCode, 504);
  assert.match(timedOutJobs.get(timedOutId).result.payload.message, /5 分钟/);

  now = 600_001;
  assert.equal(jobs.get(ids[0]).state, "pending");
  assert.equal(jobs.start(pending), null); // Expiry never removes work that still holds a slot.
  assert.equal(timedOutJobs.get(successId), null);
});

test("hung previews time out, abort work, release slots, and ignore late results", async (t) => {
  t.mock.method(console, "error", () => {});
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const jobs = createNarrativePreviewJobs();
  let signal, release;
  const id = jobs.start((jobSignal) => {
    signal = jobSignal;
    return new Promise((resolve) => { release = resolve; });
  });
  await new Promise((resolve) => setImmediate(resolve));
  t.mock.timers.tick(330_000);
  assert.equal(signal.aborted, true);
  assert.equal(jobs.get(id).result.statusCode, 504);
  release({ statusCode: 200, payload: { body: "late body" } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(jobs.get(id).result.statusCode, 504);
  assert.ok(jobs.start(async () => ({ statusCode: 200, payload: { status: "ok" } })));
});

test("completed preview retention starts when generation finishes", async () => {
  let now = 0, release;
  const jobs = createNarrativePreviewJobs({ now: () => now });
  const id = jobs.start(() => new Promise((resolve) => { release = resolve; }));
  await new Promise((resolve) => setImmediate(resolve));
  now = 300_000;
  release({ statusCode: 200, payload: { status: "ok", body: "body" } });
  await new Promise((resolve) => setImmediate(resolve));
  now = 600_001;
  assert.equal(jobs.get(id).result.payload.body, "body");
  now = 900_001;
  assert.equal(jobs.get(id), null);
});

test("narrative preview polls its job outside the original request", async (t) => {
  const requests = [];
  t.mock.method(globalThis, "setTimeout", (callback) => { queueMicrotask(callback); return 0; });
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push([url, options]);
    return new Response(JSON.stringify(requests.length === 1
      ? { status: "pending", job_id: "job-1" }
      : { status: "ok", body: "预览正文" }), {
      status: requests.length === 1 ? 202 : 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  const result = await previewNarrativeRoll({ id: "narrative_1", revision: 1, documentHash: "hash" }, "rewrite", {});
  assert.equal(result.body, "预览正文");
  assert.equal(requests[0][0], "/__serein/narrative-preview");
  assert.equal(requests[0][1].method, "POST");
  assert.equal(requests[1][0], "/__serein/narrative-preview?jobId=job-1");
  assert.equal(requests[1][1].cache, "no-store");
});

test("client polling has a deadline even when the service stays pending", async (t) => {
  const controller = new AbortController();
  t.mock.method(AbortSignal, "timeout", (milliseconds) => {
    assert.equal(milliseconds, 360_000);
    return controller.signal;
  });
  t.mock.method(globalThis, "setTimeout", callback => { queueMicrotask(callback); return 0; });
  let requests = 0;
  t.mock.method(globalThis, "fetch", async () => {
    requests++;
    if (requests === 2) controller.abort(new DOMException("deadline", "TimeoutError"));
    return new Response(JSON.stringify({status: "pending", job_id: "job-1"}), {status: 202});
  });
  await assert.rejects(previewNarrativeRoll({id: "narrative_1"}, "rewrite", {}), /预览等待超时/);
  assert.equal(requests, 2);
});

test("polling reports missing jobs and upstream timeouts without saving", async (t) => {
  t.mock.method(globalThis, "setTimeout", callback => { queueMicrotask(callback); return 0; });
  for (const [status, message] of [[404, "预览任务已中断，请重新预览。"], [504, "叙事卷生成超过 5 分钟，请重新预览。"]]) {
    let requests = 0;
    t.mock.method(globalThis, "fetch", async () => {
      requests++;
      return new Response(JSON.stringify(requests === 1 ? {status: "pending", job_id: "job-1"}
        : {status: "error", message, writes_performed: []}), {status: requests === 1 ? 202 : status});
    });
    await assert.rejects(previewNarrativeRoll({id: "narrative_1"}, "rewrite", {}), error => error.message === message);
    assert.equal(requests, 2);
  }
});

test("background preview route retains save seals and performs no publication", async (t) => {
  const previous = { url: process.env.SEREIN_MEMORY_URL, token: process.env.SEREIN_MEMORY_TOKEN };
  process.env.SEREIN_MEMORY_URL = "http://synthetic.invalid";
  process.env.SEREIN_MEMORY_TOKEN = "synthetic";
  try {
    const handlers = new Map(), paths = [];
    let releaseInput;
    const inputGate = new Promise(resolve => { releaseInput = resolve; });
    const input = {
      title: "Reading", writing_focus: "A reading plan", current_body: "Previous body",
      materials: { content: "Bound text ![notice](https://example.org/notice.png)" },
      base_revision: 2, base_document_sha256: "document-hash", material_snapshot_sha256: "materials-hash",
      current_material_ids: { events: ["e1"] }, proposed_material_ids: { events: ["e1", "e2"] },
      material_delta: { added: ["e2"] }, material_counts: { events: 2 },
    };
    t.mock.method(globalThis, "fetch", async (url, options) => {
      const path = new URL(url).pathname;
      paths.push(path);
      assert.equal(options.headers.Authorization, "Bearer synthetic");
      if (path === "/api/narrative-rolls/preview-input") {
        await inputGate;
        return new Response(JSON.stringify(input));
      }
      if (path === "/v1/settings") return new Response(JSON.stringify({
        identity: {user_name: "Nori", ai_name: "Atlas"}, upstream: {writer_enabled: true},
        models: [{id: "writer", model: "synthetic"}], assignments: {writer: "writer"},
      }));
      assert.equal(path, "/v1/models/writer");
      const task = JSON.parse(options.body);
      assert.equal(Object.hasOwn(task, "image_inputs"), false);
      assert.match(task.prompt, /Bound text/);
      return new Response(JSON.stringify({result: {
        evidence_sufficient: true, body: "New body", issues: [], self_review: {
          source_bound: true, final_supported_versions: true, no_correction_narration: true,
          material_relevance: true, no_new_inference: true, no_meta_explanation: true,
          no_forced_closure: true, dates_preserved: true, identity_correct: true,
        },
      }}));
    });
    config.plugins.flat().find(plugin => plugin.name === "serein-memory-bridge").configureServer({
      middlewares: { use: (path, handler) => { handlers.set(path, handler); } },
    });
    const handler = handlers.get("/__serein/narrative-preview");
    const call = async (method, url, body) => {
      const request = Readable.from(body ? [Buffer.from(JSON.stringify(body))] : []);
      request.method = method; request.url = url;
      const response = {headers: {}, setHeader(key, value) { this.headers[key] = value; },
        end(text) { this.payload = JSON.parse(text); }};
      await handler(request, response);
      return response;
    };
    const started = await call("POST", "/", {narrativeId: "narrative_test", mode: "update",
      expectedRevision: 2, expectedDocumentSha256: "document-hash", proposedMaterialIds: input.proposed_material_ids});
    assert.equal(started.statusCode, 202);
    const jobUrl = `/?jobId=${started.payload.job_id}`;
    assert.equal((await call("GET", jobUrl)).statusCode, 202);
    releaseInput();
    let result;
    for (let attempt = 0; attempt < 10; attempt++) {
      await new Promise(resolve => setImmediate(resolve));
      result = await call("GET", jobUrl);
      if (result.statusCode !== 202) break;
    }
    assert.equal(result.statusCode, 200);
    assert.equal(result.headers["Cache-Control"], "no-store");
    assert.equal(result.payload.body, "New body");
    assert.equal(result.payload.base_revision, 2);
    assert.deepEqual(result.payload.proposed_material_ids, input.proposed_material_ids);
    assert.deepEqual(result.payload.writes_performed, []);
    assert.equal(result.payload.preview_fingerprint, buildNarrativePreviewFingerprint({
      narrativeId: "narrative_test", revision: 2, documentSha256: "document-hash",
      body: "New body", materialSnapshotSha256: "materials-hash",
    }));
    assert.deepEqual(paths, ["/api/narrative-rolls/preview-input", "/v1/settings", "/v1/models/writer"]);
    assert.equal((await call("GET", "/?jobId=missing")).statusCode, 404);
  } finally {
    for (const [key, value] of [["SEREIN_MEMORY_URL", previous.url], ["SEREIN_MEMORY_TOKEN", previous.token]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

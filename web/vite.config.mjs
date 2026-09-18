import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { appearanceBridge } from './server/appearanceBridge.mjs';
import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, posix, win32 } from "node:path";
import { fileURLToPath } from "node:url";
import { Readable } from "node:stream";
import {
  buildSceneEvidenceRefs,
  normalizeEvidenceMessageId,
  normalizeEvidenceSearchQuery,
} from "./server/sceneEvidenceBridge.mjs";
import { narrativeBodyDiff, runNarrativeCodexTask } from "./server/narrativeCodexRunner.mjs";
import { buildNarrativePreviewFingerprint } from "./server/narrativeMaterialPreview.mjs";

const narrativeWriterRoleDir = fileURLToPath(new URL("./codex_agents/narrative_writer/", import.meta.url));

const canonicalSceneDomains = new Set([
  "relationship",
  "intimacy",
  "inner",
  "life",
  "tech",
  "project",
  "general",
]);

function secretValue(name) {
  const direct = String(process.env[name] || "").trim();
  if (direct) return direct;
  const file = String(process.env[`${name}_FILE`] || "").trim();
  if (!file) return "";
  try {
    return String(readFileSync(file, "utf8") || "").trim();
  } catch {
    return "";
  }
}

function semanticRouteDraftPath() {
  return String(
    process.env.SEREIN_ROUTE_DRAFT_FILE || fileURLToPath(new URL("./.runtime/semantic-route-draft.json", import.meta.url)),
  ).trim();
}

function readServerSemanticRouteDraft() {
  const file = semanticRouteDraftPath();
  if (!file || !existsSync(file)) return null;
  try {
    const draft = JSON.parse(readFileSync(file, "utf8"));
    return Array.isArray(draft?.routes) ? draft : null;
  } catch {
    return null;
  }
}

function saveServerSemanticRouteDraft(body) {
  if (!Array.isArray(body?.routes) || body.routes.length < 1 || body.routes.length > 50) {
    throw new Error("route_draft_invalid");
  }
  const baseDatasetVersion = Number.parseInt(body.baseDatasetVersion, 10);
  if (!Number.isInteger(baseDatasetVersion) || baseDatasetVersion < 1) {
    throw new Error("route_draft_version_invalid");
  }
  const current = readServerSemanticRouteDraft();
  const expectedRevision = body.expectedRevision == null
    ? null
    : Number.parseInt(body.expectedRevision, 10);
  const currentRevision = Number.parseInt(current?.revision, 10) || 0;
  if (expectedRevision != null && expectedRevision !== currentRevision) {
    const error = new Error("route_draft_revision_conflict");
    error.current = current;
    throw error;
  }
  const record = {
    schemaVersion: 1,
    baseDatasetVersion,
    revision: currentRevision + 1,
    updatedAt: new Date().toISOString(),
    routes: body.routes,
  };
  const file = semanticRouteDraftPath();
  mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(record, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  renameSync(temporary, file);
  return record;
}

function clearServerSemanticRouteDraft() {
  const file = semanticRouteDraftPath();
  if (file && existsSync(file)) rmSync(file);
}

async function readJsonBody(request, maxBytes = 32_768) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw new Error("request_too_large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

export function normalizeRecallSimulationOptions(body = {}) {
  const recallAblation = String(body.recall_ablation || "normal").trim().toLowerCase();
  const simulationScope = String(body.simulation_scope || "live_mirror").trim().toLowerCase();
  const allowedRecallAblations = new Set(["normal", "without_cues", "without_embedding"]);
  const allowedSimulationScopes = new Set(["live_mirror", "full_shadow"]);
  const simulation = [true, 1, "1", "true", "yes", "on"].includes(body.simulation);
  if (!allowedSimulationScopes.has(simulationScope)) {
    return { ok: false, error: "invalid_simulation_scope", message: "模拟范围不正确。" };
  }
  if (!allowedRecallAblations.has(recallAblation)) {
    return { ok: false, error: "invalid_recall_ablation", message: "消融模式不正确。" };
  }
  if (recallAblation !== "normal" && !simulation) {
    return {
      ok: false,
      error: "recall_ablation_requires_simulation",
      message: "消融只允许从召回模拟发起。",
    };
  }
  if (recallAblation !== "normal" && simulationScope !== "full_shadow") {
    return {
      ok: false,
      error: "recall_ablation_requires_full_shadow",
      message: "消融只在完整 shadow 诊断里运行。",
    };
  }
  return { ok: true, recallAblation, simulation, simulationScope };
}

function parseMcpEvent(text) {
  const data = String(text || "")
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim())
    .join("\n");
  if (!data) throw new Error("mcp_response_missing");
  return JSON.parse(data);
}

import { sereinConfigured, callSereinBackend, callSereinTool } from './server/sereinBackend.mjs';

async function callSereinDashboard(path, options = {}) { return callSereinBackend(path, options); }

async function readLiveMemoryProjection(sourceIds) {
  const safeSourceIds = Array.from(new Set(
    (Array.isArray(sourceIds) ? sourceIds : [])
      .map((sourceId) => String(sourceId || "").trim())
      .filter((sourceId) => /^[A-Za-z0-9_.:#-]{1,160}$/.test(sourceId)),
  )).slice(0, 400);
  const upstream = await callSereinDashboard("/api/serein/memory-projection", {
    method: "POST",
    body: { source_ids: safeSourceIds },
  });
  if (!upstream.ok) throw new Error(`memory_live_projection_${upstream.status}`);
  return upstream.payload;
}

async function readLiveDiaries() {
  const diaries = [];
  let offset = 0;
  while (offset < 500) {
    const upstream = await callSereinDashboard("/diaries/search", {
      method: "POST",
      body: { keyword: "", limit: 100, offset },
    });
    if (!upstream.ok) throw new Error(`diary_live_${upstream.status}`);
    const page = Array.isArray(upstream.payload?.diaries) ? upstream.payload.diaries : [];
    diaries.push(...page);
    if (page.length < 100) break;
    offset += page.length;
  }
  const fingerprint = diaries.map((entry) => (
    `${entry.id}:${entry.updated_at || entry.created_at || ""}:${entry.revision || 0}`
  )).join("|");
  return {
    status: "ok",
    snapshotId: Buffer.from(fingerprint, "utf8").toString("base64url"),
    source: "Serein DiaryStore live read-only projection",
    entries: diaries,
  };
}

function uniqueNarrativeSourceIds(items, key) {
  return Array.from(new Set(
    items.flatMap((item) => (Array.isArray(item?.[key]) ? item[key] : []))
      .map((value) => String(value ?? "").trim())
      .filter(Boolean),
  ));
}

function sourceDate(...values) {
  const value = values.find((candidate) => String(candidate || "").trim());
  return value ? String(value).slice(0, 10) : "";
}

export function buildNarrativeSourceLedgers(items, metadata = {}) {
  const eventById = new Map((metadata.events || []).map((item) => [String(item.item_id), item]));
  const sceneById = new Map((metadata.scenes || []).map((item) => [String(item.id), item]));
  const diaryById = new Map((metadata.diaries || []).map((item) => [String(item.id), item]));

  return items.map((item) => {
    const sourceLedger = [];
    for (const sourceId of item.linked_event_ids || []) {
      const id = String(sourceId);
      const source = eventById.get(id);
      sourceLedger.push({
        source_type: "event",
        source_id: id,
        title: String(source?.title || "未找到的 Event"),
        date: sourceDate(source?.local_date, source?.source_started_at, source?.created_at),
        status: String(source?.status || (source ? "active" : "missing")),
      });
    }
    for (const sourceId of item.linked_scene_ids || []) {
      const id = String(sourceId);
      const source = sceneById.get(id);
      sourceLedger.push({
        source_type: "scene",
        source_id: id,
        title: String(source?.name || source?.title || "未找到的 Scene"),
        date: sourceDate(source?.source_date, source?.created, source?.created_at, source?.updated_at),
        status: String(source?.status_view || source?.status || (source ? "active" : "missing")),
      });
    }
    for (const sourceId of item.linked_diary_ids || []) {
      const id = String(sourceId);
      const source = diaryById.get(id);
      sourceLedger.push({
        source_type: "diary",
        source_id: id,
        title: String(source?.title || `日记 ${id}`),
        date: sourceDate(source?.date, source?.created_at),
        status: String(source?.entry_type || source?.visibility || (source ? "active" : "missing")),
      });
    }
    for (const sourceId of item.linked_darkroom_ids || []) {
      const id = String(sourceId);
      const source = diaryById.get(id);
      sourceLedger.push({
        source_type: "darkroom",
        source_id: id,
        title: String(source?.title || `暗房 ${id}`),
        date: sourceDate(source?.date, source?.created_at),
        status: String(source?.entry_type || source?.visibility || (source ? "active" : "missing")),
      });
    }
    const uploadById = new Map((item.linked_uploads || []).map((upload) => [String(upload.upload_id), upload]));
    for (const sourceId of item.linked_upload_ids || []) {
      const id = String(sourceId);
      const source = uploadById.get(id);
      sourceLedger.push({
        source_type: "upload",
        source_id: id,
        title: String(source?.filename || id),
        date: sourceDate(source?.created_at),
        status: String(source?.extraction_status || (source ? "stored" : "missing")),
      });
    }
    return { ...item, source_ledger: sourceLedger };
  });
}

async function attachNarrativeSourceLedgers(items) {
  const eventIds = uniqueNarrativeSourceIds(items, "linked_event_ids");
  const sceneIds = uniqueNarrativeSourceIds(items, "linked_scene_ids");
  const diaryIds = new Set([
    ...uniqueNarrativeSourceIds(items, "linked_diary_ids"),
    ...uniqueNarrativeSourceIds(items, "linked_darkroom_ids"),
  ]);

  let events = [];
  if (eventIds.length) {
    const result = await callSereinDashboard("/api/fact-events/read-many", {
      method: "POST",
      body: { item_ids: eventIds, include_sources: false, resolve_active_successors: false },
    });
    if (result.ok) events = Array.isArray(result.payload?.items) ? result.payload.items : [];
  }

  let scenes = [];
  if (sceneIds.length) {
    const result = await callSereinDashboard("/api/buckets/light?include_archive=1&limit=2000");
    if (result.ok) scenes = Array.isArray(result.payload?.buckets) ? result.payload.buckets : [];
  }

  let diaries = [];
  if (diaryIds.size) {
    const result = await readLiveDiaries();
    diaries = result.entries.filter((entry) => diaryIds.has(String(entry.id)));
    const found = new Set(diaries.map((entry) => String(entry.id)));
    for (const diaryId of diaryIds) {
      if (found.has(diaryId)) continue;
      const exact = await callSereinDashboard(`/diaries/${encodeURIComponent(diaryId)}`);
      if (exact.ok) diaries.push(exact.payload);
    }
  }

  return buildNarrativeSourceLedgers(items, { events, scenes, diaries });
}

export async function readLiveNarratives() {
  const index = await callSereinDashboard("/api/narrative-rolls?limit=100");
  if (!index.ok) throw new Error(`narrative_live_projection_${index.status}`);
  const summaries = Array.isArray(index.payload?.items) ? index.payload.items : [];
  const items = [];
  for (const summary of summaries) {
    const narrativeId = String(summary?.narrative_id || "").trim();
    if (!narrativeId) continue;
    const result = await callSereinDashboard(`/api/narrative-rolls?narrative_id=${encodeURIComponent(narrativeId)}`);
    if (!result.ok) throw new Error(`narrative_live_projection_${result.status}`);
    items.push(result.payload);
  }
  const enrichedItems = await attachNarrativeSourceLedgers(items);
  const fingerprint = enrichedItems.map((item) => (
    `${item?.narrative_id || ""}:${item?.revision || ""}:${item?.document_sha256 || ""}:${JSON.stringify(item.source_ledger)}`
  )).join("|");
  return {
    status: "ok",
    snapshotId: createHash("sha256").update(fingerprint).digest("hex"),
    source: "Serein Narrative Roll registry live read-only projection",
    items: enrichedItems,
  };
}

export async function readLiveWindowShadows() {
  const upstream = await callSereinDashboard("/api/window-shadows?limit=100&include_content=1");
  if (!upstream.ok) throw new Error(`window_shadow_live_projection_${upstream.status}`);
  const windows = Array.isArray(upstream.payload?.windows) ? upstream.payload.windows : [];
  const shadows = windows.map((item) => {
    const text = String(item?.content || "").replace(/\r\n?/g, "\n").trim();
    const created = new Date(item?.created_at || 0);
    const createdDate = Number.isNaN(created.getTime())
      ? String(item?.source_date || "").slice(0, 10)
      : new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(created);
    const sourceDate = String(item?.source_date || createdDate).slice(0, 10);
    const historical = sourceDate !== createdDate;
    const dateLabel = sourceDate.replaceAll("-", ".");
    const timeLabel = historical || Number.isNaN(created.getTime())
      ? "历史补录"
      : new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false,
      }).format(created);
    const headings = Array.from(text.matchAll(/^\s{0,3}#{1,6}\s+(.+?)\s*$/gm))
      .map((match) => match[1].replace(/[*_`]/g, "").trim())
      .filter((heading) => !["window shadow", "窗影"].includes(heading.toLowerCase()));
    const summary = text.split(/\n\s*\n/).map((block) => block.split("\n").map((line) => line.trim()).filter(Boolean))
      .find((lines) => lines.length && !lines.every((line) => line.startsWith("#") || line.startsWith(">")))
      ?.join(" ").replace(/^\s{0,3}#{1,6}\s+|^\s*[-*+]\s+/g, "").replace(/[*_`>#]+/g, "").replace(/\s+/g, " ").trim() || "";
    const contentHash = createHash("sha256").update(text).digest("hex");
    const title = String(item?.title || "").trim() || headings[0] || `${dateLabel} 的窗影`;
    return {
      id: String(item?.window_id || ""),
      closedAt: historical ? `${sourceDate}T00:00:00+08:00` : String(item?.created_at || ""),
      dateLabel,
      timeLabel,
      relativeLabel: `${dateLabel} · ${timeLabel}`,
      title,
      summary: summary.length <= 96 ? summary : `${summary.slice(0, 95)}…`,
      text,
      scenes: Array.isArray(item?.scenes) ? item.scenes : [],
      sourceLabel: "Serein",
      statusLabel: "已入库窗影",
      sourceKind: "serein-window-shadow",
      documentOwnsTitle: headings[0] === title,
      sourceId: String(item?.window_id || ""),
      sourceSessionId: String(item?.session_id || ""),
      contentHash,
    };
  });
  const fingerprint = shadows.map((item) => `${item.sourceId}:${item.contentHash}`).join("\n");
  return {
    status: "ok",
    snapshotId: createHash("sha256").update(fingerprint).digest("hex"),
    source: "Serein Window Shadow live read-only projection",
    shadows,
  };
}

export async function readAssistantBridgeHookLedger(limit = 80, beforeId = 0, reviewIds = []) {
  const result = await callSereinBackend(`/v1/host/deliveries?limit=${limit}&before_id=${beforeId}`);
  if (!result.ok) throw new Error(`delivery_history_${result.status}`);
  return result.payload;
}
async function readAssistantBridgeEvidenceMessages(options) {
  const result = await callSereinBackend("/v1/host/messages/search", {method:"POST", body:options});
  if (!result.ok) throw new Error(`source_read_${result.status}`);
  return result.payload;
}

function sereinGatewayBridge() {
  return {
    name: "serein-gateway-bridge",
    configurePreviewServer(server) { sereinGatewayBridge().configureServer(server); },
    configureServer(server) {
      server.middlewares.use("/__serein/install-location", (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        response.setHeader("Cache-Control", "no-store");
        if (request.method !== "GET") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        const root = process.env.SEREIN_INSTALL_ROOT || fileURLToPath(new URL("../", import.meta.url));
        response.end(JSON.stringify({ root, installed: Boolean(process.env.SEREIN_INSTALL_ROOT || existsSync(fileURLToPath(new URL("../deploy/installation.json", import.meta.url)))),
          legacySourceRoot: process.env.SEREIN_LEGACY_SOURCE_HOST || "",
          legacySourceConfigured: process.env.SEREIN_LEGACY_SOURCE_CONFIGURED === "1" }));
      });

      server.middlewares.use("/__serein/assistant-bridge/hook-injections", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          response.statusCode = 200;
          response.end(JSON.stringify(await readAssistantBridgeHookLedger(
            body.limit,
            body.beforeId,
            body.reviewIds,
          )));
        } catch (error) {
          response.statusCode = 502;
          response.end(JSON.stringify({
            status: "error",
            error: "assistant_bridge_hook_ledger_failed",
            message: "本地桥没有读到 聊天宿主 hook 账本。",
            items: [],
          }));
        }
      });

      server.middlewares.use("/__serein/gateway/injections", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const limit = Math.max(1, Math.min(100, Number.parseInt(body.limit, 10) || 50));
          const beforeId = Math.max(0, Number.parseInt(body.beforeId, 10) || 0);
          const reviewIds = [...new Set((Array.isArray(body.reviewIds) ? body.reviewIds : [])
            .map((item) => Number.parseInt(item, 10))
            .filter((item) => Number.isInteger(item) && item > 0))].slice(0, 500);
          const params = new URLSearchParams({ limit: String(limit), include_context: "0" });
          if (beforeId) params.set("before_id", String(beforeId));
          if (reviewIds.length) params.set("review_ids", reviewIds.join(","));
          const upstream = await callSereinDashboard(`/api/gateway-injections?${params.toString()}`);
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            status: "error",
            error: "gateway_observation_failed",
            message: error?.name === "AbortError" ? "最近注入读取超时。" : "本地桥没有读到最近注入。",
            items: [],
          }));
        }
      });

      server.middlewares.use("/__serein/gateway/recall", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }

        const gatewayToken = String(process.env.SEREIN_MEMORY_TOKEN || "").trim();
        const gatewayBase = String(process.env.SEREIN_MEMORY_URL || "").replace(/\/$/, "");
        if (!gatewayToken) {
          response.statusCode = 503;
          response.end(JSON.stringify({
            error: "gateway_bridge_not_configured",
            message: "本地预览没有安全载入 Gateway 凭据。",
          }));
          return;
        }

        try {
          const body = await readJsonBody(request);
          const query = String(body.query || "").trim();
          if (!query) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "query_required", message: "先写一句要测试的话。" }));
            return;
          }
          const simulationOptions = normalizeRecallSimulationOptions(body);
          if (!simulationOptions.ok) {
            response.statusCode = 400;
            response.end(JSON.stringify({
              error: simulationOptions.error,
              message: simulationOptions.message,
            }));
            return;
          }
          const { recallAblation, simulation, simulationScope } = simulationOptions;

          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 30_000);
          const upstream = await fetch(`${gatewayBase}/api/hook/recall`, {
            method: "POST",
            headers: {
              Authorization: `Bearer ${gatewayToken}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              query,
              session_id: `serein-basement-${randomUUID()}`,
              recall_mode: "full",
              include_debug: true,
              simulation,
              simulation_scope: simulationScope,
              recall_ablation: recallAblation,
              ...(body.direct_threshold !== undefined ? { direct_threshold: body.direct_threshold } : {}),
              include_context: false,
              include_recent_context: false,
              max_cards: 5,
              max_chars: 1200,
            }),
            signal: controller.signal,
          });
          clearTimeout(timer);
          response.statusCode = upstream.status;
          response.end(await upstream.text());
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "gateway_bridge_failed",
            message: error?.name === "AbortError" ? "Gateway 响应超时。" : "本地桥没有完成这次模拟。",
          }));
        }
      });

      server.middlewares.use("/__serein/gateway/semantic-route-draft", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (!["GET", "PUT", "DELETE"].includes(request.method)) {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          if (request.method === "GET") {
            const draft = readServerSemanticRouteDraft();
            response.statusCode = 200;
            response.end(JSON.stringify({ status: draft ? "ok" : "empty", draft }));
            return;
          }
          if (request.method === "DELETE") {
            clearServerSemanticRouteDraft();
            response.statusCode = 200;
            response.end(JSON.stringify({ status: "cleared" }));
            return;
          }
          const body = await readJsonBody(request, 256_000);
          const draft = saveServerSemanticRouteDraft(body);
          response.statusCode = 200;
          response.end(JSON.stringify({ status: "saved", draft }));
        } catch (error) {
          const conflict = error?.message === "route_draft_revision_conflict";
          response.statusCode = conflict ? 409 : error?.message === "request_too_large" ? 413 : 400;
          response.end(JSON.stringify({
            error: error?.message || "route_draft_failed",
            ...(conflict ? { draft: error.current || null } : {}),
          }));
        }
      });

      server.middlewares.use("/__serein/gateway/semantic-routes", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (!["GET", "POST"].includes(request.method)) {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }

        const gatewayToken = String(process.env.SEREIN_MEMORY_TOKEN || "").trim();
        const gatewayBase = String(process.env.SEREIN_MEMORY_URL || "").replace(/\/$/, "");
        if (!gatewayToken) {
          response.statusCode = 503;
          response.end(JSON.stringify({
            error: "gateway_bridge_not_configured",
            message: "本地预览没有安全载入 Gateway 凭据。",
          }));
          return;
        }

        try {
          const publishing = request.method === "POST";
          const body = publishing ? await readJsonBody(request, 128_000) : undefined;
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), publishing ? 120_000 : 20_000);
          const upstream = await fetch(
            `${gatewayBase}/api/semantic-recall/routes${publishing ? "/publish" : ""}`,
            {
              method: request.method,
              headers: {
                Authorization: `Bearer ${gatewayToken}`,
                ...(publishing ? { "Content-Type": "application/json" } : {}),
              },
              body: publishing ? JSON.stringify(body) : undefined,
              signal: controller.signal,
            },
          );
          clearTimeout(timer);
          response.statusCode = upstream.status;
          response.end(await upstream.text());
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "semantic_route_bridge_failed",
            message: error?.name === "AbortError" ? "Router 数据集操作超时。" : "本地桥没有完成 Router 数据集操作。",
          }));
        }
      });

      server.middlewares.use("/__serein/gateway/domain-policies", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (!["GET", "POST"].includes(request.method)) {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }

        const gatewayToken = String(process.env.SEREIN_MEMORY_TOKEN || "").trim();
        const gatewayBase = String(process.env.SEREIN_MEMORY_URL || "").replace(/\/$/, "");
        if (!gatewayToken) {
          response.statusCode = 503;
          response.end(JSON.stringify({
            error: "gateway_bridge_not_configured",
            message: "本地预览没有安全载入 Gateway 凭据。",
          }));
          return;
        }

        try {
          const publishing = request.method === "POST";
          const body = publishing ? await readJsonBody(request, 32_000) : undefined;
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 20_000);
          const upstream = await fetch(
            `${gatewayBase}/api/semantic-recall/domain-policies${publishing ? "/publish" : ""}`,
            {
              method: request.method,
              headers: {
                Authorization: `Bearer ${gatewayToken}`,
                ...(publishing ? { "Content-Type": "application/json" } : {}),
              },
              body: publishing ? JSON.stringify(body) : undefined,
              signal: controller.signal,
            },
          );
          clearTimeout(timer);
          response.statusCode = upstream.status;
          response.end(await upstream.text());
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "domain_policy_bridge_failed",
            message: error?.name === "AbortError" ? "主域策略操作超时。" : "本地桥没有完成主域策略操作。",
          }));
        }
      });
    },
  };
}

function sereinMemoryBridge() {
  return {
    name: "serein-memory-bridge",
    configurePreviewServer(server) { sereinMemoryBridge().configureServer(server); },
    configureServer(server) {
      appearanceBridge(server, callSereinBackend, readJsonBody);
      server.middlewares.use("/__serein/export/markdown", async (request,response)=>{
        if(request.method!=="GET"){response.statusCode=405;response.end();return;}
        try {
          const base=process.env.SEREIN_MEMORY_URL;
          const token=process.env.SEREIN_MEMORY_TOKEN;
          if(!base || !token)throw new Error("backend_not_configured");
          const result=await fetch(base.replace(/\/$/,"")+"/v1/export/markdown",{headers:{Authorization:`Bearer ${token}`},signal:AbortSignal.timeout(60_000)});
          response.statusCode=result.status;
          response.setHeader("Cache-Control","no-store");
          response.setHeader("Content-Type",result.ok?"application/zip":"application/json");
          if(result.ok)response.setHeader("Content-Disposition",'attachment; filename="serein-memories.zip"');
          response.end(Buffer.from(await result.arrayBuffer()));
        } catch {response.statusCode=502;response.end("Export unavailable");}
      });
      server.middlewares.use("/__serein/export/backup", async (request,response)=>{
        if(request.method!=="GET"){response.statusCode=405;response.end();return;}
        try {
          const base=process.env.SEREIN_MEMORY_URL;
          const token=process.env.SEREIN_MEMORY_TOKEN;
          if(!base || !token)throw new Error("backend_not_configured");
          const result=await fetch(base.replace(/\/$/,"")+"/v1/export/backup",{headers:{Authorization:`Bearer ${token}`},signal:AbortSignal.timeout(60_000)});
          response.statusCode=result.status;
          response.setHeader("Cache-Control","no-store");
          response.setHeader("Content-Type",result.ok?"application/octet-stream":"application/json");
          if(result.ok)response.setHeader("Content-Disposition",'attachment; filename="serein-backup.db"');
          response.end(Buffer.from(await result.arrayBuffer()));
        } catch {response.statusCode=502;response.end("Export unavailable");}
      });
      server.middlewares.use("/__serein/migration", async (request,response)=>{
        response.setHeader("Cache-Control","no-store");
        const path=new URL(request.url,"http://localhost").pathname;
        if(request.method==="GET" && /^\/[0-9a-f]{64}\/export$/.test(path)){
          try {
            const base=process.env.SEREIN_MEMORY_URL,token=process.env.SEREIN_MEMORY_TOKEN;
            if(!base||!token)throw new Error("backend_not_configured");
            const result=await fetch(base.replace(/\/$/,"")+"/v1/migration"+path,
              {headers:{Authorization:`Bearer ${token}`},signal:AbortSignal.timeout(1_200_000)});
            response.statusCode=result.status;
            response.setHeader("Content-Type",result.ok?"application/zip":"application/json; charset=utf-8");
            if(result.ok)response.setHeader("Content-Disposition",'attachment; filename="ombre-legacy-source.zip"');
            if(result.body)Readable.fromWeb(result.body).on("error",()=>response.destroy()).pipe(response);
            else response.end();
          }catch{response.statusCode=502;response.end("Export unavailable");}
          return;
        }
        response.setHeader("Content-Type","application/json; charset=utf-8");
        const allowed=(request.method==="GET" && path==="/") ||
          (request.method==="POST" && (["/preview","/preview-path"].includes(path) || /^\/[0-9a-f]{64}\/(continue|pause)$/.test(path)));
        if(!allowed){response.statusCode=404;response.end();return;}
        try {
          let body=request.method==="POST"?await readJsonBody(request,path==="/preview"?90_000_100:8000):undefined;
          if(path==="/preview-path" && process.env.SEREIN_LEGACY_SOURCE_HOST) {
            const hostRoot=process.env.SEREIN_LEGACY_SOURCE_HOST;
            const paths=/^[A-Za-z]:[\\/]/.test(hostRoot)?win32:posix;
            const source=String(body.path||"").trim();
            if(!paths.isAbsolute(source)){response.statusCode=400;response.end(JSON.stringify({detail:"请输入运行 Serein 的机器上的绝对路径"}));return;}
            const relative=paths.relative(paths.resolve(hostRoot),paths.resolve(source));
            if(relative===".." || relative.startsWith(`..${paths.sep}`) || paths.isAbsolute(relative)) {
              response.statusCode=400;response.end(JSON.stringify({detail:`该路径尚未只读挂载；请在安装菜单 7 授权它所在的旧库目录`}));return;
            }
            body={path:`/legacy/input${relative?`/${relative.split(paths.sep).join("/")}`:""}`,display_path:source};
          }
          const result=await callSereinBackend(`/v1/migration${path==="/"?"":path}`,{
            method:request.method,...(request.method==="POST"?{body}:{}),timeoutMs:120_000,
          });
          response.statusCode=result.status;response.end(JSON.stringify(result.payload));
        }catch{response.statusCode=502;response.end(JSON.stringify({detail:"旧库操作暂未完成，请刷新任务状态后重试。"}));}
      });

      server.middlewares.use("/__serein/settings", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        response.setHeader("Cache-Control", "no-store");
        const candidates = request.url?.split("?")[0] === "/resume-candidates";
        const prepare = request.url?.split("?")[0] === "/prepare-memory";
        const discovery = request.url?.split("?")[0] === "/models/discover";
        const template = request.url?.split("?")[0] === "/upstreams-template";
        if (!(candidates ? ["GET"] : prepare || template || discovery ? ["POST"] : ["GET", "PATCH"]).includes(request.method)) {
          response.statusCode = 405; response.end(JSON.stringify({ error: "method_not_allowed" })); return;
        }
        // Mutations are same-origin JSON requests, never cross-site form posts.
        if (request.method !== "GET" && (!String(request.headers["content-type"]).startsWith("application/json")
            || (request.headers.origin && new URL(request.headers.origin).host !== request.headers.host))) {
          response.statusCode = 403; response.end(JSON.stringify({ error: "origin_not_allowed" })); return;
        }
        try {
          const result = await callSereinBackend(candidates ? "/v1/settings" + request.url : prepare ? "/v1/settings/prepare-memory" : discovery ? "/v1/settings/models/discover" : template ? "/v1/settings/upstreams-template" : "/v1/settings", {
            method: request.method,
            ...(request.method === "PATCH" || template || discovery ? { body: await readJsonBody(request, 256_000) } : {}),
          }, {timeout: prepare ? 600_000 : 30_000});
          response.statusCode = result.status; response.end(JSON.stringify(result.payload));
        } catch {
          response.statusCode = 502; response.end(JSON.stringify({ error: "settings_unavailable" }));
        }
      });
      server.middlewares.use("/__serein/pipeline", async (request,response)=>{
        response.setHeader("Content-Type","application/json; charset=utf-8");
        response.setHeader("Cache-Control","no-store");
        const action=request.url?.split("?")[0];
        if(!(request.method==="GET" && (action==="/status" || /^\/attempts\/\d+$/.test(action))) && !(request.method==="POST" && ["/next","/submit","/rebuild"].includes(action))) {
          response.statusCode=405;response.end(JSON.stringify({error:"method_not_allowed"}));return;
        }
        if(request.method==="POST" && (!String(request.headers["content-type"]).startsWith("application/json") ||
          (request.headers.origin && new URL(request.headers.origin).host!==request.headers.host))) {
          response.statusCode=403;response.end(JSON.stringify({error:"origin_not_allowed"}));return;
        }
        try {
          const path=action==="/submit"?"/v1/extensions/pipeline_submit":`/v1/pipeline${action}`;
          const result=await callSereinBackend(path,{
            method:request.method,...(request.method==="POST"?{body:await readJsonBody(request,2_000_000)}:{}),
          },{timeout:30_000});
          response.statusCode=result.status;response.end(JSON.stringify(result.payload));
        } catch {response.statusCode=502;response.end(JSON.stringify({error:"pipeline_unavailable"}));}
      });
      server.middlewares.use("/__serein/imports", async (request,response)=>{
        response.setHeader("Content-Type","application/json; charset=utf-8");
        response.setHeader("Cache-Control","no-store");
        const path=request.url?.split("?")[0] || "/";
        if(!(request.method==="GET" && path==="/") && !(request.method==="POST" &&
           (path==="/preview" || path==="/retry-tagging" || /^\/upload%3A[a-f0-9]{64}\/(continue|pause)$/i.test(path)))) {
          response.statusCode=405;response.end(JSON.stringify({error:"method_not_allowed"}));return;
        }
        if(request.method!=="GET" && (!String(request.headers["content-type"]).startsWith("application/json") ||
          (request.headers.origin && new URL(request.headers.origin).host!==request.headers.host))) {
          response.statusCode=403;response.end(JSON.stringify({error:"origin_not_allowed"}));return;
        }
        try {
          const result=await callSereinBackend('/v1/imports'+(path==='/'?'':path),{
            method:request.method,...(request.method==='POST'?{body:await readJsonBody(request,200_000_000)}:{}),
          },{timeout:120_000});
          response.statusCode=result.status;response.end(JSON.stringify(result.payload));
        } catch {response.statusCode=502;response.end(JSON.stringify({error:"import_unavailable"}));}
      });
      server.middlewares.use("/__serein/companion", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        const url = new URL(request.url, "http://localhost");
        if (!/^\/(persona|memos(?:\/[a-zA-Z0-9_-]+)?)$/.test(url.pathname) || !["GET","POST","PUT","PATCH","DELETE"].includes(request.method)) {
          response.statusCode=404; response.end(); return;
        }
        try {
          const result=await callSereinBackend(`/v1/companion${url.pathname}${url.search}`, {
            method:request.method,...(!["GET","DELETE"].includes(request.method) ? {body:await readJsonBody(request,100_000)} : {}),
          });
          response.statusCode=result.status;response.end(JSON.stringify(result.payload));
        } catch { response.statusCode=502;response.end(JSON.stringify({detail:"连接暂不可用，内容没有保存。"})); }
      });

      server.middlewares.use("/__serein/personal", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        try {
          const url = new URL(request.url, "http://localhost");
          if (!["/", "/import"].includes(url.pathname) || !["GET", "POST"].includes(request.method)) {
            response.statusCode = 405; response.end(JSON.stringify({ error: "method_not_allowed" })); return;
          }
          const upstream = await callSereinBackend(`/api/personal${url.pathname === "/import" ? "/import" : ""}${url.search}`, {
            method: request.method,
            body: request.method === "POST" ? await readJsonBody(request, 2_000_000) : undefined,
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch {
          response.statusCode = 502; response.end(JSON.stringify({ error: "personal_save_unavailable" }));
        }
      });
      server.middlewares.use("/__serein/live/memory-scenes", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request, 96_000);
          response.statusCode = 200;
          response.end(JSON.stringify(await readLiveMemoryProjection(body.sourceIds)));
        } catch (error) {
          console.error("[serein-memory-bridge] live Scene projection failed", error);
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            status: "error",
            error: "memory_live_projection_failed",
            message: "没有读到线上 Scene，已保留本地快照回退。",
            scenes: [],
            edges: [],
          }));
        }
      });

      server.middlewares.use("/__serein/live/fact-events", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const type = body.type === "fact" ? "fact" : "event";
          const status = ["active", "archived", "superseded", "all"].includes(body.status)
            ? body.status
            : "all";
          const params = new URLSearchParams({
            type,
            status,
            include_sources: body.includeSources === true ? "1" : "0",
            limit: String(Math.max(1, Math.min(500, Number.parseInt(body.limit, 10) || 500))),
            offset: String(Math.max(0, Number.parseInt(body.offset, 10) || 0)),
          });
          const query = String(body.query || "").trim();
          if (query) params.set("query", query);
          const upstream = await callSereinDashboard(`/api/fact-events?${params.toString()}`);
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "fact_events_read_failed",
            message: error?.name === "AbortError" ? "读取事实和事件超时。" : "暂时没有读到事实和事件。",
            items: [],
          }));
        }
      });

      server.middlewares.use("/__serein/memory/revise-fact-event", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const upstream = await callSereinDashboard("/api/fact-events/revise", {
            method: "POST",
            body: {
              item_id: String(body.itemId || "").trim(),
              title: body.title,
              body: body.body,
              ...(Object.prototype.hasOwnProperty.call(body, "recallable") ? { recallable: body.recallable } : {}),
            },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "fact_event_revision_failed",
            message: error?.name === "AbortError" ? "保存修订超时。" : "没有完成这次修订。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/set-fact-event-status", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const upstream = await callSereinDashboard("/api/fact-events/status", {
            method: "POST",
            body: {
              item_id: String(body.itemId || "").trim(),
              status: String(body.status || "").trim(),
            },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "fact_event_status_failed",
            message: error?.name === "AbortError" ? "更新状态超时。" : "没有完成这次状态修改。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/delete-fact-event", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const upstream = await callSereinDashboard("/api/fact-events/delete", {
            method: "POST",
            body: { item_id: String(body.itemId || "").trim() },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "fact_event_deletion_failed",
            message: error?.name === "AbortError" ? "永久删除超时。" : "没有完成永久删除。",
          }));
        }
      });

      server.middlewares.use("/__serein/live/diaries", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        const route = new URL(request.url || "/", "http://serein.local");
        const diaryId = route.pathname.match(/^\/(\d+)\/?$/u)?.[1];
        const commentRoute = route.pathname.match(/^\/(\d+)\/comments(?:\/(\d+))?\/?$/u);
        const commentDiaryId = commentRoute?.[1];
        const commentId = commentRoute?.[2];

        if (
          (request.method === "POST" && commentDiaryId && !commentId)
          || (request.method === "DELETE" && commentDiaryId && commentId)
        ) {
          try {
            const body = request.method === "POST" ? await readJsonBody(request) : undefined;
            const upstream = await callSereinDashboard(
              `/diaries/${commentDiaryId}/comments${commentId ? `/${commentId}` : ""}`,
              { method: request.method, body },
            );
            response.statusCode = upstream.status;
            response.end(JSON.stringify(upstream.payload));
          } catch (error) {
            response.statusCode = error?.name === "AbortError" ? 504 : 502;
            response.end(JSON.stringify({
              error: request.method === "POST" ? "diary_comment_save_failed" : "diary_comment_delete_failed",
              message: request.method === "POST"
                ? "这条评论没有保存，请稍后再试。"
                : "这条评论没有删掉，请稍后再试。",
            }));
          }
          return;
        }

        if (
          (request.method === "POST" && /^\/entry\/?$/u.test(route.pathname))
          || (request.method === "PUT" && diaryId)
        ) {
          try {
            const body = await readJsonBody(request, 256_000);
            const upstream = await callSereinDashboard(
              diaryId ? `/diaries/${diaryId}` : "/diaries",
              { method: diaryId ? "PUT" : "POST", body },
            );
            response.statusCode = upstream.status;
            response.end(JSON.stringify(upstream.payload));
          } catch (error) {
            response.statusCode = error?.name === "AbortError" ? 504 : 502;
            response.end(JSON.stringify({
              error: "diary_save_failed",
              message: "这篇日记没有保存，请稍后再试。",
            }));
          }
          return;
        }

        if (request.method === "DELETE" && diaryId) {
          try {
            const upstream = await callSereinDashboard(`/diaries/${diaryId}`, { method: "DELETE" });
            response.statusCode = upstream.status;
            response.end(JSON.stringify(upstream.payload));
          } catch (error) {
            response.statusCode = error?.name === "AbortError" ? 504 : 502;
            response.end(JSON.stringify({
              error: "diary_delete_failed",
              message: "这篇日记没有删掉，请稍后再试。",
            }));
          }
          return;
        }

        if (request.method !== "POST" || !/^\/?$/u.test(route.pathname)) {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          response.statusCode = 200;
          response.end(JSON.stringify(await readLiveDiaries()));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            status: "error",
            error: "diary_live_projection_failed",
            message: "没有读到线上日记，已保留本地快照回退。",
            entries: [],
          }));
        }
      });

      server.middlewares.use("/__serein/live/narratives", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          response.statusCode = 200;
          response.end(JSON.stringify(await readLiveNarratives()));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            status: "error",
            error: "narrative_live_projection_failed",
            message: "没有读到线上叙事卷，已保留本地样卷回退。",
            items: [],
          }));
        }
      });

      for (const action of ["discover-theme", "create-line"]) {
        server.middlewares.use(`/__serein/narrative-${action}`, async (request, response) => {
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          if (request.method !== "POST") {
            response.statusCode = 405;
            response.end(JSON.stringify({ error: "method_not_allowed" }));
            return;
          }
          try {
            const result = await callSereinBackend(`/api/narrative-rolls/${action}`, {
              method: "POST", body: await readJsonBody(request, 64_000),
              timeoutMs: action === "discover-theme" ? 420_000 : 30_000,
            });
            response.statusCode = result.status;
            response.end(JSON.stringify(result.payload));
          } catch {
            response.statusCode = 502;
            response.end(JSON.stringify({ status: "error", message: "叙事卷服务暂时没有响应，请重试。" }));
          }
        });
      }

      server.middlewares.use("/__serein/narrative-preview", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request, 128_000);
          const narrativeId = String(body.narrativeId || "").trim();
          const mode = ["edit", "update", "rewrite"].includes(body.mode) ? body.mode : "";
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(narrativeId) || !mode) {
            response.statusCode = 400;
            response.end(JSON.stringify({ status: "invalid", reason: "invalid_preview_request", writes_performed: [] }));
            return;
          }
          const upstream = await callSereinDashboard("/api/narrative-rolls/preview-input", {
            method: "POST",
            body: {
              narrative_id: narrativeId,
              mode,
              expected_revision: Number.parseInt(body.expectedRevision, 10) || undefined,
              expected_document_sha256: String(body.expectedDocumentSha256 || "").trim(),
              proposed_material_ids: body.proposedMaterialIds,
            },
          });
          if (!upstream.ok) {
            response.statusCode = upstream.status;
            response.end(JSON.stringify(upstream.payload));
            return;
          }
          const input = upstream.payload;
          const preview = mode === "edit"
            ? {
                status: "ok",
                evidence_sufficient: true,
                body: String(body.proposedBody || "").trim(),
                issues: [],
                diff: narrativeBodyDiff(input.current_body, String(body.proposedBody || "").trim()),
                mode,
                provider: "host_validation_only",
                publication_status: "not_published",
                writes_performed: [],
              }
            : await runNarrativeCodexTask({
                mode,
                title: input.title,
                writingFocus: input.writing_focus,
                currentBody: input.current_body,
                materials: input.materials,
                roleDir: narrativeWriterRoleDir,
              });
          if (!preview.body) {
            response.statusCode = mode === "edit" ? 400 : 200;
            response.end(JSON.stringify({
              ...preview,
              narrative_id: narrativeId,
              base_revision: input.base_revision,
              base_document_sha256: input.base_document_sha256,
              material_counts: input.material_counts,
              current_material_ids: input.current_material_ids,
              proposed_material_ids: input.proposed_material_ids,
              material_delta: input.material_delta,
              material_snapshot_sha256: input.material_snapshot_sha256,
              writes_performed: [],
            }));
            return;
          }
          const fingerprint = buildNarrativePreviewFingerprint({
            narrativeId,
            revision: input.base_revision,
            documentSha256: input.base_document_sha256,
            body: preview.body,
            materialSnapshotSha256: input.material_snapshot_sha256,
          });
          response.statusCode = 200;
          response.end(JSON.stringify({
            ...preview,
            narrative_id: narrativeId,
            base_revision: input.base_revision,
            base_document_sha256: input.base_document_sha256,
            material_counts: input.material_counts,
            current_material_ids: input.current_material_ids,
            proposed_material_ids: input.proposed_material_ids,
            material_delta: input.material_delta,
            material_snapshot_sha256: input.material_snapshot_sha256,
            preview_fingerprint: fingerprint,
          }));
        } catch (error) {
          console.error("[serein-memory-bridge] Narrative preview failed", error);
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            status: "error",
            reason: "narrative_preview_failed",
            message: error?.name === "AbortError" ? "这次预览生成超时了。" : "暂时没有生成叙事卷预览。",
            writes_performed: [],
          }));
        }
      });

      server.middlewares.use("/__serein/narrative-material-upload", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request, 14_100_000);
          const filename = String(body.filename || "").trim();
          const contentType = String(body.contentType || "application/octet-stream").trim();
          const contentBase64 = String(body.contentBase64 || "");
          if (!filename || filename.length > 240 || !contentBase64) {
            response.statusCode = 400;
            response.end(JSON.stringify({ status: "invalid", reason: "invalid_upload_request", writes_performed: [] }));
            return;
          }
          const upstream = await callSereinDashboard("/api/narrative-rolls/material-uploads", {
            method: "POST",
            body: {
              filename,
              content_type: contentType,
              content_base64: contentBase64,
            },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.message === "request_too_large" ? 413 : 502;
          response.end(JSON.stringify({
            status: "error",
            reason: error?.message === "request_too_large" ? "upload_too_large" : "narrative_material_upload_failed",
            writes_performed: [],
          }));
        }
      });

      server.middlewares.use("/__serein/narrative-save", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request, 260_000);
          const narrativeId = String(body.narrativeId || "").trim();
          const narrativeBody = String(body.body || "");
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(narrativeId) || !narrativeBody.trim()) {
            response.statusCode = 400;
            response.end(JSON.stringify({ status: "invalid", reason: "invalid_body_save_request" }));
            return;
          }
          const upstream = await callSereinDashboard("/api/narrative-rolls/save-body", {
            method: "POST",
            body: {
              narrative_id: narrativeId,
              body: narrativeBody,
              expected_revision: Number.parseInt(body.expectedRevision, 10),
              expected_document_sha256: String(body.expectedDocumentSha256 || "").trim(),
              proposed_material_ids: body.proposedMaterialIds,
              expected_material_snapshot_sha256: String(body.expectedMaterialSnapshotSha256 || "").trim(),
              preview_fingerprint: String(body.previewFingerprint || "").trim(),
            },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          console.error("[serein-memory-bridge] Narrative body save failed", error);
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            status: "error",
            reason: "narrative_body_save_failed",
            message: error?.name === "AbortError" ? "保存正文超时了。" : "这次正文没有保存。",
          }));
        }
      });

      server.middlewares.use("/__serein/live/window-shadows", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          response.statusCode = 200;
          response.end(JSON.stringify(await readLiveWindowShadows()));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            status: "error",
            error: "window_shadow_live_projection_failed",
            message: "没有读到线上窗影，已保留本地快照回退。",
            shadows: [],
          }));
        }
      });

      server.middlewares.use("/__serein/live/dreams", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const limit = Math.max(1, Math.min(Number(body.limit) || 30, 100));
          const upstream = await callSereinDashboard(`/api/dreams?limit=${limit}`);
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "dream_bridge_failed",
            message: error?.name === "AbortError" ? "读取梦境超时。" : "没有读到梦境。",
          }));
        }
      });

      server.middlewares.use("/__serein/live/dream-detail", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const dreamId = String(body.dreamId || "").trim();
          if (!dreamId) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "dream_id_required" }));
            return;
          }
          const upstream = await callSereinDashboard(`/api/dreams/${encodeURIComponent(dreamId)}`);
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "dream_bridge_failed",
            message: error?.name === "AbortError" ? "读取这场梦超时。" : "这场梦暂时翻不开。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/scene-evidence", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sceneId = String(body.sceneId || "").trim();
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(sceneId)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_id", message: "这张 Scene 没有可读取的原文证据 ID。" }));
            return;
          }
          const result = await callSereinTool("read_scene_evidence", { scene_id: sceneId });
          response.statusCode = result?.status === "invalid" ? 404 : result?.status === "error" ? 502 : 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "scene_evidence_read_failed",
            message: error?.name === "AbortError" ? "读取原文证据超时。" : "暂时没有读到这张 Scene 的原文证据。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/bridge-source-messages", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const result = await readAssistantBridgeEvidenceMessages({
            limit: body.limit,
            beforeId: body.beforeId,
            query: body.query,
            contextMessageId: body.contextMessageId,
            contextRadius: body.contextRadius,
          });
          response.statusCode = 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          console.error("[serein] bridge source messages failed", error);
          response.statusCode = 502;
          response.end(JSON.stringify({
            error: "bridge_source_messages_failed",
            message: "暂时没有读到 聊天宿主 的原文表。",
            items: [],
          }));
        }
      });

      server.middlewares.use("/__serein/memory/bind-scene-evidence", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sceneId = String(body.sceneId || "").trim();
          const selections = Array.isArray(body.selections) ? body.selections.slice(0, 12) : [];
          const messageIds = selections.map((item) => Number.parseInt(item?.messageId, 10)).filter((item) => item > 0);
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(sceneId) || !messageIds.length || messageIds.length !== selections.length) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_evidence_selection", message: "先选择要绑定的 归档原文。" }));
            return;
          }
          const source = await readAssistantBridgeEvidenceMessages({ messageIds });
          const evidenceRefs = buildSceneEvidenceRefs(source.items, selections);
          const result = await callSereinTool("bind_scene_evidence", {
            scene_id: sceneId,
            evidence_refs: evidenceRefs,
            bound_by: "serein_memory_ui",
          });
          response.statusCode = result?.status === "invalid" ? 400 : result?.status === "error" ? 502 : 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          const invalid = String(error?.message || "").startsWith("evidence_");
          response.statusCode = invalid ? 400 : error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: invalid ? error.message : "scene_evidence_bind_failed",
            message: invalid ? "选择的原文已变化，请刷新后重选。" : "没有完成这次原文绑定。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/unbind-scene-evidence", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sceneId = String(body.sceneId || "").trim();
          const evidenceIds = Array.isArray(body.evidenceIds)
            ? [...new Set(body.evidenceIds.map((item) => Number.parseInt(item, 10)).filter((item) => item > 0))].slice(0, 12)
            : [];
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(sceneId) || !evidenceIds.length) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_evidence_unbind", message: "没有找到要取消的原文绑定。" }));
            return;
          }
          const result = await callSereinTool("unbind_scene_evidence", {
            scene_id: sceneId,
            evidence_ids: evidenceIds,
            unbound_by: "serein_memory_ui",
          });
          response.statusCode = result?.status === "invalid" ? 400 : result?.status === "error" ? 502 : 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "scene_evidence_unbind_failed",
            message: "没有完成这次原文解绑。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/read-scene", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sceneId = String(body.sceneId || "").trim();
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(sceneId)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_id", message: "Scene ID 不正确。" }));
            return;
          }
          const scene = await callSereinTool("read_memory", { memory_type: "scene", memory_id: sceneId });
          response.statusCode = scene?.status === "not_found" ? 404 : 200;
          response.end(JSON.stringify(scene));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "memory_bridge_failed",
            message: error?.name === "AbortError" ? "读取 Scene 超时。" : "没有读到这张 Scene 的维护信息。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/edit-scene-domain", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sourceId = String(body.sourceId || "").trim();
          const domain = String(body.domain || "").trim();
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(sourceId)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_id", message: "这张 Scene 没有可写回的 Serein 来源。" }));
            return;
          }
          if (!canonicalSceneDomains.has(domain)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_domain", message: "主域不在允许范围内。" }));
            return;
          }

          const upstream = await callSereinDashboard("/api/buckets/bulk-update", {
            method: "POST",
            body: { bucket_ids: [sourceId], domain },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "memory_domain_write_failed",
            message: error?.name === "AbortError" ? "修改主域超时。" : "没有把主域写回线上 Serein。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/edit-scene-cues", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sceneId = String(body.sceneId || "").trim();
          const expectedUpdatedAt = String(body.expectedUpdatedAt || "").trim();
          const cues = Array.isArray(body.cues)
            ? body.cues.map((cue) => String(cue || "").trim()).filter(Boolean)
            : [];
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(sceneId) || !expectedUpdatedAt) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_revision", message: "缺少 Scene 或版本信息。" }));
            return;
          }
          if (!cues.length || cues.length > 8 || cues.some((cue) => cue.length > 80)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_cues", message: "保留 1～8 条 cue，每条不超过 80 字。" }));
            return;
          }
          const result = await callSereinTool("edit_scene", {
            scene_id: sceneId,
            expected_updated_at: expectedUpdatedAt,
            cues,
          });
          response.statusCode = result?.status === "conflict" ? 409 : 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "memory_bridge_failed",
            message: error?.name === "AbortError" ? "保存 Scene 超时。" : "没有完成这次 Scene 修订。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/edit-scene", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sceneId = String(body.sceneId || "").trim();
          const expectedUpdatedAt = String(body.expectedUpdatedAt || "").trim();
          const title = String(body.title || "").trim();
          const content = String(body.content || "").trim();
          const date = String(body.date || "").trim();
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(sceneId) || !expectedUpdatedAt || !title || !content || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_revision", message: "日期、标题、正文或 Scene 版本不完整。" }));
            return;
          }
          const result = await callSereinTool("edit_scene", {
            scene_id: sceneId,
            expected_updated_at: expectedUpdatedAt,
            title,
            content,
            date,
          });
          response.statusCode = result?.status === "conflict" ? 409 : result?.status === "invalid" ? 400 : 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "scene_revision_failed",
            message: error?.name === "AbortError" ? "保存 Scene 超时。" : "没有完成这次 Scene 修订。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/set-scene-status", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sceneId = String(body.sceneId || "").trim();
          const expectedUpdatedAt = String(body.expectedUpdatedAt || "").trim();
          const status = body.status === "archived" ? "archived" : "active";
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(sceneId) || !expectedUpdatedAt) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_status", message: "缺少 Scene 或版本信息。" }));
            return;
          }
          const result = await callSereinTool("set_scene_status", {
            scene_id: sceneId,
            expected_updated_at: expectedUpdatedAt,
            status,
          });
          response.statusCode = result?.status === "conflict" ? 409 : result?.status === "invalid" ? 400 : 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "scene_status_failed",
            message: error?.name === "AbortError" ? "更新 Scene 状态超时。" : "没有完成这次 Scene 状态修改。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/delete-scenes", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const sceneIds = Array.isArray(body.sceneIds)
            ? [...new Set(body.sceneIds.map((value) => String(value || "").trim()))]
              .filter((value) => /^[A-Za-z0-9_.:#-]{1,160}$/.test(value))
            : [];
          if (!sceneIds.length) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_ids", message: "没有找到要删除的 Scene。" }));
            return;
          }
          const upstream = await callSereinDashboard("/api/buckets/delete", {
            method: "POST",
            body: { bucket_ids: sceneIds, confirm: "DELETE" },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "scene_delete_failed",
            message: error?.name === "AbortError" ? "删除 Scene 超时。" : "没有完成这次删除。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/revision-materials", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") { response.statusCode = 405; response.end('{}'); return; }
        try {
          const body = await readJsonBody(request);
          const proposalId = String(body.proposalId || '');
          if (!/^[A-Za-z0-9_.:#-]{1,160}$/.test(proposalId)) throw new Error('invalid_proposal_id');
          const params = new URLSearchParams({ offset:String(Math.max(0, Number(body.offset) || 0)), limit:'50' });
          if (body.identifier) { params.set('identifier', String(body.identifier)); params.set('kind', String(body.kind || '')); }
          const result = await callSereinDashboard(`/api/narrative-revision-inbox/${encodeURIComponent(proposalId)}/materials?${params}`);
          response.statusCode = result.status;
          response.end(JSON.stringify(result.payload));
        } catch {
          response.statusCode = 502;
          response.end(JSON.stringify({ message:'没有读到候选材料，请重试。' }));
        }
      });

      server.middlewares.use("/__serein/memory/narrative-revisions", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const status = ["pending", "dismissed", "absorbed", "all"].includes(body.status)
            ? body.status
            : "pending";
          const narrativeId = String(body.narrativeId || "").trim();
          const result = await callSereinTool("narrative_revision_inbox", {
            status,
            narrative_id: narrativeId,
            limit: Math.max(1, Math.min(Number(body.limit) || 50, 100)),
          });
          response.statusCode = result?.status === "invalid" ? 400 : 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "revision_bridge_failed",
            message: error?.name === "AbortError" ? "读取修订箱超时。" : "没有读到修订箱。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/review-narrative-revision", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const proposalId = String(body.proposalId || "").trim();
          const action = String(body.action || "").trim();
          if (!proposalId || !["save_draft", "dismiss", "reopen", "save_line", "write"].includes(action)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_revision_action", message: "修订动作不正确。" }));
            return;
          }
          const result = await callSereinTool("review_narrative_revision", {
            proposal_id: proposalId,
            action,
            draft_delta: String(body.draftDelta || ""),
            note: String(body.note || ""),
          });
          response.statusCode = result?.status === "not_found" ? 404 : result?.status === "conflict" ? 409 : result?.status === "invalid" ? 400 : 200;
          response.end(JSON.stringify(result));
        } catch (error) {
          response.statusCode = error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: "revision_bridge_failed",
            message: error?.name === "AbortError" ? "保存修订草稿超时。" : "没有完成这次修订箱操作。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/save-narrative-materials", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") { response.statusCode = 405; response.end(); return; }
        try {
          const result = await callSereinDashboard("/api/narrative-rolls/save-materials", {
            method: "POST", body: await readJsonBody(request),
          });
          response.statusCode = result.status;
          response.end(JSON.stringify(result.payload));
        } catch (error) {
          response.statusCode = 502;
          response.end(JSON.stringify({ message: "没有保存叙事线材料，请刷新后重试。" }));
        }
      });

      server.middlewares.use("/__serein/memory/scene-edge-proposals", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const allowedStatuses = ["pending", "accepted", "rejected", "superseded", "error", "all"];
          const status = allowedStatuses.includes(body.status) ? body.status : "pending";
          const limit = Math.max(1, Math.min(Number(body.limit) || 30, 100));
          const query = new URLSearchParams({ status, limit: String(limit), include_context: "true" });
          const upstream = await callSereinDashboard(`/api/scene-edge-proposals?${query}`);
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          const unconfigured = error?.message === "dashboard_bridge_not_configured";
          response.statusCode = unconfigured ? 503 : error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: unconfigured ? "dashboard_bridge_not_configured" : "relationship_bridge_failed",
            message: unconfigured ? "本地预览没有安全载入 Dashboard 凭据。" : "没有读到关系提案。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/retry-scene-relation", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const upstream = await callSereinDashboard("/api/scene-relation-jobs/retry", {
            method: "POST",
            body: { scene_id: body.scene_id, attempt_id: body.attempt_id },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch {
          response.statusCode = 502;
          response.end(JSON.stringify({ message: "未能提交重试，请刷新查看任务状态。" }));
        }
      });

      server.middlewares.use("/__serein/memory/review-scene-edge-proposal", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const proposalId = String(body.proposalId || "").trim();
          const decision = String(body.decision || "").trim();
          if (!proposalId || !["accept", "reject"].includes(decision)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_relationship_action", message: "关系审核动作不正确。" }));
            return;
          }
          const upstream = await callSereinDashboard("/api/scene-edge-proposals/review", {
            method: "POST",
            body: {
              proposal_id: proposalId,
              decision,
              confirm: decision === "accept" ? "ACCEPT_SCENE_EDGE" : "REJECT_SCENE_EDGE",
            },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          const unconfigured = error?.message === "dashboard_bridge_not_configured";
          response.statusCode = unconfigured ? 503 : error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: unconfigured ? "dashboard_bridge_not_configured" : "relationship_bridge_failed",
            message: unconfigured ? "本地预览没有安全载入 Dashboard 凭据。" : "没有完成这次关系审核。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/create-scene-edge-proposal", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const safeId = /^[A-Za-z0-9_.:#-]{1,160}$/;
          const sourceSceneId = String(body.sourceSceneId || "").trim();
          const targetSceneId = String(body.targetSceneId || "").trim();
          if (!safeId.test(sourceSceneId) || !safeId.test(targetSceneId) || sourceSceneId === targetSceneId) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_edge_target", message: "需要两张不同的 Scene。" }));
            return;
          }
          const upstream = await callSereinDashboard("/api/scene-edge-proposals/manual", {
            method: "POST",
            body: {
              source_scene_id: sourceSceneId,
              target_scene_id: targetSceneId,
              relation_type: String(body.relationType || "").trim(),
              source_evidence: String(body.sourceEvidence || ""),
              target_evidence: String(body.targetEvidence || ""),
              reason: String(body.reason || ""),
              supersedes_edge_id: String(body.supersedesEdgeId || "").trim(),
              confidence: 1,
              confirm: "CREATE_SCENE_EDGE_PROPOSAL",
            },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          const unconfigured = error?.message === "dashboard_bridge_not_configured";
          response.statusCode = unconfigured ? 503 : error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: unconfigured ? "dashboard_bridge_not_configured" : "scene_edge_create_bridge_failed",
            message: unconfigured ? "本地预览没有安全载入 Dashboard 凭据。" : "没有创建这条关系提案。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/scene-edges", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const query = new URLSearchParams({ include_inactive: "true" });
          if (body.sceneId) query.set("scene_id", String(body.sceneId).trim());
          const upstream = await callSereinDashboard(`/api/scene-edges?${query}`);
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          const unconfigured = error?.message === "dashboard_bridge_not_configured";
          response.statusCode = unconfigured ? 503 : error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: unconfigured ? "dashboard_bridge_not_configured" : "scene_edge_history_bridge_failed",
            message: unconfigured ? "本地预览没有安全载入 Dashboard 凭据。" : "没有读到关系边历史。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/delete-scene-edge", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const edgeId = String(body.edgeId || "").trim();
          const sceneId = String(body.sceneId || "").trim();
          const safeId = /^[A-Za-z0-9_.:#-]{1,160}$/;
          if (!safeId.test(edgeId) || !safeId.test(sceneId)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_edge_target", message: "关系边或 Scene ID 不正确。" }));
            return;
          }
          const upstream = await callSereinDashboard(`/api/scene-edges/${encodeURIComponent(edgeId)}`, {
            method: "DELETE",
            body: {
              scene_id: sceneId,
              confirm: "DELETE_SCENE_EDGE",
              reason: "manual_ui_remove",
            },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          const unconfigured = error?.message === "dashboard_bridge_not_configured";
          response.statusCode = unconfigured ? 503 : error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: unconfigured ? "dashboard_bridge_not_configured" : "scene_edge_delete_bridge_failed",
            message: unconfigured ? "本地预览没有安全载入 Dashboard 凭据。" : "没有完成这次关系边移除。",
          }));
        }
      });

      server.middlewares.use("/__serein/memory/restore-scene-edge", async (request, response) => {
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end(JSON.stringify({ error: "method_not_allowed" }));
          return;
        }
        try {
          const body = await readJsonBody(request);
          const edgeId = String(body.edgeId || "").trim();
          const safeId = /^[A-Za-z0-9_.:#-]{1,160}$/;
          if (!safeId.test(edgeId)) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "invalid_scene_edge_target", message: "关系边 ID 不正确。" }));
            return;
          }
          const upstream = await callSereinDashboard(`/api/scene-edges/${encodeURIComponent(edgeId)}/restore`, {
            method: "POST",
            body: { confirm: "RESTORE_SCENE_EDGE" },
          });
          response.statusCode = upstream.status;
          response.end(JSON.stringify(upstream.payload));
        } catch (error) {
          const unconfigured = error?.message === "dashboard_bridge_not_configured";
          response.statusCode = unconfigured ? 503 : error?.name === "AbortError" ? 504 : 502;
          response.end(JSON.stringify({
            error: unconfigured ? "dashboard_bridge_not_configured" : "scene_edge_restore_bridge_failed",
            message: unconfigured ? "本地预览没有安全载入 Dashboard 凭据。" : "没有恢复这条关系边。",
          }));
        }
      });
    },
  };
}

export default defineConfig({
  base: String(process.env.SEREIN_BASE_PATH || "/"),
  build: {
    outDir: "dist/client",
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        garden: fileURLToPath(new URL("./garden.html", import.meta.url)),
      },
    },
  },
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    host: "127.0.0.1",
    allowedHosts: ["localhost"],
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
  },
  plugins: [react(), sereinGatewayBridge(), sereinMemoryBridge()],
});

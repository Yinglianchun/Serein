import { Readable } from 'node:stream';
import { memoryToken } from './sereinBackend.mjs';
import { pipeline } from 'node:stream/promises';

export async function backupDownload(request, response) {
  response.setHeader('Cache-Control', 'no-store');
  if (request.method !== 'GET') {
    response.statusCode = 405; response.end(); return;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 1_200_000);
  const disconnect = () => { if (!response.writableEnded) controller.abort(); };
  response.on('close', disconnect);
  try {
    const base = process.env.SEREIN_MEMORY_URL;
    const token = memoryToken();
    if (!base || !token) throw new Error('backend_not_configured');
    const result = await fetch(base.replace(/\/$/, '') + '/v1/export/backup', {
      headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
    });
    response.statusCode = result.status;
    response.setHeader('Content-Type', result.ok ? 'application/octet-stream' : 'application/json; charset=utf-8');
    if (result.ok) {
      response.setHeader('Content-Disposition', 'attachment; filename="serein-backup.db"');
      const length = result.headers.get('Content-Length');
      if (length) response.setHeader('Content-Length', length);
    }
    if (result.body) await pipeline(Readable.fromWeb(result.body), response);
    else response.end();
  } catch {
    if (response.headersSent || response.destroyed) {
      response.destroy();
    } else {
      response.statusCode = controller.signal.aborted ? 504 : 502;
      response.setHeader('Content-Type', 'application/json; charset=utf-8');
      response.end(JSON.stringify({ detail: controller.signal.aborted
        ? '数据库备份超时，请稍后重试。' : '数据库备份未完成，请检查服务状态后重试。' }));
    }
  } finally {
    clearTimeout(timer);
    response.off('close', disconnect);
  }
}

import http from 'node:http';
import https from 'node:https';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';
import { renameSync, rmSync, writeFileSync } from 'node:fs';
import { memoryToken } from './sereinBackend.mjs';
import { preview } from 'vite';
import { requireWebAuth } from './webAuth.mjs';

const filename = process.env.SEREIN_WEB_AUTH_FILE;
if (!filename) throw new Error('SEREIN_WEB_AUTH_FILE is required');
const core = new URL(process.env.SEREIN_MEMORY_URL);
let publicOrigin = null;
if (process.env.SEREIN_PUBLIC_ORIGIN) {
  publicOrigin = new URL(process.env.SEREIN_PUBLIC_ORIGIN);
  const loopback = ['localhost','127.0.0.1','[::1]'].includes(publicOrigin.hostname);
  if ((publicOrigin.protocol !== 'https:' && !(publicOrigin.protocol === 'http:' && loopback))
      || publicOrigin.username || publicOrigin.password || publicOrigin.pathname !== '/'
      || publicOrigin.search || publicOrigin.hash) throw new Error('SEREIN_PUBLIC_ORIGIN must be an HTTPS origin or loopback HTTP origin');
}
const tokenFile = process.env.SEREIN_MEMORY_TOKEN_FILE;
if (!tokenFile || !memoryToken()) throw new Error('Memory token is empty');
const web = await preview({ root: fileURLToPath(new URL('../', import.meta.url)),
  preview: { host: '127.0.0.1', port: Number(process.env.SEREIN_PREVIEW_PORT || 4173), strictPort: true, allowedHosts: true } });
const bearerPaths = new Set(['/v1/models', '/v1/chat/completions',
  '/api/hook/recall', '/v1/host/deliveries']);
const hopHeaders = ['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'];

const server = http.createServer((req, res) => {
  let path;
  try { path = new URL(req.url, 'http://localhost').pathname; }
  catch {
    res.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8', 'X-Content-Type-Options': 'nosniff' });
    res.end('Invalid request URL'); return;
  }
  if ((path === '/ready' || path === '/live') && req.method === 'GET') {
    Promise.all([fetch(new URL(path === '/ready' ? '/health' : '/live', core), {signal:AbortSignal.timeout(3000)}),
      fetch(`http://127.0.0.1:${web.httpServer.address().port}/`, {signal:AbortSignal.timeout(3000)})])
      .then(results => { res.writeHead(results.every(r => r.ok) ? 200 : 503); res.end(); })
      .catch(() => { res.writeHead(503); res.end(); });
    return;
  }
  const mcp = ['/serein/mcp', '/serein/mcp/', '/mcp', '/mcp/'].includes(path);
  const oauth = path === '/authorize' || path === '/token' || path === '/register'
    || path.startsWith('/.well-known/oauth-');
  let requestHost;
  try { requestHost = new URL(`http://${req.headers.host}`).hostname; } catch {}
  const loopbackOAuth = ['localhost','127.0.0.1','[::1]'].includes(requestHost);
  if (oauth && !publicOrigin && !loopbackOAuth) {
    res.writeHead(400, {'Content-Type':'application/json'});
    res.end('{"detail":"OAuth requires a configured HTTPS public origin"}');return;
  }
  const chat = bearerPaths.has(path) || mcp;
  if (mcp && req.headers.origin) {
    let originOK = false;
    try { originOK = new URL(req.headers.origin).host === req.headers.host; } catch {}
    if (!originOK) { res.writeHead(403); res.end('Invalid origin'); return; }
  }
  if (!chat && !oauth && !requireWebAuth(req, res, filename)) return;
  if (path === '/__serein/gateway-key/copy' || path === '/__serein/gateway-key/rotate') {
    const respond = (status, data) => {
      res.writeHead(status, {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store',
        'X-Content-Type-Options':'nosniff'});
      res.end(JSON.stringify(data));
    };
    if (req.method !== 'POST') return respond(405, {detail:'POST required'});
    const origin = req.headers.origin;
    if (!origin || (origin !== publicOrigin?.origin && origin !== `http://${req.headers.host}`)
        || !String(req.headers['content-type'] || '').startsWith('application/json')) {
      return respond(403, {detail:'Same-origin JSON required'});
    }
    req.resume();
    if (path.endsWith('/copy')) {
      const key = memoryToken();
      return key ? respond(200, {key}) : respond(503, {detail:'Gateway Key 暂不可用。'});
    }
    if (!memoryToken()) return respond(503, {detail:'Gateway Key 暂不可用。'});
    const temporary = `${tokenFile}.${randomBytes(12).toString('hex')}.tmp`;
    try {
      writeFileSync(temporary, randomBytes(36).toString('base64url') + '\n', {mode:0o600, flag:'wx'});
      renameSync(temporary, tokenFile);
      return respond(200, {status:'rotated'});
    } catch {
      try { rmSync(temporary, {force:true}); } catch {}
      return respond(503, {detail:'无法写入 Gateway Key 文件，请检查部署目录权限。'});
    }
  }
  if (!chat && !oauth && !['GET','HEAD','OPTIONS'].includes(req.method)) {
    let originOK = true;
    try { if (req.headers.origin) originOK = new URL(req.headers.origin).host === req.headers.host; }
    catch { originOK = false; }
    if (!originOK || !String(req.headers['content-type'] || '').startsWith('application/json')) {
      res.writeHead(403); res.end('Same-origin JSON required'); return;
    }
  }
  if (chat && !mcp && !req.headers.authorization?.startsWith('Bearer ')) {
    res.writeHead(401); res.end('Bearer token required'); return;
  }
  const backend = chat || oauth;
  const target = backend ? core : new URL(`http://127.0.0.1:${process.env.SEREIN_PREVIEW_PORT || 4173}`);
  const headers = { ...req.headers, host: backend ? target.host : req.headers.host };
  if (mcp || oauth) {
    headers['x-forwarded-host'] = publicOrigin?.host || req.headers.host;
    headers['x-forwarded-proto'] = publicOrigin?.protocol.slice(0,-1) || 'http';
  }
  for (const key of hopHeaders) delete headers[key];
  if (!backend) delete headers.authorization;
  const proxy = (target.protocol === 'https:' ? https : http).request(target, {
    method: req.method, path: req.url, headers,
  }, upstream => {
    const responseHeaders = { ...upstream.headers };
    for (const key of hopHeaders) delete responseHeaders[key];
    res.writeHead(upstream.statusCode, responseHeaders);
    upstream.pipe(res);
  });
  proxy.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end('Backend unavailable'); });
  res.on('close', () => { if (!res.writableEnded) proxy.destroy(); });
  req.pipe(proxy);
});
server.listen(Number(process.env.SEREIN_GATEWAY_PORT || 8080), process.env.SEREIN_GATEWAY_BIND || '0.0.0.0');
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => {
  server.close(); web.httpServer.close();
});

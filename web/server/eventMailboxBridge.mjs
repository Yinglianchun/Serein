// Same-origin bridge: only the Node process holds backend credentials.
export function eventMailboxBridge(server, callBackend, readJsonBody) {
  server.middlewares.use('/__serein/event-mailbox-promote', async (request, response) => {
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    response.setHeader('Cache-Control', 'no-store');
    if (request.method !== 'POST' || !['', '/'].includes((request.url || '').split('?')[0])) {
      response.statusCode = 405; response.end(JSON.stringify({error:'method_not_allowed'})); return;
    }
    try {
      const result = await callBackend('/v1/extensions/promote_event_to_scene', {method:'POST', body:await readJsonBody(request)});
      response.statusCode=result.status; response.end(JSON.stringify(result.payload));
    } catch { response.statusCode=502; response.end(JSON.stringify({message:'升为 Scene 暂未完成，请重新读取确认；草稿仍会保留。'})); }
  });
  server.middlewares.use('/__serein/event-mailbox', async (request, response) => {
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    response.setHeader('Cache-Control', 'no-store');
    const rawPath=(request.url || '/').split('?')[0];
    const url = new URL(request.url || '/', 'http://localhost');
    const suffix = url.pathname === '/' ? '' : url.pathname;
    if (rawPath.includes('..') || !['GET','POST'].includes(request.method) || (suffix && !/^\/[A-Za-z0-9_.:%#-]{1,480}$/.test(suffix)) || (request.method==='POST'&&!suffix)) {
      response.statusCode=400; response.end(JSON.stringify({error:'invalid_mailbox_request'})); return;
    }
    try {
      const params = new URLSearchParams();
      for (const key of ['status','limit','offset','cursor']) if(url.searchParams.has(key))params.set(key,url.searchParams.get(key));
      const result=await callBackend(`/api/event-mailbox${suffix}${params.size?'?'+params:''}`,{method:request.method,...(request.method==='POST'?{body:await readJsonBody(request)}:{})});
      response.statusCode=result.status; response.end(JSON.stringify(result.payload));
    } catch { response.statusCode=502; response.end(JSON.stringify({message:'信箱暂时无法连接，请稍后重试。'})); }
  });
}

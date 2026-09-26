"""Bound public attachments: freeze bytes, verify receipts, and bind transcriptions."""
import base64
import hashlib
import ipaddress
import socket
import json
import re
import http.client
import ssl
import time
from copy import deepcopy
from urllib.parse import urljoin, urlsplit
from ..core.store import Store

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_IMAGE_REDIRECTS = 3
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 20


def _public_image_target(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        raise ValueError('图片地址无效') from None
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError('图片地址无效')
    addresses = socket.getaddrinfo(parsed.hostname, port)
    try:
        public = bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except ValueError:
        public = False
    if not public:
        raise ValueError('附件地址必须是公开图片地址或直接上传的图片数据')
    return parsed, port, addresses


def _remote_image_bytes(url):
    current = url
    visited = set()
    deadline = time.monotonic() + IMAGE_DOWNLOAD_TIMEOUT_SECONDS
    for redirect_count in range(MAX_IMAGE_REDIRECTS + 1):
        if current in visited:
            raise ValueError('图片重定向形成循环')
        visited.add(current)
        parsed, port, addresses = _public_image_target(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('读取原图超时')
        # Connect to the checked address, retaining hostname for Host and TLS.
        # Every redirect is resolved and pinned again, so a public URL cannot
        # redirect or rebind the downloader onto an internal service.
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining)
        try:
            connection.sock = socket.create_connection((addresses[0][4][0], port), timeout=remaining)
            if parsed.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(
                    connection.sock, server_hostname=parsed.hostname)
            connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''))
            response = connection.getresponse()
            if response.status == 200:
                body = bytearray()
                while chunk := response.read(65536):
                    body.extend(chunk)
                    if len(body) > MAX_IMAGE_BYTES:
                        raise ValueError('图片超过 10 MB')
                return bytes(body)
            if response.status not in (301, 302, 303, 307, 308):
                raise ValueError(f'读取原图失败（HTTP {response.status}）')
            if redirect_count >= MAX_IMAGE_REDIRECTS:
                raise ValueError(f'图片重定向超过 {MAX_IMAGE_REDIRECTS} 次')
            location = response.getheader('Location')
            if not isinstance(location, str) or not location.strip() or len(location) > 4096:
                raise ValueError('图片重定向缺少有效 Location')
            next_url = urljoin(current, location.strip())
            next_parsed = urlsplit(next_url)
            if parsed.scheme == 'https' and next_parsed.scheme != 'https':
                raise ValueError('图片重定向不能从 HTTPS 降级到 HTTP')
            current = next_url
        finally:
            connection.close()
    raise ValueError(f'图片重定向超过 {MAX_IMAGE_REDIRECTS} 次')


def image_bytes(url):
    if url.startswith('data:image/'):
        header, encoded = url.split(',', 1)
        if not header.endswith(';base64') or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
            raise ValueError('图片编码无效或超过 10 MB')
        body = base64.b64decode(encoded, validate=True)
    else:
        body = _remote_image_bytes(url)
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise ValueError('图片为空或超过 10 MB')
    if body.startswith(b'\x89PNG\r\n\x1a\n'): mime = 'image/png'
    elif body.startswith(b'\xff\xd8\xff'): mime = 'image/jpeg'
    elif body.startswith((b'GIF87a', b'GIF89a')): mime = 'image/gif'
    elif body.startswith(b'RIFF') and body[8:12] == b'WEBP': mime = 'image/webp'
    else: raise ValueError('附件不是可读取的 PNG、JPEG、GIF 或 WebP 原图')
    return body, mime


def freeze_images(images, previous=()):
    cache = {(item['source_message_id'], item['position']): item for item in previous}
    result = []; total = 0
    for image in images:
        key = (image['source_message_id'], image['position'])
        old = cache.get(key)
        body, mime = image_bytes(image['url'])
        sha = hashlib.sha256(body).hexdigest()
        if old and old.get('sha256') != sha:
            raise ValueError('冻结图片摘要不匹配，请重新领取任务')
        total += len(body)
        if len(result) >= 24 or total > MAX_TOTAL_BYTES:
            raise ValueError('本批图片超过 24 张或 40 MB，请减小输入批次')
        result.append({**image, 'sha256': sha,
                       'url': 'data:' + mime + ';base64,' + base64.b64encode(body).decode()})
    return result


def freeze_task_images(database,batch_id,images,previous=()):
    """Freeze canonical bytes once outside JSON, then hydrate only live requests."""
    previous_by_key={(item['source_message_id'],item['position']):item for item in previous}
    with Store(database,read_only=True) as store:
        cached={(row['source_message_id'],row['position']):dict(row) for row in store.conn.execute(
            'SELECT * FROM pipeline_media WHERE batch_id=?',(batch_id,))}
    frozen=[];new=[];total=0
    for image in images:
        key=(image['source_message_id'],image['position']);old=previous_by_key.get(key);row=cached.get(key)
        if row:
            body=row['body'];mime=row['mime_type'];sha=row['sha256']
        else:
            body,mime=image_bytes(image['url']);sha=hashlib.sha256(body).hexdigest()
            new.append((batch_id,key[0],key[1],sha,mime,body))
        if old and old.get('sha256')!=sha:
            raise ValueError('冻结图片摘要不匹配，请重新领取任务')
        total+=len(body)
        if len(frozen)>=24 or total>MAX_TOTAL_BYTES:
            raise ValueError('本批图片超过 24 张或 40 MB，请减小输入批次')
        frozen.append({**image,'sha256':sha,
                       'url':'data:'+mime+';base64,'+base64.b64encode(body).decode()})
    if new:
        with Store(database) as store,store.transaction(immediate=True):
            store.conn.executemany('INSERT OR IGNORE INTO pipeline_media VALUES (?,?,?,?,?,?)',new)
    return frozen


def persistable_request(request):
    """Keep image receipts in job JSON; canonical bytes live once in pipeline_media."""
    value=deepcopy(request)
    for image in value.get('images',[]):
        image['url']='[frozen task image]'
    return value


def hydrate_request_images(database,batch_id,request):
    images=request.get('images') or []
    pending=[image for image in images if image.get('url')=='[frozen task image]']
    if not pending:return request
    with Store(database,read_only=True) as store:
        cached={(row['source_message_id'],row['position']):dict(row) for row in store.conn.execute(
            'SELECT * FROM pipeline_media WHERE batch_id=?',(batch_id,))}
    for image in pending:
        key=(image['source_message_id'],image['position']);row=cached.get(key)
        if row is None or row['sha256']!=image.get('sha256'):
            raise ValueError('冻结图片材料缺失或摘要不匹配，请重建任务')
        image['url']='data:'+row['mime_type']+';base64,'+base64.b64encode(row['body']).decode()
    return request


def verify_images(images):
    for item in images:
        if not item['url'].startswith('data:image/'):
            raise ValueError('模型输入必须使用已冻结的原图')
        body, _ = image_bytes(item['url'])
        if hashlib.sha256(body).hexdigest() != item['sha256']:
            raise ValueError('冻结图片摘要不匹配')


def bind_transcriptions(output, images):
    rows = output.get('image_transcriptions', [])
    if not isinstance(rows, list) or len(rows) != len(images):
        raise ValueError('每张输入图片必须恰有一份 image_transcriptions 转录')
    bound = {}; verify_images(images)
    for row in rows:
        if isinstance(row, dict) and set(row) == {'input_image', 'text'} and isinstance(row.get('text'), str):
            row = {**row, 'unreadable': not bool(row['text'].strip())}
        if not isinstance(row, dict) or set(row) != {'input_image', 'text', 'unreadable'}:
            raise ValueError('图片转录字段无效；出处由 host 绑定')
        index = row['input_image']
        if type(index) is not int or not 1 <= index <= len(images) or index in bound:
            raise ValueError('图片转录序号重复或超出输入范围')
        if not isinstance(row['text'], str) or len(row['text']) > 40000 or type(row['unreadable']) is not bool:
            raise ValueError('图片转录文字或 unreadable 类型无效')
        if not row['text'].strip() and not row['unreadable']:
            raise ValueError('空白转录必须标记 unreadable')
        image = images[index - 1]
        bound[index] = {**{key: image[key] for key in ('source_message_id', 'position', 'sha256', 'evidence_role')},
                        'text': row['text'], 'unreadable': row['unreadable']}
    return [bound[index] for index in sorted(bound)]


def decision(output):
    return {key: value for key, value in output.items() if key != 'image_transcriptions'}


def verify_transcriptions(transcriptions, images):
    """Require exact coverage, unchanged bytes and unchanged evidence roles."""
    if any(item.get('url') for item in images):verify_images(images)
    actual = {(item['source_message_id'], item['position']): item for item in images}
    seen = set()
    for row in transcriptions:
        key = (row['source_message_id'], row['position'])
        image = actual.get(key)
        if (key in seen or image is None or row['sha256'] != image['sha256']
                or row['evidence_role'] != image['evidence_role']):
            raise ValueError('Writer 转录与绑定图片的字节或阅读范围不匹配')
        seen.add(key)
    if seen != set(actual):
        raise ValueError('Writer 转录必须覆盖全部绑定图片')


def _strip_task_media(value,key=''):
    if key in ('image_transcriptions','curator_image_transcriptions'):return value
    if isinstance(value,dict):return {name:_strip_task_media(item,name) for name,item in value.items()}
    if isinstance(value,list):return [_strip_task_media(item,key) for item in value]
    if isinstance(value,str):
        if key=='content_base64' or value.startswith('data:image/'):
            return '[expired task image]'
        return re.sub(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+','[expired task image]',value)
    return value


def compact_batch_snapshot(data):
    """Keep the exact Router proof while dropping completed downstream material."""
    keys=('contract','scope','source','day','routing_messages','routing_result',
          'last_routing_repair','rebuild_of')
    compact={key:data[key] for key in keys if key in data}
    compact['task_snapshot_compacted']=True
    return _strip_task_media(compact)


def compact_job_request(value):
    """Retain Router replay inputs; downstream jobs keep only an audit receipt."""
    request=json.loads(value) if isinstance(value,str) else value
    if str(request.get('role','')).startswith('track_router') or request.get('role')=='track_router':
        return _strip_task_media(request)
    raw=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return {'role':request.get('role',''),'batch_id':request.get('batch_id',''),
            'contract':request.get('contract',''),'execution':request.get('execution',{}),
            'task_snapshot_compacted':True,'request_sha256':hashlib.sha256(raw.encode()).hexdigest(),
            'prompt_chars':len(request.get('prompt',''))+len(request.get('rules',''))}


def compact_completed_snapshots(store):
    """One-time recovery for historical done batches with materialized task copies."""
    rows=store.conn.execute("SELECT id,input_json,result_json FROM pipeline_batches WHERE status='done' "
        "AND COALESCE(json_extract(result_json,'$.task_snapshot_compacted'),0)=0").fetchall()
    for row in rows:
        try:
            data=json.loads(row['input_json']);result=json.loads(row['result_json'] or '{}')
            if not isinstance(data,dict) or not isinstance(data.get('routing_messages'),list):continue
            batch_json=json.dumps(compact_batch_snapshot(data),ensure_ascii=False,sort_keys=True,separators=(',',':'))
            jobs=[]
            for job in store.conn.execute('SELECT id,request_json FROM pipeline_jobs WHERE batch_id=?',(row['id'],)).fetchall():
                compact=compact_job_request(job['request_json'])
                jobs.append((json.dumps(compact,ensure_ascii=False,sort_keys=True,separators=(',',':')),job['id']))
        except (KeyError,TypeError,ValueError,json.JSONDecodeError):
            continue
        result['task_snapshot_compacted']=True
        for request_json,job_id in jobs:
            store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',(request_json,job_id))
        store.conn.execute('DELETE FROM pipeline_media WHERE batch_id=?',(row['id'],))
        store.conn.execute('UPDATE pipeline_batches SET input_json=?,result_json=? WHERE id=?',
            (batch_json,json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':')),row['id']))


def expire_completed_media(store):
    """Only disposable task copies expire. Canonical raw attachments remain intact."""
    rows=store.conn.execute("SELECT id,input_json,result_json FROM pipeline_batches WHERE status='done' AND json_extract(result_json,'$.media_cache_expired') IS NULL AND datetime(json_extract(result_json,'$.completed_at'))<datetime('now','-7 days')").fetchall()
    for row in rows:
        result=json.loads(row['result_json']);result['media_cache_expired']=True
        store.conn.execute('UPDATE pipeline_batches SET input_json=?,result_json=? WHERE id=?',
            (json.dumps(_strip_task_media(json.loads(row['input_json'])),ensure_ascii=False),json.dumps(result),row['id']))
        for job in store.conn.execute('SELECT id,request_json FROM pipeline_jobs WHERE batch_id=?',(row['id'],)).fetchall():
            store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',
                (json.dumps(_strip_task_media(json.loads(job['request_json'])),ensure_ascii=False),job['id']))

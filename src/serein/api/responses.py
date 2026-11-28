"""A dialogue view for Serein memory, alongside the original Responses wire items."""
import json

import httpx
from fastapi import HTTPException

from ..core.store import Store, encode


def dialogue_part(part):
    if not isinstance(part, dict):
        raise HTTPException(400, 'Responses content parts must be objects')
    kind = part.get('type')
    if kind in ('input_text', 'output_text'):
        return {**part, 'type': 'text'}
    if kind == 'input_image':
        return {'type': 'image_url', 'image_url': {k: v for k, v in (
            ('url', part.get('image_url')), ('file_id', part.get('file_id')), ('detail', part.get('detail'))
        ) if v is not None}}
    return part


def native_part(part):
    if part.get('type') == 'text':
        return {**part, 'type': 'input_text'}
    if part.get('type') == 'image_url':
        image = dict(part['image_url'])
        if 'url' in image:
            image['image_url'] = image.pop('url')
        return {'type': 'input_image', **image}
    return part


class ResponseInput:
    def __init__(self, body):
        value = body.get('input')
        if 'messages' in body:
            raise HTTPException(400, 'Use input for the Responses API')
        self.was_string = isinstance(value, str)
        self.items = [{'role': 'user', 'content': value}] if self.was_string else value
        if self.items is None and ('prompt' in body or 'instructions' in body or body.get('previous_response_id') or body.get('conversation')):
            self.items = []
        if not isinstance(self.items, list) or any(not isinstance(i, dict) for i in self.items):
            raise HTTPException(400, 'Responses input must be text or an array of items')
        if body.get('background'):
            raise HTTPException(400, 'Background Responses polling is not supported by this chat gateway')
        conversation = body.get('conversation')
        self.conversation_id = conversation.get('id') if isinstance(conversation, dict) else conversation
        if self.conversation_id is not None and not isinstance(self.conversation_id, str):
            raise HTTPException(400, 'Responses conversation must specify a conversation ID')
        self.tool_submission = bool(self.items and str(self.items[-1].get('type','')).endswith(('_output','_response')))
        self.messages = []
        for index, item in enumerate(self.items):
            if item.get('type', 'message') == 'message' and item.get('role') in ('system','developer','user','assistant'):
                content = item.get('content')
                if not isinstance(content, (str, list)):
                    raise HTTPException(400, 'Responses messages require content')
                message = {'role': item['role'], 'content': (
                    [dialogue_part(p) for p in content] if isinstance(content, list) else content)}
                if item.get('id'):
                    message['id'] = item['id']
            else:
                # Reasoning, tool results and item references stay opaque to memory.
                message = {'role': 'tool', 'content': '', 'tool_call_id': item.get('call_id') or item.get('id')}
            self.messages.append({**message, '_response_index': index})

    def wire_input(self, prepared):
        items = []
        for message in prepared:
            index = message.get('_response_index')
            if index is None:
                items.append({'role': message['role'], 'content': message['content']})
                continue
            item = self.items[index]
            content = message.get('content')
            if content != self.messages[index]['content']:
                item = {**item, 'content': [native_part(p) for p in content] if isinstance(content, list) else content}
            items.append(item)
        if self.was_string and len(items) == 1 and isinstance(items[0].get('content'), str):
            return items[0]['content']
        return items


def assistant_output(result):
    """Use only final assistant speech for the archive. Keep the wire response intact."""
    text, calls = [], []
    if not isinstance(result.get('output'), list):
        raise ValueError('Missing Responses output')
    for item in result['output']:
        if not isinstance(item, dict):
            raise ValueError('Invalid Responses output item')
        kind = item.get('type')
        if kind == 'message' and item.get('role') == 'assistant' and item.get('phase') != 'commentary':
            for part in item['content']:
                if not isinstance(part, dict):
                    raise ValueError('Invalid Responses message content')
                if part.get('type') == 'output_text':
                    text.append(part['text'])
                elif part.get('type') == 'refusal':
                    text.append(part['refusal'])
        elif kind in ('function_call', 'custom_tool_call', 'computer_call', 'local_shell_call',
                      'shell_call', 'apply_patch_call', 'mcp_approval_request'):
            calls.append({'id': item.get('call_id') or item.get('id'), 'type': kind})
    return {'role': 'assistant', 'content': ''.join(text), 'tool_calls': calls}


def reference_key(model, window, identifier):
    return 'responses-turn:' + encode([model.get('upstream_id'), model['base_url'], window, identifier])


def remember_tool_turn(database, model, turn, result, message, receipt):
    ids = receipt.get('message_ids') or []
    if not turn or not ids or not message.get('tool_calls'):
        return
    refs = [result.get('id'), *(call.get('id') for call in message['tool_calls'])]
    with Store(database) as store, store.transaction(immediate=True):
        store.conn.executemany('INSERT OR REPLACE INTO background_state(name,value_json) VALUES (?,?)',
            ((reference_key(model, turn['window_id'], ref), encode(ids[0])) for ref in refs if ref))


def previous_tool_turn(database, model, window, body, items):
    # previous_response_id continuations need not replay the original user message.
    refs = []
    for item in reversed(items):
        if item.get('role') == 'user':
            break
        if str(item.get('type','')).endswith(('_output','_response')):
            refs.append(item.get('call_id') or item.get('approval_request_id'))
    else:
        refs.append(body.get('previous_response_id'))
    if not any(refs):
        return None
    with Store(database, read_only=True) as store:
        for ref in filter(None, refs):
            link = store.conn.execute('SELECT value_json FROM background_state WHERE name=?',
                                      (reference_key(model, window, ref),)).fetchone()
            if not link:
                continue
            row = store.conn.execute("SELECT * FROM raw_events WHERE id=? AND session_id=? AND source='serein_chat' AND role='user'",
                                     (json.loads(link[0]), window)).fetchone()
            if row:
                meta = json.loads(row['metadata_json'])
                return {'window_id': window, 'key': row['source_event_id'].removesuffix(':user'),
                        'message_id': meta.get('original_message_id',''), 'received_at': row['created_at'],
                        'user': {'role':'user', 'text':row['text'], 'attachments':meta.get('attachments',[])}}


async def response_stream(upstream, client, finish, observation):
    """Forward native SSE frames, including event names and opaque reasoning deltas."""
    data = []
    terminal = False
    try:
        async for line in upstream.aiter_lines():
            if line.startswith('data:'):
                data.append(line[5:].lstrip(' '))
            if not line and data:
                payload = '\n'.join(data)
                data.clear()
                if payload != '[DONE]':
                    event = json.loads(payload)
                    if not isinstance(event, dict):
                        raise ValueError('Invalid Responses stream event')
                    kind = event.get('type')
                    if kind == 'response.completed' and not terminal:
                        finish(event['response'])
                        terminal = True
                    elif kind in ('response.failed','response.incomplete','error'):
                        observation.finish('failed', reason=kind)
                        terminal = True
            yield line + '\n'
        if not terminal:
            yield '\nevent: error\ndata: {"type":"error","code":"incomplete_stream","message":"Upstream stream ended before completion"}\n\n'
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        observation.finish('failed', reason='invalid_upstream_stream')
        yield '\nevent: error\ndata: {"type":"error","code":"invalid_stream","message":"Upstream response stream was interrupted or invalid"}\n\n'
    finally:
        observation.finish('interrupted', reason='stream_not_completed')
        await upstream.aclose()
        await client.aclose()

"""Archive the current client turn, never the proxy's prepared prompt/history."""
import re

from .chat_context import ClientContext
from .compat.raw_archive import raw_archive
from .compat.germany.raw_text import strip_worldbook_context
from .core.store import digest, encode, now


def original(message):
    context = ClientContext()
    text = context._coerce_message_text(message.get('content'))
    # Clients can echo a previously prepared user message on tool continuations.
    text = re.sub(r'<serein_live_context>.*?</serein_live_context>\s*(?:Current user message:\s*)?', '', text, flags=re.S)
    # Clean before turn identity is hashed, not only when the row is inserted.
    text = strip_worldbook_context(text)
    text = context._strip_external_context_from_user_text(text).strip()
    images = []
    content = message.get('content')
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict) or part.get('type') != 'image_url':
            continue
        image = part.get('image_url')
        url = image.get('url') if isinstance(image, dict) else image
        if isinstance(url, str) and url.startswith(('https://', 'http://', 'data:image/')):
            images.append({'kind': 'image', 'url': url})
    return {'role': message['role'], 'text': text or ('[图片]' if images else ''),
            'attachments': images}


def prepare_turn(window_id, incoming):
    """Stable across tool continuations/retries; only hash prior dialogue for identity."""
    history = ''
    anchor = None
    for message in incoming:
        if message.get('role') not in ('user', 'assistant') or message.get('tool_calls'):
            continue
        item = original(message)
        if not item['text']:
            # A context-only user turn must not reuse an older dialogue anchor.
            # Native tool-result user messages still continue the original turn.
            if item['role'] == 'user' and not ClientContext._message_has_tool_protocol(message):
                anchor = None
            continue
        history = digest(encode([history, item]))
        if item['role'] == 'user':
            message_id = message.get('id') or message.get('message_id')
            identity = [str(message_id), item] if message_id else history
            anchor = {'key': digest(encode([window_id, identity])), 'user': item,
                      'message_id': str(message_id) if message_id else '',
                      'window_id': window_id, 'received_at': now()}
    return anchor


def _user_event(turn):
    user = turn['user']
    metadata = {'archive_version': 1, 'timestamp_source': 'proxy_received',
                'attachments': user['attachments']}
    if turn['message_id']:
        metadata['original_message_id'] = turn['message_id']
    return {'source': 'serein_chat', 'source_event_id': turn['key'] + ':user',
            'role': user['role'], 'text': user['text'], 'created_at': turn['received_at'],
            'session_id': turn['window_id'], 'conversation_id': turn['window_id'],
            'client': 'serein_chat_proxy', 'metadata': metadata}


def archive_user_turn(settings, turn):
    if turn is None:
        return {'status': 'skipped', 'reason': 'no_user_message', 'message_ids': []}
    result = raw_archive(settings).ingest([_user_event(turn)], source='serein_chat')
    return {'status': 'rejected' if result['rejected'] else 'recorded',
            'inserted': result['inserted'], 'duplicate': result['duplicate'],
            'rejected': result['rejected'], 'message_ids': [item['id'] for item in result['items'] if item.get('id')]}


def archive_turn(settings, turn, message):
    if turn is None:
        return {'status': 'skipped', 'reason': 'no_user_message'}
    items = []
    if not message.get('tool_calls'):
        answer = original({**message, 'role': 'assistant'})
        if answer['text']:
            items.append((answer, 'assistant:' + digest(encode(answer)), now(), 'response_completed'))
    events = [_user_event(turn)]
    for item, suffix, stamp, time_source in items:
        metadata = {'archive_version': 1, 'timestamp_source': time_source,
                    'attachments': item['attachments']}
        events.append({'source': 'serein_chat', 'source_event_id': turn['key'] + ':' + suffix,
                       'role': item['role'], 'text': item['text'], 'created_at': stamp,
                       'session_id': turn['window_id'], 'conversation_id': turn['window_id'],
                       'client': 'serein_chat_proxy', 'metadata': metadata})
    result = raw_archive(settings).ingest(events, source='serein_chat')
    return {'status': 'rejected' if result['rejected'] else 'recorded',
            'inserted': result['inserted'], 'duplicate': result['duplicate'],
            'rejected': result['rejected'], 'message_ids': [item['id'] for item in result['items'] if item.get('id')]}

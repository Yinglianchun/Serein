"""Optional minimum of two complete owned exchanges before Event writing."""

from copy import deepcopy

from .pipeline_materials import substantive_ids


def add_lookahead(components):
    """Show a few later originals from the same session without making them owned."""
    known = {int(message['id']): message for component in components
             for message in component.get('context_messages') or []}
    for component in components:
        tail = max(int(message['id']) for message in component['messages'])
        sessions = {message['session_id'] for message in component['messages']}
        extra, size = [], 0
        for source_id, message in sorted(known.items()):
            if source_id <= tail or message['session_id'] not in sessions:
                continue
            next_size = size + len(str(message.get('content') or ''))
            if len(extra) >= 6 or next_size > 12_000:
                break
            extra.append(message)
            size = next_size
        context = {int(message['id']): message for message in
                   [*component.get('context_messages', []), *extra]}
        component['context_messages'] = sorted(context.values(), key=lambda message: int(message['id']))
        component['boundary_lookahead'] = extra


def _visible(message):
    metadata = message.get('metadata') or {}
    return (message.get('role') in {'user', 'assistant'}
            and message.get('source') not in {'memory_event', 'trace', 'error'}
            and not any(metadata.get(key) for key in
                        ('memory_review_only', 'memory_event_source', 'draft', 'discarded')))


def exchanges(messages):
    """Group adjacent role runs; separate a completed exchange at the next user."""
    result, current = [], []
    for message in sorted(messages, key=lambda row: int(row['id'])):
        if not _visible(message):
            continue
        if current and message.get('session_id') != current[0].get('session_id'):
            result.append(current)
            current = []
        if {row['role'] for row in current} == {'user', 'assistant'} and message['role'] == 'user':
            result.append(current)
            current = []
        current.append(message)
    if current:
        result.append(current)
    return result


def count_rounds(owned_ids, messages):
    owned = set(owned_ids)
    return sum({row['role'] for row in unit} == {'user', 'assistant'}
               and all(int(row['id']) in owned for row in unit)
               for unit in exchanges(messages))


def validate(review, output, component):
    if not component.get('writer_round_gate'):
        return {}
    if not isinstance(review, dict) or not isinstance(review.get('events'), list):
        raise ValueError('Round admission needs per-Event decisions')
    messages = {int(row['id']): row for row in component.get('context_messages') or []}
    result = {}
    for row in review['events']:
        index = row['event_index']
        admission = row.get('admission')
        if not isinstance(admission, dict) or set(admission) != {'closed_by'}:
            raise ValueError('Every Event needs admission.closed_by')
        event = output['events'][index]
        owned = {item['source_message_id'] for item in event['source_bindings']}
        closure = admission['closed_by']
        if closure is not None:
            if not isinstance(closure, dict) or set(closure) != {'source_message_id', 'quote'}:
                raise ValueError('Admission closure needs a source ID and verbatim quote')
            source_id, quote = closure['source_message_id'], closure['quote']
            if (type(source_id) is not int or source_id not in messages or source_id < max(owned)
                    or not isinstance(quote, str) or not quote.strip()
                    or quote not in str(messages[source_id].get('content') or '')
                    or messages[source_id].get('session_id') not in
                    {messages[key].get('session_id') for key in owned if key in messages}):
                raise ValueError('Admission closure must quote later original in the same session')
        result[event['event_ref']] = admission
    return result


def apply_gate(plan, component):
    """Short open activities remain pending; an explicitly closed short activity is skipped."""
    if not component.get('writer_round_gate'):
        return plan
    result = deepcopy(plan)
    scope = {int(row['id']): row for row in component.get('context_messages') or []}
    stable = {int(row['id']) for row in component['messages']}
    admissions = plan['event_admissions']
    accepted, pending, skipped, receipts = [], set(), set(), []
    for event in result['events']:
        admission = admissions[event['event_ref']]
        rounds = count_rounds(substantive_ids(event), scope.values())
        owned = set(event['source_message_ids'])
        waiting_reply = (admission['closed_by'] is None and any(
            {row['role'] for row in unit} == {'user'}
            and owned.intersection(int(row['id']) for row in unit)
            for unit in exchanges(scope.values())))
        disposition = ('defer' if waiting_reply else 'write' if rounds >= 2 else
                       'skip' if admission['closed_by'] else 'defer')
        receipts.append({'event_ref': event['event_ref'], 'rounds': rounds,
                         'minimum': 2, 'disposition': disposition})
        if disposition == 'write':
            accepted.append(event)
        else:
            (pending if disposition == 'defer' else skipped).update(owned & stable)
    while True:
        blocked = [event for event in accepted if pending.intersection(event['source_message_ids'])]
        if not blocked:
            break
        for event in blocked:
            pending.update(set(event['source_message_ids']) & stable)
            accepted.remove(event)
            next(row for row in receipts if row['event_ref'] == event['event_ref'])['disposition'] = 'defer_shared_bridge'
    retained = {source_id for event in accepted for source_id in event['source_message_ids']}
    result['events'] = accepted
    result['defer_source_message_ids'] = sorted(set(result['defer_source_message_ids']) | pending)
    result['skip_source_message_ids'] = sorted(set(result['skip_source_message_ids']) | (skipped - pending - retained))
    result['admission_receipts'] = receipts
    return result

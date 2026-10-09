"""Per-Event writing choices grounded in the frozen owned originals."""

from copy import deepcopy
import hashlib
import json


USES = {'main', 'background', 'omit', 'mixed'}
MATERIAL_CONTRACT_VERSION = 1
_SOURCE_FIELDS = ('id', 'content', 'role', 'session_id', 'original_session_id',
                  'source', 'source_event_id', 'created_at', 'metadata',
                  'image_transcription')


def _source_ids(event):
    """Return complete, unambiguous ownership, never a silently deduplicated set."""
    if not isinstance(event, dict):
        return None
    ids = event.get('source_message_ids')
    if 'source_bindings' in event:
        bindings = event['source_bindings']
        if not isinstance(bindings, list) or any(not isinstance(row, dict) for row in bindings):
            return None
        bound = [row.get('source_message_id') for row in bindings]
        if any(type(value) is not int for value in bound):
            return None
        if ids is not None:
            if (not isinstance(ids, list) or any(type(value) is not int for value in ids)
                    or len(ids) != len(set(ids)) or set(ids) != set(bound)):
                return None
        ids = bound
    if (not isinstance(ids, list) or any(type(value) is not int for value in ids)
            or len(ids) != len(set(ids))):
        return None
    return list(ids)


def _source_fingerprints(source_ids, messages):
    """Hash the canonical originals and their evidence-bearing metadata."""
    by_id = {}
    try:
        for message in messages:
            if not isinstance(message, dict) or type(message.get('id')) is not int:
                return None
            source_id = message['id']
            if source_id in by_id:
                return None
            by_id[source_id] = message
        if not set(source_ids).issubset(by_id):
            return None
        fingerprints = []
        for source_id in sorted(source_ids):
            message = by_id[source_id]
            canonical = {key: message.get(key) for key in _SOURCE_FIELDS}
            canonical['image_transcription'] = _image_evidence(message.get('image_transcription'), source_id)
            encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                                 separators=(',', ':'), allow_nan=False).encode('utf-8')
            fingerprints.append({'source_message_id': source_id,
                                 'fingerprint': hashlib.sha256(encoded).hexdigest()})
        return fingerprints
    except (TypeError, ValueError, OverflowError):
        return None


def _image_evidence(record, source_id):
    """Bind image meaning, not cache timestamps or corridor-specific read roles."""
    if record is None:
        return None
    if not isinstance(record, dict) or not isinstance(record.get('items'), list):
        raise ValueError('Invalid image evidence')
    items, positions = [], set()
    for row in record['items']:
        if (not isinstance(row, dict) or type(row.get('position')) is not int
                or row['position'] < 1 or row['position'] in positions
                or not isinstance(row.get('sha256'), str)
                or not isinstance(row.get('text'), str)
                or type(row.get('unreadable')) is not bool
                or ('source_message_id' in row and
                    (type(row['source_message_id']) is not int or row['source_message_id'] != source_id))):
            raise ValueError('Invalid image evidence')
        positions.add(row['position'])
        items.append({key: row[key] for key in ('position', 'sha256', 'text', 'unreadable')})
    return sorted(items, key=lambda row: row['position'])


def _valid_materials(materials, source_ids, messages):
    """Snapshots must satisfy the same complete-material checks as fresh reviews."""
    event = {'event_ref': 'snapshot', 'source_bindings': [
        {'source_message_id': source_id} for source_id in source_ids]}
    try:
        attach({'events': [{'event_index': 0, 'materials': materials}]},
               {'events': [event]}, {'events': []},
               {'writer_material_review': True, 'context_messages': messages})
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return False
    return True


def snapshot_for(event, messages):
    """Freeze a full per-Event material receipt; settlement binds the saved identity.

    The host adds event_id and event_fingerprint only after saving the Event. A
    constructor result alone cannot be reused as a trusted predecessor receipt.
    """
    source_ids = _source_ids(event)
    if (not source_ids or not isinstance(event.get('event_ref'), str)
            or not event['event_ref'].strip()):
        return None
    try:
        messages = list(messages)
    except TypeError:
        return None
    fingerprints = _source_fingerprints(source_ids, messages)
    materials = event.get('source_materials')
    if fingerprints is None or not _valid_materials(materials, source_ids, messages):
        return None
    return {'schema': MATERIAL_CONTRACT_VERSION, 'event_ref': event['event_ref'],
            'source_fingerprints': fingerprints, 'source_materials': deepcopy(materials)}


def trusted_materials(base, messages):
    """Return complete inherited decisions, or fail closed to full annotation."""
    if not isinstance(base, dict):
        return None
    snapshot = base.get('material_snapshot')
    if (not isinstance(snapshot, dict) or type(snapshot.get('schema')) is not int
            or snapshot['schema'] != MATERIAL_CONTRACT_VERSION
            or not isinstance(snapshot.get('event_ref'), str)
            or not snapshot['event_ref'].strip()):
        return None
    for snapshot_key, base_key in (('event_id', 'event_id'), ('event_fingerprint', 'fingerprint')):
        value = base.get(base_key)
        if not isinstance(value, str) or not value.strip() or snapshot.get(snapshot_key) != value:
            return None
    source_ids = _source_ids(base)
    if not source_ids:
        return None
    try:
        messages = list(messages)
    except TypeError:
        return None
    fingerprints = _source_fingerprints(source_ids, messages)
    saved = snapshot.get('source_fingerprints')
    if (fingerprints is None or not isinstance(saved, list)
            or any(not isinstance(row, dict)
                   or set(row) != {'source_message_id', 'fingerprint'}
                   or type(row['source_message_id']) is not int
                   or not isinstance(row['fingerprint'], str) for row in saved)
            or saved != fingerprints):
        return None
    materials = snapshot.get('source_materials')
    if not _valid_materials(materials, source_ids, messages):
        return None
    return deepcopy(materials)


def annotation_contract(event, component):
    """Derive annotation scope without changing the Event's expanded ownership."""
    owned_ids = _source_ids(event) if isinstance(event, dict) and 'source_bindings' in event else None
    if owned_ids is None:
        raise ValueError('Material contract needs complete, unique Event source_bindings')
    contract = {'annotation_source_message_ids': owned_ids, 'inherited_materials': []}
    version = component.get('material_contract_version')
    if (not component.get('writer_material_review') or type(version) is not int
            or version != MATERIAL_CONTRACT_VERSION or event.get('action') != 'extend'):
        return contract
    base_ids = event.get('base_event_ids')
    if not isinstance(base_ids, list) or len(base_ids) != 1 or not isinstance(base_ids[0], str):
        return contract
    base_id = base_ids[0].strip()
    bases = [base for base in component.get('base_event_candidates') or []
             if isinstance(base, dict) and (base.get('event_id') == base_id
                 or (isinstance(base.get('predecessor_event_ids'), list)
                     and base_id in base['predecessor_event_ids']))]
    if len(bases) != 1:
        return contract
    materials = trusted_materials(bases[0], component.get('context_messages') or [])
    if materials is None:
        return contract
    inherited_ids = {row['source_message_id'] for row in materials}
    if not inherited_ids.issubset(owned_ids):
        return contract
    return {'annotation_source_message_ids': [value for value in owned_ids if value not in inherited_ids],
            'inherited_materials': materials}


def material_contracts(output, component):
    """Map expanded Event indexes to their host-computed annotation contracts."""
    return {index: annotation_contract(event, component)
            for index, event in enumerate(output['events'])}


def merge_review(review, output, component):
    """Validate exact annotation deltas, then restore full material receipts.

    Boundary and admission checks consume this complete review. Neither the
    model's reply nor the frozen candidate snapshot is mutated.
    """
    merged = deepcopy(review)
    if not component.get('writer_material_review'):
        return merged
    if not isinstance(merged, dict) or not isinstance(merged.get('events'), list):
        raise ValueError('Curator material review is missing')
    contracts = material_contracts(output, component)
    seen_events = set()
    for row in merged['events']:
        if not isinstance(row, dict) or type(row.get('event_index')) is not int:
            raise ValueError('Curator material review needs an integer event_index')
        index = row['event_index']
        if index not in contracts or index in seen_events:
            raise ValueError(f'Curator material review has unexpected/duplicate event_index={index}')
        seen_events.add(index)
        contract = contracts[index]
        required = set(contract['annotation_source_message_ids'])
        materials = row.get('materials')
        if not isinstance(materials, list):
            raise ValueError(f'Event {index} needs materials; missing source_message_ids={sorted(required)}')
        seen, duplicates, unexpected = set(), set(), set()
        for item in materials:
            if not isinstance(item, dict) or type(item.get('source_message_id')) is not int:
                raise ValueError(f'Event {index} has invalid material source_message_id; '
                                 f'expected source_message_ids={sorted(required)}')
            source_id = item['source_message_id']
            if source_id in seen:
                duplicates.add(source_id)
            if source_id not in required:
                unexpected.add(source_id)
            seen.add(source_id)
        missing = required - seen
        if missing or duplicates or unexpected:
            raise ValueError(f'Event {index} materials must exactly cover annotation sources once: '
                             f'missing source_message_ids={sorted(missing)}, '
                             f'duplicate source_message_ids={sorted(duplicates)}, '
                             f'unexpected source_message_ids={sorted(unexpected)}; '
                             f'expected source_message_ids={sorted(required)}')
        by_id = {item['source_message_id']: item for item in
                 [*contract['inherited_materials'], *materials]}
        row['materials'] = [by_id[binding['source_message_id']]
                            for binding in output['events'][index]['source_bindings']]
    missing_events = set(contracts) - seen_events
    if missing_events:
        raise ValueError(f'Curator material review is missing event_indexes={sorted(missing_events)}')
    try:
        attach(merged, output, {'events': []}, component)
    except (TypeError, KeyError, IndexError) as error:
        raise ValueError('Invalid Curator material fields in merged review') from error
    return merged


def retains_boundary_quote(material, content, quote):
    """An original quote must survive without joining text across omissions."""
    if not isinstance(material, dict) or material.get('use') not in {'main', 'mixed'}:
        return False
    omissions = material.get('omit_quotes')
    if (not isinstance(omissions, list)
            or any(not isinstance(item, str) or not item.strip() or item not in content for item in omissions)
            or (material['use'] == 'main' and omissions)
            or (material['use'] == 'mixed' and not omissions)):
        return False
    # Compare intervals in the original. Repeated omission text is ambiguous,
    # so none of its occurrences may serve as this side's boundary evidence.
    omitted = []
    for text in omissions:
        start = content.find(text)
        while start >= 0:
            omitted.append((start, start + len(text)))
            start = content.find(text, start + 1)
    start = content.find(quote)
    while start >= 0:
        if not any(start < end and start + len(quote) > left for left, end in omitted):
            return True
        start = content.find(quote, start + 1)
    return False


def attach(review, output, plan, component):
    """Validate Curator annotations, then pass them to the matching Writer job."""
    if not component.get('writer_material_review'):
        return
    messages = {int(item['id']): str(item.get('content') or '')
                for item in component.get('context_messages') or []}
    if not isinstance(review, dict) or not isinstance(review.get('events'), list):
        raise ValueError('Curator material review is missing')
    accepted = {event['event_ref']: event for event in plan['events']}
    for row in review['events']:
        event = output['events'][row['event_index']]
        materials = row.get('materials')
        if not isinstance(materials, list):
            raise ValueError('Every Event needs materials for each owned source')
        owned = {item['source_message_id'] for item in event['source_bindings']}
        seen = set()
        for item in materials:
            if not isinstance(item, dict) or set(item) != {'source_message_id', 'use', 'reason', 'omit_quotes'}:
                raise ValueError('Invalid Curator material fields')
            source_id, use, quotes = item['source_message_id'], item['use'], item['omit_quotes']
            if type(source_id) is not int or source_id not in owned or source_id in seen:
                raise ValueError('Materials must exactly cover owned sources once')
            seen.add(source_id)
            if use not in USES or not isinstance(item['reason'], str) or not item['reason'].strip():
                raise ValueError('Invalid material use or reason')
            if not isinstance(quotes, list) or any(not isinstance(quote, str) or not quote.strip()
                    or quote not in messages.get(source_id, '') for quote in quotes):
                raise ValueError('Material omission quotes must be verbatim')
            if (use in {'main', 'background'} and quotes) or (use == 'mixed' and not quotes):
                raise ValueError('Mixed sources need omission quotes; main/background cannot omit')
            if use == 'mixed':
                remaining = messages.get(source_id, '')
                for quote in quotes:
                    remaining = remaining.replace(quote, '', 1)
                if not remaining.strip():
                    raise ValueError('Mixed source must retain content')
        if seen != owned:
            raise ValueError('Materials must exactly cover owned sources once')
        if event['event_ref'] in accepted:
            accepted[event['event_ref']]['source_materials'] = materials


def substantive_ids(event):
    materials = event.get('source_materials')
    if materials is None:
        return event['source_message_ids']
    return [item['source_message_id'] for item in materials if item['use'] in {'main', 'mixed'}]

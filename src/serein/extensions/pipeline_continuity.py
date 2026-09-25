"""Bounded joint review of Tracks connected by a declared stable bridge."""

from copy import deepcopy

MAX_TRACKS = 3
MAX_CHARACTERS = 120_000


def validate_bridge_owners(output, component, review=None):
    """Require an explicit source-backed decision before leaving a bridge one-sided."""
    units = {int(unit['unit_root_message_id']): unit
             for unit in component.get('memberships') or []}
    missing = set()
    for edge in component.get('context_edges') or []:
        root = int(edge['unit_root_message_id'])
        unit = units.get(root)
        if not unit or unit.get('routing_role') != 'bridge' or edge.get('relation') != 'bridge':
            continue
        tracks = {str(unit['track_id']), str(edge['track_id'])}
        if len(tracks) != 2:
            continue
        events = [event for event in output['events'] if event['primary_track_id'] in tracks]
        if {event['primary_track_id'] for event in events} != tracks:
            continue
        sources = set(unit.get('source_message_ids') or [root])
        owners = {event['primary_track_id'] for event in events
                  if sources.issubset({binding['source_message_id']
                                       for binding in event['source_bindings']})}
        if owners:
            missing.update((root, track) for track in tracks - owners)
    if review is not None and not isinstance(review, dict):
        raise ValueError('Curator decision_review must be an object')
    exclusions = (review or {}).get('bridge_exclusions', [])
    if not isinstance(exclusions, list):
        raise ValueError('bridge_exclusions must be a list')
    messages = {int(message['id']): message for message in component.get('messages') or []}
    seen = set()
    for row in exclusions:
        if not isinstance(row, dict) or set(row) != {
                'unit_root_message_id', 'excluded_track_id', 'reason', 'evidence'}:
            raise ValueError('Invalid bridge_exclusions fields')
        root, track = row['unit_root_message_id'], row['excluded_track_id']
        if (type(root) is not int or not isinstance(track, str)
                or (root, track) not in missing or (root, track) in seen):
            raise ValueError('Bridge exclusion must identify one missing side exactly once')
        if not isinstance(row['reason'], str) or not row['reason'].strip():
            raise ValueError('Bridge exclusion needs a grounded reason')
        sources = set(units[root].get('source_message_ids') or [root])
        evidence = row['evidence']
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('Bridge exclusion needs verbatim evidence from its unit')
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {'source_message_id', 'quote'}:
                raise ValueError('Invalid bridge exclusion evidence')
            source_id, quote = item['source_message_id'], item['quote']
            if (type(source_id) is not int or source_id not in sources
                    or source_id not in messages or not isinstance(quote, str)
                    or not quote.strip()
                    or quote not in str(messages[source_id].get('content') or '')):
                raise ValueError('Bridge exclusion evidence must be verbatim in its unit')
        seen.add((root, track))
    if seen != missing:
        raise ValueError('Missing bridge ownership decision for ' + str(sorted(missing - seen)))


def bounded_components(components):
    components = deepcopy(components)
    by_track = {track: index for index, component in enumerate(components)
                for track in component['track_ids']}
    parent = list(range(len(components)))

    def find(index):
        while parent[index] != index:
            index = parent[index]
        return index

    memberships = {}
    stable = {int(message['id']) for component in components for message in component['messages']}
    edges = set()
    for component in components:
        for unit in component['memberships']:
            root = int(unit['unit_root_message_id'])
            previous = memberships.get(root)
            if previous and any(previous.get(key) != unit.get(key)
                                for key in ('track_id', 'source_message_ids', 'session_id')):
                raise ValueError('Continuity review has conflicting unit membership')
            memberships[root] = unit
        edges.update((int(edge['unit_root_message_id']), str(edge['track_id']))
                     for edge in component.get('context_edges') or []
                     if edge.get('relation') == 'bridge')
    pairs, excluded = [], []
    for root, other in sorted(edges):
        unit = memberships.get(root, {})
        owner = str(unit.get('track_id') or '')
        sources = set(unit.get('source_message_ids') or [root])
        if (owner == other or owner not in by_track or other not in by_track
                or unit.get('routing_role') != 'bridge' or not sources.issubset(stable)):
            continue
        left, right = find(by_track[owner]), find(by_track[other])
        if left == right:
            continue
        group = [component for index, component in enumerate(components)
                 if find(index) in {left, right}]
        tracks = {track for component in group for track in component['track_ids']}
        messages = {int(message['id']): message for component in group
                    for message in component['context_messages']}
        sessions = {message.get('session_id') for component in group
                    for message in component['messages']}
        pair = {'left_track_id': owner, 'right_track_id': other, 'bridge_unit_root': root}
        if (len(tracks) > MAX_TRACKS or len(sessions) != 1
                or sum(len(str(message.get('content') or '')) for message in messages.values()) > MAX_CHARACTERS
                or any(component.get('base_event_candidate_overflow') for component in group)):
            excluded.append({**pair, 'reason': 'bounded_review_limit'})
            continue
        parent[right] = left
        pairs.append(pair)
    groups = {}
    for index, component in enumerate(components):
        groups.setdefault(find(index), []).append(component)
    result = []
    for group in groups.values():
        tracks = list(dict.fromkeys(track for component in group for track in component['track_ids']))
        merged = dict(group[0])
        merged['track_ids'] = tracks
        merged['component_id'] = 'component:' + '+'.join(sorted(tracks))
        for key, identity in (('messages', 'id'), ('context_messages', 'id'),
                              ('memberships', 'unit_root_message_id'), ('track_cards', 'track_id')):
            merged[key] = list({row[identity]: row for component in group
                                for row in component.get(key) or []}.values())
        for key in ('messages', 'context_messages'):
            merged[key].sort(key=lambda message: (str(message.get('created_at') or ''), int(message['id'])))
        merged['memberships'].sort(key=lambda unit: int(unit['unit_root_message_id']))
        merged['context_edges'] = list({(int(edge['unit_root_message_id']), edge['track_id']): edge
                                        for component in group for edge in component.get('context_edges') or []}.values())
        for key in ('parked_context_source_ids', 'context_session_ids'):
            merged[key] = sorted({value for component in group for value in component.get(key) or []})
        merged['base_event_candidates'] = [row for component in group
                                           for row in component.get('base_event_candidates') or []]
        merged['base_event_candidate_overflow'] = [row for component in group
                                                    for row in component.get('base_event_candidate_overflow') or []]
        merged['continuity_pairs'] = [pair for pair in pairs
                                      if pair['left_track_id'] in tracks and pair['right_track_id'] in tracks]
        merged['unreviewed_continuity_pairs'] = [pair for pair in excluded
                                                 if pair['left_track_id'] in tracks or pair['right_track_id'] in tracks]
        result.append(merged)
    return result


def validate_continuations(review, plan, component):
    """Cross-Track Event merging needs exact evidence from both sides and bridge."""
    allowed = component.get('continuity_pairs') or []
    if not allowed:
        return
    units = {int(unit['unit_root_message_id']): unit for unit in component['memberships']}
    messages = {int(message['id']): message for message in component['messages']}
    track_by_source = {int(source_id): str(unit['track_id']) for unit in units.values()
                       for source_id in unit.get('source_message_ids') or [unit['unit_root_message_id']]}
    bridge_sources = {int(source_id) for unit in units.values()
                      if unit.get('routing_role') == 'bridge'
                      for source_id in unit.get('source_message_ids') or [unit['unit_root_message_id']]}
    required = {}
    for index, event in enumerate(plan['events']):
        owned = set(event['source_message_ids'])
        tracks = {track_by_source[source_id] for source_id in owned - bridge_sources
                  if source_id in track_by_source}
        if len(tracks) > 1:
            if event['primary_track_id'] not in tracks:
                raise ValueError('Cross-Track Event primary must own substantive units')
            required[index] = (owned, tracks)
    rows = review.get('continuations', []) if isinstance(review, dict) else []
    if not isinstance(rows, list):
        raise ValueError('Curator continuations must be a list')
    seen = set()
    connected = {index: {track: {track} for track in tracks}
                 for index, (_, tracks) in required.items()}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'event_index', 'left_track_id', 'right_track_id',
                                                    'bridge_unit_root', 'reason', 'evidence'}:
            raise ValueError('Curator continuation has invalid fields')
        index, root = row['event_index'], row['bridge_unit_root']
        left, right = row['left_track_id'], row['right_track_id']
        if type(index) is not int or index not in required or type(root) is not int or root not in units:
            raise ValueError('Curator continuation references an invalid Event or bridge')
        owned, tracks = required[index]
        if (left == right or left not in tracks or right not in tracks
                or not isinstance(row['reason'], str) or not row['reason'].strip()
                or not any(pair['bridge_unit_root'] == root and
                           {pair['left_track_id'], pair['right_track_id']} == {left, right}
                           for pair in allowed)):
            raise ValueError('Curator continuation needs an in-scope bridge and reason')
        key = (index, root, tuple(sorted((left, right))))
        if key in seen:
            raise ValueError('Curator continuation repeats a bridge')
        seen.add(key)
        bridge_ids = set(units[root].get('source_message_ids') or [root])
        if not bridge_ids.issubset(owned):
            raise ValueError('Curator continuation must own its complete bridge unit')
        evidence = row['evidence']
        if not isinstance(evidence, list):
            raise ValueError('Curator continuation needs bilateral verbatim evidence')
        witnessed, cited = set(), set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {'source_message_id', 'quote'}:
                raise ValueError('Curator continuation evidence has invalid fields')
            source_id, quote = item['source_message_id'], item['quote']
            if (type(source_id) is not int or source_id not in owned or source_id not in messages
                    or track_by_source.get(source_id) not in {left, right}
                    or not isinstance(quote, str) or not quote.strip()
                    or quote not in str(messages[source_id].get('content') or '')):
                raise ValueError('Curator continuation quote must occur verbatim in owned originals')
            witnessed.add(track_by_source[source_id])
            cited.add(source_id)
        if witnessed != {left, right} or not cited.intersection(bridge_ids):
            raise ValueError('Curator continuation must cite both Tracks and the bridge')
        union = connected[index][left] | connected[index][right]
        for track in union:
            connected[index][track] = union
    for index, (_, tracks) in required.items():
        if any(group != tracks for group in connected[index].values()):
            raise ValueError('Cross-Track Event requires source-backed continuation review')

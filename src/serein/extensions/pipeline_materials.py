"""Per-Event writing choices grounded in the frozen owned originals."""

USES = {'main', 'background', 'omit', 'mixed'}


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

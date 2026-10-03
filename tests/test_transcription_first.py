"""Saved raw transcriptions must not depend on live original URLs."""
import asyncio
import copy
import json

import pytest

from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.extensions import pipeline as p, pipeline_images as images
from serein.image_transcription import persist_transcriptions, transcribed_image_receipts
from test_event_handoff import PNG
from test_public_features import settings, output_for


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('add_new_image', [False, True])
def test_saved_transcript_survives_expired_external_original(settings, monkeypatch, legacy, add_new_image):
    original = images.image_bytes
    remote = 'https://synthetic.invalid/cover.png'
    unavailable = False
    reads = []
    def read(url):
        if url == remote:
            reads.append(url)
            assert not unavailable, 'Successful transcription must be read before the expired original'
            return original(PNG)
        return original(url)
    monkeypatch.setattr(images, 'image_bytes', read)
    transcribed = []
    writers = []
    curators = []
    async def runner(role, request):
        if request.get('transcription_only'):
            transcribed.append(request['images'][0]['source_message_id'])
            return {'image_transcriptions': [{'input_image': 1,
                'text': 'Synthetic cover title', 'unreadable': False}]}
        if role == 'event_writer':
            writers.append(request)
        elif role == 'event_curator':
            curators.append(copy.deepcopy(request))
        return output_for(role, request)
    for window, day in [('earlier', '2025-01-01'), ('later', '2025-01-02')]:
        attachment = remote if window == 'earlier' else PNG
        raw_archive(settings).ingest([
            {'source_event_id': window + '-user', 'session_id': window, 'role': 'user',
             'text': 'Synthetic cover plan', 'created_at': day + 'T00:00:00Z',
             'metadata': {'attachments': [{'kind': 'image', 'url': attachment}]}
                 if window == 'earlier' or add_new_image else {}},
            {'source_event_id': window + '-reply', 'session_id': window, 'role': 'assistant',
             'text': 'Synthetic response', 'created_at': day + 'T00:01:00Z'}], source='test')
        if window == 'later':
            unavailable = True
            if legacy:
                with Store(settings.database) as store:
                    store.conn.execute("UPDATE raw_events SET image_transcription_json="
                        "json_remove(image_transcription_json,'$.items[0].source_fingerprint') WHERE id=1")
        result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
        assert result.get('events') == 1, result
    assert reads == [remote]
    assert transcribed == ([1, 3] if add_new_image else [1])
    request = writers[-1]
    assert request['writer_mode'] == 'append'
    assert {message['original_session_id'] for message in request['messages']} == {'later'}
    assert request['images'] == [] and request['image_input_mode'] == 'transcriptions_only'
    assert {row['source_message_id'] for row in request['curator_image_transcriptions']} == (
        {3} if add_new_image else set())
    assert {row['source_message_id'] for row in curators[-1]['curator_image_transcriptions']} == (
        {1, 3} if add_new_image else {1})
    with Store(settings.database, read_only=True) as store:
        cached = json.loads(store.conn.execute('SELECT image_transcription_json FROM raw_events WHERE id=1').fetchone()[0])
        sources = store.conn.execute('SELECT session_id,message_id FROM fact_event_sources WHERE item_id IN '
            "(SELECT item_id FROM fact_events WHERE status='active')").fetchall()
    assert cached['items'][0]['source_fingerprint']
    assert set(map(tuple, sources)) == {(window, window + '-' + role)
                                      for window in ('earlier', 'later') for role in ('user', 'reply')}


def test_replaced_attachment_requires_fresh_bytes(settings, monkeypatch):
    remote = 'https://synthetic.invalid/original.png'
    result = raw_archive(settings).ingest([{
        'source_event_id': 'image', 'session_id': 'one', 'role': 'user', 'text': 'Synthetic title',
        'created_at': '2025-01-01T00:00:00Z',
        'metadata': {'attachments': [{'kind': 'image', 'url': remote}]}}], source='test')
    message_id = result['items'][0]['id']
    persist_transcriptions(settings.database, [{'source_message_id': message_id, 'position': 1,
        'sha256': 'a' * 64, 'evidence_role': 'owned', 'text': 'Original title', 'unreadable': False}])
    with Store(settings.database) as store:
        metadata = {'attachments': [{'kind': 'image', 'url': 'https://synthetic.invalid/replacement.png'}]}
        store.conn.execute('UPDATE raw_events SET metadata_json=? WHERE id=?', (json.dumps(metadata), message_id))
    messages = p.image_source_messages(settings.database, [{'id': message_id}])
    refs, _ = p.writer_images(messages, {message_id})
    assert transcribed_image_receipts(messages, refs) == []
    reads = []
    original = images.image_bytes
    def read(url):
        reads.append(url)
        return original(PNG)
    monkeypatch.setattr(images, 'image_bytes', read)
    p.initialize(settings.database)
    frozen = images.freeze_task_images(settings.database, 'synthetic-replacement', refs)
    from serein.image_transcription import reusable_transcriptions
    assert reusable_transcriptions(settings.database, messages, frozen) == []
    assert reads == [metadata['attachments'][0]['url']]


@pytest.mark.parametrize('change', ['foreign', 'malformed', 'inline_bytes'])
def test_invalid_saved_receipt_cannot_skip_original(change):
    frozen = images.freeze_images([{'source_message_id': 1, 'position': 1,
        'evidence_role': 'owned', 'url': PNG}])[0]
    item = images.bind_transcriptions({'image_transcriptions': [
        {'input_image': 1, 'text': 'Synthetic title', 'unreadable': False}]}, [frozen])[0]
    message = {'id': 1, 'image_transcription': {'items': [copy.deepcopy(item)]}}
    refs = [{'source_message_id': 1, 'position': 1, 'evidence_role': 'context_only', 'url': PNG}]
    if change == 'foreign':
        message['image_transcription']['items'][0]['source_message_id'] = 2
    elif change == 'malformed':
        message['image_transcription']['items'][0]['sha256'] = 'invalid'
    else:
        import base64
        body = base64.b64decode(PNG.split(',', 1)[1]) + b'changed'
        refs[0]['url'] = 'data:image/png;base64,' + base64.b64encode(body).decode()
    assert transcribed_image_receipts([message], refs) == []


def test_partial_cache_keeps_each_images_original_source_fingerprint(settings):
    metadata = {'attachments': [{'kind': 'image', 'url': 'https://synthetic.invalid/one.png'},
                                {'kind': 'image', 'url': 'https://synthetic.invalid/two.png'}]}
    raw_archive(settings).ingest([{'source_event_id': 'two-images', 'session_id': 'one', 'role': 'user',
        'text': 'Synthetic pages', 'created_at': '2025-01-01T00:00:00Z', 'metadata': metadata}], source='test')
    rows = [{'source_message_id': 1, 'position': position, 'sha256': 'a' * 64,
             'evidence_role': 'owned', 'text': 'Synthetic page', 'unreadable': False} for position in (1, 2)]
    persist_transcriptions(settings.database, rows)
    metadata['attachments'][1]['url'] = 'https://synthetic.invalid/replaced-two.png'
    with Store(settings.database) as store:
        store.conn.execute('UPDATE raw_events SET metadata_json=? WHERE id=1', (json.dumps(metadata),))
    persist_transcriptions(settings.database, rows[1:])
    messages = p.image_source_messages(settings.database, [{'id': 1}])
    refs, _ = p.writer_images(messages, {1})
    assert [row['position'] for row in transcribed_image_receipts(messages, refs)] == [2]

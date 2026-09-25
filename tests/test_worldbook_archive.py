"""Worldbook is model context, not raw speech. All data here is synthetic."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from serein.chat_archive import original, prepare_turn, archive_turn, archive_user_turn
from serein.chat_context import ClientContext
from serein.compat.germany import raw_events, raw_text
from serein.compat.raw_archive import raw_archive


WORLDBOOK = '<worldbook><entry name="A">WORLDBOOK_ONLY_SENTINEL</entry></worldbook>'
IMAGE = {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/synthetic.png'}}


@pytest.mark.parametrize('cleaner', [
    raw_events.strip_raw_client_context,
    raw_text.strip_raw_client_context,
    lambda text: original({'role': 'user', 'content': text})['text'],
], ids=['raw-store', 'raw-text', 'chat-archive'])
@pytest.mark.parametrize('text,expected', [
    (WORLDBOOK + 'Current question', 'Current question'),
    ('Before' + WORLDBOOK + 'After', 'Before\nAfter'),
    (WORLDBOOK + 'Question' + WORLDBOOK, 'Question'),
    ('<worldbook>\n<entry>A\nB</entry>\n<entry>C</entry>\n</worldbook>Question', 'Question'),
    ('<WORLDBOOK source="operit"><entry>A</entry></WORLDBOOK >Question', 'Question'),
    (WORLDBOOK, ''),
    ('<worldbook>\n【当前天气】\nInjected\n</worldbook>Question', 'Question'),
    ('Question\n' + WORLDBOOK + '\n【当前时间】\n2026-09-25T17:00:00+08:00', 'Question'),
    ('Question' + WORLDBOOK + '<attachment>App context</attachment>'
     '<workspace_attachment>Workspace</workspace_attachment><attachment filename="time:"/>', 'Question'),
    ('Keep <entry name="example">ordinary text</entry>', 'Keep <entry name="example">ordinary text</entry>'),
    ('Keep <worldbooks>ordinary XML</worldbooks>', 'Keep <worldbooks>ordinary XML</worldbooks>'),
    ('Keep <worldbook>an unfinished example', 'Keep <worldbook>an unfinished example'),
    ('Keep ordinary speech.', 'Keep ordinary speech.'),
])
def test_worldbook_speech_projections(text, expected, cleaner):
    assert cleaner(text) == expected
    assert cleaner(cleaner(text)) == expected


def test_chat_cleans_split_text_blocks_without_mutating_forwarded_input():
    message = {'role': 'user', 'id': 'synthetic-1', 'content': [
        {'type': 'text', 'text': '<proxy_sender name="phone"/>【系统提示】<worldbook><entry>A</entry>'},
        {'type': 'input_text', 'input_text': '<entry>B</entry></worldbook>Picture question'},
        IMAGE,
        {'type': 'tool_result', 'text': 'TOOL_OUTPUT_SENTINEL'},
    ]}
    untouched = deepcopy(message)
    projected = original(message)
    assert projected == {'role': 'user', 'text': 'Picture question',
                         'attachments': [{'kind': 'image', 'url': IMAGE['image_url']['url']}]}
    assert message == untouched
    for enabled in (False, True):
        assert ClientContext(operit=enabled)._extract_current_turn_user_query([message]) == 'Picture question'
        assert message == untouched


def test_worldbook_with_image_keeps_the_image_turn():
    message = {'role': 'user', 'content': [{'type': 'text', 'text': WORLDBOOK}, IMAGE]}
    turn = prepare_turn('worldbook-test', [message])
    assert turn['user']['text'] == '[图片]'
    assert turn['user']['attachments'] == [{'kind': 'image', 'url': IMAGE['image_url']['url']}]


@pytest.mark.parametrize('role', ['user', 'assistant'])
@pytest.mark.parametrize('as_blocks', [False, True])
def test_raw_ingest_stores_only_speech_and_indexes_no_worldbook(tmp_path, role, as_blocks):
    archive = raw_events.RawEventStore({'raw_events': {'db_path': str(tmp_path / 'raw.db')}})
    text = 'Before' + WORLDBOOK + 'After'
    content = ([{'type': 'text', 'text': 'Before<worldbook><entry>WORLDBOOK_ONLY_SENTINEL'},
                {'type': 'text', 'text': '</entry></worldbook>After'}] if as_blocks else text)
    event = {'role': role, 'content': content, 'id': 'synthetic-1',
             'created_at': '2026-09-25T09:00:00+00:00'}
    untouched = deepcopy(event)
    result = archive.ingest([event], source='synthetic')
    assert (result['inserted'], result['rejected']) == (1, 0)
    saved = archive.get_event(result['items'][0]['id'])
    assert saved['text'] == 'Before\nAfter'
    assert saved['source_event_id'] == 'synthetic-1' and saved['role'] == role
    assert archive.search('WORLDBOOK_ONLY_SENTINEL')['items'] == []
    assert archive.search('Before')['count'] == 1
    assert archive.ingest([event], source='synthetic')['duplicate'] == 1
    assert event == untouched


def test_raw_ingest_rejects_context_only_and_preserves_role_guards(tmp_path):
    archive = raw_events.RawEventStore({'raw_events': {'db_path': str(tmp_path / 'raw.db')}})
    for event, reason in [
        ({'role': 'user', 'text': WORLDBOOK}, 'empty_text'),
        ({'role': 'system', 'text': 'Do not store this'}, 'invalid_role'),
        ({'role': 'user', 'text': WORLDBOOK + 'Text', 'metadata': {'kind': 'injection'}}, 'injected_context'),
    ]:
        result = archive.ingest([event])
        assert result['inserted'] == 0 and result['rejected'] == 1
        assert result['items'][0]['reason'] == reason
    assert archive.search()['items'] == []


@pytest.mark.parametrize('message_id', [None, 'stable-client-id'])
def test_archive_retry_identity_ignores_changing_worldbook(tmp_path, message_id):
    settings = SimpleNamespace(database=tmp_path / 'archive.db')
    message = {'role': 'user', 'content': WORLDBOOK + 'Current question'}
    if message_id:
        message['id'] = message_id
    first = prepare_turn('worldbook-test', [message])
    changed = {**message, 'content': WORLDBOOK.replace('WORLDBOOK_ONLY_SENTINEL', 'Changed context') + 'Current question'}
    second = prepare_turn('worldbook-test', [changed])
    assert first['key'] == second['key']
    assert first['user']['text'] == second['user']['text'] == 'Current question'
    reply = {'role': 'assistant', 'content': 'Current answer'}
    assert archive_turn(settings, first, reply)['inserted'] == 2
    assert archive_turn(settings, second, reply)['duplicate'] == 2
    saved = raw_archive(settings).search()['items']
    assert sorted(row['text'] for row in saved) == ['Current answer', 'Current question']
    assert all(row['session_id'] == 'worldbook-test' for row in saved)
    other_window = prepare_turn('another-window', [changed])
    assert other_window['key'] != first['key']
    assert archive_turn(settings, other_window, reply)['inserted'] == 2


def test_context_only_turn_never_reuses_an_older_user_anchor(tmp_path):
    settings = SimpleNamespace(database=tmp_path / 'archive.db')
    history = [{'role': 'user', 'content': 'Old question'},
               {'role': 'assistant', 'content': 'Old answer'}]
    for incoming in ([{'role': 'user', 'content': WORLDBOOK}],
                     [*history, {'role': 'user', 'content': WORLDBOOK}]):
        turn = prepare_turn('worldbook-test', incoming)
        assert turn is None
        assert archive_user_turn(settings, turn)['reason'] == 'no_user_message'
        assert archive_turn(settings, turn, {'content': 'Not a dialogue reply'})['reason'] == 'no_user_message'
    assert not settings.database.exists()


@pytest.mark.parametrize('native', [False, True])
def test_tool_continuations_keep_the_original_anchor(native):
    question = {'role': 'user', 'content': WORLDBOOK + 'Use a tool'}
    turn = prepare_turn('worldbook-test', [question])
    if native:
        continuation = [
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-1', 'name': 'lookup', 'input': {}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'Private output'}]},
        ]
    else:
        continuation = [
            {'role': 'assistant', 'content': 'Looking', 'tool_calls': [{'id': 'call-1'}]},
            {'role': 'tool', 'content': 'Private output', 'tool_call_id': 'call-1'},
        ]
    continued = prepare_turn('worldbook-test', [question, *continuation])
    assert continued['key'] == turn['key']
    assert continued['user']['text'] == 'Use a tool'


def test_upgrade_does_not_rewrite_existing_raw_rows(tmp_path):
    archive = raw_events.RawEventStore({'raw_events': {'db_path': str(tmp_path / 'raw.db')}})
    # Insert a legacy fixture directly, bypassing the new ingestion projection.
    legacy, reason = archive._normalize_event(
        {'role': 'user', 'text': 'Legacy question', 'id': 'legacy'},
        default_source='synthetic', ingested_at='2026-09-25T09:00:00+00:00')
    assert not reason
    legacy['text'] = WORLDBOOK + legacy['text']
    _, row_id = archive._insert_event(legacy)
    before = archive.get_event(row_id)
    result = archive.ingest([{'role': 'user', 'text': WORLDBOOK + 'New question', 'id': 'new'}])
    assert result['inserted'] == 1
    assert archive.get_event(result['items'][0]['id'])['text'] == 'New question'
    assert archive.get_event(row_id) == before

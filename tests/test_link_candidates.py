"""Lexical manual linking is read-only and independent of model configuration."""
import json
import sqlite3

import pytest

from serein.api.settings import _link_candidates


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE documents(id TEXT PRIMARY KEY, kind TEXT, lifecycle TEXT, revision INTEGER, created_at TEXT);
        CREATE TABLE revisions(document_id TEXT, number INTEGER, title TEXT, body_md TEXT, metadata_json TEXT);
        CREATE TABLE deletions(document_id TEXT PRIMARY KEY);
        CREATE TABLE diary_entries(id INTEGER PRIMARY KEY,kind TEXT,title TEXT,body_md TEXT,day TEXT,
            visibility TEXT,unlock_at TEXT,deleted_at TEXT);
        CREATE TABLE import_records(origin TEXT,path TEXT,content BLOB,PRIMARY KEY(origin,path));
        CREATE TABLE narrative_uploads(id TEXT PRIMARY KEY,origin TEXT,path TEXT,metadata_json TEXT);
    ''')
    yield conn
    conn.close()


def document(db, identifier='scene_a', kind='scene', title='雨夜散步', body='沿着河边回家',
             status='active', date='2026-09-20'):
    db.execute('INSERT INTO documents VALUES (?,?,?,?,?)', (identifier,kind,status,1,date))
    db.execute('INSERT INTO revisions VALUES (?,?,?,?,?)', (identifier,1,title,body,json.dumps({'date':date})))


def notebook(db, identifier, kind='diary', **options):
    db.execute('INSERT INTO diary_entries VALUES (?,?,?,?,?,?,?,?)', (
        identifier,kind,options.get('title','河边记录'),options.get('body','散步后回家'),
        options.get('day','2026-09-20'),options.get('visibility','active'),
        options.get('unlock_at',''),options.get('deleted_at','')))


@pytest.mark.parametrize('query', ['雨夜','散步','河边','雨夜 河边','  雨夜\t河边  ','scene_a'])
def test_titles_body_keywords_and_ids(db, query):
    document(db)
    result = _link_candidates(db, 'scene', query)
    assert result['total'] == 1 and result['purpose'] == 'link'
    assert result['items'][0] == {'id':'scene_a','kind':'scene','title':'雨夜散步',
                                  'date':'2026-09-20','excerpt':'沿着河边回家'}


@pytest.mark.parametrize('query', ['OTHER', 'missing 河边', "' OR 1=1 --", '%', '_'])
def test_queries_are_literal_and_require_every_keyword(db, query):
    document(db, identifier='plain')
    assert _link_candidates(db, 'scene', query)['total'] == 0


def test_unicode_casefold_and_no_sql_wildcard_expansion(db):
    document(db, title='Straße 100%_计划')
    assert _link_candidates(db, 'scene', 'STRASSE')['total'] == 1
    assert _link_candidates(db, 'scene', '%_')['total'] == 1


def test_exact_id_then_exact_title_then_title_keywords_then_body(db):
    document(db, 'key', title='其他')
    document(db, 'a', title='key')
    document(db, 'b', title='A key moment')
    document(db, 'c', title='其他', body='the KEY in the body')
    assert [r['id'] for r in _link_candidates(db, 'scene', 'key')['items']] == ['key','a','b','c']


def test_duplicate_titles_have_distinct_canonical_ids_and_dates(db):
    document(db, 'scene_a', date='2026-09-20')
    document(db, 'scene_b', date='2026-09-21')
    found = _link_candidates(db, 'scene', '雨夜')['items']
    assert [r['id'] for r in found] == ['scene_b','scene_a']
    assert len({r['date'] for r in found}) == 2


def test_search_reaches_beyond_the_first_500_and_pages_without_duplicates(db):
    for index in range(651):
        document(db, f'scene_{index:04d}', title=f'合成记录 {index}')
    assert _link_candidates(db,'scene','合成记录 650')['items'][0]['id'] == 'scene_0650'
    pages = [_link_candidates(db,'scene',offset=offset,limit=30) for offset in range(0,660,30)]
    assert all(page['total'] == 651 for page in pages)
    assert len({row['id'] for page in pages for row in page['items']}) == 651
    assert all(page['has_more'] for page in pages[:-1]) and not pages[-1]['has_more']
    assert _link_candidates(db,'scene',offset=999)['items'] == []


@pytest.mark.parametrize('status', ['archived','deleted','superseded'])
def test_nonactive_documents_do_not_leak_titles_or_counts(db, status):
    document(db, status=status, title='不可显示')
    assert _link_candidates(db, 'scene', '不可显示')['total'] == 0


def test_deletion_tombstone_and_wrong_kind_are_excluded(db):
    document(db)
    document(db, 'event_a', kind='event')
    db.execute('INSERT INTO deletions VALUES (?)', ('scene_a',))
    assert _link_candidates(db,'scene')['total'] == 0
    assert _link_candidates(db,'event')['items'][0]['id'] == 'event_a'


@pytest.mark.parametrize('options', [
    {'unlock_at':'2999-01-01T00:00:00+00:00'},
    {'unlock_at':'2000-01-01T00:00:00'},
    {'unlock_at':'not-a-time'},
    {'visibility':'locked'}, {'visibility':'archived'}, {'visibility':'deleted'},
    {'deleted_at':'2026-01-01T00:00:00Z'},
])
def test_notebook_protected_content_is_not_searchable(db, options):
    notebook(db,1,title='隐藏标题',body='隐藏正文',**options)
    before=db.total_changes
    for query in ('','隐藏标题','隐藏正文','1'):
        assert _link_candidates(db,'diary',query)['total'] == 0
    assert db.total_changes == before


@pytest.mark.parametrize('kind', ['diary','darkroom'])
def test_unlocked_notebooks_return_canonical_integer_ids_as_strings(db, kind):
    notebook(db,17,kind=kind,unlock_at='2000-01-01T00:00:00+08:00')
    notebook(db,18,kind='darkroom' if kind=='diary' else 'diary')
    result=_link_candidates(db,kind,'河边')
    assert result['total']==1 and result['items'][0]['id']=='17'
    assert result['items'][0]['kind']==kind


def test_file_name_and_extracted_text_search_does_not_read_original_bytes(db):
    db.execute('INSERT INTO import_records VALUES (?,?,?)',('fixture','notes.txt',b'secret original bytes'))
    db.execute('INSERT INTO narrative_uploads VALUES (?,?,?,?)',('upload_a','fixture','notes.txt',
        json.dumps({'filename':'散步笔记.txt','extracted_text':'河边细节','created_at':'2026-09-21T12:00:00Z','private':'secret metadata'})))
    for query in ('笔记','河边','upload_a'):
        result=_link_candidates(db,'upload',query)
        assert result['total']==1
        assert result['items'][0]['title']=='散步笔记.txt'
        assert 'secret' not in json.dumps(result)
    db.execute('INSERT INTO deletions VALUES (?)',('upload_a',))
    assert _link_candidates(db,'upload')['total']==0


def test_missing_file_backing_record_is_not_selectable(db):
    db.execute('INSERT INTO narrative_uploads VALUES (?,?,?,?)',('missing','fixture','missing.txt','{"filename":"不存在"}'))
    assert _link_candidates(db,'upload')['total']==0


def test_current_revision_date_and_exact_id_filter(db):
    document(db)
    db.execute('INSERT INTO revisions VALUES (?,?,?,?,?)',('scene_a',2,'新标题','新正文','{"date":"2026-09-22"}'))
    db.execute('UPDATE documents SET revision=2')
    assert _link_candidates(db,'scene','雨夜')['total']==0
    assert _link_candidates(db,'scene','新标题',date_filter='2026-09-22',ids=['scene_a'])['total']==1
    assert _link_candidates(db,'scene',date_filter='2026-09-20')['total']==0
    assert _link_candidates(db,'scene',ids=[])['total']==0
    assert _link_candidates(db,'scene',ids=["' OR 1=1 --"])['total']==0


def test_excerpt_is_bounded_and_shows_keyword_deep_in_body(db):
    document(db,body='前文'*300+'关键词'+'后文'*300)
    result=_link_candidates(db,'scene','关键词')
    assert '关键词' in result['items'][0]['excerpt']
    assert len(result['items'][0]['excerpt'])<=182
    assert set(result['items'][0])=={'id','kind','title','date','excerpt'}


def test_read_only_connection_accepts_search_and_no_records_change(db):
    document(db)
    db.commit()
    db.execute('PRAGMA query_only=ON')
    before=db.total_changes
    assert _link_candidates(db,'scene','雨夜')['total']==1
    assert db.total_changes==before


@pytest.mark.parametrize('options', [
    {'kind':'narrative'}, {'kind':''}, {'kind':'scene','query':'x'*201},
    {'kind':'scene','offset':-1}, {'kind':'scene','limit':0}, {'kind':'scene','limit':51},
])
def test_invalid_bounds(db, options):
    with pytest.raises(ValueError):
        _link_candidates(db,**options)

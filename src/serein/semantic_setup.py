"""Create provider-matched routing vectors from authored, deployment-owned examples."""
from .recall.index_safety import guarded_index
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse
from .adapters.embedding import EmbeddingClient


def render_example(text, names):
    """Render configured names once, without interpreting inserted text as a template."""
    return re.sub(r'\{(user_name|ai_name)\}', lambda match: names[match[1]], text)


@guarded_index
def prepare(settings, profile_path, examples_path):
    profile=json.loads(Path(profile_path).read_text('utf-8'))
    dimension=profile.pop('dimension')
    required={'model','provider_host','document_instruction','query_instruction','max_chars'}
    if set(profile)!=required or type(dimension) is not int or dimension<1:
        raise ValueError('Provide complete embedding profile and positive dimension')
    if urlparse(settings.embedding['endpoint']).hostname != profile['provider_host']:
        raise ValueError('Profile provider_host must match embedding endpoint')
    with sqlite3.connect(settings.index) as conn:
        existing=dict(conn.execute("SELECT key,value FROM settings WHERE key LIKE 'embedding_%'"))
        proposed={'embedding_profile':json.dumps(profile),'embedding_dimension':json.dumps(dimension)}
        if existing and any(json.loads(existing.get(k,'null'))!=json.loads(v) for k,v in proposed.items()):
            raise ValueError('Profile changed; rebuild a separate disposable index before re-embedding')
        conn.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', proposed.items())
    client=EmbeddingClient(settings.database,settings.index,**settings.embedding)
    source=json.loads(Path(examples_path).read_text('utf-8'))
    from .deployment import identity
    names=identity(settings.database)
    routes=[]
    for row in source['routes']:
        if row.get('enabled') is False:
            continue
        if row['action'] not in ('recall','skip') or not row['examples']:
            raise ValueError('Each route needs an action and nonempty authored examples')
        routes.append({'name':row['name'],'action':row['action'],'threshold':row.get('threshold'),
                       'vectors':[client.query(render_example(text, names))['embedding'] for text in row['examples']]})
    result={'format':'serein-query-routes-v1','profile':profile,'dimension':dimension,
            'generation':source.get('generation',1),'policy':source['policy'],'routes':routes,
            'boundaries':[{'action':r['action'],'vector':client.query(render_example(r['text'], names))['embedding']}
                          for r in source.get('boundaries',[])]}
    target=Path(settings.recall['routing_file']); target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix('.pending');temp.write_text(json.dumps(result),encoding='utf-8');os.replace(temp,target)
    return {'status':'prepared','routes':len(routes),'dimension':dimension}

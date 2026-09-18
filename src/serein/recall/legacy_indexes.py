"""Rebuildable Germany cue bindings and Event BM25; never a second memory store."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from ..adapters.embedding import EmbeddingClient
from ..compat.background import germany_config
from .germany.memory_recall.fact_event_lexical_shadow import FactEventLexicalShadowIndex
from .index import Search, content_stamp


def config(settings):
    cfg={**germany_config(settings), 'state_dir':str(Path(settings.index).parent)}
    # The migration intentionally keeps provider secrets in the container env.
    # Germany's cue binder shares its provider with embedding on this deployment.
    raw=dict(cfg.get('cue_passage_shadow') or {})
    dehy=cfg.get('dehydration') or {}
    if not raw.get('api_key') and not dehy.get('api_key'):
        key_env=raw.get('api_key_env')
        if not key_env and urlparse(raw.get('base_url') or dehy.get('base_url') or '').netloc==urlparse(settings.embedding.get('endpoint','')).netloc:
            key_env=settings.embedding.get('api_key_env')
        if key_env:raw['api_key']=os.environ.get(key_env,'')
    cfg['cue_passage_shadow']=raw
    return cfg


def lexical_index(settings):
    return FactEventLexicalShadowIndex(config(settings))


def cue_index(settings, *, writable=False, profile=None):
    from .germany.memory_recall.cue_passage_shadow import CuePassageShadowIndex, BINDING_PROMPT
    from ..deployment import task_model
    from ..core.store import digest, encode
    cfg=config(settings)
    client=EmbeddingClient(settings.database,settings.index,**settings.embedding) if writable else None
    if profile is None:
        import json
        with Search(settings.database,settings.index) as search:
            profile=json.loads(search.conn.execute("SELECT value FROM settings WHERE key='embedding_profile'").fetchone()[0])
    class Embedding:
        model=profile['model']
        document_instruction=profile['document_instruction']
        enabled=True
        async def embed_document(self,text):
            return (await asyncio.to_thread(client.documents,[text]))[0]
    raw=cfg.get('cue_passage_shadow') or {}; llm=cfg.get('dehydration') or {}
    binder=None if writable else SimpleNamespace(model=raw.get('binding_model') or llm.get('model') or '')
    selected=task_model(settings.database,'operit_tagging')
    if selected:
        class ConfiguredBinder:
            model=selected['model']+':'+digest(encode({key:selected.get(key) for key in ('id','base_url','protocol')}))[:12]
            async def bind(self, **materials):
                import json
                from ..model_runtime import complete, non_thinking_options
                response=await complete(selected,{'messages':[
                    {'role':'system','content':BINDING_PROMPT},
                    {'role':'user','content':json.dumps(materials,ensure_ascii=False)}],
                    'response_format':{'type':'json_object'},'temperature':0,
                    **non_thinking_options(selected)})
                return json.loads(response['choices'][0]['message']['content'])
        binder=ConfiguredBinder()
    return CuePassageShadowIndex(cfg,Embedding(),binder=binder)


async def refresh(settings, *, dry_run=False, retry_failed=False):
    from .policy import RecallPolicy
    from ..configured_models import recall_settings
    passages_enabled = RecallPolicy.from_config(recall_settings(settings)).passages_enabled
    scenes, events, passages = [], [], {}
    with Search(settings.database,settings.index) as search:
        for row in search.conn.execute("SELECT * FROM documents WHERE kind IN ('scene','event')"):
            obj=search.reader.read(row['id'],with_evidence=False)
            if not obj['readable'] or obj['document']['lifecycle']!='active':continue
            doc=obj['document']
            if content_stamp(doc)!=row['stamp']:continue
            if doc['kind']=='event':
                events.append({**doc['metadata'],'item_id':doc['id'],'item_type':'event',
                               'title':doc['title'],'body':doc['body_md'],'status':'active'})
            elif obj['surface_state']['can_surface']:
                scenes.append({'id':doc['id'],'title':doc['title'],'cues':doc['metadata'].get('scene_cues')})
                if passages_enabled and search.has_passages:
                    passages[('scene',doc['id'])]=[dict(r) for r in search.conn.execute(
                        'SELECT ordinal,start_offset,end_offset,text FROM passages WHERE document_id=? AND stamp=? ORDER BY ordinal',
                        (doc['id'],row['stamp']))]
    lexical=lexical_index(settings).sync(events,dry_run=dry_run)
    if not passages_enabled:
        return {'lexical':lexical,'cues':{'status':'disabled'},'canonical_writes':0}
    cues=cue_index(settings,writable=not dry_run)
    try:
        cue_result=await cues.sync(scenes=scenes,passages_by_owner=passages,
                                   dry_run=dry_run,retry_failed=retry_failed)
    finally:
        if getattr(cues.binder,'client',None):await cues.binder.client.close()
    return {'lexical':lexical,'cues':cue_result,'canonical_writes':0}

"""Resolve saved task selections without mutating TOML or reusing foreign vectors."""
import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from .deployment import read_settings, task_model

_prepare_lock = threading.Lock()
_resources = Path(__file__).parent / 'resources'


def recall_resource_version():
    """Changing published examples or gates requires explicit re-preparation."""
    return hashlib.sha256(b''.join((_resources/name).read_bytes() for name in
        ('route-examples.json','recall-policy.json'))).hexdigest()


def model_index(settings, model):
    profile = {key:model.get(key) for key in ('id','model','base_url','dimension','query_instruction','document_instruction')}
    if model.get('tokenizer'):
        profile['tokenizer'] = model['tokenizer']
    key = hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()[:24]
    return settings.database.parent / 'model-indexes' / key


def recall_settings(settings):
    """Saved recall options override TOML only after an explicit save."""
    return {**settings.recall, **read_settings(settings.database)['recall']}


def effective_settings(settings, *, preparing=False):
    embedding=task_model(settings.database,'embedding')
    reranker=task_model(settings.database,'reranker')
    changes={'recall':recall_settings(settings)}
    if embedding:
        root=model_index(settings,embedding)
        if not preparing:
            ready=json.loads((root/'ready.json').read_text('utf-8')) if (root/'ready.json').is_file() else {}
            if ready.get('recall_resources') != recall_resource_version() or not (root/'scope.sqlite').is_file() or not (root/'policy.json').is_file():
                raise ValueError('Prepare the selected embedding model in Settings before recall or indexing')
        changes.update(index=root/'index.sqlite',
            embedding={'endpoint':embedding['base_url']+'/embeddings','api_key':embedding.get('api_key',''),
                       **({'tokenizer':embedding['tokenizer']} if embedding.get('tokenizer') else {})},
            recall={**changes['recall'],'routing_file':str(root/'routes.json'),
                    'germany_policy_file':str(root/'policy.json')})
    if reranker:
        changes['reranker']={'endpoint':reranker['base_url']+'/rerank','model':reranker['model'],'api_key':reranker.get('api_key',''),
                            **({'tokenizer':reranker['tokenizer']} if reranker.get('tokenizer') else {})}
    return replace(settings,**changes)


def memory_ready(settings):
    return memory_status(settings)['ready']


def memory_status(settings):
    """Check the actual route/domain read path without any provider requests."""
    import sqlite3
    import yaml
    stage='configuration'
    try:
        effective=effective_settings(settings)
        if not (effective.embedding and effective.reranker and effective.index
                and effective.index.is_file() and effective.recall.get('routing_file')):
            return {'ready':False,'stage':stage,'reason':'memory_not_prepared'}
        from .adapters.embedding import EmbeddingClient
        from .compat.publication import recall_policy_data
        from .recall.routing import validate_routes
        stage='embedding_profile'
        client=EmbeddingClient(effective.database,effective.index,**effective.embedding)
        stage='live_policy'
        published,domains=recall_policy_data(effective)
        stage='query_routes'
        data=published if published is not None else json.loads(Path(effective.recall['routing_file']).read_text('utf-8'))
        validate_routes(data,client.profile,client.dimension)
        return {'ready':True,'route_source':'published_or_seed' if published is not None else 'prepared',
                'domain_source':'published' if domains is not None else 'instance',
                'routes':len(data['routes']),'boundaries':len(data['boundaries'])}
    except (OSError,sqlite3.Error,yaml.YAMLError):
        return {'ready':False,'stage':stage,'reason':'policy_or_index_unreadable'}
    except (ValueError,RuntimeError,KeyError,TypeError,AttributeError) as error:
        reason=str(error) if isinstance(error,(ValueError,RuntimeError)) and not isinstance(error,json.JSONDecodeError) else 'policy_or_index_invalid'
        return {'ready':False,'stage':stage,'reason':reason}


def prepare_selected(settings, *, before_fill=None):
    import httpx
    from .recall.index import build_index
    from .recall.vectors import fill_vectors
    from .recall.passages import fill_passages
    from .semantic_setup import prepare
    model=task_model(settings.database,'embedding')
    if not model:raise ValueError('Select an embedding model first')
    if model.get('protocol')!='openai':raise ValueError('Embedding requires a compatible embeddings API')
    with _prepare_lock:
        effective=effective_settings(settings,preparing=True)
        root=effective.index.parent;root.mkdir(parents=True,exist_ok=True)
        (root/'ready.json').unlink(missing_ok=True)
        # Discovery is explicit: clicking Prepare authorizes this provider request.
        with httpx.Client(timeout=30,follow_redirects=False) as client:
            response=client.post(model['base_url']+'/embeddings',json={'model':model['model'],'input':'dimension probe','encoding_format':'float'},
                headers={'Authorization':'Bearer '+model['api_key']} if model.get('api_key') else {})
        if not response.is_success:raise ValueError(f'Embedding provider returned HTTP {response.status_code}')
        dimension=len(response.json()['data'][0]['embedding'])
        if model.get('dimension') and model['dimension']!=dimension:raise ValueError('Configured dimension differs from the provider')
        profile={'model':model['model'],'provider_host':urlsplit(model['base_url']).hostname,'dimension':dimension,
                 'document_instruction':model.get('document_instruction',''),'query_instruction':model.get('query_instruction',''),'max_chars':12000}
        if not effective.index.exists():build_index(settings.database,effective.index)
        profile_file=root/'profile.json';profile_file.write_text(json.dumps(profile),'utf-8')
        prepared=prepare(effective,profile_file,_resources/'route-examples.json')
        if before_fill:before_fill(effective,profile)
        whole=fill_vectors(effective)
        passages=fill_passages(effective)
        policy=json.loads((_resources/'recall-policy.json').read_text('utf-8'))
        policy['entity_database']='scope.sqlite'
        (root/'policy.json').write_text(json.dumps(policy,ensure_ascii=False),'utf-8')
        from .compat.entity_scope import update_scope
        from .recall.entities import rebuild_entities
        entities=update_scope(effective)
        rebuild_entities(effective,legacy_rows=entities)
        from .recall.surface_gate import load_gate
        load_gate.cache_clear()
        (root/'ready.json').write_text(json.dumps({'profile':profile,'status':'ready',
            'recall_resources':recall_resource_version()}),'utf-8')
        return {'status':'ready','dimension':dimension,'routes':prepared['routes'],'vectors':whole,
                'passages':passages,'scope_entities':len(entities)}

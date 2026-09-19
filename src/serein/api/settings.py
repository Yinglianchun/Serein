from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, ValidationError
from ..deployment import read_settings, save_settings, TASKS, DEFAULT_FEATURES, DEFAULT_RESUME
from typing import Literal
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def api_base(value):
    if value:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Use an HTTP(S) API base URL without credentials, query or fragment')
    return value.rstrip('/') if value else value


class IdentityPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    user_name: str | None = Field(default=None, min_length=1, max_length=64)
    ai_name: str | None = Field(default=None, min_length=1, max_length=64)
    user_description: str | None = Field(default=None, max_length=2000)
    ai_description: str | None = Field(default=None, max_length=2000)
    meeting_date: str | None = Field(default=None, max_length=10)

    @field_validator('meeting_date')
    @classmethod
    def calendar_date(cls, value):
        if value and date.fromisoformat(value).isoformat() != value:
            raise ValueError('Use a calendar date in YYYY-MM-DD format')
        return value


class UpstreamPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    base_url: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=200)
    writer_model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=4000)
    writer_enabled: bool | None = None
    memory_enabled: bool | None = None
    operit_enabled: bool | None = None
    _endpoint = field_validator('base_url')(api_base)


class ModelConnection(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    base_url: str = Field(min_length=1, max_length=2000)
    api_key: str | None = Field(default=None, max_length=4000)
    protocol: Literal['openai', 'anthropic'] = 'openai'
    prompt_cache: Literal['', 'openai', 'anthropic', 'anthropic-explicit'] = ''
    prompt_cache_retention: Literal['', 'in-memory', '24h', '5m', '1h'] = ''
    _endpoint = field_validator('base_url')(api_base)

    @field_validator('prompt_cache', mode='before')
    @classmethod
    def cache_alias(cls, value):
        return 'anthropic-explicit' if value == 'anthropic_explicit' else value

    @model_validator(mode='after')
    def cache_options(self):
        if self.protocol == 'openai' and self.prompt_cache == 'anthropic-explicit':
            raise ValueError('Explicit Anthropic cache breakpoints require the Messages format')
        if self.protocol == 'anthropic' and self.prompt_cache == 'openai':
            raise ValueError('Prompt cache key requires the Chat Completions format')
        allowed = {'', 'in-memory', '24h'} if self.prompt_cache == 'openai' else {'', '5m', '1h'} if self.prompt_cache else {''}
        if self.prompt_cache_retention not in allowed:
            raise ValueError('Cache retention does not match the selected cache strategy')
        return self


class ModelEntry(ModelConnection):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    dimension: int | None = Field(default=None, ge=1, le=65536)
    query_instruction: str = Field(default='', max_length=1000)
    document_instruction: str = Field(default='', max_length=1000)


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=200)
    upstream_model: str = Field(min_length=1, max_length=200)
    label: str = Field(default='', max_length=100)
    dimension: int | None = Field(default=None, ge=1, le=65536)
    query_instruction: str = Field(default='', max_length=1000)
    document_instruction: str = Field(default='', max_length=1000)


class UpstreamEntry(ModelConnection):
    id: str | None = Field(default=None, min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    api_key_env: str = Field(default='', max_length=200, pattern=r'^$|^[A-Za-z_][A-Za-z0-9_]*$')
    default_model: str = Field(default='', max_length=200)
    anthropic_version: str = Field(default='2023-06-01', max_length=100, pattern=r'^[^\r\n]+$')
    anthropic_beta: str = Field(default='', max_length=500, pattern=r'^[^\r\n]*$')
    models: list[ModelRoute] = Field(default_factory=list, max_length=500)

    @field_validator('models', mode='before')
    @classmethod
    def simple_model_names(cls, value):
        if isinstance(value,list):
            return [{'id':item,'upstream_model':item} if isinstance(item,str) else item for item in value]
        return value

    @model_validator(mode='after')
    def model_list(self):
        if not self.models and not self.default_model:
            raise ValueError('Each upstream needs a model list or default_model')
        return self


class ModelDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    upstream_id: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=2000)
    api_key: str | None = Field(default=None, max_length=4000, pattern=r'^[^\r\n]*$')
    clear_key: bool = False
    protocol: Literal['openai', 'anthropic'] = 'openai'
    _endpoint = field_validator('base_url')(api_base)


class DomainEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    key: str = Field(min_length=1, max_length=60, pattern=r'^[a-z][a-z0-9_-]*$')
    label: str = Field(min_length=1, max_length=40)
    description: str = Field(default='', max_length=300)
    policy: Literal['normal', 'explicit_only', 'excluded'] = 'normal'


class TaggingPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    domains: list[DomainEntry] = Field(max_length=50)

    @field_validator('domains')
    @classmethod
    def unique_domains(cls, value):
        if len({item.key for item in value}) != len(value):
            raise ValueError('Domain keys must be unique')
        return value


class ResumePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    latest_shadow: bool | None = None
    recent_events: bool | None = None
    favorite_scenes: bool | None = None
    selected_memories: bool | None = None
    recent_originals: bool | None = None
    recent_original_limit: int | None = Field(default=None, ge=1, le=50, strict=True)
    pending_originals: bool | None = None
    selected_ids: list[str] | None = Field(default=None, max_length=200)

    @field_validator('selected_ids')
    @classmethod
    def unique_selected_ids(cls, value):
        if value is None:return value
        if any(not key.strip() or len(key)>200 for key in value):raise ValueError('Invalid memory ID')
        return list(dict.fromkeys(value))


class DreamPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    main_prompt: str | None = Field(default=None, max_length=40000)
    daily_probability: float | None = Field(default=None, ge=0, le=1)


class ClockPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator('timezone')
    @classmethod
    def known_timezone(cls, value):
        if value is None:return value
        try:ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):raise ValueError('Unknown time zone') from None
        return value


class PipelinePatch(BaseModel):
    auto_enabled: bool | None = None
    execution_mode: Literal['legacy','api','agent'] | None = None
    model_config = ConfigDict(extra='forbid')
    max_input_chars: int | None = Field(default=None,ge=2000,le=100000)
    max_prompt_chars: int | None = Field(default=None,ge=8000,le=4000000)
    timeout_seconds: int | None = Field(default=None,ge=30,le=1800)
    event_writer_concurrency: int | None = Field(default=None,ge=1,le=8,strict=True)


class RecallPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    direct_threshold: float | None = Field(default=None, ge=0, le=1, strict=True)
    body_candidate_threshold: float | None = Field(default=None, ge=0, le=1, strict=True)
    cue_candidate_threshold: float | None = Field(default=None, ge=0, le=1, strict=True)
    passages_enabled: bool | None = Field(default=None, strict=True)
    passage_min_chars: int | None = Field(default=None, ge=1, le=100000, strict=True)

    @field_validator('direct_threshold', 'body_candidate_threshold', 'cue_candidate_threshold',
                     'passages_enabled', 'passage_min_chars', mode='before')
    @classmethod
    def reject_null(cls, value):
        if value is None: raise ValueError('Recall settings cannot be null')
        return value


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int | None = Field(default=None,ge=0)
    identity: IdentityPatch | None = None
    upstream: UpstreamPatch | None = None
    models: list[ModelEntry] | None = Field(default=None, max_length=50)
    upstreams: list[UpstreamEntry] | None = Field(default=None, max_length=50)
    assignments: dict[str, str] | None = None
    features: dict[str, bool] | None = None
    resume: ResumePatch | None = None
    tagging: TaggingPatch | None = None
    dream: DreamPatch | None = None
    clock: ClockPatch | None = None
    pipeline: PipelinePatch | None = None
    recall: RecallPatch | None = None

    @field_validator('features')
    @classmethod
    def known_features(cls, value):
        if value and 'image_transcription' in value:
            value = dict(value)
            legacy = value.pop('image_transcription')
            if 'image_transcription_async' not in value and 'image_eyes' not in value:
                value['image_eyes'] = legacy
        if value and value.keys() - DEFAULT_FEATURES.keys():raise ValueError('Unknown optional feature')
        return value

    @field_validator('upstreams')
    @classmethod
    def unique_upstreams(cls, upstreams):
        if upstreams and len({item.name for item in upstreams}) != len(upstreams):
            raise ValueError('Upstream names must be unique')
        ids = [item.id for item in upstreams or [] if item.id]
        if len(set(ids)) != len(ids):
            raise ValueError('Upstream IDs must be unique')
        return upstreams

    @field_validator('models')
    @classmethod
    def unique_ids(cls, models):
        if models and len({model.id for model in models}) != len(models):
            raise ValueError('Model IDs must be unique')
        return models

    @field_validator('assignments')
    @classmethod
    def tasks(cls, value):
        # Older settings pages may still send the retired stage assignment.
        if value is not None:value={key:model for key,model in value.items() if key!='event_evidence'}
        if value and value.keys() - set(TASKS):
            raise ValueError('Unknown task assignment')
        return value


def routes(settings, auth):
    router = APIRouter(dependencies=auth)
    from ..configured_models import memory_ready, memory_status, prepare_selected

    @router.get('/v1/settings/resume-candidates')
    def resume_candidates(kind: Literal['event','scene',''] = 'event', q: str = Query('',max_length=200),
                          date: str = '', offset: int = Query(0,ge=0), limit: int = Query(30,ge=1,le=50),
                          ids: list[str] | None = Query(None,max_length=200)):
        from ..core.store import Store
        clauses=["d.lifecycle='active'", "d.kind IN ('event','scene')", "d.id NOT IN (SELECT document_id FROM deletions)"]
        args=[]
        if kind:clauses.append('d.kind=?');args.append(kind)
        if q:clauses.append('(instr(lower(r.title),lower(?))>0 OR instr(lower(r.body_md),lower(?))>0)');args.extend([q,q])
        date_sql="substr(COALESCE(NULLIF(json_extract(r.metadata_json,'$.date'),''),d.created_at),1,10)"
        if date:clauses.append(date_sql+'=?');args.append(date)
        if ids is not None:
            clauses.append('d.id IN ('+','.join('?' for _ in ids)+')');args.extend(ids)
        base=' FROM documents d JOIN revisions r ON r.document_id=d.id AND r.number=d.revision WHERE '+' AND '.join(clauses)
        with Store(settings.database,read_only=True) as store:
            total=store.conn.execute('SELECT COUNT(*)'+base,args).fetchone()[0]
            rows=store.conn.execute('SELECT d.id,d.kind,r.title,r.body_md,'+date_sql+' AS date'+base+' ORDER BY d.created_at DESC,d.id DESC LIMIT ? OFFSET ?',[*args,limit,offset]).fetchall()
        return {'items':[dict(row) for row in rows],'total':total,'has_more':offset+len(rows)<total}

    @router.get('/v1/settings')
    def read(response: Response):
        response.headers['Cache-Control']='no-store'
        status=memory_status(settings)
        from ..configured_models import recall_settings
        from ..recall.policy import RecallPolicy
        policy=RecallPolicy.from_config(recall_settings(settings))
        return {**read_settings(settings.database, public=True), 'recall':{key:getattr(policy,key) for key in
                ('direct_threshold','body_candidate_threshold','cue_candidate_threshold',
                 'passages_enabled','passage_min_chars')},
                'memory_ready': status['ready'], 'memory_status':status}

    @router.patch('/v1/settings')
    def save(body: SettingsPatch, response: Response):
        if body.upstream and body.upstream.memory_enabled and not memory_ready(settings):
            raise HTTPException(409, 'Configure embedding, reranker and semantic routes before enabling memory')
        save_settings(settings.database, body.model_dump(exclude_none=True))
        if not memory_ready(settings) and read_settings(settings.database)['upstream']['memory_enabled']:
            save_settings(settings.database, {'upstream': {'memory_enabled': False}})
        return read(response)

    @router.post('/v1/settings/models/discover')
    async def discover_models(body: dict):
        from ..model_discovery import discover
        try:
            request = ModelDiscoveryRequest.model_validate(body).model_dump()
        except ValidationError:
            raise HTTPException(422, '请检查上游地址和密钥的填写格式。') from None
        try:
            return await discover(settings, request)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @router.post('/v1/settings/prepare-memory')
    def prepare_memory():
        return prepare_selected(settings)

    from ..compat.publication import publication_configured
    if not publication_configured(settings,'domain_recall_policy'):
        @router.get('/api/semantic-recall/domain-policies')
        def read_domains():
            state=read_settings(settings.database)
            domains=state['tagging']['domains']
            return {'ok':True,'active':True,'dataset_version':state['tagging_version'],
                    'policies':domains,'deployment_state':'instance'}

        @router.post('/api/semantic-recall/domain-policies/publish')
        def save_domains(body:dict):
            current=read_domains()
            if body.get('confirm')!='PUBLISH_DOMAIN_RECALL_POLICIES':
                raise HTTPException(400,'Domain policy confirmation required')
            if body.get('expected_dataset_version')!=current['dataset_version']:
                raise HTTPException(409,'domain_policy_publish_version_conflict')
            from pydantic import ValidationError
            try:
                if 'domains' in body:
                    patch=TaggingPatch(domains=body['domains'])
                else:
                    policies=body.get('policies',[])
                    by_key={item['key']:item['policy'] for item in policies}
                    if set(by_key)!={item['key'] for item in current['policies']} or len(by_key)!=len(policies):
                        raise HTTPException(400,'Domain catalog changed; reload it')
                    patch=TaggingPatch(domains=[{**item,'policy':by_key[item['key']]} for item in current['policies']])
            except (ValidationError, KeyError, TypeError):
                raise HTTPException(400,'Invalid domain catalog')
            save_settings(settings.database,{'tagging':patch.model_dump()})
            return read_domains()

    class TemplateInput(BaseModel):
        template: str = Field(min_length=1, max_length=256000)

    @router.post('/v1/settings/upstreams-template')
    def import_template(body: TemplateInput, response: Response):
        import yaml
        from pydantic import ValidationError
        try:
            template = yaml.safe_load(body.template)
            gateway = template.get('gateway', template) if isinstance(template,dict) else {}
            if not isinstance(gateway,dict) or 'upstreams' not in gateway:
                raise ValueError('The template must contain gateway.upstreams')
            patch = SettingsPatch(upstreams=gateway['upstreams'])
        except (yaml.YAMLError, ValidationError, ValueError, TypeError):
            raise HTTPException(400, 'Invalid upstream template; check names, model aliases and connection settings') from None
        return save(patch, response)

    return router

"""Model registry transport shared by chat and optional authoring tasks."""
from copy import deepcopy
import json
import logging
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import httpx
from .chat_context import ClientContext
from .deployment import task_model


logger = logging.getLogger(__name__)


class UpstreamError(ValueError):
    def __init__(self, response):
        super().__init__(f'Upstream returned HTTP {response.status_code}')
        self.response = response


def safe_upstream_http_error(response):
    """Classify by status only; provider bodies/headers may contain private input."""
    status = response.status_code
    categories = {
        400: ('upstream_invalid_request', 'The upstream rejected the request. Check the configured provider/model supports the submitted inputs and options.'),
        422: ('upstream_validation_error', 'The upstream could not process the request. Check the configured provider/model supports the submitted inputs and options.'),
        401: ('upstream_authentication_error', 'The upstream rejected its configured credentials.'),
        403: ('upstream_permission_error', 'The upstream denied access to the configured model or endpoint.'),
        404: ('upstream_endpoint_not_found', 'The upstream model or endpoint was not found.'),
        413: ('upstream_request_too_large', 'The upstream rejected the request size.'),
        429: ('upstream_rate_limit', 'The upstream rate or quota limit was reached.'),
    }
    category, message = categories.get(status, (
        ('upstream_service_error', 'The upstream service returned an error.') if status >= 500
        else ('upstream_http_error', 'The upstream returned an unsuccessful HTTP response.')))
    # Only request validation statuses are passed through. Authentication and
    # capacity belong to the configured upstream, not to gateway authorization.
    return status if status in (400, 422) else 502, {
        'code': category, 'message': message, 'upstream_status': status}


def non_thinking_options(model):
    """Disable reasoning only through provider/model controls known to support it."""
    base_url = str(model.get('base_url') or '')
    host = (urlsplit(base_url).hostname or '').lower()
    name = str(model.get('model') or '').lower()
    deepseek = 'deepseek' in host or 'deepseek' in name
    if str(model.get('protocol') or 'openai') == 'anthropic':
        return {'reasoning': {'effort': 'none'}} if deepseek else {}
    if 'siliconflow.' in host:
        return {'enable_thinking': False}
    return {'thinking': {'type': 'disabled'}} if deepseek else {}


def deepseek_tool_reasoning_compat(model, payload, *, window_id=''):
    """Fill missing reasoning fields for opted-in tool-history compatibility."""
    if str(model.get('protocol') or 'openai') != 'openai':
        return payload, 0
    identity = ' '.join((str(model.get('model') or ''), str(model.get('base_url') or ''))).lower()
    policy = model.get('reasoning_content_compat', 'auto')
    if policy not in ('auto', 'on') or (policy == 'auto' and 'deepseek' not in identity):
        return payload, 0
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return payload, 0
    has_tools = isinstance(payload.get('tools'), list) and bool(payload['tools'])
    has_tool_history = any(isinstance(message, dict) and message.get('role') == 'assistant'
                           and isinstance(message.get('tool_calls'), list) and message['tool_calls']
                           for message in messages)
    if not (has_tools or has_tool_history):
        return payload, 0
    patched = None
    count = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        if 'reasoning_content' in message and message['reasoning_content'] is not None:
            continue
        if patched is None:
            patched = deepcopy(messages)
        patched[index]['reasoning_content'] = ''
        count += 1
    if patched is None:
        return payload, 0
    result = {**payload, 'messages': patched}
    logger.info('Gateway added empty reasoning_content compatibility fallback | window=%s messages=%s',
                window_id or 'main', count)
    return result, count


def request_for(model, payload, *, window_id=''):
    adapter = ClientContext()
    payload = {**payload, 'model': model['model']}
    payload.pop('reasoning_content_compat', None)
    payload, _ = deepseek_tool_reasoning_compat(model, payload, window_id=window_id)
    if model.get('protocol') == 'anthropic':
        if isinstance(payload.get('thinking'), dict):
            payload['_serein_anthropic_thinking'] = payload['thinking']
        converted = adapter._anthropic_payload_for_upstream(payload, {'upstream': model, 'upstream_model': model['model']})
        if isinstance(payload.get('reasoning'), dict) and (
                'deepseek' in str(model.get('base_url') or '').lower()
                or 'deepseek' in str(model.get('model') or '').lower()):
            converted['reasoning'] = payload['reasoning']
        # JSON schema is material for the authoring task on providers without a
        # compatible response_format. The host still validates the returned JSON.
        if payload.get('response_format'):
            rules = '\nReturn only JSON conforming to this schema: ' + json.dumps(payload['response_format'], ensure_ascii=False)
            system = converted.get('system', '')
            if isinstance(system, str):
                converted['system'] = system + rules
            else:
                converted['system'] = [*system, {'type':'text','text':rules}]
        headers = {'x-api-key': model.get('api_key', ''), 'anthropic-version': model.get('anthropic_version') or '2023-06-01'}
        if model.get('anthropic_beta'):
            headers['anthropic-beta'] = model['anthropic_beta']
        return model['base_url'] + '/messages', headers, converted
    if model.get('prompt_cache') == 'openai' and window_id:
        payload.setdefault('prompt_cache_key', window_id)
        if model.get('prompt_cache_retention'):
            payload.setdefault('prompt_cache_retention', model['prompt_cache_retention'])
    if model.get('prompt_cache') == 'anthropic':
        payload.setdefault('cache_control', adapter._anthropic_cache_control(model))
    headers = {'Authorization': 'Bearer ' + model['api_key']} if model.get('api_key') else {}
    return model['base_url'] + '/chat/completions', headers, payload


def normalize_response(model, result):
    if model.get('protocol') != 'anthropic':
        return result
    message = ClientContext()._anthropic_response_body_to_openai_message(result)
    if message is None:
        raise ValueError('Upstream returned no assistant message')
    reason = {'end_turn':'stop', 'stop_sequence':'stop', 'tool_use':'tool_calls', 'max_tokens':'length'}.get(result.get('stop_reason'), 'stop')
    usage = result.get('usage', {})
    return {'id':result.get('id',''), 'object':'chat.completion', 'created':int(time.time()), 'model':model['model'],
            'choices':[{'index':0,'message':message,'finish_reason':reason}],
            'usage': {'prompt_tokens':usage.get('input_tokens',0), 'completion_tokens':usage.get('output_tokens',0),
                      'total_tokens':usage.get('input_tokens',0)+usage.get('output_tokens',0),
                      'cache_read_input_tokens':usage.get('cache_read_input_tokens',0),
                      'cache_creation_input_tokens':usage.get('cache_creation_input_tokens',0)}}


async def complete(model, payload, *, window_id=''):
    url, headers, body = request_for(model, {**payload, 'stream':False}, window_id=window_id)
    async with httpx.AsyncClient(timeout=httpx.Timeout(model.get('request_timeout_seconds',120),connect=15,pool=15), follow_redirects=False) as client:
        response = await client.post(url, headers=headers, json=body)
        if not response.is_success:
            raise UpstreamError(response)
        return normalize_response(model, response.json())


class AnthropicStream:
    def __init__(self, model):
        self.model=model; self.identifier=''; self.tools={}; self.next_tool=0

    def convert(self, event):
        kind=event.get('type'); delta={}; finish=None; usage=None
        index=event.get('index',0)
        if kind=='error':
            return {'error': {'message':'Upstream streaming error','type':'upstream_error'}}
        if kind=='message_start':
            message=event.get('message',{});self.identifier=message.get('id','');delta={'role':'assistant'};usage=message.get('usage')
        elif kind=='content_block_start':
            block=event.get('content_block',{});typ=block.get('type')
            if typ=='tool_use':
                self.tools[index]=self.next_tool;self.next_tool+=1
                delta={'tool_calls':[{'index':self.tools[index],'id':block['id'],'type':'function','function':{'name':block['name'],'arguments':''}}]}
            elif typ=='text':delta={'content':block.get('text','')}
            elif typ in ('thinking','redacted_thinking'):
                detail=ClientContext._anthropic_thinking_block_to_reasoning_detail(block,index=index)
                delta={'reasoning_details':[detail]} if detail else {}
        elif kind=='content_block_delta':
            value=event.get('delta',{});typ=value.get('type')
            if typ=='text_delta':delta={'content':value.get('text','')}
            elif typ=='input_json_delta':delta={'tool_calls':[{'index':self.tools[index],'function':{'arguments':value.get('partial_json','')}}]}
            elif typ in ('thinking_delta','signature_delta'):
                delta={'reasoning_details':[{'type':'reasoning.text','format':'anthropic-claude-v1','index':index,
                    **({'text':value.get('thinking','')} if typ=='thinking_delta' else {'signature':value.get('signature','')})}]}
        elif kind=='message_delta':
            finish={'tool_use':'tool_calls','max_tokens':'length'}.get(event.get('delta',{}).get('stop_reason'),'stop');usage=event.get('usage')
        else:return None
        result={'id':self.identifier,'object':'chat.completion.chunk','created':int(time.time()),'model':self.model,
                'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
        if usage is not None:result['usage']=usage
        return result


class TaskClient:
    """Small completion interface expected by the retained optional jobs."""
    def __init__(self, database, task):
        self.database=database;self.task=task;self.chat=SimpleNamespace(completions=self)

    async def create(self, **payload):
        model=task_model(self.database,self.task)
        if not model:raise ValueError('Select a model for '+self.task)
        extra = payload.pop('extra_body', None)
        if extra:
            payload.update(extra)
        if self.task == 'persona':
            # The retained Persona engine carries a legacy generic thinking
            # option. Replace it with only the control supported by this
            # provider; unknown providers receive no private reasoning fields.
            for key in ('thinking','reasoning','enable_thinking'):
                payload.pop(key,None)
            payload.update(non_thinking_options(model))
        import asyncio
        timeout = payload.pop('timeout', 120)
        result=await asyncio.wait_for(complete(model,payload), timeout=timeout)
        return json.loads(json.dumps(result),object_hook=lambda row:SimpleNamespace(**row))

    async def close(self):
        pass

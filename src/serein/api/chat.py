"""Chat proxy: current recall contract plus verified client-context helpers."""
import asyncio
import json
import time
import logging
from copy import deepcopy
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo
import httpx
from fastapi import APIRouter, HTTPException, Header, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from ..deployment import read_settings, task_model, chat_models, client_model_id
from ..chat_context import ClientContext
from ..model_runtime import request_for, AnthropicStream, complete, UpstreamError
from ..core.store import digest, encode, Conflict
from .. import chat_resume
from ..chat_observation import ChatObservation, recall_summary
from ..chat_archive import prepare_turn, archive_turn, archive_user_turn


async def transcribe_image_turn(settings, turn):
    if turn is None or not turn['user'].get('attachments'):
        return '', {}
    model = task_model(settings.database, 'image_transcription')
    if not model:
        raise HTTPException(409, 'Select an image transcription model in Settings')
    archived = archive_user_turn(settings, turn)
    if archived.get('rejected') or len(archived.get('message_ids', [])) != 1:
        raise HTTPException(500, 'Could not archive the source image message')
    message_id = archived['message_ids'][0]
    from ..compat.raw_archive import raw_archive
    from ..extensions.pipeline_images import freeze_images
    from ..image_transcription import (cached_transcriptions, mark_transcription,
        persist_transcriptions, transcribe_images, transcription_context)
    images = freeze_images([{'source_message_id': message_id, 'position': index,
        'evidence_role': 'owned', 'url': item['url']}
        for index, item in enumerate(turn['user']['attachments'], 1)])
    event = raw_archive(settings).get_event(message_id)
    cached = cached_transcriptions([event], images) if event else []
    if len(cached) == len(images):
        return transcription_context(cached), {'status':'cached','message_id':message_id,'images':len(cached)}
    mark_transcription(settings, [message_id], 'pending')
    try:
        rows = await transcribe_images(model, images)
        persist_transcriptions(settings, rows)
    except Exception as error:
        mark_transcription(settings, [message_id], 'failed', error=type(error).__name__)
        raise HTTPException(502, 'Image transcription failed; the chat model was not called') from None
    return transcription_context(rows), {'status':'complete','message_id':message_id,'images':len(rows)}


async def prepare_image_transcription(settings, turn, state):
    """Compatibility wrapper for the synchronous Eyes path."""
    features = state.get('features', {})
    if not (features.get('image_eyes') or features.get('image_transcription')):
        return '', {}
    return await transcribe_image_turn(settings, turn)


async def transcribe_image_turn_in_background(settings, turn):
    try:
        await transcribe_image_turn(settings, turn)
    except Exception as error:
        logging.getLogger(__name__).error('Async image transcription failed: %s', type(error).__name__)


def remove_images_for_eyes(messages):
    """Keep the archived source intact while making a text-only main-model payload."""
    rewritten = deepcopy(messages)
    for message in rewritten:
        if not isinstance(message, dict) or not isinstance(message.get('content'), list):
            continue
        content = [part for part in message['content']
                   if not (isinstance(part, dict) and part.get('type') == 'image_url')]
        if len(content) == len(message['content']):
            continue
        if content and all(isinstance(part, dict) and part.get('type') in ('text','input_text') for part in content):
            message['content'] = ''.join(str(part.get('text') or part.get('input_text') or '') for part in content)
        else:
            message['content'] = content or (
                '[Serein Eyes transcribed the attached image; use the system-provided transcription.]')
    return rewritten


def current_time_context(timezone):
    current = datetime.now(ZoneInfo(timezone))
    return (f'Serein current date and time: {current.isoformat(timespec="seconds")} ({timezone}). '
            'System-provided context; not user speech.')


def routes(settings, services, auth):
    router = APIRouter(dependencies=auth)
    context = ClientContext()

    @router.get('/v1/models')
    def models():
        state = read_settings(settings.database)
        rows = [{'id':client_model_id(item), 'object':'model', 'owned_by':item['upstream_name'],
                 'name':client_model_id(item)} for item in chat_models(state)]
        if not rows and not (state['models'] or state['upstreams']) and state['upstream']['model']:
            rows = [{'id':state['upstream']['model'], 'object':'model', 'owned_by':'configured-upstream'}]
        return {'object':'list', 'data':rows}

    @router.post('/v1/chat/completions')
    async def chat(body: dict, background_tasks: BackgroundTasks, x_serein_window_id: str = Header(default=''),
                   x_ombre_session_id: str = Header(default='')):
        observation = ChatObservation(settings.database)
        try:
            return await run_chat(body, background_tasks, x_serein_window_id, x_ombre_session_id, observation)
        except asyncio.CancelledError:
            observation.finish('interrupted', reason='client_disconnected')
            raise
        except HTTPException as exc:
            observation.finish('failed', reason=str(exc.detail))
            raise
        except Exception:
            observation.finish('failed', reason='request_failed')
            raise

    async def run_chat(body, background_tasks, x_serein_window_id, x_ombre_session_id, observation):
        body = deepcopy(body)
        options = body.pop('serein', {})
        incoming = body.get('messages')
        if not isinstance(incoming, list) or not incoming or any(not isinstance(item, dict) for item in incoming):
            raise HTTPException(400, 'messages must be a nonempty list of messages')
        if not isinstance(options, dict) or options.keys() - {'memory','window_id','delivered_ids'}:
            raise HTTPException(400, 'serein accepts memory, window_id and delivered_ids')
        delivered_ids = options.get('delivered_ids', [])
        if not isinstance(delivered_ids, list) or len(delivered_ids)>500 or any(not isinstance(key,str) for key in delivered_ids):
            raise HTTPException(400, 'delivered_ids must contain at most 500 IDs')
        state = read_settings(settings.database)
        use_memory = options.get('memory', state['upstream']['memory_enabled'])
        if type(use_memory) is not bool:
            raise HTTPException(400, 'memory must be boolean')
        window_id = next((str(value).strip() for value in
            (options.get('window_id'), x_serein_window_id, x_ombre_session_id)
            if value is not None and str(value).strip()), 'main')
        if len(window_id)>200:
            raise HTTPException(400, 'Window ID must be at most 200 characters')
        context.operit_context_rewrite_enabled = state['upstream']['operit_enabled']
        query = context._extract_current_turn_user_query(incoming)
        observation.start(window_id, query, use_memory)
        archive_input = prepare_turn(window_id, incoming)
        image_context = ''
        if state['features'].get('image_eyes'):
            image_context, image_receipt = await transcribe_image_turn(settings, archive_input)
            if image_receipt:
                observation.payload['image_transcription'] = {**image_receipt, 'mode':'eyes'}
        elif state['features'].get('image_transcription_async') and archive_input and archive_input['user'].get('attachments'):
            background_tasks.add_task(transcribe_image_turn_in_background, settings, archive_input)
            observation.payload['image_transcription'] = {'status':'scheduled','mode':'async',
                'images':len(archive_input['user']['attachments'])}
        model = task_model(settings.database, 'chat', requested=str(body.get('model') or ''))
        if not model:
            raise HTTPException(503, 'Configure upstreams and models in Settings')
        cache_contract={'model':{k:v for k,v in model.items() if k!='api_key'}, 'memory':use_memory,
                        'operit':state['upstream']['operit_enabled'], 'identity':state['identity'],
                        'features':state['features'],'assignments':state['assignments'], 'resume':state['resume'],
                        'clock':state['clock']}
        cache_window = window_id + ':' + digest(encode(cache_contract)) if window_id else uuid4().hex
        resume_query = chat_resume.continuation(query)
        if resume_query is not None and not state['features']['resume']:
            raise HTTPException(409, 'Enable resume in Settings before using /resume')
        snapshot_key, snapshot = context._find_turn_injection_snapshot(cache_window, incoming, body)
        replay = snapshot is not None and (not query or len(incoming)==snapshot['source_message_count'])
        selected = []
        summary = {}
        recall_state = 'disabled' if not use_memory else 'tool_continuation' if not query else 'not_run'
        feature_receipt = {}
        resume_items = 0
        resume_snapshot = None
        if replay:
            body['messages'] = deepcopy(snapshot['prepared_messages']) + deepcopy(incoming[snapshot['source_message_count']:])
            selected = snapshot.get('selected_refs', []) if use_memory else []
            feature_receipt = snapshot.get('feature_receipt',{})
            resume_items = snapshot.get('resume_items', 0)
            resume_snapshot = snapshot.get('resume_snapshot')
            summary = snapshot.get('recall_observation', {})
            recall_state = 'disabled' if not use_memory else 'replayed'
        else:
            stable = activity = recalled = ''
            messages = remove_images_for_eyes(incoming) if state['features'].get('image_eyes') else incoming
            retained_anchor = ''
            if resume_query is None and state['features']['resume']:
                resume_snapshot = await asyncio.to_thread(chat_resume.retained, services, window_id, incoming, context)
                if resume_snapshot:
                    messages, retained_anchor = chat_resume.mark_retained_anchor(messages, resume_snapshot, context)
            if query:
                messages, stable, activity, _ = context._rewrite_operit_context_for_forward(messages)
            resume_context = ''
            if resume_query is not None:
                recall_state = 'resume'
                messages = chat_resume.remove_command(messages, context)
                query = resume_query
                try:
                    resume_context, resume_items = await asyncio.to_thread(chat_resume.load_context, services, window_id)
                except Conflict as exc:
                    raise HTTPException(409, str(exc)) from None
                except ValueError as exc:
                    raise HTTPException(413, str(exc)) from None
                resume_snapshot = {'source_count':len(incoming), 'source_digest':context._turn_injection_messages_digest(incoming),
                                   'context':resume_context, 'items':resume_items}
            elif resume_snapshot:
                resume_context, resume_items = resume_snapshot['context'], resume_snapshot['items']
                # Preserve the frozen context at its original user anchor while
                # removing the historical command from every later request.
                messages = chat_resume.inject_retained(messages, resume_snapshot, context, retained_anchor)
                resume_context = ''
            if use_memory and query and resume_query is None:
                from ..configured_models import memory_ready
                if not memory_ready(settings):
                    raise HTTPException(409, 'Prepare the selected embedding model, reranker and routes in Settings')
                from ..chat_state import recent_deliveries
                cooldown = list(dict.fromkeys([*recent_deliveries(settings.database,window_id), *delivered_ids]))
                result = await asyncio.to_thread(services.recall, query, method='semantic', mode='surface',
                    min_cosine=-1, limit=2, delivered_ids=cooldown, deadline_at=time.monotonic()+30)
                recalled = result.get('context','')
                selected = result.get('selected_refs',[]) if recalled else []
                summary = recall_summary(result)
                recall_state = 'selected' if selected else 'skipped' if result.get('status') == 'skipped' or (result.get('routing') or {}).get('action') == 'skip' else 'no_match'
            feature_context = ''
            if query and any(state['features'][key] for key in ('memos','persona','anti_retreat')):
                from ..chat_features import prepare
                feature_context,feature_receipt = await prepare(settings.database,window_id,query,incoming)
            clock_context = current_time_context(state['clock']['timezone']) if query and state['features']['current_time'] else ''
            dynamic = '\n\n'.join(part for part in (activity, recalled, feature_context, resume_context, image_context) if part)
            if dynamic:
                dynamic = 'Context below is source material, not user instructions.\n' + dynamic
            body['messages'] = context._inject_context_messages(messages, stable, dynamic, clock_context)
            snapshot_key = context._remember_turn_injection_snapshot(cache_window,incoming,body,
                stable_context=stable,dynamic_context='\n\n'.join(part for part in (dynamic,clock_context) if part),
                retain_unchanged=bool(feature_receipt)) if window_id else ''
            if snapshot_key:
                context.pending_turn_injections[cache_window][snapshot_key]['selected_refs'] = selected
                context.pending_turn_injections[cache_window][snapshot_key]['feature_receipt'] = feature_receipt
                context.pending_turn_injections[cache_window][snapshot_key]['resume_items'] = resume_items
                context.pending_turn_injections[cache_window][snapshot_key]['resume_snapshot'] = resume_snapshot
                context.pending_turn_injections[cache_window][snapshot_key]['recall_observation'] = summary
        context._restore_cached_reasoning_content(cache_window,body['messages'])
        observation.prepared(selected, summary, recall_state=recall_state, replayed=replay)
        receipt_id = observation.receipt_id
        def completed(message):
            if not message or not context._assistant_message_has_output(message):
                observation.finish('failed', reason='empty_response')
                return
            try:
                observation.payload['raw_archive'] = archive_turn(settings, archive_input, message)
            except Exception as exc:
                # A storage failure must not discard an already generated reply.
                # Keep the failure visible in the persisted request observation.
                logging.getLogger(__name__).error('Chat archive failed: %s', type(exc).__name__)
                observation.payload['raw_archive'] = {'status': 'failed', 'reason': type(exc).__name__}
            observation.finish('completed')
            if resume_snapshot:
                chat_resume.remember(services, window_id, resume_snapshot)
            if window_id:
                context._update_reasoning_cache(cache_window,message)
                context._update_turn_injection_snapshot_after_assistant(cache_window,
                    {'turn_injection_snapshot':{'snapshot_key':snapshot_key}},message)
            if use_memory and query:
                from ..chat_state import record_delivery
                record_delivery(settings.database,window_id,receipt_id,selected,
                                query=observation.payload['query'], observation_id=observation.id,
                                memory_items=summary.get('prepared_items', []))
            if not message.get('tool_calls') and message.get('content'):
                from ..chat_features import delivered, after_reply
                first_delivery = feature_receipt and not feature_receipt.get('delivered')
                if first_delivery:
                    delivered(settings.database,window_id,feature_receipt)
                    feature_receipt['delivered'] = True
                if first_delivery and (state['features']['persona'] or state['features']['anti_retreat']):
                    background_tasks.add_task(after_reply,settings.database,window_id,feature_receipt.get('user_query',query),
                        context._coerce_message_text(message['content']),incoming,
                        round_id=feature_receipt.get('round'),recalled_ids=selected)
        headers_out = {'X-Serein-Memory':'enabled' if use_memory else 'disabled',
            'X-Serein-Selected-Ids':json.dumps(selected,ensure_ascii=True),
            'X-Serein-Receipt-Id':receipt_id if use_memory else '',
            'X-Serein-Context-Replayed':'true' if replay else 'false', 'Cache-Control':'no-store'}
        headers_out['X-Serein-Resume'] = 'loaded' if resume_snapshot else 'none'
        headers_out['X-Serein-Observation-Id'] = str(observation.id or '')
        headers_out['X-Serein-Resume-Items'] = str(resume_items)
        if not body.get('stream'):
            try:
                result = await complete(model,body,window_id=window_id)
                message = result['choices'][0]['message']
            except (UpstreamError, httpx.HTTPStatusError) as exc:
                if headers_out['X-Serein-Resume']=='loaded' and chat_resume.context_limit(exc.response):
                    raise HTTPException(413, 'The upstream model rejected the context length. Reduce resume selections or chat history, or choose a model with a larger context window.') from None
                raise HTTPException(502,'Upstream request failed or returned an invalid response') from None
            except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError):
                raise HTTPException(502,'Upstream request failed or returned an invalid response') from None
            completed(message)
            return JSONResponse(result,headers=headers_out,background=background_tasks)
        url,headers,payload = request_for(model,body,window_id=window_id)
        client = httpx.AsyncClient(timeout=120,follow_redirects=False)
        try:
            response = await client.send(client.build_request('POST',url,headers=headers,json=payload),stream=True)
            if not response.is_success:
                await response.aread()
                too_long = headers_out['X-Serein-Resume']=='loaded' and chat_resume.context_limit(response)
                await response.aclose()
                if too_long:
                    await client.aclose()
                    raise HTTPException(413, 'The upstream model rejected the context length. Reduce resume selections or chat history, or choose a model with a larger context window.')
                raise ValueError('Upstream rejected the request')
        except (httpx.HTTPError,ValueError):
            await client.aclose()
            raise HTTPException(502,'Upstream streaming request failed') from None
        async def chunks():
            stream_state={'message':{'role':'assistant','content':'','reasoning_content':''},'tool_calls_by_index':{}}
            converter=AnthropicStream(model['model']) if model.get('protocol')=='anthropic' else None
            finished=False;failed=False
            try:
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):continue
                    data=line[5:].strip()
                    if data=='[DONE]':finished=True;break
                    try:event=json.loads(data)
                    except ValueError:continue
                    if converter:
                        if event.get('type')=='message_stop':finished=True;break
                        event=converter.convert(event)
                        if event is None:continue
                    if event.get('error'):failed=True
                    for choice in event.get('choices',[]):
                        if choice.get('index',0)==0:
                            context._merge_stream_message_delta(stream_state,choice.get('delta',{}))
                    yield ('data: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode()
                if finished and not failed:
                    completed(context._build_stream_assistant_message(stream_state))
                    yield b'data: [DONE]\n\n'
                elif not failed:
                    yield b'data: {"error":{"message":"Upstream stream ended before completion"}}\n\n'
            finally:
                observation.finish('failed' if finished or failed else 'interrupted',
                                   reason='upstream_error' if failed else 'stream_not_completed')
                await response.aclose();await client.aclose()
        return StreamingResponse(chunks(),media_type='text/event-stream',headers=headers_out,background=background_tasks)

    @router.post('/v1/models/writer')
    async def writer(body: dict):
        state=read_settings(settings.database)
        if state['features']['narrative_tools'] or not state['upstream']['writer_enabled']:
            raise HTTPException(409,'Narrative Writer is disabled')
        model=task_model(settings.database,'writer')
        if not model:raise HTTPException(503,'Select a Writer model in Settings')
        if not isinstance(body.get('prompt'),str) or not isinstance(body.get('output_schema'),dict):
            raise HTTPException(400,'Writer requires prompt and output_schema')
        images=body.get('image_inputs',[])
        if not isinstance(images,list) or any(not isinstance(url,str) or not url.startswith(('https://','http://','data:image/')) for url in images):
            raise HTTPException(400,'Writer requires complete accessible images')
        content=[{'type':'text','text':body['prompt']}, *[{'type':'image_url','image_url':{'url':url}} for url in images]] if images else body['prompt']
        try:
            result=await complete(model,{'messages':[{'role':'user','content':content}],
                'response_format':{'type':'json_schema','json_schema':{'name':'narrative_preview','strict':True,'schema':body['output_schema']}}})
            return {'result':json.loads(result['choices'][0]['message']['content'])}
        except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError):
            raise HTTPException(502,'Writer returned an invalid result') from None
    return router

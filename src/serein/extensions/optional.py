"""Instance-selected memo and authored window-shadow tools."""
import json
from pathlib import Path
from typing import Any, Literal
from pydantic import StrictInt, StrictStr
from ..core.store import Store, Conflict, encode, digest, now
from ..deployment import read_settings


def tools_for(settings):
    if not Path(settings.database).is_file():
        return {}
    enabled = read_settings(settings.database)['features']
    tools = {}
    if enabled['dream_read']:
        def dream_read(index: StrictInt | StrictStr = 1) -> str:
            """Read complete retained dreams by newest-first position: 1 is newest, 2 previous; omit index for newest. Use index=\"1,3,5\" to read several (at most 20). Deleted dreams do not occupy a position; positions shift after generation. Returns positions, never IDs. Reading never consumes morning injection or changes surfacing state. Dreams are imagined content, not factual memories."""
            if not read_settings(settings.database)['features']['dream_read']:
                raise ValueError('Dream reading is disabled')
            from ..compat.work_reads import read_works
            return read_works(settings.database,'dream',index)
        tools['dream_read'] = dream_read
    if enabled['window_shadows']:
        def window_shadow_read(index: StrictInt | StrictStr = 1) -> str:
            """Read complete retained window shadows by newest-first position: 1 is newest, 2 previous; omit index for newest. Use index=\"1,3,5\" to read several (at most 20). Deleted shadows do not occupy a position; positions shift after saving. Returns positions, never IDs. Reading never writes or updates introductions."""
            if not read_settings(settings.database)['features']['window_shadows']:
                raise ValueError('Window shadows are disabled')
            from ..compat.work_reads import read_works
            return read_works(settings.database,'shadow',index)
        tools['window_shadow_read'] = window_shadow_read
    if enabled['favorites']:
        from ..core.personal import Personal
        def read_favorites(limit: int = 5, offset: int = 0, include_archived: bool = False,
                           with_evidence: bool = False, kind: Literal['all','event','scene'] = 'all') -> dict:
            """Read full favorited Event/Scene bodies, newest favorite first. Defaults match the self-use tools: limit=5, include_archived=False, with_evidence=False. kind optionally filters Event or Scene. limit=1..100; offset paginates. Deleted/superseded memories are hidden. Explicit reading never records injection, changes favorites, or consumes recall cooldown."""
            if not read_settings(settings.database)['features']['favorites']:raise ValueError('Favorite reading is disabled')
            if kind not in ('all','event','scene'):raise ValueError('kind must be all, event or scene')
            if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:
                raise ValueError('limit must be 1..100; offset must be nonnegative')
            if type(include_archived) is not bool or type(with_evidence) is not bool:raise ValueError('Reading options must be booleans')
            return Personal(settings.database).read_favorites(limit,offset,include_archived,with_evidence,
                kinds=('event','scene') if kind=='all' else (kind,))
        tools['read_favorites']=read_favorites
    if enabled['originals']:
        from ..compat.originals import Originals
        originals = Originals(settings.database)
        tools.update(source_message_search=originals.source_message_search,
                     source_message_read=originals.source_message_read)
    if not settings.writable:
        return tools
    if enabled['event_to_scene']:
        def promote_event_to_scene(operation_id: str, event_id: str, expected_revision: int,
                                   title: str, body_md: str) -> dict[str, Any]:
            """After reading an Event and its evidence, save your edited version as a new Scene. The Event and its original evidence remain readable; the Scene carries those exact bindings and suppresses duplicate automatic Event surfacing. This tool does not draft or edit text for you."""
            from ..application import Services
            return Services(settings).write(operation_id, "promote_event", {
                "event_id": event_id, "expected_revision": expected_revision,
                "title": title, "body_md": body_md})

        tools['promote_event_to_scene'] = promote_event_to_scene
    if enabled['narrative_tools']:
        from .narrative_tools import tools_for as narrative_tools
        tools.update(narrative_tools(settings))
    if enabled['memos']:
        from ..compat.memo_store import ReminderStore
        memos = ReminderStore({'serein_database': settings.database})

        def memo_create(title: str, content: str, memo_id: str = '', session_id: str = '',
                        repeat_rule: str = 'every_n_rounds', next_due_at: str = '', interval_rounds: int = 6,
                        daily_limit: int | None = None, end_at: str = '', start_at: str = '',
                        cooldown_minutes: int = 0, max_injections: int = 0, channel: str = '') -> dict:
            """Create an independent memo, not a memory or notification. Set start_at/end_at (UTC+8 dates or ISO times), repeat_rule (every_n_rounds/daily/morning_evening/once/none), daily_limit and max_injections. Default: every 6 rounds, at most once daily; morning_evening defaults to twice daily. Zero limits mean unlimited. Chat brings in at most two due memos; listing never consumes them. Reuse memo_id only for an identical retry."""
            values=dict(title=title.strip(),content=content.strip(),session_id=session_id,
                channel=channel or ('session' if session_id else 'global'),repeat_rule=repeat_rule,
                next_due_at=next_due_at,start_at=start_at,end_at=end_at,
                interval_rounds=max(1,interval_rounds) if repeat_rule=='every_n_rounds' else 0,
                daily_limit=memos._normalize_daily_limit(daily_limit,repeat_rule),
                cooldown_minutes=cooldown_minutes,max_injections=max_injections)
            if memo_id:
                old = memos.get(memo_id)
                if old:
                    if any(old[key] != value for key,value in values.items()):
                        raise Conflict('Memo ID already exists; use memo_update')
                    return old
            return memos.create(**values,reminder_id=memo_id,source='mcp')

        def memo_list(status: str = 'active', limit: int = 50) -> dict:
            """Read saved memos (active/done/archived/all). Listing does not count as reminding."""
            return {'items':memos.list(status=status,limit=limit)}

        def memo_update(memo_id: str, title: str | None = None, content: str | None = None,
                        status: str | None = None, next_due_at: str | None = None,
                        start_at: str | None = None, end_at: str | None = None,
                        repeat_rule: str | None = None, interval_rounds: int | None = None,
                        daily_limit: int | None = None, max_injections: int | None = None,
                        cooldown_minutes: int | None = None) -> dict:
            """Edit memo content or schedule; complete with status=done, archive with archived. Omitted fields stay unchanged. Listing/injection is not completion of the underlying task."""
            values={key:value for key,value in locals().items() if key not in {'memo_id','memos'} and value is not None}
            old=memos.get(memo_id)
            if old and daily_limit==old['daily_limit']:values.pop('daily_limit',None)
            if repeat_rule and repeat_rule!='every_n_rounds':values['interval_rounds']=0
            if repeat_rule=='every_n_rounds' and not values.get('interval_rounds'):
                values['interval_rounds']=(old or {}).get('interval_rounds') or 6
            return {'memo':memos.update(memo_id,**values)}

        tools.update(memo_create=memo_create,memo_list=memo_list,memo_update=memo_update)
    if enabled['window_shadows']:
        from ..compat.window_shadows import WindowShadows
        shadows = WindowShadows(settings.database)
        tools['window_shadow_write'] = shadows.write
    if enabled['resume']:
        from .handoff import factory
        from ..application import Services
        tools['resume'] = factory(Services(settings),settings.extensions.get('handoff',{})).tools['resume']
    return tools

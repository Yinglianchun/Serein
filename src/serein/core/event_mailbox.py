"""Explicitly selected Events and user/AI drafts, separate from recall and review.

Uses the reserved event_mailbox scope in schema-v9 personal_records. No rows are
created by reading, importing Events, recall, or background work. Removed entries
are retained as revisioned tombstones, including their drafts. All mutations run
inside Writer's receipt transaction; a failed promotion leaves the queue intact.
"""
import json
import base64
from .store import Store, Conflict, encode, now, promoted_scene_id

SCOPE = 'event_mailbox'


def row_for(store, event_id):
    return store.conn.execute('SELECT * FROM personal_records WHERE scope=? AND key=?', (SCOPE,event_id)).fetchone()


def item_for(store, event_id, *, detail=False):
    event=store.read(event_id)
    if not event or event['kind']!='event':raise ValueError('Event not found')
    row=row_for(store,event_id)
    value=json.loads(row['payload_json']) if row else {}
    scene=store.read(promoted_scene_id(event_id))
    status=value.get('status','not_selected')
    result={'event_id':event_id,'queue_revision':row['revision'] if row else 0,
        'status':status,'title':event['title'] if event['lifecycle']!='deleted' else 'Deleted Event','event_revision':event['revision'],
        'lifecycle':event['lifecycle'],'processable':status=='pending' and event['lifecycle']=='active' and scene is None,
        'scene_id':scene['id'] if scene else None,'created_at':row['created_at'] if row else None,
        'updated_at':row['updated_at'] if row else None,
        'draft_event_revision':value.get('draft_event_revision',event['revision']),
        'draft_stale':value.get('draft_event_revision',event['revision'])!=event['revision']}
    if detail:
        result['draft']=(value.get('draft',{'title':event['title'],'body_md':event['body_md'],'cues':[]})
                         if event['lifecycle']!='deleted' else {'title':'','body_md':'','cues':[]})
    return result


def save_row(store,event_id,value,revision):
    stamp=now()
    store.conn.execute('INSERT INTO personal_records VALUES (?,?,?,?,?,?,?,?) '
        'ON CONFLICT(scope,key) DO UPDATE SET payload_json=excluded.payload_json,revision=excluded.revision,'
        'deleted=excluded.deleted,updated_at=excluded.updated_at',
        (SCOPE,event_id,event_id,encode(value),revision+1,int(value['status']=='removed'),stamp,stamp))


def current(store,request,*,active=True):
    event=store.read(request['event_id'])
    if not event or event['kind']!='event':raise ValueError('Event not found')
    if type(request.get('expected_revision')) is not int or event['revision']!=request['expected_revision']:
        raise Conflict('Event revision changed; reload before saving')
    row=row_for(store,event['id'])
    revision=row['revision'] if row else 0
    if type(request.get('expected_queue_revision')) is not int or request['expected_queue_revision']!=revision:
        raise Conflict('Mailbox revision changed; reload before saving')
    if active and (event['lifecycle']!='active' or store.read(promoted_scene_id(event['id']))):
        raise Conflict('Only an active, unpromoted Event can be processed')
    return event,row,revision


def mutate(store,request,action):
    event,row,revision=current(store,request,active=action!='remove')
    value=json.loads(row['payload_json']) if row else {'draft':{'title':event['title'],'body_md':event['body_md'],'cues':[]},'draft_event_revision':event['revision']}
    if action=='select':
        if value.get('status')=='completed':raise Conflict('Completed entries cannot be selected again')
        value['status']='pending'
    elif action=='remove':
        if not row:raise Conflict('Event is not in the mailbox')
        if value.get('status')!='pending':raise Conflict('Only pending entries can be removed')
        value['status']='removed'
    elif action=='draft':
        if value.get('status')!='pending':raise Conflict('Select this Event before saving a draft')
        draft=dict(value['draft'])
        for key in ('title','body_md'):
            if key in request:
                if not isinstance(request[key],str) or not request[key].strip():raise ValueError('Draft title and body must be nonempty strings')
                draft[key]=request[key].strip()
        if 'cues' in request:
            from ..compat.scenes import Scenes
            draft['cues']=[] if request['cues']==[] else Scenes._cues(request['cues'])
        if len(encode(draft))>150000:raise ValueError('Draft is too large')
        value['draft']=draft
        value['draft_event_revision']=event['revision']
    else:raise ValueError('Unknown mailbox action')
    save_row(store,event['id'],value,revision)
    return item_for(store,event['id'],detail=True)


def check_promotion(store,request):
    row=row_for(store,request['event_id'])
    if row:
        if json.loads(row['payload_json'])['status']!='pending':raise Conflict('Mailbox entry is not pending; explicitly select it again before promotion')
        current(store,request)
    elif request.get('expected_queue_revision') is not None:
        raise Conflict('Mailbox entry is not pending')
    return row


def complete(store,row,scene_id):
    if row and json.loads(row['payload_json'])['status']=='pending':
        value=json.loads(row['payload_json'])
        value.update(status='completed',scene_id=scene_id)
        save_row(store,row['key'],value,row['revision'])
        return {'queue_revision':row['revision']+1,'queue_status':'completed'}
    return {}


class EventMailbox:
    def __init__(self,database):self.database=database

    def list(self,*,status='pending',limit=20,offset=0,processable_only=False,cursor=None):
        if status not in ('pending','completed','removed','all'):raise ValueError('Invalid mailbox status')
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:raise ValueError('Invalid pagination')
        with Store(self.database,read_only=True) as store:
            store.conn.create_function('promoted_scene_id',1,promoted_scene_id)
            where='p.scope=?';params=[SCOPE]
            if status!='all':where+=" AND json_extract(p.payload_json,'$.status')=?";params.append(status)
            if cursor is not None:
                if offset:raise ValueError('Use cursor or offset, not both')
                try:
                    if not isinstance(cursor,str) or len(cursor)>2000:raise ValueError()
                    position=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                    if not isinstance(position,list) or len(position)!=2 or not all(isinstance(v,str) and v for v in position):raise ValueError()
                except Exception:raise ValueError('Invalid mailbox cursor') from None
                where+=' AND (p.created_at,p.key) > (?,?)';params.extend(position)
            if processable_only:
                where+=" AND json_extract(p.payload_json,'$.status')='pending' AND d.lifecycle='active' AND NOT EXISTS (SELECT 1 FROM documents s WHERE s.id=promoted_scene_id(d.id))"
            rows=store.conn.execute('SELECT p.key,p.created_at FROM personal_records p JOIN documents d ON d.id=p.document_id WHERE '+where+
                ' ORDER BY p.created_at,p.key LIMIT ? OFFSET ?',(*params,limit+1,offset)).fetchall()
            items=[item_for(store,row['key']) for row in rows[:limit]]
            return {'items':items,'has_more':len(rows)>limit,'next_offset':offset+len(items),
                'next_cursor':base64.urlsafe_b64encode(encode([rows[len(items)-1]['created_at'],rows[len(items)-1]['key']]).encode()).decode() if items else None}

    def read(self,event_id):
        from .reader import Reader
        with Reader(self.database) as reader:
            result=item_for(reader.store,event_id,detail=True)
            result['event']=reader.read(event_id,with_evidence=True)
            return result

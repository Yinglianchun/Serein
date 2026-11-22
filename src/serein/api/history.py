"""Read retained Window Shadow and Dream works; never create a handoff."""

import json

from fastapi import APIRouter, HTTPException, Query
from ..core.store import Store


def routes(settings, auth):
    router = APIRouter(dependencies=auth)

    @router.get('/api/window-shadows')
    def shadows(window_id: str='', limit: int=Query(10,ge=1,le=100), include_content: bool=True):
        from ..deployment import feature_enabled
        if not feature_enabled(settings.database,'window_shadows'):
            raise HTTPException(404,'Window shadows are disabled')
        from ..compat.window_shadows import continuity_shadow
        with Store(settings.database,read_only=True) as store, store.transaction():
            windows=[]
            rows=store.conn.execute("SELECT * FROM historical_works WHERE kind='shadow' AND id NOT IN (SELECT document_id FROM deletions) "
                "ORDER BY COALESCE(julianday(json_extract(metadata_json,'$.created_at')),0) DESC,id DESC").fetchall()
            portrait_views={}
            display_content=''
            if rows and include_content and not window_id:
                display_content, portrait_views=continuity_shadow(store,rows[0])
            for row in rows:
                item=json.loads(row['metadata_json'])
                for field,default in (('sections_json',{}),('moment_bucket_ids_json',[])):
                    item['sections' if field=='sections_json' else 'moment_bucket_ids']=json.loads(item.pop(field,None) or json.dumps(default))
                item.update(title=row['title'],content=row['body_md'] if include_content else '',ordinary_recall=False,
                            scene_bucket_ids=list(item['moment_bucket_ids']))
                item['scenes'] = []
                for scene_id in item['scene_bucket_ids']:
                    scene = store.conn.execute("SELECT d.id,r.title FROM documents d JOIN revisions r ON r.document_id=d.id AND r.number=d.revision WHERE d.id=? AND d.kind='scene' AND d.lifecycle='active'", (scene_id,)).fetchone()
                    if scene:
                        item['scenes'].append(dict(scene))
                windows.append(item)
            if windows and include_content and not window_id:
                windows[0]['display_content']=display_content
            if window_id:
                item=next((item for item in windows if item['window_id']==window_id),None)
                if item is None:raise HTTPException(404,'Window Shadow not found')
                root=item.get('revision_root_id') or window_id
                head=next((row for row in windows if (row.get('revision_root_id') or row['window_id'])==root),item)
                return {'status':'ok','mode':'window_shadow','window':item,'revision_head_id':head['window_id'],
                        'is_revision_head':head['window_id']==window_id,'ordinary_recall':False}
            return {'status':'ok','mode':'window_shadow','windows':windows[:limit],
                    'portrait_views':portrait_views,
                    'stats':{'total':len(windows)},'ordinary_recall':False}

    @router.get('/api/dreams')
    def dreams(limit: int=Query(30,ge=1,le=100)):
        with Store(settings.database,read_only=True) as store:
            records={}
            for row in store.conn.execute('SELECT * FROM historical_work_events ORDER BY origin,line_number'):
                meta=json.loads(row['metadata_json']);key=row['work_id']
                if not key:continue
                record=records.setdefault(key,{'dream_id':key,'generated_at':meta.get('generated_at',''),
                    'local_date':meta.get('local_date',''),'ai_name':meta.get('ai_name') or 'AI',
                    'status':'latent','has_body':False})
                if row['event']=='surfaced':record['status']='surfaced'
                elif row['event']=='deleted' and record['status']!='surfaced':record['status']='forgotten'
            for row in store.conn.execute("SELECT * FROM historical_works WHERE kind='dream' AND id NOT IN (SELECT document_id FROM deletions)"):
                meta=json.loads(row['metadata_json'])
                records[row['id']]={key:meta.get(key,'') for key in ('dream_id','generated_at','local_date','ai_name')}
                records[row['id']].update(status='surfaced' if meta.get('surfaced') else 'latent',has_body=True)
            return {'records':sorted(records.values(),key=lambda item:item['generated_at'],reverse=True)[:limit]}

    @router.get('/api/dreams/{dream_id}')
    def dream(dream_id: str):
        with Store(settings.database,read_only=True) as store:
            row=store.conn.execute("SELECT * FROM historical_works WHERE id=? AND kind='dream' AND id NOT IN (SELECT document_id FROM deletions)",(dream_id,)).fetchone()
            if row is None:raise HTTPException(404,'Dream body unavailable')
            meta=json.loads(row['metadata_json'])
            return {**{key:meta.get(key,'') for key in ('dream_id','generated_at','local_date','ai_name')},
                    'status':'surfaced' if meta.get('surfaced') else 'latent','body':row['body_md']}

    return router

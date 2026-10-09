import {useEffect,useRef,useState} from 'react';
import {EnvelopeSimple} from '@phosphor-icons/react';
import {instanceSettings} from '../storage/instanceStore.js';
import {mailboxRequest,mailboxWrite} from '../utils/eventMailbox.js';

export function EventMailboxSelect({eventId,disabled=false}) {
  const [enabled,setEnabled]=useState(false),[item,setItem]=useState(null),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  const lock=useRef(false),generation=useRef(0);
  useEffect(()=>{
    const current=++generation.current;setItem(null);setMessage('');setEnabled(false);
    const features=event=>{if(current===generation.current)setEnabled(!!event.detail?.event_to_scene);};
    window.addEventListener('serein:features',features);
    instanceSettings().then(config=>{if(current===generation.current)setEnabled(!!config.features.event_to_scene);}).catch(()=>{});
    return()=>{generation.current++;window.removeEventListener('serein:features',features);};
  },[eventId]);
  useEffect(()=>{
    if(!enabled)return;
    let active=true;
    mailboxRequest('/'+encodeURIComponent(eventId)).then(value=>{if(active)setItem(value);}).catch(()=>{});
    return()=>{active=false;};
  },[enabled,eventId]);
  async function select() {
    if(lock.current||!enabled)return;lock.current=true;setBusy(true);setMessage('');const current=generation.current;
    try {
      const live=await mailboxRequest('/'+encodeURIComponent(eventId));
      const selected=live.status==='pending'||live.status==='completed'?live:await mailboxRequest('/'+encodeURIComponent(live.event_id),mailboxWrite(live,'select'));
      if(current===generation.current){setItem(selected);setMessage(selected.status==='completed'?'这条 Event 已升为 Scene。':'已加入信箱，可以在那里编辑草稿与召回入口。');}
    } catch(error){if(current===generation.current)setMessage(error.message);}
    finally{lock.current=false;if(current===generation.current)setBusy(false);}
  }
  if(!enabled)return null;
  return <div className="event-mailbox-select">
    {item?.status==='removed'?<button type="button" disabled={disabled||busy} onClick={select}><EnvelopeSimple size={15} aria-hidden="true"/>{busy?'正在恢复…':'重新放入信箱'}</button>:<a href="#mailbox"><EnvelopeSimple size={15} aria-hidden="true"/>打开信箱</a>}
    {message&&<p role="status">{message} {item&&<a href="#mailbox">打开信箱</a>}</p>}
  </div>;
}

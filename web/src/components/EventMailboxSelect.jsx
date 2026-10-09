import {useEffect,useState} from 'react';
import {CheckSquare} from '@phosphor-icons/react';
import {instanceSettings} from '../storage/instanceStore.js';
export function EventMailboxSelect(){
  const [enabled,setEnabled]=useState(false);
  useEffect(()=>{let active=true;const features=event=>{if(active)setEnabled(!!event.detail?.event_to_scene);};window.addEventListener('serein:features',features);instanceSettings().then(config=>{if(active)setEnabled(!!config.features.event_to_scene);}).catch(()=>{});return()=>{active=false;window.removeEventListener('serein:features',features);};},[]);
  return enabled?<div className="event-mailbox-select"><a href="#mailbox"><CheckSquare size={15} aria-hidden="true"/>打开沉淀</a></div>:null;
}

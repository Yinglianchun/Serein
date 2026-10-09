import {fixture} from './event-mailbox-fixture.js';
import React from 'react';
import {createRoot} from 'react-dom/client';
import {EventMailboxPage} from '../src/pages/EventMailboxPage.jsx';
import {EventMailboxSelect} from '../src/components/EventMailboxSelect.jsx';
import {Sidebar} from '../src/components/Sidebar.jsx';
import '../src/styles.css';
createRoot(document.getElementById('root')).render(<main className="app-shell app-shell--mailbox"><section className="mailbox-page"><div style={{padding:'24px 24px 0 118px'}}><p>仅合成数据 · 不连接后端</p><div style={{display:'flex',gap:16,flexWrap:'wrap'}}><label><input type="checkbox" defaultChecked onChange={event=>{fixture.enabled=event.target.checked;}}/>开启功能（点刷新生效）</label><label><input type="checkbox" onChange={event=>{fixture.failPromotion=event.target.checked;}}/>模拟晋升失败</label><label><input type="checkbox" onChange={event=>{fixture.conflictDraft=event.target.checked;}}/>模拟版本冲突</label></div><EventMailboxSelect eventId="event_synthetic"/></div><EventMailboxPage onOpenMemory={()=>{}} onOpenSettings={()=>{}}/><Sidebar activeArea="信箱" onNavigate={()=>{}} onOpenSettings={()=>{}}/></section></main>);

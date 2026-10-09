import {fixture} from './event-mailbox-fixture.js';
import React from 'react';
import {createRoot} from 'react-dom/client';
import {EventMailboxPage} from '../src/pages/EventMailboxPage.jsx';
import {Sidebar} from '../src/components/Sidebar.jsx';
import '../src/styles.css';
// All requests terminate in the synthetic fixture; no deployment is reachable.
void fixture;
createRoot(document.getElementById('root')).render(<main className="app-shell app-shell--mailbox"><section className="mailbox-page"><EventMailboxPage onOpenSettings={()=>{}}/><Sidebar activeArea="信箱" onNavigate={()=>{}} onOpenSettings={()=>{}}/></section></main>);

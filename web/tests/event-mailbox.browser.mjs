// Synthetic-only browser acceptance. Never contacts a deployment.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const base='http://127.0.0.1:5193';
const server=spawn(process.execPath,['node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','5193','--strictPort'],{stdio:['ignore','pipe','pipe']});
let output='',browser;server.stdout.on('data',chunk=>output+=chunk);server.stderr.on('data',chunk=>output+=chunk);
try{
 for(let attempt=0;;attempt++){try{await fetch(base);break;}catch{if(attempt>100||server.exitCode!=null)throw new Error(output);await new Promise(resolve=>setTimeout(resolve,100));}}
 browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{})});
 const page=await browser.newPage({viewport:{width:1200,height:850}}),errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto(base+'/tests/event-mailbox-preview.html');
 await page.locator('.mailbox-card-heading').first().waitFor();assert.equal(await page.locator('textarea').count(),0);assert.equal(await page.locator('.mailbox-card-content').count(),0);
 await page.getByRole('button',{name:/event_synthetic/}).click();await page.locator('.mailbox-quote').waitFor();
 await page.getByRole('button',{name:/查看原文/}).click();await page.locator('.mailbox-evidence-list').waitFor();assert.match(await page.locator('.mailbox-evidence-list').textContent(),/Exact original evidence/);
 await page.getByRole('button',{name:'选入',exact:true}).click();await page.getByText('1 件已选入，等待 AI 书写。').waitFor();assert.equal(await page.evaluate(()=>mailboxFixture.promoteCalls),0);
 await page.getByRole('button',{name:/^已选/}).click();await page.getByRole('button',{name:/event_synthetic/}).click();await page.getByRole('button',{name:'重新选择',exact:true}).click();await page.getByText('1 件已放回待选。').waitFor();
 await page.getByRole('button',{name:/^待选/}).click();await page.getByRole('button',{name:'继续读取',exact:true}).click();await page.getByRole('button',{name:'继续读取',exact:true}).click();
 await page.getByRole('button',{name:'批量操作',exact:true}).click();await page.getByLabel('全选已加载候选').check();await page.getByRole('button',{name:'批量跳过',exact:true}).click();await page.getByText('3 件已跳过，Event 仍保留。').waitFor();assert.equal(await page.evaluate(()=>mailboxFixture.promoteCalls),0);
 await page.getByRole('button',{name:/^已跳过/}).click();await page.getByRole('button',{name:/event_synthetic/}).click();await page.getByRole('button',{name:'重新选择',exact:true}).click();await page.getByText('1 件已放回待选。').waitFor();
 await page.getByRole('button',{name:/^待选/}).click();await page.getByRole('button',{name:/event_synthetic/}).click();await page.locator('.mailbox-quote').waitFor();
 await page.evaluate(()=>{
  const seed=mailboxFixture.rows.get('event_synthetic');
  seed.event.document.body_md=Array(12).fill('这是一段用于验证阅读滚动的合成 Event 正文，原文和内容都不会来自真实实例。').join('\n\n');
  for(let index=0;index<8;index++){const item=structuredClone(seed);item.event_id='event_scroll_'+index;item.title='合成候选 '+(index+1);item.status='pending';mailboxFixture.rows.set(item.event_id,item);}
 });
 await page.getByRole('button',{name:'刷新沉淀'}).click();
 for(let index=0;index<8;index++)await page.getByRole('button',{name:'继续读取',exact:true}).click();
 // Collapse and reopen to read the updated synthetic body.
 await page.getByRole('button',{name:'收起',exact:true}).click();await page.getByRole('button',{name:/event_synthetic/}).click();await page.locator('.mailbox-original').waitFor();
 const scroll=await page.evaluate(()=>{
  const list=document.querySelector('.mailbox-candidates'),reader=document.querySelector('.mailbox-detail-panel');
  const before=list.getBoundingClientRect();list.scrollTop=180;reader.scrollTop=180;
  return {list:list.scrollTop,reader:reader.scrollTop,outer:window.scrollY,top:before.top,after:list.getBoundingClientRect().top,pageHeight:document.querySelector('.mailbox-layout').getBoundingClientRect().height,viewport:innerHeight};
 });
 assert(scroll.list>0&&scroll.reader>0,'Both panes scroll independently');assert.equal(scroll.outer,0);assert.equal(scroll.top,scroll.after);assert.equal(scroll.pageHeight,scroll.viewport);
 await page.evaluate(()=>{document.querySelector('.mailbox-candidates').scrollTop=0;document.querySelector('.mailbox-detail-panel').scrollTop=0;});
 await mkdir('../tmp/mailbox-review',{recursive:true});
 await page.screenshot({path:'../tmp/mailbox-review/desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:'../tmp/mailbox-review/mobile.png',fullPage:true});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'No mobile horizontal overflow');
 await page.evaluate(()=>{mailboxFixture.enabled=false;});const before=await page.evaluate(()=>mailboxFixture.mutations);await page.getByRole('button',{name:'刷新沉淀'}).click();await page.getByText('“Event 升为 Scene”尚未开启。已有决定会保留。').waitFor();assert(await page.getByRole('button',{name:'批量操作',exact:true}).isDisabled());assert.equal(await page.evaluate(()=>mailboxFixture.mutations),before);
 assert.deepEqual(errors,[]);console.log('PASS browser: collapsed/expanded cards, original evidence, decisions only, retained/removed tabs, batch, restore, disabled feature, mobile layout');
}finally{await browser?.close();server.kill('SIGTERM');}

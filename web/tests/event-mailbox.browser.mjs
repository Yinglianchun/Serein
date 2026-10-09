// Synthetic-only acceptance. Optional PLAYWRIGHT_MODULE / CHROMIUM_EXECUTABLE
// reuse official local installations. This never contacts a real backend.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const base='http://127.0.0.1:5193';
const server=spawn(process.execPath,['node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','5193','--strictPort'],{stdio:['ignore','pipe','pipe']});
let output='',browser;server.stdout.on('data',chunk=>output+=chunk);server.stderr.on('data',chunk=>output+=chunk);
try {
 for(let attempt=0;;attempt++){try{await fetch(base);break;}catch{if(attempt>100||server.exitCode!=null)throw new Error(output);await new Promise(resolve=>setTimeout(resolve,100));}}
 browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{})});
 const page=await browser.newPage({viewport:{width:1200,height:850}}),errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto(base+'/tests/event-mailbox-preview.html');
 const select=page.getByRole('button',{name:'加入信箱',exact:true});await select.waitFor();await select.evaluate(button=>{button.click();button.click();});
 await page.getByText('已加入信箱，可以在那里编辑草稿与召回入口。').waitFor();assert.equal(await page.evaluate(()=>mailboxFixture.selectCalls),1);
 await page.getByRole('button',{name:'刷新信箱'}).click();await page.getByRole('button',{name:'继续读取'}).click();await page.getByRole('button',{name:'继续读取'}).click();
 const detail=page.getByRole('region',{name:'信箱详情'});
 await page.getByRole('button',{name:/event_synthetic/}).click();await detail.getByText('Exact original evidence, never rewritten.').waitFor();assert.match(await detail.textContent(),/Last paragraph/);
 await detail.getByLabel('标题',{exact:true}).fill('Edited title');await detail.getByLabel('正文',{exact:true}).fill('Edited Scene body');
 await detail.getByRole('button',{name:'增加一条'}).click();await detail.getByLabel('召回入口 1',{exact:true}).fill('When we talk about rain');
 page.once('dialog',dialog=>dialog.dismiss());await page.getByRole('button',{name:/event_second/}).click();assert.equal(await detail.getByLabel('标题',{exact:true}).inputValue(),'Edited title');
 await detail.getByRole('button',{name:'保存草稿',exact:true}).click();await detail.getByText('草稿已保存。').waitFor();assert.deepEqual(await page.evaluate(()=>mailboxFixture.rows.get('event_synthetic').draft.cues),['When we talk about rain']);
 await page.evaluate(()=>{mailboxFixture.failPromotion=true;});await detail.getByRole('button',{name:'升为 Scene',exact:true}).click();await detail.getByRole('alert').filter({hasText:'Synthetic promotion failed'}).waitFor();assert.equal(await page.evaluate(()=>mailboxFixture.rows.get('event_synthetic').status),'pending');assert.equal(await detail.getByLabel('标题',{exact:true}).inputValue(),'Edited title');
 await page.evaluate(()=>{mailboxFixture.failPromotion=false;});await detail.getByRole('button',{name:'升为 Scene',exact:true}).evaluate(button=>{button.click();button.click();});await detail.getByText('已升为 Scene，并从待处理信箱移出。').waitFor();assert.equal(await page.evaluate(()=>mailboxFixture.promoteCalls),2);
 await detail.getByRole('button',{name:'关闭信箱详情'}).click();await page.getByLabel('信箱状态').selectOption('completed');await page.getByRole('button',{name:/event_synthetic/}).click();await detail.getByText('关联 Scene：scene_synthetic').waitFor();assert.equal(await detail.getByRole('button',{name:'升为 Scene',exact:true}).count(),0);
 await detail.getByRole('button',{name:'关闭信箱详情'}).click();await page.getByLabel('信箱状态').selectOption('pending');await page.getByRole('button',{name:/event_second/}).click();await detail.getByLabel('标题',{exact:true}).fill('Unsaved retained');
 await page.evaluate(()=>{mailboxFixture.conflictDraft=true;});await detail.getByRole('button',{name:'保存草稿',exact:true}).click();await detail.getByRole('alert').filter({hasText:'已有新版本'}).waitFor();assert.equal(await detail.getByLabel('标题',{exact:true}).inputValue(),'Unsaved retained');assert(await detail.getByRole('button',{name:'升为 Scene',exact:true}).isDisabled());
 await page.evaluate(()=>{mailboxFixture.conflictDraft=false;});page.once('dialog',dialog=>dialog.accept());await detail.getByRole('button',{name:'重新读取',exact:true}).click();await detail.getByRole('button',{name:'移出信箱',exact:true}).click();await detail.getByText('已移出信箱，Event 与已保存草稿仍保留。').waitFor();assert.equal(await page.evaluate(()=>mailboxFixture.rows.get('event_second').event.document.body_md),'Full original Event\n\nLast paragraph');
 await detail.getByRole('button',{name:'关闭信箱详情'}).click();await page.evaluate(()=>{mailboxFixture.rows.get('event_third').draft_stale=true;});await page.getByRole('button',{name:/event_third/}).click();await detail.getByText(/草稿仍基于旧版本/).waitFor();assert(await detail.getByRole('button',{name:'升为 Scene',exact:true}).isDisabled());await detail.getByRole('button',{name:'保存草稿',exact:true}).click();await detail.getByText('草稿已保存。').waitFor();
 await page.evaluate(()=>{mailboxFixture.enabled=false;});const before=await page.evaluate(()=>mailboxFixture.mutations);await page.getByRole('button',{name:'刷新信箱'}).click();await page.getByText('“Event 升为 Scene”尚未开启。已有信箱和草稿会保留。').waitFor();
 await page.getByRole('button',{name:/event_third/}).click();assert(await detail.getByRole('button',{name:'升为 Scene',exact:true}).isDisabled());assert(await detail.getByRole('button',{name:'移出信箱',exact:true}).isDisabled());assert.equal(await page.evaluate(()=>mailboxFixture.mutations),before);
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:'/tmp/event-mailbox-mobile.png',fullPage:true});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'mobile has no horizontal overflow');
 assert.deepEqual(errors,[]);console.log('PASS mailbox synthetic browser: explicit selection, pagination, full evidence, cues/draft, dirty guard, failure preservation, stale revision, promotion, removal, disabled opt-in, mobile');
}finally{await browser?.close();server.kill('SIGTERM');}

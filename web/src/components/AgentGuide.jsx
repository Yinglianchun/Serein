import {useRef,useState} from 'react';

const config={mcpServers:{serein_events:{command:'python',args:['/absolute/path/serein/scripts/event_agent_mcp.py'],
  env:{SEREIN_AGENT_URL:'http://127.0.0.1:8011',SEREIN_AGENT_TOKEN:'填写你的 Serein 访问密钥'}}}};
const runner={SEREIN_WRITER_ENABLED:'1',SEREIN_WRITER_MODEL:'你的 agent 使用的模型名',
  SEREIN_WRITER_COMMAND:'["python", "/absolute/path/your-agent-runner.py"]'};

export function AgentGuide({initial='event',label='如何接入 Agent'}) {
  const dialog=useRef(null),[tab,setTab]=useState(initial),[status,setStatus]=useState('');
  function download(){
    const payload=tab==='event'?config:runner;
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=tab==='event'?'serein-agent-mcp.example.json':'serein-writer-agent-env.example.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    setStatus('配置示例已下载。请替换路径、地址和占位值后使用。');
  }
  return <>
    <button type="button" className="agent-guide-trigger settings-link" onClick={()=>{setTab(initial);setStatus('');dialog.current.showModal();dialog.current.scrollTop=0;}}>{label}</button>
    <dialog ref={dialog} className="agent-guide" aria-labelledby={'agent-guide-'+initial} onClick={event=>{if(event.target===dialog.current){const rect=dialog.current.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.current.close();}}}>
      <header><h3 id={'agent-guide-'+initial}>用自己的 Agent 完成写作</h3><button type="button" aria-label="关闭 Agent 配置说明" onClick={()=>dialog.current.close()}>×</button></header>
      <div className="agent-guide-tabs" role="group" aria-label="Agent 用途">
        <button type="button" aria-pressed={tab==='event'} onClick={()=>{setTab('event');setStatus('');}}>Event 流水线</button>
        <button type="button" aria-pressed={tab==='writer'} onClick={()=>{setTab('writer');setStatus('');}}>叙事卷 Writer</button>
      </div>
      {tab==='event'?<>
        <p>模型留空表示这一步等待你的 Agent。需要完成一次接入配置，之后 Agent 可以领取任务、提交结果并继续下一步。</p>
        <ol>
          <li>保持 Serein HTTP 后端运行。在发行包源码目录、使用同一 Python 环境安装 MCP 支持：<code>python -m pip install ".[mcp]"</code>。</li>
          <li>把下面配置加入 Agent 客户端的 MCP 设置。路径指向发行包里的 <code>scripts/event_agent_mcp.py</code>，地址填后端地址，密钥填 Serein 访问密钥。</li>
          <li>重新连接客户端的 MCP。让 Agent 调用 <code>pipeline_next</code>，完整读取返回的 <code>request.prompt</code> 与角色规则；完成后用 <code>pipeline_submit(job_id, output)</code> 提交，再领取下一步。</li>
          <li>手动立即整理时传 <code>include_recent: true</code>；未完成的对话仍会等待。Agent 客户端需支持 MCP 工具调用；留空不会自行启动一个 Agent。</li>
        </ol>
        <pre>{JSON.stringify(config,null,2)}</pre>
        <p>这个接入脚本只转发到现有后端，不另开后台任务。三阶段为归线 → 切分与转录 → Event 写作；每一步的 AGENTS.md 和冻结原文随任务提供。也可以沿用下方下载任务、粘贴 JSON 的手动方式。</p>
      </>:<>
        <p>叙事卷 Writer 接收绑定材料，生成待确认的正文预览。它使用独立的 Agent runner，与 Event 流水线分别配置。</p>
        <ol>
          <li>准备能以非交互方式运行的 Agent runner：从标准输入读取一份 JSON，读取其中的 <code>prompt</code>、<code>materials</code> 和 <code>output_schema</code>，标准输出只返回符合 schema 的 JSON；日志写入标准错误。叙事卷 Writer 只读文字，不加载原消息图片。</li>
          <li>在启动前端 Node 服务的环境中设置下方三个变量，替换模型名和 runner 的绝对路径，然后重启前端 Node 服务使环境变量生效。</li>
          <li>设置页的“启用 API Writer”保持关闭，避免优先使用模型 API。在叙事卷里绑定材料，再选择更新或重写，现有预览入口就会调用这个 runner。</li>
          <li>更新使用现有正文与新增材料；重写使用全部绑定材料。查看预览后明确保存，才会写入叙事卷。</li>
        </ol>
        <pre>{JSON.stringify(runner,null,2)}</pre>
        <p>Writer 的完整角色规则和输出 schema 位于 <code>web/codex_agents/narrative_writer/</code>，每次请求也会附带。示例中的 runner 路径需要换成你实际配置的 Agent 适配脚本。</p>
      </>}
      <div className="settings-actions"><button type="button" onClick={download}>下载配置示例</button><button type="button" onClick={()=>dialog.current.close()}>知道了</button></div>
      <p role="status">{status}</p>
    </dialog>
  </>;
}

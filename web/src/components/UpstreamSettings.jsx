import {ModelDiscovery} from "./ModelDiscovery.jsx";
import {useState} from "react";
import {createUuid} from "../utils/createUuid.js";

export function UpstreamSettings({upstreams,modelIds=[],assignments={},onChange,onImported,busy,setBusy,setStatus}) {
  const [template,setTemplate]=useState("");
  const edit=(id,patch)=>onChange(upstreams.map(item=>item.id===id?{...item,...patch}:item));
  const editModel=(upstream,index,patch)=>edit(upstream.id,{models:upstream.models.map((model,i)=>i===index?{...model,...patch}:model)});
  async function importTemplate() {
    setBusy(true);setStatus("");
    try {
      const response=await fetch("/__serein/settings/upstreams-template",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({template})});
      if(!response.ok)throw new Error("上游未导入，请检查模板、重复模型名，以及仍被功能使用的模型。");
      onImported(await response.json());setTemplate("");setStatus("上游模板已保存，聊天模型列表已更新。");
    } catch(error){setStatus(error.message);}
    finally{setBusy(false);}
  }
  return <>
    <p className="model-connection-help">每个上游配置一次地址和密钥。客户端显示“上游名 / 模型别名”，别名留空使用模型号；已选为向量或重排的模型不列入聊天选项。</p>
    <p className="model-connection-help">整体保存会检查所有上游：名称、地址，以及每个模型号都需要填完整。无需先拉取模型，可以手动填写；不用的空白模型行请先移除。上游暂时离线也能保存，保存配置不会调用模型。</p>
    <div className="configured-models">{upstreams.map(upstream=><details className="configured-model" key={upstream.id}>
      <summary>{upstream.name || "新上游"}<small>{upstream.models.length} 个模型</small></summary>
      <label className="settings-field"><span>上游名称</span><input required value={upstream.name} onChange={event=>edit(upstream.id,{name:event.target.value})} /></label>
      <label className="settings-field"><span>API Base URL</span><input required type="url" placeholder="https://api.example.com/v1" value={upstream.base_url} onChange={event=>edit(upstream.id,{base_url:event.target.value})} /></label>
      <label className="settings-field"><span>API Key</span><input type="password" autoComplete="new-password" value={upstream.api_key || ""}
        data-upstream-api-key data-upstream-id={upstream.id}
        placeholder={upstream.api_key_configured?"已配置；留空保留":"本地无认证上游可留空"}
        onChange={event=>edit(upstream.id,{api_key:event.target.value,...(event.target.value?{clear_key:false}:{})})} /></label>
      {upstream.api_key_configured && <label className="settings-toggle"><span>删除已保存的密钥</span><input type="checkbox" role="switch" checked={upstream.clear_key || false} onChange={event=>edit(upstream.id,{clear_key:event.target.checked})} /></label>}
      <label className="settings-field"><span>接口格式</span><select value={upstream.protocol} onChange={event=>edit(upstream.id,{protocol:event.target.value,prompt_cache:"",prompt_cache_retention:""})}>
        <option value="openai">Chat Completions / Embeddings / Rerank</option><option value="anthropic">Anthropic Messages</option></select></label>
      <details><summary>缓存与连接选项</summary>
        <label className="settings-field"><span>密钥环境变量（可选）</span><input value={upstream.api_key_env || ""} onChange={event=>edit(upstream.id,{api_key_env:event.target.value})} /></label>
        <label className="settings-field"><span>提示缓存</span><select value={upstream.prompt_cache || ""} onChange={event=>edit(upstream.id,{prompt_cache:event.target.value,prompt_cache_retention:""})}>
          <option value="">跟随服务商默认值</option>{upstream.protocol==="openai" && <option value="openai">Prompt cache key</option>}<option value="anthropic">Anthropic 自动缓存</option>{upstream.protocol==="anthropic" && <option value="anthropic-explicit">Anthropic 明确断点</option>}</select></label>
        <label className="settings-field"><span>缓存时长</span><select value={upstream.prompt_cache_retention || ""} onChange={event=>edit(upstream.id,{prompt_cache_retention:event.target.value})}>
          <option value="">默认</option>{upstream.prompt_cache==="openai"?<><option value="in-memory">in-memory</option><option value="24h">24 小时</option></>:upstream.prompt_cache?<><option value="5m">5 分钟</option><option value="1h">1 小时</option></>:null}</select></label>
        <label className="settings-field"><span>默认模型名（仅单上游时使用）</span><input value={upstream.default_model || ""} onChange={event=>edit(upstream.id,{default_model:event.target.value})} /></label>
      </details>
      <ModelDiscovery upstream={upstream} onAdd={names=>{
        const models=upstream.models.filter(m=>m.id || m.upstream_model);
        const ids=new Set(modelIds);
        for(const name of names.filter(name=>!models.some(m=>m.upstream_model.trim()===name))){
          const id=ids.has(name)?createUuid():name;ids.add(id);
          models.push({id,upstream_model:name,label:''});
        }
        edit(upstream.id,{models});
      }}/>
      <div className="upstream-models">{upstream.models.map((model,index)=><div className="upstream-model" key={index}>
        <label className="settings-field"><span>上游模型号</span><input required value={model.upstream_model} onChange={event=>editModel(upstream,index,{upstream_model:event.target.value,
          ...(!model.id?{id:createUuid()}:{})})} /></label>
        <label className="settings-field"><span>模型别名（可选）</span><input value={model.label || ""} placeholder="留空使用上游模型号" onChange={event=>editModel(upstream,index,{label:event.target.value})} /></label>
        <label className="settings-field"><span>工具历史推理字段兼容</span><select disabled={(upstream.protocol || "openai")!=="openai"} value={model.reasoning_content_compat || "auto"} onChange={event=>editModel(upstream,index,{reasoning_content_compat:event.target.value})}>
          <option value="auto">自动（识别 DeepSeek）</option><option value="on">开启（此模型要求 reasoning_content）</option><option value="off">关闭补空</option></select></label>
        <p className="model-connection-help">仅用于 Chat Completions：优先恢复已缓存原文；工具请求或历史含工具调用时，为缺失字段补空。中转隐藏模型名时可手动开启。这不是思考开关，也不能恢复已丢失的推理；关闭只停止补空。请在工具轮次结束后调整，修改设置会切换缓存命名空间。</p>
        {[assignments.embedding,assignments.reranker].includes(model.id)&&<p className="model-connection-help">用于{assignments.embedding===model.id?'向量':'重排'}，不出现在聊天模型列表中。</p>}
        <details><summary>向量选项</summary>
          <label className="settings-field"><span>向量维度（可留空自动读取）</span><input type="number" min="1" max="65536" value={model.dimension || ""} onChange={event=>editModel(upstream,index,{dimension:Number(event.target.value) || null})} /></label>
          <label className="settings-field"><span>Embedding 查询指令</span><input value={model.query_instruction || ""} onChange={event=>editModel(upstream,index,{query_instruction:event.target.value})} /></label>
          <label className="settings-field"><span>Embedding 文档指令</span><input value={model.document_instruction || ""} onChange={event=>editModel(upstream,index,{document_instruction:event.target.value})} /></label>
        </details>
        <button type="button" onClick={()=>edit(upstream.id,{models:upstream.models.filter((_,i)=>i!==index)})}>移除此模型</button>
      </div>)}</div>
      <div className="settings-actions"><button type="button" onClick={()=>edit(upstream.id,{models:[...upstream.models,{id:"",upstream_model:""}]})}>添加模型</button>
        <button type="button" onClick={()=>onChange(upstreams.filter(item=>item.id!==upstream.id))}>移除上游</button></div>
    </details>)}</div>
    <div className="settings-actions"><button type="button" onClick={()=>onChange([...upstreams,{id:createUuid(),name:"",base_url:"",protocol:"openai",models:[{id:"",upstream_model:""}]}])}>添加上游</button></div>
    <details className="upstream-template"><summary>从配置模板导入</summary>
      <p className="model-connection-help">粘贴 gateway.upstreams 模板，导入会替换已保存的上游列表。可以用 api_key_env 指定服务端环境变量。</p>
      <label className="settings-field"><span>上游 YAML / JSON 配置</span><textarea rows={12} value={template} onChange={event=>setTemplate(event.target.value)} spellCheck={false} /></label>
      <button type="button" disabled={busy || !template.trim()} onClick={importTemplate}>导入并保存上游</button>
    </details>
  </>;
}

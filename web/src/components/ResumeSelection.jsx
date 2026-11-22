import {ResumeMemoryPicker} from './ResumeMemoryPicker.jsx';

export const countFields = {
  recent_events:'recent_event_limit',
  recent_scenes:'recent_scene_limit',
  recent_originals:'recent_original_limit',
};

export function ResumeSelection({selection,onChange,disabled=false,windowShadows=false}) {
  function change(key,value) {
    onChange({...selection,[key]:value,
      ...(value&&key==='recent_originals'?{pending_originals:false}:value&&key==='pending_originals'?{recent_originals:false}:{})});
  }
  return <fieldset className="resume-selection"><legend>每次开窗读取</legend>
    <p>只读最新一份窗影；事件和 Scene 附记忆 ID，可用 read_memory 继续阅读绑定的原文。收藏续接只读取 Scene，也可以单独选择事件。“最近原话”和“尚未整理的原话”只能开启一个。</p>
    {Object.entries({latest_shadow:'最新窗影',recent_events:'最近事件',recent_scenes:'最近 Scene',favorite_scenes:'舍不得丢的 Scene',selected_memories:'自选事件 / Scene',recent_originals:'最近原话',pending_originals:'尚未整理的原话'}).map(([key,label])=>
      <div key={key} className={key==='selected_memories'||countFields[key]?'resume-custom-choice':undefined}>
        <label><input type="checkbox" checked={!!selection[key]} disabled={disabled} onChange={event=>change(key,event.target.checked)}/><span>{label}</span></label>
        {key==='selected_memories'&&<ResumeMemoryPicker ids={selection.selected_ids||[]} disabled={disabled} onChange={ids=>onChange({...selection,selected_ids:ids,selected_memories:ids.length>0})}/>}
        {countFields[key]&&<label className="resume-original-count"><span>带入</span><input type="number" min="1" max="50" required aria-label={label+'带入条数'} disabled={disabled||!selection[key]} value={selection[countFields[key]]} onChange={event=>change(countFields[key],event.target.value===''?'':Number(event.target.value))}/><span>条</span></label>}
      </div>)}
    {!windowShadows&&selection.latest_shadow&&<small>读取窗影还需在功能设置开启“窗影”。</small>}
  </fieldset>;
}

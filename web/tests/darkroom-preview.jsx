import React from 'react';
import {createRoot} from 'react-dom/client';
import '@fontsource/noto-serif-sc/400.css';
import '@fontsource/source-serif-4/400.css';
import '../src/styles.css';
import {DiaryPage} from '../src/pages/DiaryPage.jsx';

const titles=['未寄出的夏夜清单','第五百天，门还在这边','给未来的你'];
const entries=titles.map((title,index)=>({id:index+1,title,date:`2026-10-0${9-index}`,author:index%2?'user':'ai',
  created_at:`2026-10-0${9-index}T20:30:00+08:00`,entry_type:'darkroom',locked:index===2,
  unlock_at:index===2?'2028-09-16T09:00:00+08:00':'2026-09-01T09:00:00+08:00',
  content:index===2?'':'今晚，想把还没说出口的话留在这里。\n\n等你读到的时候，夏夜或许已经很远。\n\n但这份心意，会一直在。'}));
window.localStorage.removeItem('serein.diary.entries.v1');
window.fetch=async url=>{
  if(url!=='/__serein/live/diaries')throw new Error('Synthetic preview refuses backend requests');
  return {ok:true,json:async()=>({status:'ok',snapshotId:'synthetic-darkroom',entries})};
};
createRoot(document.getElementById('root')).render(<DiaryPage/>);

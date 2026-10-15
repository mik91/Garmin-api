'use strict';
let dashboard=null, dashboardRequest=0, activePage='overview', calendarMonth=new Date(), calendarDay=null, chatMessages=[], chatBusy=false, pendingActivity=null;
const pages=['overview','discoveries','trends','training','calendar','activities','ai','connections'];
const titles={overview:'Your training at a glance.',discoveries:'Find your patterns.',trends:'See what is changing.',training:'Build on your training.',calendar:'Your training calendar.',activities:'Every session, in detail.',ai:'Put your history in context.',connections:'Connect your accounts.'};
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sum=(list,key)=>{const v=list.map(r=>r[key]).filter(valid);return v.length?v.reduce((a,b)=>a+b,0):null;};
const mean=(list,key)=>{const v=list.map(r=>r[key]).filter(valid);return v.length?v.reduce((a,b)=>a+b,0)/v.length:null;};
const day=value=>String(value).slice(0,10);
const localDay=value=>`${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,'0')}-${String(value.getDate()).padStart(2,'0')}`;
const colorFor={Running:green,Swimming:blue,Cycling:'#fbbf24',Other:'#c4a5ff'};
const typeFilter={training:null,activities:null};
const typeLabel=t=>String(t||'other').replace(/_/g,' ').replace(/^./,c=>c.toUpperCase());
const acts=()=>$('show-other').checked?dashboard.activities:dashboard.activities.filter(r=>r.category!=='Other');
function chips(id,all,key,rerender){
  const el=$(id),sel=typeFilter[key];
  el.innerHTML=all.map(t=>`<label class="chip"><input type="checkbox" value="${esc(t)}" ${!sel||sel.has(t)?'checked':''}>${esc(t)}</label>`).join('');
  el.querySelectorAll('input').forEach(i=>i.onchange=()=>{const now=new Set([...el.querySelectorAll('input:checked')].map(x=>x.value));typeFilter[key]=now.size===all.length?null:now;rerender();});
  return r=>!typeFilter[key]||typeFilter[key].has(typeLabel(r.type));
}
function periodAvg(list,key,scale=1){const g=new Map();list.forEach(r=>{if(!valid(r[key]))return;const k=periodKey(r.date);if(!g.has(k))g.set(k,[]);g.get(k).push(r[key]/scale);});return [...g].sort((a,b)=>a[0].localeCompare(b[0])).map(([k,v])=>[k,v.reduce((a,b)=>a+b,0)/v.length]);}
function insightCards(list){return list.map(i=>`<article><span class="eyebrow">${esc(i.category)} · ${esc(i.confidence)} confidence</span><h3>${esc(i.title)}</h3><p>${esc(i.detail)}</p><p class="muted">${esc(i.action||'')}</p></article>`).join('');}
function latest(list,key){for(let i=list.length-1;i>=0;i--)if(list[i][key]!==null&&list[i][key]!==undefined&&list[i][key]!=='')return list[i][key];return null;}
let scrollToDetail=false;
const zoneNamesLong=['Z1 Easy','Z2 Aerobic','Z3 Tempo','Z4 Threshold','Z5 Max'];
function zoneBar(zones){
  const z=(zones||[]).map(v=>valid(v)&&v>0?v:0),total=z.reduce((a,b)=>a+b,0);
  if(!total)return '<span class="zbar empty"></span>';
  return '<span class="zbar">'+z.slice(0,5).map((v,i)=>`<b style="width:${v/total*100}%" title="${zoneNamesLong[i]} ${Math.round(v/total*100)}%"></b>`).join('')+'</span>';
}
function linkTiles(id,map){document.querySelectorAll('#'+id+' .metric-card').forEach(card=>{const target=map[card.querySelector('span').textContent];if(!target)return;card.dataset.go=target;card.classList.add('link');card.tabIndex=0;card.setAttribute('role','link');});}
function go(target){
  const [page,arg]=String(target).split(':');
  if(page==='act'){openActivity(arg);return;}
  if(page==='trends'&&arg){$('trend-metric').value=arg;}
  if(page==='activities'&&arg){$('activity-sport').value=arg;typeFilter.activities=null;}
  if(location.hash==='#'+page)renderPage();else location.hash=page;
  window.scrollTo({top:0,behavior:'smooth'});
}
function openActivity(id){
  id=String(id);if(!dashboard||!dashboard.activities.some(r=>String(r.activity_id)===id))return false;
  pendingActivity=id;$('activity-sport').value='All sports';$('activity-search').value='';typeFilter.activities=null;scrollToDetail=true;
  if(activePage==='activities')renderActivities();else location.hash='activities';
  return true;
}
document.addEventListener('click',event=>{const card=event.target.closest('[data-go]');if(card)go(card.dataset.go);});
document.addEventListener('keydown',event=>{if(event.key!=='Enter')return;const card=event.target.closest('[data-go]');if(card)go(card.dataset.go);});
function metricCards(id,cards){$(id).innerHTML=cards.map(([label,value,note])=>`<article class="metric-card"><span>${esc(label)}</span><strong>${esc(value)}</strong>${note?`<small>${esc(note)}</small>`:''}</article>`).join('');}
function tableMarkup(list,columns){return '<table><thead><tr>'+columns.map(([,label])=>`<th>${esc(label)}</th>`).join('')+'</tr></thead><tbody>'+list.map(r=>'<tr>'+columns.map(([key])=>`<td>${esc(r[key]??'—')}</td>`).join('')+'</tr>').join('')+'</tbody></table>';}
function downloadRows(list,name){if(!list.length)return;const keys=Object.keys(list[0]).filter(k=>!list.some(r=>typeof r[k]==='object'&&r[k]!==null));const csv=[keys,...list.map(r=>keys.map(k=>r[k]??''))].map(row=>row.map(v=>'"'+String(v).replaceAll('"','""')+'"').join(',')).join('\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function periodKey(date){const d=new Date(day(date)+'T00:00:00Z'),period=$('group-period').value;if(period==='week')d.setUTCDate(d.getUTCDate()-(d.getUTCDay()+6)%7);if(period==='month')d.setUTCDate(1);if(period==='year'){d.setUTCMonth(0);d.setUTCDate(1);}return d.toISOString().slice(0,10);}
function nextPeriod(k){const d=new Date(k+'T00:00:00Z'),p=$('group-period').value;if(p==='week')d.setUTCDate(d.getUTCDate()+7);else if(p==='month')d.setUTCMonth(d.getUTCMonth()+1);else if(p==='year')d.setUTCFullYear(d.getUTCFullYear()+1);else d.setUTCDate(d.getUTCDate()+1);return d.toISOString().slice(0,10);}
function fillPeriods(sorted){if(sorted.length<2)return sorted;const map=new Map(sorted),last=sorted[sorted.length-1][0],out=[];let k=sorted[0][0],guard=0;while(k<=last&&guard++<5000){out.push([k,map.get(k)||0]);k=nextPeriod(k);}return out;}
function rollN(pairs,n){return pairs.map((p,i)=>{if(i<n-1)return [p[0],null];const w=pairs.slice(i-n+1,i+1).map(x=>x[1]).filter(valid);return [p[0],w.length?w.reduce((a,b)=>a+b,0)/w.length:null];});}
function rollDays(pts,days=7,min=4){const t=pts.map(p=>new Date(p[0]).getTime());return pts.map((p,i)=>{let sum=0,n=0;for(let j=i;j>=0&&t[j]>t[i]-days*864e5;j--){if(valid(pts[j][1])){sum+=pts[j][1];n++;}}return [p[0],n>=min?sum/n:null];});}
function groupActivities(list,key,divisor=1){const groups=new Map();list.forEach(r=>{if(!valid(r[key]))return;const p=periodKey(r.start_time);groups.set(p,(groups.get(p)||0)+r[key]/divisor);});return fillPeriods([...groups].sort((a,b)=>a[0].localeCompare(b[0])));}
function plot(id,traces,y,height=350,extra={}){const l=layout(y,height);l.showlegend=traces.length>1;l.legend={orientation:'h',y:1.16};l.margin.t=35;l.xaxis.type='date';l.hovermode='x unified';Object.assign(l,extra);if(!traces.some(t=>t.y?.some(valid)))l.annotations=[{text:'No recorded data for this view.',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false,font:{color:muted}}];return Plotly.react(id,traces,l,{...config,displayModeBar:'hover',modeBarButtonsToRemove:['select2d','lasso2d']});}
async function refreshDashboard(){
  if(!data)return;const request=++dashboardRequest;$('dashboard-status').textContent='Loading dashboard…';
  try{const response=await fetch('/api/dashboard?'+new URLSearchParams({start:$('start').value,end:$('end').value}));if(!response.ok)throw new Error('Could not load dashboard history.');const result=await response.json();if(request!==dashboardRequest)return;dashboard=result;$('dashboard-status').textContent='';const state=result.sync_state;$('sync-note').textContent=state.last_synced_at?`Last sync ${dateLabel(state.last_synced_at)}`:'Not synced yet';renderPage();}
  catch(error){if(request===dashboardRequest)$('dashboard-status').textContent=error.message;}
}
function navigate(){activePage=pages.includes(location.hash.slice(1))?location.hash.slice(1):'overview';pages.forEach(page=>$(page+'-page').hidden=page!==activePage);document.querySelectorAll('.main-nav a').forEach(a=>{const current=a.hash==='#'+activePage;a.classList.toggle('active',current);if(current)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});$('page-title').textContent=titles[activePage];renderPage();}
function renderPage(){document.body.dataset.page=activePage;if(activePage==='connections'){loadConnections();return;}if(activePage==='discoveries'){if(data)render();return;}if(!dashboard)return;({overview:renderOverview,trends:renderTrends,training:renderTraining,calendar:renderCalendar,activities:renderActivities,ai:renderAI})[activePage]?.();}
function renderOverview(){
  const m=dashboard.insights.metrics.current,p=dashboard.insights.metrics.previous,list=acts();
  const delta=(key,unit='',digits=1)=>valid(m[key])&&valid(p[key])?`${m[key]-p[key]>=0?'+':''}${fmt(m[key]-p[key],digits)}${unit} vs previous period`:'No previous comparison';
  metricCards('overview-metrics',[
    ['Activities',list.length,delta('activity_count','',0)],['Distance',fmt(sum(list,'distance_m')===null?null:sum(list,'distance_m')/1000)+' km',delta('distance_km',' km')],['Training time',fmt(sum(list,'duration_s')===null?null:sum(list,'duration_s')/3600)+' h',delta('duration_hours',' h')],['Avg sleep',fmt(m.avg_sleep_hours)+' h',delta('avg_sleep_hours',' h')],['Resting HR',fmt(m.avg_resting_hr,0)+' bpm',delta('avg_resting_hr',' bpm')],['Avg steps',fmt(m.avg_steps,0),delta('avg_steps','',0)]
  ]);
  metricCards('overview-metrics2',[['Total steps',fmt(m.total_steps,0),delta('total_steps','',0)],['Body battery high',fmt(m.avg_body_battery_high,0),delta('avg_body_battery_high','',0)],['Elevation gain',fmt(sum(list,'elevation_gain_m'),0)+' m'],['Avg workout HR',fmt(mean(list,'avg_hr'),0)+' bpm']]);
  linkTiles('overview-metrics',{'Activities':'activities','Distance':'training','Training time':'training','Avg sleep':'trends:sleep_hours','Resting HR':'trends:resting_hr','Avg steps':'trends:steps'});
  linkTiles('overview-metrics2',{'Total steps':'trends:steps','Body battery high':'trends:body_battery_high','Elevation gain':'activities','Avg workout HR':'training'});
  const pr=dashboard.insights.previous_range;$('overview-compare').textContent=pr?`Compared with the previous period of equal length: ${dateLabel(pr.start)} – ${dateLabel(pr.end)}.`:'';
  const daily=dashboard.daily_stats;
  const rec=[['Overnight HRV',latest(daily,'hrv_overnight'),' ms',0],['Training readiness',latest(daily,'training_readiness'),'',0],['Training status',latest(daily,'training_status'),'',0],['VO₂ max',latest(daily,'vo2_max'),'',1],['Recovery time',latest(daily,'recovery_time_hours'),' h',1]];
  $('recovery-panel').hidden=rec.every(r=>r[1]===null);
  metricCards('overview-recovery',rec.map(([l,v,u,d])=>[l,v===null?'—':typeof v==='number'?fmt(v,d)+u:v]));
  linkTiles('overview-recovery',{'Overnight HRV':'trends:hrv_overnight','Training readiness':'trends:training_readiness','VO₂ max':'trends:vo2_max','Recovery time':'trends:recovery_time_hours'});
const rhrPts=daily.filter(r=>valid(r.resting_hr)).map(r=>[day(r.date),r.resting_hr]),rhrAvg=rollDays(rhrPts,7,4);
  plot('overview-steps',[{type:'scatter',mode:'markers',name:'Daily',x:rhrPts.map(r=>r[0]),y:rhrPts.map(r=>r[1]),marker:{size:5,color:'#fb7185',opacity:.4},hovertemplate:'%{y:.0f} bpm'},{type:'scatter',mode:'lines',name:'7-day average',x:rhrAvg.map(r=>r[0]),y:rhrAvg.map(r=>r[1]),line:{color:'#fb7185',width:3},connectgaps:true,hovertemplate:'%{y:.1f} bpm'}],'bpm',280).then(()=>{const g=$('overview-steps');g.removeAllListeners('plotly_click');g.on('plotly_click',()=>go('trends:resting_hr'));});
  const mix=Object.keys(colorFor).map(c=>[c,(sum(list.filter(r=>r.category===c),'distance_m')||0)]).filter(r=>r[1]>0);
  const pl=layout('',280);pl.margin={l:10,r:10,t:10,b:10};pl.showlegend=true;pl.legend={font:{color:muted}};
  if(!mix.length)pl.annotations=[{text:'No activities in this range.',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false,font:{color:muted}}];
  Plotly.react('overview-mix',[{type:'pie',labels:mix.map(r=>r[0]),values:mix.map(r=>r[1]),hole:.55,marker:{colors:mix.map(r=>colorFor[r[0]]),line:{color:'#20242b',width:2}},textinfo:'label+percent',hovertemplate:'%{label}<br>%{value:.0f} m<extra></extra>',textfont:{color:'#14200a'}}],pl,config).then(()=>{const g=$('overview-mix');g.removeAllListeners('plotly_click');g.on('plotly_click',e=>go('activities:'+e.points[0].label));});
  const traces=Object.entries(colorFor).map(([sport,color])=>{const grouped=groupActivities(list.filter(r=>r.category===sport),'distance_m',1000);return {type:'bar',name:sport,x:grouped.map(r=>r[0]),y:grouped.map(r=>r[1]),marker:{color}};});
  const totals=groupActivities(list,'distance_m',1000),avgLine=rollN(totals,4);
  traces.push({type:'scatter',mode:'lines',name:'4-period average',x:avgLine.map(r=>r[0]),y:avgLine.map(r=>r[1]),line:{color:'#e6ebf2',width:2,dash:'dot'},hovertemplate:'%{y:.1f} km'});
  plot('overview-chart',traces,'Distance (km)',350,{barmode:'stack'}).then(()=>{const graph=$('overview-chart');graph.removeAllListeners('plotly_click');graph.on('plotly_click',e=>{const start=day(e.points[0].x),end=new Date(start+'T00:00:00Z'),period=$('group-period').value;if(period==='week')end.setUTCDate(end.getUTCDate()+6);if(period==='month'){end.setUTCMonth(end.getUTCMonth()+1);end.setUTCDate(0);}if(period==='year'){end.setUTCFullYear(end.getUTCFullYear()+1);end.setUTCDate(0);}$('start').value=start;$('end').value=end.toISOString().slice(0,10);location.hash='activities';load();});});
  $('overview-insights').innerHTML=dashboard.insights.insights.length?insightCards(dashboard.insights.insights.slice(0,5)):'<p class="muted">No rule-based signals for this period. This does not establish that nothing changed.</p>';
  $('overview-quality').innerHTML=dashboard.data_quality.length?dashboard.data_quality.map(n=>`<li>${esc(n)}</li>`).join(''):'<li>Data coverage looks good for this range.</li>';
}
const trendMetrics={steps:['Steps','steps'],resting_hr:['Resting heart rate','bpm'],hrv_overnight:['Overnight HRV','ms'],stress_avg:['Stress','points'],training_readiness:['Training readiness','points'],body_battery_high:['Body battery high','points'],body_battery_low:['Body battery low','points'],vo2_max:['VO₂ max','ml/kg/min'],recovery_time_hours:['Recovery time','hours'],sleep_hours:['Sleep duration','hours'],sleep_score:['Sleep score','points'],deep_hours:['Deep sleep','hours'],rem_hours:['REM sleep','hours'],weight_kg:['Weight','kg'],body_fat_pct:['Body fat','%'],avg_respiration:['Respiration','breaths/min'],avg_spo2:['Oxygen saturation','%']};
Object.entries(trendMetrics).forEach(([value,[label]])=>{$('trend-metric').add(new Option(label,value));});$('trend-metric').value='hrv_overnight';
function dailyRows(){const merged=new Map();for(const source of [dashboard.daily_stats,dashboard.sleep,dashboard.body_composition])source.forEach(r=>{const key=day(r.date);merged.set(key,{...(merged.get(key)||{}),...r,date:key});});return [...merged.values()].sort((a,b)=>a.date.localeCompare(b.date)).map(r=>({...r,sleep_hours:valid(r.total_sleep_s)?r.total_sleep_s/3600:null,deep_hours:valid(r.deep_s)?r.deep_s/3600:null,rem_hours:valid(r.rem_s)?r.rem_s/3600:null}));}
function renderTrends(){
  const key=$('trend-metric').value,list=dailyRows(),[label,unit]=trendMetrics[key];$('trend-title').textContent=label;
  const pts=list.filter(r=>valid(r[key])).map(r=>[day(r.date),r[key]]);
  const digits=pts.length&&Math.abs(pts.reduce((a,p)=>a+p[1],0)/pts.length)>=100?0:1,show=v=>fmt(v,digits)+' '+unit;
  if(pts.length){
    const vals=pts.map(p=>p[1]),mean=vals.reduce((a,b)=>a+b,0)/vals.length,sd=Math.sqrt(vals.reduce((a,b)=>a+(b-mean)**2,0)/vals.length);
    const lo=pts.reduce((a,p)=>p[1]<a[1]?p:a),hi=pts.reduce((a,p)=>p[1]>a[1]?p:a),last=pts[pts.length-1],t=new Date(last[0]).getTime();
    const win=(from,to)=>{const v=pts.filter(p=>{const x=new Date(p[0]).getTime();return x>t-to*864e5&&x<=t-from*864e5;}).map(p=>p[1]);return v.length>=3?v.reduce((a,b)=>a+b,0)/v.length:null;};
    const recent=win(0,7),before=win(7,14),change=valid(recent)&&valid(before)?recent-before:null;
    metricCards('trend-stats',[['Latest',show(last[1]),dateLabel(last[0])],['Average',show(mean),`${pts.length} recorded days`],['Lowest',show(lo[1]),dateLabel(lo[0])],['Highest',show(hi[1]),dateLabel(hi[0])],['Last 7 days',valid(recent)?show(recent):'—',change===null?'Not enough data to compare':`${change>=0?'+':''}${fmt(change,digits)} vs the 7 days before`]]);
    const band={type:'rect',xref:'paper',x0:0,x1:1,yref:'y',y0:mean-sd,y1:mean+sd,fillcolor:'rgba(159,211,86,.08)',line:{width:0},layer:'below'},mid={type:'line',xref:'paper',x0:0,x1:1,yref:'y',y0:mean,y1:mean,line:{color:'#475569',width:1,dash:'dash'}};
    if($('trend-mode').value==='rolling'){
      const avg=rollDays(pts,7,4);
      plot('trend-chart',[{type:'scatter',mode:'markers',name:'Daily',x:pts.map(p=>p[0]),y:pts.map(p=>p[1]),marker:{size:5,color:blue,opacity:.45},hovertemplate:'%{y:.1f} '+unit},{type:'scatter',mode:'lines',name:'7-day average',x:avg.map(p=>p[0]),y:avg.map(p=>p[1]),line:{color:green,width:3},connectgaps:true,hovertemplate:'%{y:.1f} '+unit}],unit,410,{shapes:[band,mid]});
      $('trend-note').textContent='Dots are daily values; the line is the average of the preceding 7 days (at least 4 recorded). The shaded band is your usual range, the average ± one standard deviation, with the dashed line at your average. Points outside the band are unusual for you.';
    }else{
      const g=new Map();list.forEach(r=>{const k=periodKey(r.date);if(!g.has(k))g.set(k,[]);if(valid(r[key]))g.get(k).push(r[key]);});
      const pp=[...g].map(([d,v])=>[d,v.length?v.reduce((a,b)=>a+b,0)/v.length:null]);
      plot('trend-chart',[{type:'scatter',mode:'lines+markers',name:'Average',x:pp.map(r=>r[0]),y:pp.map(r=>r[1]),line:{color:green,width:2.5},marker:{size:7},connectgaps:false,hovertemplate:'%{y:.1f} '+unit}],unit,410,{shapes:[band,mid]});
      $('trend-note').textContent='Averages of recorded days in each group. The shaded band is your usual range (average ± one standard deviation). Missing values are excluded, not counted as zero.';
    }
  }else{
    metricCards('trend-stats',[]);plot('trend-chart',[],unit,410);$('trend-note').textContent='No recorded values in this range.';
  }
  renderSleepStages();
}
function renderSleepStages(){
  const stages=[['deep_s','Deep',blue],['light_s','Light','#6cc04a'],['rem_s','REM','#fbbf24'],['awake_s','Awake','#c4a5ff']];
  const traces=stages.map(([key,name,color])=>{const pts=periodAvg(dashboard.sleep,key,3600);return {type:'bar',name,x:pts.map(r=>r[0]),y:pts.map(r=>r[1]),marker:{color}};});
  plot('sleep-stages',traces,'Avg hours / night',280,{barmode:'stack'});
}
function renderTraining(){
  const sport=$('training-sport').value,base=acts().filter(r=>sport==='All'||r.category===sport);
  const types=[...new Set(base.map(r=>typeLabel(r.type)))].sort();
  const keep=chips('training-types',types,'training',renderTraining),list=base.filter(keep);
  $('training-empty').hidden=list.length>0;$('training-body').hidden=!list.length;
  const color=colorFor[sport]||green,days=Math.max(1,Math.round((new Date($('end').value)-new Date($('start').value))/86400000)+1);
  if(list.length){
  const km=(sum(list,'distance_m')||0)/1000,hours=(sum(list,'duration_s')||0)/3600;
  metricCards('training-metrics',[['Sessions',list.length],['Distance',fmt(km)+' km'],['Time',fmt(hours)+' h'],['Weekly volume',fmt(km/days*7)+' km','per week'],['Average HR',fmt(mean(list,'avg_hr'),0)+' bpm'],['Load',fmt(sum(list,'training_load'),0)]]);
  const dist=groupActivities(list,'distance_m',1000),avg=rollN(dist,4);
  plot('training-chart',[{type:'bar',name:'Distance',x:dist.map(r=>r[0]),y:dist.map(r=>r[1]),marker:{color,opacity:.85},hovertemplate:'%{y:.1f} km'},{type:'scatter',mode:'lines',name:'4-period average',x:avg.map(r=>r[0]),y:avg.map(r=>r[1]),line:{color:'#e6ebf2',width:2,dash:'dot'},hovertemplate:'%{y:.1f} km'}],'km',350);
  const zoneColors=['#5aa9e6','#9fd356','#fbbf24','#fb923c','#fb7185'],zoneNames=['Z1 Easy','Z2 Aerobic','Z3 Tempo','Z4 Threshold','Z5 Max'];
  const zoneTraces=zoneNames.map((name,z)=>{const g=new Map();list.forEach(r=>{const v=r.hr_zones?.[z];if(!valid(v))return;const k=periodKey(r.start_time);g.set(k,(g.get(k)||0)+v/60);});const pts=fillPeriods([...g].sort((a,b)=>a[0].localeCompare(b[0])));return {type:'bar',name,x:pts.map(r=>r[0]),y:pts.map(r=>r[1]),marker:{color:zoneColors[z]},hovertemplate:'%{y:.0f} min'};});
  plot('training-time-chart',zoneTraces,'minutes',350,{barmode:'stack'});
  const sessions=[...list].sort((a,b)=>a.start_time.localeCompare(b.start_time));
  const hrSeries=sessions.map(r=>valid(r.avg_hr)?r.avg_hr:null),trendHr=hrSeries.map((v,i)=>{const w=hrSeries.slice(Math.max(0,i-4),i+1).filter(valid);return w.length>=3?median(w):null;});
  const names=sessions.map(r=>`${r.name||r.type} · ${fmt(valid(r.distance_m)?r.distance_m/1000:null)} km`);
  plot('effort-chart',[{type:'scatter',mode:'markers',name:'Session',x:sessions.map(r=>r.start_time),y:hrSeries,text:names,marker:{size:sessions.map(r=>6+Math.sqrt(Math.max(0,(r.distance_m||0)/1000))*3),color:sessions.map(r=>valid(r.training_load)?r.training_load:null),colorscale:[[0,blue],[1,'#fb7185']],showscale:false,line:{width:1,color:'#1a1d22'},opacity:.85},hovertemplate:'%{text}<br>%{y:.0f} bpm<extra></extra>'},{type:'scatter',mode:'lines',name:'5-session median',x:sessions.map(r=>r.start_time),y:trendHr,line:{color:'#e6ebf2',width:2,dash:'dot'},connectgaps:true,hovertemplate:'%{y:.0f} bpm<extra>trend</extra>'}],'avg HR (bpm)',280,{hovermode:'closest'}).then(()=>{const g=$('effort-chart');g.removeAllListeners('plotly_click');g.on('plotly_click',e=>{if(e.points[0].curveNumber!==0)return;const id=sessions[e.points[0].pointIndex]?.activity_id;if(id!==undefined)openActivity(id);});});
  const moving=r=>valid(r.moving_duration_s)&&r.moving_duration_s>0?r.moving_duration_s:r.duration_s;
  const recentRows=[...sessions].reverse().slice(0,8),maxKm=Math.max(1,...recentRows.map(r=>(r.distance_m||0)/1000));
  const paceVals=recentRows.map(r=>r.distance_m>0&&valid(moving(r))?moving(r)/60/(r.distance_m/1000):null).filter(valid),paceMid=paceVals.length?median(paceVals):null;
  $('recent-sessions').innerHTML='<table><thead><tr><th>Date</th><th>Activity</th><th>Distance</th><th>Pace</th><th>Avg HR</th><th>HR zones</th><th>Load</th></tr></thead><tbody>'+recentRows.map(r=>{const km=(r.distance_m||0)/1000,pc=r.distance_m>0&&valid(moving(r))?moving(r)/60/km:null;return `<tr class="link-row" data-go="act:${r.activity_id}"><td>${esc(dateLabel(r.start_time))}</td><td>${esc(r.name||r.type)}</td><td class="bar-cell">${fmt(km,2)} km<b class="mini" style="width:${Math.max(4,km/maxKm*100)}%"></b></td><td class="${pc!==null&&paceMid!==null&&pc<paceMid?'good':''}">${pc===null?'—':pace(pc)}</td><td>${fmt(r.avg_hr,0)}</td><td>${zoneBar(r.hr_zones)}</td><td>${fmt(r.training_load,0)}</td></tr>`;}).join('')+'</tbody></table>';
  renderStats(list,sport);
  const rf=dashboard.running_focus,showRun=!!rf&&(sport==='Running'||sport==='All');
  $('running-focus').hidden=!showRun;
  if(showRun){
    metricCards('running-metrics',[['Avg pace',valid(rf.avg_pace_min_km)?pace(rf.avg_pace_min_km)+' /km':'—'],['Avg run distance',fmt(rf.avg_run_distance_km)+' km'],['Est. weekly volume',fmt(rf.weekly_volume_km)+' km'],['Longest run',fmt(rf.longest_run.distance_km)+' km',dateLabel(rf.longest_run.date)]]);
    const best=(label,b)=>[label,b?finishTime(b.seconds):'—',b?`${dateLabel(b.date)} · ${b.activity}`:'No effort recorded'];
    metricCards('running-best',[best('Best 5K',rf.best_5k),best('Best 10K',rf.best_10k),best('Best mile',rf.best_mile)]);
    linkTiles('running-metrics',{'Longest run':'act:'+rf.longest_run.activity_id});
    linkTiles('running-best',{'Best 5K':rf.best_5k&&'act:'+rf.best_5k.activity_id,'Best 10K':rf.best_10k&&'act:'+rf.best_10k.activity_id,'Best mile':rf.best_mile&&'act:'+rf.best_mile.activity_id});
  }
  const cats=['Running','Swimming','Cycling','Other'],ins=dashboard.insights.insights.filter(i=>i.category===sport||['Training load','Recovery'].includes(i.category)||(sport==='All'&&cats.includes(i.category))).slice(0,4);
  $('training-insights-panel').hidden=!ins.length;$('training-insights').innerHTML=insightCards(ins);
  }
  const model=dashboard.race_model;metricCards('race-cards',Object.entries(goalDistances).map(([label])=>{const p=model.predictions[label];return [label,p?.expected||'No estimate',p?`Range ${p.range.join('–')} · ${p.confidence} model confidence`:'Needs a race or time trial'];}));
  $('race-evidence').innerHTML=Object.entries(model.predictions).map(([label,p])=>`<p><strong>${esc(label)}:</strong> ${esc(p.source_effort.activity)} on ${esc(p.source_effort.date)}; race equivalent ${esc(p.base_equivalent)}, recency +${esc(p.recency_adjustment_pct)}%, endurance +${esc(p.endurance_adjustment_pct)}%. Garmin comparison: ${esc(p.garmin_comparison||'unavailable')}.</p>`).join('')+model.limitations.map(t=>`<p class="muted">${esc(t)}</p>`).join('');
  const key={'5K':'time_5k_s','10K':'time_10k_s','Half marathon':'time_half_s',Marathon:'time_marathon_s'}[$('race-distance').value],history=dashboard.race_history.filter(r=>day(r.date)>=$('start').value&&day(r.date)<=$('end').value);plot('race-chart',[{type:'scatter',mode:'lines',x:history.map(r=>r.date),y:history.map(r=>valid(r[key])?r[key]/60:null),line:{color:blue}}],'Garmin predicted minutes',280);
}
function renderStats(list,sport){
  const months=Number($('stats-range').value),groups=new Map();
  list.forEach(r=>{const k=String(r.start_time).slice(0,7);if(!groups.has(k))groups.set(k,[]);groups.get(k).push(r);});
  let keys=[...groups.keys()].sort().reverse();
  const cutoff=new Date();cutoff.setMonth(cutoff.getMonth()-months);const cut=localDay(cutoff).slice(0,7);
  const all=keys,shown=months?keys.filter(k=>k>cut):keys;
  const swim=sport==='Swimming',ride=sport==='Cycling';
  const row=k=>{const g=groups.get(k),m=sum(g,'distance_m')||0,t=g.reduce((a,r)=>a+(valid(actMoving(r))?actMoving(r):0),0);
    const zones=[0,1,2,3,4].map(i=>g.reduce((a,r)=>a+(valid(r.hr_zones?.[i])?r.hr_zones[i]:0),0));
    const dur=sum(g,'duration_s')||0,pc=m>0&&t>0?(swim?t/(m/100)/60:ride?m/t*3.6:t/(m/1000)/60):null;
    return {k,n:g.length,m,dur,pc,zones,hr:mean(g,'avg_hr'),up:sum(g,'elevation_gain_m'),load:sum(g,'training_load')};};
  const rows=shown.map(row),prev=k=>{const i=all.indexOf(k);return i>=0&&i+1<all.length?row(all[i+1]).m:null;};
  $('stats-table').innerHTML=rows.length?'<table><thead><tr><th>Month</th><th>Count</th><th>Distance</th><th>vs prev.</th><th>Duration</th><th>'+(ride?'Speed':'Pace')+'</th><th>HR zones</th><th>Avg HR</th><th>Ascent</th><th>Load</th></tr></thead><tbody>'+rows.map(r=>{
    const p=prev(r.k),d=p?(r.m-p)/p*100:null,label=new Date(r.k+'-15T12:00:00').toLocaleDateString(undefined,{month:'long',year:'numeric'});
    const pcText=r.pc===null?'—':ride?fmt(r.pc,1)+' km/h':pace(r.pc)+(swim?'/100m':'/km');
    return `<tr><td><span class="month-link" data-month="${r.k}">${esc(label)}</span></td><td>${r.n}×</td><td>${fmt(r.m/1000,1)} km</td><td class="${d===null?'':d>=0?'num-up':'num-down'}">${d===null?'—':(d>=0?'+':'')+Math.round(d)+' %'}</td><td>${r.dur?finishTime(r.dur):'—'}</td><td>${pcText}</td><td>${zoneBar(r.zones)}</td><td>${fmt(r.hr,0)}</td><td>${valid(r.up)?fmt(r.up,0)+' m':'—'}</td><td>${fmt(r.load,0)}</td></tr>`;}).join('')+'</tbody></table>':'<p class="muted">No activities to summarise.</p>';
  $('stats-table').querySelectorAll('[data-month]').forEach(el=>el.onclick=()=>{const [y,mo]=el.dataset.month.split('-').map(Number);$('start').value=el.dataset.month+'-01';$('end').value=localDay(new Date(y,mo,0));$('date-preset').value='custom';$('activity-sport').value=sport==='All'?'All sports':sport;typeFilter.activities=null;location.hash='activities';load();});
}
$('stats-range').onchange=renderTraining;
// page / metric search in the top bar
(function(){
  const items=[...pages.map(p=>[p.charAt(0).toUpperCase()+p.slice(1)+' page',p]),...Object.entries(trendMetrics).map(([k,[l]])=>['Trend: '+l,'trends:'+k])];
  $('page-list').innerHTML=items.map(([l])=>`<option value="${esc(l)}">`).join('');
  const jump=()=>{const q=$('page-search-input').value.trim().toLowerCase();if(!q)return;const hit=items.find(([l])=>l.toLowerCase()===q)||items.find(([l])=>l.toLowerCase().includes(q));if(hit){go(hit[1]);$('page-search-input').value='';$('page-search-input').blur();}};
  $('page-search').onsubmit=e=>{e.preventDefault();jump();};$('page-search-input').onchange=jump;
})();
let actSort={key:'start_time',dir:-1};
const actMoving=r=>valid(r.moving_duration_s)&&r.moving_duration_s>0?r.moving_duration_s:r.duration_s;
function actPace(r){const d=r.distance_m,t=actMoving(r);if(!(d>0)||!valid(t))return null;return r.category==='Swimming'?t/(d/100)/60:r.category==='Cycling'?d/t*3.6:t/(d/1000)/60;}
function actPaceText(r){const v=actPace(r);return v===null?'—':r.category==='Cycling'?fmt(v,1)+' km/h':pace(v)+(r.category==='Swimming'?' /100m':' /km');}
function sortActs(list){const k=actSort.key,val=r=>k==='pace'?actPace(r):k==='name'?String(r.name||r.type).toLowerCase():r[k];return [...list].sort((a,b)=>{const x=val(a),y=val(b);if(x===null||x===undefined||(typeof x==='number'&&!valid(x)))return 1;if(y===null||y===undefined||(typeof y==='number'&&!valid(y)))return -1;return (x<y?-1:x>y?1:0)*actSort.dir;});}
function activityTableMarkup(list){
  if(!list.length)return '<p>No activities match these filters.</p>';
  const cols=[['start_time','Date'],['name','Activity'],['category','Sport'],['distance_m','Distance'],['duration_s','Duration'],['pace','Pace'],['avg_hr','Avg HR'],['hr_zones','HR zones'],['training_load','Load']];
  const maxKm=Math.max(1,...list.map(r=>(r.distance_m||0)/1000));
  const head=cols.map(([k,l])=>k==='hr_zones'?`<th>${l}</th>`:`<th class="sortable" data-sort="${k}">${l}${actSort.key===k?`<span class="sort-arrow">${actSort.dir<0?'▾':'▴'}</span>`:''}</th>`).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>`+list.map(r=>{const km=(r.distance_m||0)/1000;return `<tr data-id="${r.activity_id}"><td>${esc(dateLabel(r.start_time))}</td><td><button class="text-button" data-activity="${r.activity_id}">${esc(r.name||r.type)}</button></td><td>${esc(r.category)}</td><td class="bar-cell">${valid(r.distance_m)?(km<1?fmt(r.distance_m,0)+' m':fmt(km,2)+' km'):'—'}<b class="mini" style="width:${valid(r.distance_m)?Math.max(3,km/maxKm*100):0}%"></b></td><td>${valid(r.duration_s)?finishTime(r.duration_s):'—'}</td><td>${esc(actPaceText(r))}</td><td>${fmt(r.avg_hr,0)}</td><td>${zoneBar(r.hr_zones)}</td><td>${fmt(r.training_load,0)}</td></tr>`;}).join('')+'</tbody></table>';
}
function filteredActivities(){const sport=$('activity-sport').value,q=$('activity-search').value.toLowerCase();return acts().filter(r=>(sport==='All sports'||r.category===sport)&&`${r.name||''} ${r.location||''} ${r.type}`.toLowerCase().includes(q)).sort((a,b)=>b.start_time.localeCompare(a.start_time));}
function renderActivities(){const sport=$('activity-sport').value,q=$('activity-search').value.toLowerCase();const pre=acts().filter(r=>(sport==='All sports'||r.category===sport)&&`${r.name||''} ${r.location||''} ${r.type}`.toLowerCase().includes(q));
  const keep=chips('activity-types',[...new Set(pre.map(r=>typeLabel(r.type)))].sort(),'activities',renderActivities);const list=sortActs(pre.filter(keep));const old=$('activity-select').value;$('activity-select').replaceChildren();list.forEach(r=>$('activity-select').add(new Option(`${dateLabel(r.start_time)} · ${r.name||r.type}`,r.activity_id)));if(pendingActivity&&list.some(r=>String(r.activity_id)===pendingActivity)){$('activity-select').value=pendingActivity;pendingActivity=null;}else if(list.some(r=>String(r.activity_id)===old))$('activity-select').value=old;
  const km=(sum(list,'distance_m')||0)/1000,hrs=(sum(list,'duration_s')||0)/3600;
  metricCards('activity-summary',[['Activities',list.length],['Distance',fmt(km)+' km'],['Time',fmt(hrs)+' h'],['Average HR',fmt(mean(list,'avg_hr'),0)+' bpm'],['Elevation gain',fmt(sum(list,'elevation_gain_m'),0)+' m']]);
  const per=$('group-period').selectedOptions[0].text,dist=groupActivities(list,'distance_m',1000),time=groupActivities(list,'duration_s',3600);
  $('activity-dist-title').textContent=`Distance by ${per.toLowerCase()}`;$('activity-time-title').textContent=`Time by ${per.toLowerCase()}`;
  plot('activity-dist-chart',[{type:'bar',x:dist.map(r=>r[0]),y:dist.map(r=>r[1]),marker:{color:green}}],'km',280);plot('activity-time-chart',[{type:'bar',x:time.map(r=>r[0]),y:time.map(r=>r[1]),marker:{color:blue}}],'hours',280);
  $('activity-table').innerHTML=activityTableMarkup(list);
  document.querySelectorAll('#activity-table th[data-sort]').forEach(th=>th.onclick=()=>{actSort=actSort.key===th.dataset.sort?{key:actSort.key,dir:-actSort.dir}:{key:th.dataset.sort,dir:th.dataset.sort==='name'||th.dataset.sort==='category'?1:-1};renderActivities();});
  document.querySelectorAll('#activity-table tbody tr').forEach(tr=>{tr.onclick=()=>{$('activity-select').value=tr.dataset.id;renderActivity();$('activity-detail').scrollIntoView({behavior:'smooth',block:'start'});};});
  renderActivity();
  if(scrollToDetail){scrollToDetail=false;$('activity-detail').scrollIntoView({behavior:'smooth',block:'start'});}
}
const splitsCache=new Map();let splitsRequest=0;
async function loadSplits(r){
  const request=++splitsRequest,body=$('splits-body'),chart=$('splits-chart');
  chart.hidden=true;body.className='table-scroll muted';body.textContent='Loading splits…';
  try{
    let result=splitsCache.get(r.activity_id);
    if(!result){result=await post('/api/activity-splits',{id:r.activity_id});splitsCache.set(r.activity_id,result);}
    if(request!==splitsRequest)return;
    renderSplits(r,result.laps);
  }catch(error){if(request===splitsRequest){body.className='muted';body.textContent=error.message;}}
}
function renderSplits(r,laps){
  const body=$('splits-body'),chart=$('splits-chart');
  if(!laps.length){body.textContent='Garmin has no splits for this activity.';return;}
  const swim=r.category==='Swimming',ride=r.category==='Cycling',seconds=l=>valid(l.moving_s)&&l.moving_s>0?l.moving_s:l.duration_s;
  const paceOf=l=>{const d=l.distance_m,t=seconds(l);if(!(d>0)||!valid(t))return null;return swim?t/(d/100)/60:ride?d/t*3.6:t/(d/1000)/60;};
  const paceText=l=>{const v=paceOf(l);return v===null?'—':ride?fmt(v,1)+' km/h':pace(v)+(swim?' /100m':' /km');};
  const distText=l=>valid(l.distance_m)?(swim||l.distance_m<1000?fmt(l.distance_m,0)+' m':fmt(l.distance_m/1000,2)+' km'):'—';
  const any=key=>laps.some(l=>valid(l[key])&&l[key]>0);
  const cols=[['lap','#',l=>l.lap??'—'],['d','Distance',distText],['t','Time',l=>valid(l.duration_s)?finishTime(l.duration_s):'—'],['p',ride?'Speed':'Pace',paceText],['hr','Avg HR',l=>fmt(l.avg_hr,0)],['mhr','Max HR',l=>fmt(l.max_hr,0)]];
  if(!swim&&!ride)cols.push(['g','Elev +/−',l=>`${fmt(l.gain_m,0)} / ${fmt(l.loss_m,0)}`]);
  if(any('run_cadence'))cols.push(['c','Cadence',l=>fmt(l.run_cadence,0)]);
  if(any('power'))cols.push(['w','Power',l=>fmt(l.power,0)+' W']);
  if(any('swolf'))cols.push(['s','SWOLF',l=>fmt(l.swolf,0)]);
  if(any('strokes'))cols.push(['st','Strokes',l=>fmt(l.strokes,0)]);
  const effort=l=>!['REST','RECOVERY'].includes(l.intensity)&&l.distance_m>=200,effortPace=l=>effort(l)?paceOf(l):null;
  const values=laps.map(effortPace).filter(valid),best=values.length?(ride?Math.max(...values):Math.min(...values)):null;
  body.className='table-scroll';
  body.innerHTML='<table><thead><tr>'+cols.map(c=>`<th>${c[1]}</th>`).join('')+'</tr></thead><tbody>'+laps.map(l=>{const v=effortPace(l),fast=best!==null&&v===best&&values.length>2,tag=l.intensity&&!['ACTIVE','INTERVAL'].includes(l.intensity)?` <span class="tag">${esc(String(l.intensity).toLowerCase())}</span>`:'';return `<tr${fast?' class="best"':''}>`+cols.map((c,i)=>`<td>${esc(c[2](l))}${i===0?tag:''}</td>`).join('')+'</tr>';}).join('')+'</tbody></table>';
  if(values.length<2)return;chart.hidden=false;
  const x=laps.map((l,i)=>l.lap??i+1),y=laps.map(effortPace);
  plot('splits-chart',[{type:'bar',name:ride?'Speed (km/h)':'Pace (min'+(swim?'/100m':'/km')+')',x,y,marker:{color:laps.map(l=>effortPace(l)===best?green:blue)},hovertemplate:'Lap %{x}<br>%{y:.2f}<extra></extra>'},{type:'scatter',mode:'lines+markers',name:'Avg HR',x,y:laps.map(l=>valid(l.avg_hr)?l.avg_hr:null),yaxis:'y2',line:{color:'#fb7185',width:2}}],ride?'km/h':swim?'min / 100 m':'min / km',260,{xaxis:{type:'category',gridcolor:'transparent',title:{text:'Lap'}},yaxis2:{title:{text:'bpm'},overlaying:'y',side:'right',gridcolor:'transparent'}});
}
function renderActivity(){
  document.querySelectorAll('#activity-table tr[data-id]').forEach(tr=>tr.classList.toggle('selected',tr.dataset.id===$('activity-select').value));const r=dashboard.activities.find(r=>String(r.activity_id)===$('activity-select').value);if(!r){$('activity-detail').textContent='Choose a broader date range or sport.';$('activity-zones').hidden=true;return;}$('activity-zones').hidden=false;
  const moving=valid(r.moving_duration_s)&&r.moving_duration_s>0?r.moving_duration_s:r.duration_s;
  $('activity-detail').innerHTML=`<h2>${esc(r.name||r.type)}</h2><p class="muted">${esc(dateLabel(r.start_time))} · ${esc(r.location||r.category)}</p><div id="activity-metrics" class="metric-strip"></div><div class="splits"><h3>Splits</h3><div id="splits-body" class="table-scroll muted">Loading splits…</div><div id="splits-chart" class="medium-chart" hidden></div></div><details id="raw-details"><summary>Raw Garmin activity payload</summary><pre id="raw-payload">Open to load.</pre></details>`;
  $('raw-details').ontoggle=async()=>{if(!$('raw-details').open)return;$('raw-payload').textContent='Loading…';try{const res=await fetch('/api/activity-raw?id='+encodeURIComponent(r.activity_id));if(!res.ok)throw new Error('Could not load the stored payload.');$('raw-payload').textContent=JSON.stringify(await res.json(),null,2);}catch(e){$('raw-payload').textContent=e.message;}};
  metricCards('activity-metrics',[['Distance',fmt(valid(r.distance_m)?r.distance_m/1000:null,2)+' km'],['Moving time',valid(moving)?finishTime(moving):'—'],['Pace',r.distance_m>0&&valid(moving)?pace(moving/60/(r.distance_m/1000))+' /km':'—'],['Average HR',fmt(r.avg_hr,0)+' bpm'],['Max HR',fmt(r.max_hr,0)+' bpm'],['Climbing',fmt(r.elevation_gain_m,0)+' m'],['Garmin load',fmt(r.training_load,0)],['Aerobic effect',fmt(r.aerobic_te)],['Anaerobic effect',fmt(r.anaerobic_te)],['Power',fmt(r.avg_power,0)+' W'],['Calories',fmt(r.calories,0)]]);
  loadSplits(r);
  const l=layout('Minutes',260);l.xaxis.type='category';const zones=(r.hr_zones||[]).map(x=>valid(x)?x/60:null);if(!zones.some(valid))l.annotations=[{text:'No heart-rate zone data for this activity.',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false,font:{color:muted}}];Plotly.react('activity-zones',[{type:'bar',x:['Z1','Z2','Z3','Z4','Z5'],y:zones,marker:{color:[green,blue,'#fbbf24','#fb923c','#c4a5ff']}}],l,config);
}
let calendarView='month';
function calendarRange(){const a=new Date(calendarMonth.getFullYear(),calendarMonth.getMonth(),calendarMonth.getDate());if(calendarView==='month')return [new Date(a.getFullYear(),a.getMonth(),1),new Date(a.getFullYear(),a.getMonth()+1,0)];const start=new Date(a);start.setDate(a.getDate()-(a.getDay()+6)%7);const end=new Date(start);end.setDate(start.getDate()+6);return [start,end];}
function renderCalendar(){
  const [first,last]=calendarRange(),keyOf=localDay,inRange=r=>day(r.date)>=keyOf(first)&&day(r.date)<=keyOf(last);
  $('calendar-month').textContent=calendarView==='month'?first.toLocaleDateString(undefined,{month:'long',year:'numeric'}):`Week of ${first.toLocaleDateString(undefined,{month:'short',day:'numeric'})} – ${last.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}`;
  const plansIn=dashboard.calendar.filter(inRange),count=st=>plansIn.filter(r=>r.status===st).length,done=count('completed'),missed=count('missed'),eligible=done+missed;
  metricCards('calendar-metrics',[['Scheduled',plansIn.length],['Completed',done],['Adherence',eligible?fmt(done/eligible*100,0)+'%':'—'],['Upcoming',count('planned')],['Missed / skipped',`${missed} / ${count('skipped')}`]]);
  const offset=calendarView==='month'?(first.getDay()+6)%7:0;let html=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(t=>`<div class="calendar-weekday">${t}</div>`).join('');for(let i=0;i<offset;i++)html+='<div class="calendar-blank"></div>';
  for(let d=new Date(first);d<=last;d.setDate(d.getDate()+1)){const key=keyOf(d),plans=dashboard.calendar.filter(r=>day(r.date)===key),actual=dashboard.calendar_activities.filter(r=>day(r.start_time)===key);html+=`<button class="calendar-cell ${key===keyOf(new Date())?'today':''}" data-calendar-date="${key}" aria-label="${key}, ${plans.length} planned, ${actual.length} recorded"><strong>${d.getDate()}</strong>${plans.map(r=>`<span class="plan-chip">${esc(r.name)} · ${esc(r.status)}</span>`).join('')}${actual.map(r=>`<span class="activity-chip">${esc(r.name||r.type)}</span>`).join('')}</button>`;}
  $('calendar-grid').innerHTML=html;document.querySelectorAll('[data-calendar-date]').forEach(b=>b.onclick=()=>{calendarDay=b.dataset.calendarDate;renderCalendarDay();$('calendar-day-title').scrollIntoView({behavior:'smooth',block:'center'});});
  if(!calendarDay||calendarDay<keyOf(first)||calendarDay>keyOf(last)){const t=new Date();calendarDay=t>=first&&t<=last?keyOf(t):keyOf(first);}renderCalendarDay();
  $('calendar-table').innerHTML=plansIn.length?tableMarkup(plansIn.map(r=>({date:dateLabel(r.date),name:r.name,sport:r.sport||'—',time:valid(r.duration_s)?finishTime(r.duration_s):'—',km:valid(r.distance_m)?fmt(r.distance_m/1000):'—',status:r.status,confidence:r.confidence||'—',matched:r.matched_activity_name||'—',manual:r.manual_override?'Yes':''})),[['date','Date'],['name','Workout'],['sport','Sport'],['time','Planned time'],['km','Planned km'],['status','Status'],['confidence','Match confidence'],['matched','Matched activity'],['manual','Manual']]):'<p class="muted">No scheduled workouts in this range.</p>';
  const err=dashboard.sync_state.scheduled_workouts_error,at=dashboard.sync_state.scheduled_workouts_last_synced_at;
  $('calendar-note').className=err?'notice':'muted';
  $('calendar-note').textContent=err?`Garmin calendar refresh failed, so cached workouts are shown where available. ${err}`:at?`Garmin calendar last refreshed ${dateLabel(at)}. Completion matching uses the existing calendar engine; you can correct a match above.`:'Use Sync now to pull scheduled workouts from your Garmin calendar.';
}
function renderCalendarDay(){
  document.querySelectorAll('[data-calendar-date]').forEach(b=>b.classList.toggle('selected',b.dataset.calendarDate===calendarDay));const plans=dashboard.calendar.filter(r=>day(r.date)===calendarDay),actual=dashboard.calendar_activities.filter(r=>day(r.start_time)===calendarDay);$('calendar-day-title').textContent=dateLabel(calendarDay);$('calendar-day').innerHTML=actual.map(r=>`<p><button class="text-button" data-calendar-activity="${r.activity_id}">${esc(r.name||r.type)} ↗</button> ${fmt(valid(r.distance_m)?r.distance_m/1000:null)} km · ${valid(r.duration_s)?finishTime(r.duration_s):'—'}</p>`).join('');
  for(const p of plans){const box=document.createElement('div');box.className='workout-editor';const title=document.createElement('h3');title.textContent=p.name;const meta=document.createElement('p');meta.textContent=`${p.status} · ${p.manual_override?'manual override':p.confidence?`${p.confidence} match confidence`:'no match'}`;const status=document.createElement('select');status.setAttribute('aria-label','Workout completion status');['automatic','completed','missed','planned','skipped'].forEach(s=>status.add(new Option(s,s)));status.value=p.manual_override?p.status:'automatic';const match=document.createElement('select');match.setAttribute('aria-label','Matching activity');match.add(new Option('No activity selected',''));actual.forEach(r=>match.add(new Option(r.name||r.type,r.activity_id)));if(p.matched_activity_id&&!actual.some(r=>r.activity_id===p.matched_activity_id))match.add(new Option(p.matched_activity_name||'Existing matched activity',p.matched_activity_id));if(p.matched_activity_id)match.value=String(p.matched_activity_id);const button=document.createElement('button');button.textContent='Save status';button.onclick=async()=>{button.disabled=true;try{await post('/api/adherence',{scheduled_workout_id:p.scheduled_workout_id,status:status.value,matched_activity_id:match.value?Number(match.value):null});await refreshDashboard();}catch(e){meta.textContent=e.message;}finally{button.disabled=false;}};box.append(title,meta,status,match,button);$('calendar-day').append(box);}
  if(!plans.length&&!actual.length)$('calendar-day').textContent='No scheduled or recorded sessions on this day.';
  document.querySelectorAll('[data-calendar-activity]').forEach(b=>b.onclick=()=>{if(openActivity(b.dataset.calendarActivity))return;pendingActivity=b.dataset.calendarActivity;$('start').value=calendarDay;$('end').value=calendarDay;$('activity-sport').value='All sports';$('activity-search').value='';location.hash='activities';load();});
}
async function post(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Garmin-Request':'local-web'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw new Error(result.error||'The action failed.');return result;}
const aiPrompts=['What are the most important signals in the insight engine report?','What should I focus on for recovery this week?','Summarize my training consistency, load risk, and sport-specific changes.'];
let aiPreviewKey='';
function aiPayload(){return {start:$('start').value,end:$('end').value,target:{distance:$('goal-distance').value,finish_time:$('goal-time-label').textContent,weeks:$('goal-weeks').value}};}
async function loadAiPreview(){const key=JSON.stringify(aiPayload());if(key===aiPreviewKey)return;$('ai-preview').textContent='Loading…';try{const result=await post('/api/ai-preview',aiPayload());aiPreviewKey=key;$('ai-preview').textContent=JSON.stringify(result.summary,null,2);}catch(e){$('ai-preview').textContent=e.message;}}
function renderAI(){const ai=dashboard.ai;$('ai-provider').textContent=ai.configured?`Using ${ai.provider} · ${ai.model}`:'AI is not configured. Open Connections to set up Claude or Codex.';['chat-send','chat-input','ai-summary'].forEach(id=>$(id).disabled=!ai.configured||chatBusy);
  $('ai-focus').innerHTML=dashboard.insights.insights.slice(0,5).map(i=>`<li><strong>${esc(i.category)}</strong>: ${esc(i.detail)}</li>`).join('')||'<li class="muted">No priority signals for this range.</li>';
  $('ai-prompts').innerHTML=aiPrompts.map((t,i)=>`<button class="chip" data-prompt="${i}" ${!ai.configured||chatBusy?'disabled':''}>${esc(t)}</button>`).join('');$('ai-prompts').querySelectorAll('button').forEach(b=>b.onclick=()=>sendChat(aiPrompts[b.dataset.prompt]));
  if($('ai-preview-details').open)loadAiPreview();
  if(!$('context-form').dataset.dirty){$('context-primary').value=dashboard.goals.primary_goal||'';$('context-event').value=dashboard.goals.target_event||'';$('context-constraints').value=dashboard.goals.constraints||'';}renderMessages();}
// Minimal, safe markdown for chat replies: text is HTML-escaped before any markup is added.
function inlineMd(text){
  return esc(text)
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*]+?)\*\*/g,'<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?![*\w])/g,'$1<em>$2</em>')
    .replace(/(^|[^_\w])_([^_\s][^_]*?)_(?![_\w])/g,'$1<em>$2</em>');
}
function markdown(source){
  const lines=String(source||'').replace(/\r\n/g,'\n').split('\n'),out=[];
  let i=0;
  const isRow=l=>/^\s*\|.*\|\s*$/.test(l),isRule=l=>/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells=l=>l.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
  while(i<lines.length){
    const line=lines[i];
    if(/^\s*```/.test(line)){const code=[];i++;while(i<lines.length&&!/^\s*```/.test(lines[i]))code.push(lines[i++]);i++;out.push('<pre>'+esc(code.join('\n'))+'</pre>');continue;}
    if(!line.trim()){i++;continue;}
    let m;
    if((m=line.match(/^(#{1,6})\s+(.*)$/))){const level=Math.min(m[1].length+2,6);out.push(`<h${level}>${inlineMd(m[2])}</h${level}>`);i++;continue;}
    if(/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)){out.push('<hr>');i++;continue;}
    if(isRow(line)&&i+1<lines.length&&isRule(lines[i+1])){
      const head=cells(line);i+=2;const body=[];while(i<lines.length&&isRow(lines[i]))body.push(cells(lines[i++]));
      out.push('<div class="md-table"><table><thead><tr>'+head.map(c=>`<th>${inlineMd(c)}</th>`).join('')+'</tr></thead><tbody>'+body.map(r=>'<tr>'+r.map(c=>`<td>${inlineMd(c)}</td>`).join('')+'</tr>').join('')+'</tbody></table></div>');continue;
    }
    if(/^\s*([-*•])\s+/.test(line)||/^\s*\d+[.)]\s+/.test(line)){
      const ordered=/^\s*\d+[.)]\s+/.test(line),items=[];
      while(i<lines.length){
        const bullet=lines[i].match(ordered?/^\s*\d+[.)]\s+(.*)$/:/^\s*[-*•]\s+(.*)$/);
        if(bullet){items.push(bullet[1]);i++;}
        else if(items.length&&/^\s{2,}\S/.test(lines[i])){items[items.length-1]+='\n'+lines[i].trim();i++;}
        else break;
      }
      const tag=ordered?'ol':'ul';out.push(`<${tag}>`+items.map(t=>'<li>'+inlineMd(t).replace(/\n/g,'<br>')+'</li>').join('')+`</${tag}>`);continue;
    }
    const para=[line];i++;
    while(i<lines.length&&lines[i].trim()&&!/^(\s*```|#{1,6}\s|\s*([-*•]|\d+[.)])\s+)/.test(lines[i])&&!isRow(lines[i]))para.push(lines[i++]);
    out.push('<p>'+inlineMd(para.join('\n')).replace(/\n/g,'<br>')+'</p>');
  }
  return out.join('');
}
function renderMessages(){$('chat-messages').replaceChildren();chatMessages.forEach(message=>{const item=document.createElement('article');item.className='chat-message '+message.role;const label=document.createElement('strong'),text=document.createElement('div');label.textContent=message.role==='user'?'You':'Training analyst';if(message.role==='user')text.textContent=message.content;else{text.className='md';text.innerHTML=markdown(message.content);}item.append(label,text);$('chat-messages').append(item);});}
async function sendChat(text){if(chatBusy||!text.trim())return;chatBusy=true;const previous=chatMessages.slice();chatMessages.push({role:'user',content:text.trim()});renderAI();$('chat-status').textContent='Looking at your history…';try{const result=await post('/api/chat',{messages:chatMessages.slice(-29),...aiPayload()});chatMessages.push({role:'assistant',content:result.answer});$('chat-input').value='';$('chat-status').textContent='';}catch(error){chatMessages=previous;$('chat-status').textContent=error.message;}finally{chatBusy=false;renderAI();}}
let syncTimer;
async function pollSync(){try{const response=await fetch('/api/sync-status');if(!response.ok)throw new Error('Could not check sync status.');const state=await response.json();$('sync-note').textContent=state.message;$('sync-now').disabled=state.running;if(state.running)syncTimer=setTimeout(pollSync,2000);else{await load();$('sync-note').textContent=state.message;}}catch(error){$('sync-note').textContent=error.message;$('sync-now').disabled=false;}}
$('sync-now').onclick=async()=>{if(!$('sync-days').reportValidity())return;$('sync-now').disabled=true;try{const result=await post('/api/sync',{days:Number($('sync-days').value)});$('sync-note').textContent=result.message;clearTimeout(syncTimer);pollSync();}catch(error){$('sync-note').textContent=error.message;$('sync-now').disabled=false;}};
$('date-preset').onchange=()=>{const value=$('date-preset').value;if(value==='custom')return;if(value==='all'){load(true);return;}const end=new Date(),start=new Date();start.setDate(end.getDate()-Number(value)+1);$('start').value=localDay(start);$('end').value=localDay(end);load();};
['start','end'].forEach(id=>$(id).addEventListener('change',()=>{$('date-preset').value='custom';}));$('group-period').onchange=renderPage;$('show-other').onchange=renderPage;$('ai-preview-details').ontoggle=()=>{if($('ai-preview-details').open)loadAiPreview();};
['trend-metric','trend-mode'].forEach(id=>$(id).onchange=renderTrends);$('trend-export').onclick=()=>downloadRows(dailyRows(),'garmin-daily.csv');
$('training-sport').onchange=()=>{typeFilter.training=null;renderTraining();};$('race-distance').onchange=renderTraining;
$('activity-sport').onchange=()=>{typeFilter.activities=null;renderActivities();};$('activity-search').oninput=renderActivities;$('activity-select').onchange=renderActivity;$('activity-export').onclick=()=>downloadRows(filteredActivities(),'garmin-activities.csv');
const shiftCalendar=n=>{calendarMonth=calendarView==='month'?new Date(calendarMonth.getFullYear(),calendarMonth.getMonth()+n,1):new Date(calendarMonth.getFullYear(),calendarMonth.getMonth(),calendarMonth.getDate()+7*n);renderCalendar();};$('month-prev').onclick=()=>shiftCalendar(-1);$('month-next').onclick=()=>shiftCalendar(1);$('calendar-view').onchange=()=>{calendarView=$('calendar-view').value;renderCalendar();};$('month-today').onclick=()=>{calendarMonth=new Date();calendarDay=localDay(calendarMonth);renderCalendar();};
$('context-form').oninput=()=>{$('context-form').dataset.dirty='true';};$('context-form').onsubmit=async event=>{event.preventDefault();try{const result=await post('/api/goals',{primary_goal:$('context-primary').value,target_event:$('context-event').value,constraints:$('context-constraints').value});delete $('context-form').dataset.dirty;$('context-result').textContent=result.message;await refreshDashboard();}catch(error){$('context-result').textContent=error.message;}};
$('chat-form').onsubmit=event=>{event.preventDefault();sendChat($('chat-input').value);};$('ai-summary').onclick=()=>sendChat('Summarize the most important evidence in this selected period, in the context of my goal.');$('ai-clear').onclick=()=>{if(!chatBusy){chatMessages=[];renderMessages();}};
// Connections page: Garmin sign-in, AI provider (Claude/Codex CLI) and auto-sync settings.
let connections=null,garminMfa=false,cliLoginTimer=null,connectionsRequest=0,connectionsLoading=false;
const providerNotes={claude:'Uses your Claude Pro or Max subscription.',codex:'Uses your ChatGPT Plus, Pro or Business plan.'};
const installHelp={claude:'Install Claude Code from claude.com/claude-code',codex:'Install with npm install -g @openai/codex'};
function badge(id,text,state){const el=$(id);el.textContent=text;el.dataset.state=state;}
async function loadConnections(force=false){if(connectionsLoading&&!force)return;connectionsLoading=true;const request=++connectionsRequest;try{const response=await fetch('/api/connections');if(response.status===404)throw new Error('The running server is an older version. Restart it (python -m garmin_ai.web_server) to use this page.');if(!response.ok)throw new Error('Could not check connections.');const result=await response.json();if(request!==connectionsRequest)return;connections=result;renderConnections();}catch(error){badge('garmin-badge','Unavailable','off');badge('ai-badge','Unavailable','off');$('ai-result').textContent=error.message;}finally{if(request===connectionsRequest)connectionsLoading=false;}}
function renderConnections(){renderGarmin();renderSyncSettings();renderAiProviders();}
function renderGarmin(){
  const g=connections.garmin,body=$('garmin-body');garminMfa=garminMfa||g.awaiting_mfa;
  if(g.connected&&!garminMfa){
    badge('garmin-badge','Connected','on');
    body.innerHTML=`<dl class="conn-facts"><dt>Account</dt><dd>${esc(g.display_name||g.email||'Signed in')}</dd>${g.display_name&&g.email?`<dt>Email</dt><dd>${esc(g.email)}</dd>`:''}<dt>Last sync</dt><dd>${g.last_synced_at?`${esc(syncedLabel(g.last_synced_at))}`:'Not synced yet'}</dd></dl><div class="conn-footer"><button class="primary icon-button" id="garmin-sync"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.9-3M4 13a8 8 0 0 0 14.9 3"/><path d="M4 4v4h4M20 20v-4h-4"/></svg><span>Sync now</span></button><span class="conn-footer-note" id="garmin-sync-note">Fetches the last ${Number($('sync-days').value)||7} days</span><button class="ghost-danger icon-button" id="garmin-logout"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l-5-5 5-5M5 12h11"/></svg><span>Sign out</span></button></div>`;
    $('garmin-sync').onclick=()=>syncFromCard();
    $('garmin-logout').onclick=async()=>{if(!confirm('Sign out of Garmin? Your synced history stays on this computer.'))return;try{connections=await post('/api/connections/garmin-logout',{});$('garmin-result').textContent='Signed out.';renderConnections();}catch(e){$('garmin-result').textContent=e.message;}};
    return;
  }
  if(garminMfa){
    badge('garmin-badge','Verification needed','pending');
    body.innerHTML=`<form id="garmin-mfa-form" class="conn-form"><label>Code from Garmin<input id="garmin-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{4,10}" maxlength="10" required></label><button type="submit" class="primary">Verify</button><button type="button" class="text-button" id="garmin-cancel">Start over</button></form><p class="muted small">Garmin sent a verification code to your email or authenticator app.</p>`;
    $('garmin-code').focus();
    $('garmin-cancel').onclick=()=>{garminMfa=false;connections.garmin.awaiting_mfa=false;renderGarmin();};
    $('garmin-mfa-form').onsubmit=async event=>{event.preventDefault();await garminAction(event.submitter,'/api/connections/garmin-mfa',{code:$('garmin-code').value},'Verifying…');};
    return;
  }
  badge('garmin-badge','Not connected','off');
  body.innerHTML=`<form id="garmin-login-form" class="conn-form stacked"><label>Email<input id="garmin-email" type="email" autocomplete="username" required value="${esc(g.email)}"></label><label>Password<input id="garmin-password" type="password" autocomplete="current-password" required maxlength="256"></label><button type="submit" class="primary">Sign in to Garmin</button></form><p class="muted small">Your password is sent only to Garmin to get a sign-in token, then discarded.</p>`;
  $('garmin-login-form').onsubmit=async event=>{event.preventDefault();await garminAction(event.submitter,'/api/connections/garmin-login',{email:$('garmin-email').value,password:$('garmin-password').value},'Signing in…');};
}
function syncedLabel(value){const then=new Date(value),mins=Math.round((Date.now()-then)/60000);if(isNaN(mins))return dateLabel(value);const rel=mins<1?'just now':mins<60?`${mins} min ago`:mins<1440?`${Math.round(mins/60)} h ago`:`${Math.round(mins/1440)} d ago`;return `${then.toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})} · ${rel}`;}
async function syncFromCard(){
  const button=$('garmin-sync'),note=$('garmin-sync-note');button.disabled=true;button.classList.add('spinning');button.querySelector('span').textContent='Syncing…';
  $('sync-now').click();
  const finish=async()=>{try{const state=await fetch('/api/sync-status').then(r=>r.json());if(state.running){if($('garmin-sync-note'))$('garmin-sync-note').textContent=state.message;setTimeout(finish,2000);return;}await loadConnections(true);$('garmin-result').textContent=state.message;}catch(e){$('garmin-result').textContent=e.message;}};
  note.textContent='Starting sync…';setTimeout(finish,1500);
}
async function garminAction(button,path,body,busy){
  button.disabled=true;$('garmin-result').textContent=busy;
  try{const result=await post(path,body);connections=result;garminMfa=!!result.mfa_required;$('garmin-result').textContent=garminMfa?'':result.garmin.connected?'Connected to Garmin. Sync to pull in your history.':'';renderConnections();if(result.garmin.connected&&!garminMfa)refreshDashboard();}
  catch(error){$('garmin-result').textContent=error.message;button.disabled=false;}
}
function renderSyncSettings(){
  const g=connections.garmin,select=$('sync-interval');
  if(![...select.options].some(o=>Number(o.value)===g.auto_sync_interval_min))select.add(new Option(`${g.auto_sync_interval_min} minutes`,g.auto_sync_interval_min));
  if(!$('sync-settings').dataset.dirty){select.value=String(g.auto_sync_interval_min);$('sync-window').value=g.auto_sync_days;}
  badge('sync-badge',g.auto_sync_interval_min>0?'On':'Off',g.auto_sync_interval_min>0?'on':'off');
}
function renderAiProviders(){
  const ai=connections.ai,chosen=$('provider-options').dataset.choice||ai.provider||'claude',active=ai.providers[ai.provider];
  if(!$('ai-settings').dataset.dirty)$('ai-model').value=ai.model;
  const ready=active?.installed&&active.logged_in;
  badge('ai-badge',ready?'Connected':active?.installed?'Sign-in needed':'Not set up',ready?'on':active?.installed?'pending':'off');
  $('provider-options').innerHTML=Object.entries(ai.providers).map(([key,p])=>{
    const state=!p.installed?`<span class="muted">Not installed · ${esc(installHelp[key])}</span>`:p.logged_in?`<span class="ok">✓ Signed in</span>${p.account?` · ${esc(p.account)}`:''}`:p.signing_in?'<span class="pending">Waiting for you to finish signing in in your browser…</span>':'<span class="pending">Not signed in</span>';
    const action=p.installed&&!p.logged_in?`<button type="button" data-cli-login="${key}" ${p.signing_in?'disabled':''}>${p.signing_in?'Signing in…':'Sign in'}</button>`:'';
    return `<label class="provider${key===chosen?' selected':''}"><input type="radio" name="ai-provider" value="${key}" aria-label="${esc(p.label)}" ${key===chosen?'checked':''}><span class="provider-text"><strong>${esc(p.label)}${key===ai.provider?' <span class="muted small">· in use</span>':''}</strong><span class="small muted">${esc(providerNotes[key])}${p.version?' · '+esc(p.version):''}</span><span class="small">${state}</span></span>${action}</label>`;
  }).join('');
  $('provider-options').querySelectorAll('input').forEach(input=>input.onchange=()=>{$('provider-options').dataset.choice=input.value;renderAiProviders();});
  $('provider-options').querySelectorAll('[data-cli-login]').forEach(button=>button.onclick=async event=>{event.preventDefault();button.disabled=true;try{await post('/api/connections/cli-login',{provider:button.dataset.cliLogin});$('ai-result').textContent='A browser window opened to sign in. Finish there; this page updates automatically.';watchCliLogin(button.dataset.cliLogin,Date.now());}catch(e){$('ai-result').textContent=e.message;button.disabled=false;}});
}
function watchCliLogin(provider,started){clearTimeout(cliLoginTimer);cliLoginTimer=setTimeout(async()=>{await loadConnections(true);const p=connections?.ai.providers[provider];if(p?.logged_in){$('ai-result').textContent=`Signed in to ${p.label}.`;return;}if(p?.signing_in&&Date.now()-started<300000)watchCliLogin(provider,started);else $('ai-result').textContent=`Sign-in did not finish. You can also run "${p?.command||provider}" in a terminal to sign in.`;},3000);}
$('ai-settings').oninput=()=>{$('ai-settings').dataset.dirty='true';};
$('ai-settings').onsubmit=async event=>{event.preventDefault();const provider=$('provider-options').querySelector('input:checked')?.value;try{connections=await post('/api/connections/ai',{provider,model:$('ai-model').value});delete $('ai-settings').dataset.dirty;delete $('provider-options').dataset.choice;$('ai-result').textContent='Saved.';renderConnections();refreshDashboard();}catch(e){$('ai-result').textContent=e.message;}};
$('ai-test').onclick=async()=>{$('ai-test').disabled=true;$('ai-result').textContent='Asking the AI coach… (uses the saved provider)';try{const result=await post('/api/connections/ai-test',{});$('ai-result').textContent=`✓ Replied in ${result.seconds}s: “${result.answer}”`;}catch(e){$('ai-result').textContent=e.message;}finally{$('ai-test').disabled=false;}};
$('connections-refresh').onclick=()=>{$('ai-result').textContent='';loadConnections(true);};
$('sync-settings').oninput=()=>{$('sync-settings').dataset.dirty='true';};
$('sync-settings').onsubmit=async event=>{event.preventDefault();try{connections=await post('/api/connections/sync',{interval_min:Number($('sync-interval').value),days:Number($('sync-window').value)});delete $('sync-settings').dataset.dirty;$('sync-result').textContent='Saved.';renderConnections();}catch(e){$('sync-result').textContent=e.message;}};
window.addEventListener('hashchange',navigate);window.addEventListener('history-loaded',refreshDashboard);navigate();if(data)refreshDashboard();

// Pick up background (auto) syncs: reload when one starts or finishes.
let knownFinished=null;
setInterval(async()=>{if(document.hidden)return;try{const r=await fetch('/api/sync-status');if(!r.ok)return;const state=await r.json();if(knownFinished===null)knownFinished=state.finished;if(state.running){$('sync-now').disabled=true;$('sync-note').textContent=state.message;}else if(state.finished!==knownFinished){knownFinished=state.finished;$('sync-now').disabled=false;await load();$('sync-note').textContent=state.message;}}catch{}},30000);

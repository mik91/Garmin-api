'use strict';
const $ = id => document.getElementById(id);
let data, view = 'efficiency', selected = null, requestId = 0, controller, syncing = false, plotVersion = 0;
const green = '#9fd356', blue = '#5aa9e6', muted = '#8b97a8';
const valid = x => typeof x === 'number' && Number.isFinite(x);
const median = values => {const a = values.filter(valid).sort((a,b)=>a-b); return a.length ? (a[Math.floor((a.length-1)/2)] + a[Math.floor(a.length/2)])/2 : null;};
const fmt = (x, digits=1) => valid(x) ? x.toFixed(digits) : '—';
const pace = x => {if(!valid(x)) return '—'; const seconds=Math.round(x*60);return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;};
const dateLabel = value => new Date(value.slice(0,10)+'T12:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
const efficiency = r => valid(r.pace_min_km) && valid(r.avg_hr) && r.avg_hr > 0 ? (1000 / r.pace_min_km) / r.avg_hr : null;
const rows = () => view==='recovery' ? data.recovery : data.runs;
const keyDate = r => r.date.slice(0,10);
const config = {responsive:true,scrollZoom:true,displaylogo:false,displayModeBar:false};
function layout(y, height) {
  return {height,margin:{l:65,r:28,t:20,b:40},paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{family:'Segoe UI, Arial',size:12,color:muted},showlegend:false,hovermode:'closest',dragmode:'zoom',
    xaxis:{gridcolor:'#2c313a',zeroline:false,tickfont:{size:12}},yaxis:{title:{text:y,font:{size:12}},gridcolor:'#2c313a',zeroline:false},hoverlabel:{bgcolor:'#1b2433',font:{color:'#e6ebf2'}}};
}
async function load(all=false) {
  const id=++requestId;
  if(controller) controller.abort(); controller=new AbortController();
  document.body.classList.add('loading');$('status').classList.remove('error');$('status').textContent='Updating your observations…';
  const query=new URLSearchParams();
  for(const name of ['min_km','max_km','min_hr','max_hr','climb','lag'])query.set(name,$(name).value);
  if(!all && $('start').value && $('end').value){query.set('start',$('start').value);query.set('end',$('end').value);}
  try {
    const response=await fetch('/api/discoveries?'+query,{signal:controller.signal});
    if(!response.ok)throw new Error(response.status===400?'Check your dates and filters. Minimums must not exceed maximums.':'Could not load your history. Try applying the filters again.');
    const next=await response.json();if(id!==requestId)return;data=next;data.runs.forEach(r=>{r.efficiency=efficiency(r);});
    $('start').value=data.start;$('end').value=data.end;
    $('status').textContent=`${data.runs.length} qualifying run days · ${dateLabel(data.start)} – ${dateLabel(data.end)}`;
    updateGoal(); await render(); window.dispatchEvent(new Event("history-loaded"));
  }catch(error){if(error.name!=='AbortError' && id===requestId){$('status').classList.add('error');$('status').textContent=error.message;const globalStatus=document.getElementById('dashboard-status');if(globalStatus)globalStatus.textContent=error.message;}}
  finally{if(id===requestId)document.body.classList.remove('loading');}
}
function trace(list,x,y,color){return {type:'scatter',mode:'markers',x:list.map(x),y:list.map(y),customdata:list.map(r=>r._key||keyDate(r)),text:list.map(r=>r.n?`${aggMode==='week'?'Week of':'Month of'} ${dateLabel(r.date)} · ${r.n} run${r.n>1?'s':''}`:dateLabel(r.date)),marker:{size:9,color,line:{width:1,color:'#1a1d22'},opacity:.9},hovertemplate:'%{text}<br>%{y:.2f}<extra></extra>'};}
let aggMode='week', plotCtx={list:[],agg:false}, subRun=null;
function aggregate(list){
  if(aggMode==='run'||view!=='efficiency')return list;
  const groups=new Map();
  list.forEach(r=>{const d=new Date(keyDate(r)+'T00:00:00Z');if(aggMode==='week')d.setUTCDate(d.getUTCDate()-(d.getUTCDay()+6)%7);else d.setUTCDate(1);const k=d.toISOString().slice(0,10);if(!groups.has(k))groups.set(k,[]);groups.get(k).push(r);});
  return [...groups].sort((a,b)=>a[0].localeCompare(b[0])).map(([k,rs])=>{const f=key=>median(rs.map(r=>r[key]));return {date:k,_key:keyDate(rs[rs.length-1]),n:rs.length,pace_min_km:f('pace_min_km'),avg_hr:f('avg_hr'),efficiency:f('efficiency'),distance_km:f('distance_km'),runs:rs};});
}
async function render(){
  if(!data)return;
  const version=++plotVersion, list=rows(), plotList=aggregate(list), agg=plotList!==list;
  plotCtx={list:plotList,agg};
  $('agg-wrap').hidden=view!=='efficiency';
  $('lag-wrap').hidden=view!=='recovery';
  document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-pressed',String(b.dataset.view===view));});
  const chartLayout=layout(view==='recovery'?'HRV change (ms)':'Pace (min/km)', $('main-chart').clientHeight || 370);
  let main, companion;
  if(view==='efficiency'){
    $('chart-title').textContent=agg?`${aggMode==='week'?'Weekly':'Monthly'} pace trend`:'Pace through your training';
    $('chart-subtitle').textContent=agg?'Median pace of qualifying runs per period; bigger dots mean more runs. Lower is faster.':'Lower means faster. Select a run or drag to zoom into a period.';
    main=trace(plotList,r=>r.date,r=>r.pace_min_km,plotList.map(r=>r.avg_hr));
    if(agg){main.mode='lines+markers';main.line={color:'#334155',width:2};main.marker.size=plotList.map(r=>Math.min(8+r.n*2,26));main.hovertemplate='%{text}<br>%{y:.2f} min/km<extra></extra>';}
    main.marker.colorscale=[[0,blue],[1,green]];main.marker.showscale=false;
    main.hovertemplate='%{text}<br>%{y:.2f} min/km<extra></extra>';
  }else{
    $('chart-title').textContent='Your response to training load';
    $('chart-subtitle').textContent='HRV change from the activity morning. Green marks higher-load days.';
    main=trace(list,r=>r.date,r=>r.hrv_change,list.map(r=>r.group==='Higher load'?green:blue));
    chartLayout.yaxis.zeroline=true;chartLayout.yaxis.zerolinecolor='#475569';
    main.hovertemplate='%{text}<br>HRV change %{y:.1f} ms<extra></extra>';
  }
  chartLayout.xaxis.type='date';
  const companionLayout=layout(view==='recovery'?'Garmin load':'m/min per bpm',155);
  companionLayout.margin.t=8;companionLayout.xaxis.type='date';
  if(view==='recovery'){
    $('companion-title').textContent='Training load on those days';
    companion=trace(list,r=>r.date,r=>r.load,blue);
  }else{
    $('companion-title').textContent=agg?'Aerobic efficiency (median per period)':'Aerobic efficiency per run';
    companion=trace(plotList,r=>r.date,r=>r.efficiency,blue);
    companion.hovertemplate='%{text}<br>%{y:.3f} m/min per bpm<extra></extra>';
    if(agg){companion.mode='lines+markers';companion.line={color:'#334155',width:2};}
  }
  document.querySelector('.dot-label').textContent=agg?'Higher is better · shared date zoom':'Same days · shared date zoom';
  if(!list.length)chartLayout.annotations=[{text:'No matching observations. Broaden your dates or filters.',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false,font:{color:muted}}];
  await Promise.all([Plotly.react('main-chart',[main],chartLayout,config),Plotly.react('companion',[companion],companionLayout,config)]);
  if(version!==plotVersion)return;
  for(const id of ['main-chart','companion']){
    const graph=$(id);graph.removeAllListeners('plotly_click');graph.removeAllListeners('plotly_relayout');
    graph.on('plotly_click', event=>select(event.points[0].customdata));
    graph.on('plotly_relayout',event=>syncZoom(id,event));
  }
  $('sample').textContent=`${agg?plotList.length+' '+(aggMode==='week'?'weeks':'months')+' · ':''}${list.length} observations · ${view==='efficiency'?'Color: lower HR (blue) → higher HR (green)':'Click a point to inspect'}`;
  $('observation').replaceChildren();
  const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='Choose a day';$('observation').append(placeholder);
  [...list].reverse().forEach(r=>{const option=document.createElement('option');option.value=keyDate(r);option.textContent=dateLabel(r.date);$('observation').append(option);});
  finding(list);evidence(list);
  select(list.some(r=>keyDate(r)===selected)?selected:list.length?keyDate(list[list.length-1]):null);
}
async function syncZoom(source,event){
  if(syncing)return;
  const target=source==='main-chart'?'companion':'main-chart';let update;
  if(event['xaxis.range[0]']!==undefined)update={'xaxis.range':[event['xaxis.range[0]'],event['xaxis.range[1]']]};
  else if(event['xaxis.autorange'])update={'xaxis.autorange':true};
  if(!update)return;syncing=true;try{await Plotly.relayout(target,update);}finally{syncing=false;}
}
function runDetail(r){
  const stat=(label,value,unit)=>`<div><span>${label}</span><strong>${value}</strong> <small>${unit}</small></div>`;
  return `<div class="big-value">${pace(r.pace_min_km)}<small>/km</small></div><div class="detail-grid">${stat('Distance',fmt(r.distance_km),'km')}${stat('Average HR',fmt(r.avg_hr,0),'bpm')}${stat('Efficiency',fmt(r.efficiency,3),'m/min/bpm')}${stat('Overnight HRV',fmt(r.hrv_overnight,0),'ms')}${stat('Resting HR',fmt(r.resting_hr,0),'bpm')}${stat('Climbing',fmt(r.climb_m_per_km),'m/km')}</div>`;
}
function periodLabel(g){return aggMode==='month'?new Date(g.date+'T12:00:00').toLocaleDateString(undefined,{month:'long',year:'numeric'}):'Week of '+dateLabel(g.date);}
function select(date,run=null){
  selected=date;const list=rows(),r=list.find(r=>keyDate(r)===date);$('observation').value=date||'';
  if(!r){$('selected-date').textContent='No observation';$('detail').innerHTML='<p class="muted">Broaden your filters to see more history.</p>';return;}
  const stat=(label,value,unit)=>`<div><span>${label}</span><strong>${value}</strong> <small>${unit}</small></div>`;
  const group=plotCtx.agg?plotCtx.list.find(g=>g.runs.some(x=>keyDate(x)===date)):null;
  let sizes;
  if(group){
    subRun=run&&group.runs.some(x=>keyDate(x)===run)?run:null;
    $('selected-date').textContent=periodLabel(group);
    const total=group.runs.reduce((a,x)=>a+(valid(x.distance_km)?x.distance_km:0),0);
    const rowsHtml=[...group.runs].reverse().map(x=>`<button class="run-row${keyDate(x)===subRun?' on':''}" data-run="${keyDate(x)}"><span>${dateLabel(x.date)}</span><span>${fmt(x.distance_km)} km</span><span>${pace(x.pace_min_km)} /km</span><span>${fmt(x.avg_hr,0)} bpm</span></button>`).join('');
    const sub=subRun?runDetail(group.runs.find(x=>keyDate(x)===subRun)):'';
    $('detail').innerHTML=`<div class="big-value">${pace(group.pace_min_km)}<small>/km median</small></div><div class="detail-grid">${stat('Runs',group.n,'')}${stat('Total distance',fmt(total),'km')}${stat('Median HR',fmt(group.avg_hr,0),'bpm')}${stat('Efficiency',fmt(group.efficiency,3),'m/min/bpm')}</div><div class="run-list-head">Runs in this ${aggMode==='month'?'month':'week'}</div><div class="run-list" role="list">${rowsHtml}</div>${sub?`<div class="run-sub">${sub}</div>`:''}`;
    $('detail').querySelectorAll('[data-run]').forEach(b=>b.onclick=()=>select(date,subRun===b.dataset.run?null:b.dataset.run));
    sizes=plotCtx.list.map(g=>Math.min(8+g.n*2,26)+(g===group?6:0));
  }else{
    subRun=null;$('selected-date').textContent=dateLabel(r.date);
    $('detail').innerHTML=view==='recovery'?`<div class="big-value">${fmt(r.hrv_change,0)}<small>ms change</small></div><div class="detail-grid">${stat('Training load',fmt(r.load,0),'')}${stat('Resting HR change',fmt(r.resting_hr_change,0),'bpm')}</div><p class="muted">${r.group}</p><p class="muted">Observed ${dateLabel(r.outcome_date)}</p>`:runDetail(r);
    sizes=list.map(item=>keyDate(item)===date?15:9);
  }
  Plotly.restyle('main-chart',{'marker.size':[sizes]});Plotly.restyle('companion',{'marker.size':[sizes]});
}
function finding(list){
  let first,second,metric,label,unit,method;
  if(view==='recovery'){
    first=list.filter(r=>r.group==='Higher load');second=list.filter(r=>r.group!=='Higher load');metric='hrv_change';label='Higher-load days';unit='ms';
    method='Higher load is above the 75th percentile of total Garmin activity load on active days in this date range, across all sports. Missing recovery days are excluded. Other exercise during the recovery window may affect the result.';
  }else{
    const midpoint=(new Date(data.start).getTime()+new Date(data.end).getTime())/2;
    first=list.filter(r=>new Date(r.date).getTime()>midpoint);second=list.filter(r=>new Date(r.date).getTime()<=midpoint);metric='pace_min_km';label='The later half of this period';unit='min/km';
    method='Compares median pace in the later and earlier halves of the selected dates. Standard and treadmill runs only, with at most one qualifying run per day. Heart rate, distance and climb filters narrow the comparison; they do not fully control conditions. Aerobic efficiency is speed (m/min) divided by average heart rate; higher means more speed per heartbeat.';
  }
  first=first.filter(r=>valid(r[metric]));second=second.filter(r=>valid(r[metric]));
  if(Math.min(first.length,second.length)<5){$('finding-title').textContent='More comparable days needed';$('finding').textContent=`The two groups have ${first.length} and ${second.length} valid days. Each needs at least 5 before a numerical comparison is shown.`;}
  else{const difference=median(first.map(r=>r[metric]))-median(second.map(r=>r[metric]));
    $('finding-title').textContent=Math.abs(difference)<.005?'No difference in group medians':`${fmt(Math.abs(difference),2)} ${unit} ${difference>0?'higher':'lower'}`;
    $('finding').textContent=`${label}: ${fmt(median(first.map(r=>r[metric])),2)} ${unit}, versus ${fmt(median(second.map(r=>r[metric])),2)} in the comparison group (${first.length} and ${second.length} days). ${metric==='pace_min_km'?'Lower pace means faster running.':''}`;
  }
  $('method').textContent=method+' Five days is a display minimum, not statistical validation. Comparisons use the full filtered range, even when you zoom.';
}
function evidence(list){
  const columns=view==='recovery'?[['date','Activity date'],['load','Garmin load'],['group','Load group'],['outcome_date','Recovery date'],['hrv_change','HRV change, ms'],['resting_hr_change','Resting HR change, bpm']]:[['date','Run date'],['distance_km','Distance, km'],['pace_min_km','Pace, min/km'],['avg_hr','Avg HR, bpm'],['efficiency','Efficiency, m/min per bpm'],['climb_m_per_km','Climb, m/km']];
  const table=document.createElement('table'),head=table.createTHead().insertRow();
  columns.forEach(([,name])=>{const th=document.createElement('th');th.textContent=name;head.append(th);});
  const body=table.createTBody();list.forEach(r=>{const row=body.insertRow();row.dataset.date=keyDate(r);row.addEventListener('click',()=>select(keyDate(r)));columns.forEach(([key])=>{const value=r[key];row.insertCell().textContent=key.includes('date')?dateLabel(value):valid(value)?fmt(value,2):value??'—';});});
  $('evidence').replaceChildren(table);
  $('export').onclick=()=>{const csv=[columns.map(([,label])=>label),...list.map(r=>columns.map(([key])=>r[key]??''))].map(row=>row.map(value=>'"'+String(value).replaceAll('"','""')+'"').join(',')).join('\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));const link=document.createElement('a');link.href=url;link.download=`garmin-${view}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
}
$('filters').addEventListener('submit',event=>{event.preventDefault();load();});
['start','end','lag'].forEach(id=>$(id).addEventListener('change',()=>load()));
$('all').addEventListener('click',()=>load(true));
$('observation').addEventListener('change',event=>select(event.target.value));
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{view=button.dataset.view;render();}));
$('agg').addEventListener('change',()=>{aggMode=$('agg').value;render();});
$('reset').addEventListener('click',async()=>{syncing=true;try{await Promise.all(['main-chart','companion'].map(id=>Plotly.relayout(id,{'xaxis.autorange':true,'yaxis.autorange':true})));}finally{syncing=false;}});
const goalDistances={'5K':5,'10K':10,'Half marathon':21.0975,'Marathon':42.195};
const goalLimits={'5K':[12,90,25],'10K':[25,150,50],'Half marathon':[60,240,105],'Marathon':[120,480,240]};
const finishTime=seconds=>{const value=Math.round(seconds);return `${Math.floor(value/3600)}:${String(Math.floor(value%3600/60)).padStart(2,'0')}:${String(value%60).padStart(2,'0')}`;};
function configureGoal(reset=false){
  const [low,high,defaultTime]=goalLimits[$('goal-distance').value];
  $('goal-time').min=low;$('goal-time').max=high;
  if(reset)$('goal-time').value=defaultTime;
  updateGoal();
}
function updateGoal(){
  const distance=$('goal-distance').value, seconds=Number($('goal-time').value)*60,weeks=Number($('goal-weeks').value);
  $('goal-time-label').textContent=finishTime(seconds);$('goal-exact').value=finishTime(seconds);$('goal-exact').setCustomValidity('');
  $('goal-weeks-label').textContent=`${weeks} week${weeks===1?'':'s'}`;
  const raceDate=new Date();raceDate.setDate(raceDate.getDate()+7*weeks);
  $('goal-date').textContent=raceDate.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
  $('goal-pace').textContent=pace(seconds/60/goalDistances[distance])+' /km';
  try{localStorage.setItem('garminGoal',JSON.stringify({distance,minutes:seconds/60,weeks}));}catch{}
  if(!data?.goal_context)return;
  const context=data.goal_context,model=context.model,prediction=model.predictions[distance],profile=model.training_profile;
  const profileText=`Recent six-week history: ${fmt(profile.weekly_km)} km/week, ${fmt(profile.runs_per_week)} runs/week, longest run ${fmt(profile.longest_run_km)} km.`;
  $('goal-current').textContent=prediction?prediction.expected:'Not established';
  const gap=prediction?prediction.expected_seconds-seconds:null;
  $('goal-gap').textContent=gap===null?'Needs race evidence':`${finishTime(Math.abs(gap))} ${gap>0?'to gain':'in hand'}`;
  $('goal-context-note').textContent=prediction?`Current model as of ${dateLabel(model.as_of)}: ${prediction.range.join('–')}. Based on a race/time-trial labelled activity on ${dateLabel(prediction.source_effort.date)} (${prediction.source_age_days} days ago). This is an indicative model range, not a calibrated probability. Latest daily data: ${context.latest_daily_date||'none'}.`:`No eligible race or time-trial labelled activity supports an independent finish-time estimate. Your target pace is available, but its feasibility is not established. Latest daily data: ${context.latest_daily_date||'none'}.`;
  const pairs=context.matched_runs,changes=pairs.map(r=>r.pace_change_seconds),change=median(changes);
  $('improvement-title').textContent=pairs.length<4?'More comparable sessions needed':Math.abs(change)<1?'Matched pace is essentially unchanged':`${fmt(Math.abs(change),0)} sec/km ${change<0?'faster':'slower'} in matched runs`;
  $('improvement-detail').textContent=`${pairs.length} distinct pairs across the last two 28-day periods. Matched within 3 bpm, 20% distance, and 5 m/km climbing. ${pairs.length<4?'At least four pairs are needed to display a pace change.':'The median paired change describes training pace, not proven fitness improvement.'} Workout purpose and weather remain uncontrolled.`;
  $('matched-evidence').replaceChildren();pairs.forEach(pair=>{const line=document.createElement('p');line.textContent=`${dateLabel(pair.earlier_date)} → ${dateLabel(pair.recent_date)}: ${pair.pace_change_seconds>=0?'+':''}${fmt(pair.pace_change_seconds,0)} sec/km; ${fmt(pair.earlier_hr,0)} → ${fmt(pair.recent_hr,0)} bpm; ${fmt(pair.earlier_km)} → ${fmt(pair.recent_km)} km.`;$('matched-evidence').append(line);});
  const metricLabels={hrv_overnight:['HRV','ms'],resting_hr:['Resting HR','bpm'],training_readiness:['Readiness','points'],stress_avg:['Stress','points']};
  const trends=Object.entries(context.recovery_trends).filter(([,value])=>valid(value.change));
  $('response-title').textContent=trends.length?'Your recent recovery signals':'Recent recovery evidence is sparse';
  $('response-detail').textContent=profileText+' '+trends.map(([name,value])=>`${metricLabels[name][0]} ${value.change>=0?'+':''}${fmt(value.change)} ${metricLabels[name][1]} (${value.recent_n} vs ${value.baseline_n} days)`).join('; ')+(trends.length?'. Last 7 days versus the preceding 21 days. These changes do not establish a response caused by training.':' At least 4 recent days and 10 baseline days per metric are needed.');
  const previous=context.previous_predictions[distance];
  if(!prediction){$('progress-title').textContent='Target set; feasibility unknown';$('progress-detail').textContent=`${distance} in ${finishTime(seconds)}, ${weeks} weeks from now. A correctly labelled recent race or time trial is needed to estimate the performance gap. ${profileText}`;}
  else{
    $('progress-title').textContent=seconds<prediction.range_low_seconds?'Faster than the current model range':seconds<=prediction.range_high_seconds?'Within the current model range':'More time than the current model range';
    let trend='There is no earlier estimate to establish whether the gap is closing.';
    if(previous){const delta=previous.expected_seconds-prediction.expected_seconds;trend=`The model estimate is ${finishTime(Math.abs(delta))} ${delta>=0?'faster':'slower'} than its estimate as of 28 days ago. Changes can reflect evidence age, training volume or a different source effort.`;}
    $('progress-detail').textContent=`${trend} ${weeks} weeks remain in this scenario; the model does not forecast gains over that time. ${profileText}`;
  }
}
try{const saved=JSON.parse(localStorage.getItem('garminGoal'));if(saved&&goalLimits[saved.distance]){const [low,high]=goalLimits[saved.distance];if(Number.isFinite(saved.minutes)&&saved.minutes>=low&&saved.minutes<=high&&Number.isInteger(saved.weeks)&&saved.weeks>=1&&saved.weeks<=52){$('goal-distance').value=saved.distance;$('goal-time').min=low;$('goal-time').max=high;$('goal-time').value=saved.minutes;$('goal-weeks').value=saved.weeks;}}}catch{}
$('goal-distance').addEventListener('change',()=>configureGoal(true));
['goal-time','goal-weeks'].forEach(id=>$(id).addEventListener('input',updateGoal));
$('goal-exact').addEventListener('change',()=>{const value=$('goal-exact').value.match(/^(\d{1,2}):([0-5]\d):([0-5]\d)$/);const minutes=value?Number(value[1])*60+Number(value[2])+Number(value[3])/60:NaN;const [low,high]=goalLimits[$('goal-distance').value];if(!Number.isFinite(minutes)||minutes<low||minutes>high){$('goal-exact').setCustomValidity(`Enter H:MM:SS between ${finishTime(low*60)} and ${finishTime(high*60)}.`);$('goal-exact').reportValidity();return;}$('goal-time').step='any';$('goal-time').value=minutes;updateGoal();});
configureGoal();load(true);

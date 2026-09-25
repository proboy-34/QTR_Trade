import {useState} from 'react'
import {request} from '../api'
import {num,when,type Obj} from '../format'
import {useData} from '../hooks'
import {Card,DataTable,Drawer,ErrorBanner,Metric,PageHeading,Status} from '../ui'
import './autonomy.css'

export default function IntelligencePage(){
  const [verification,setVerification]=useState('')
  const events=useData<{items:Obj[]}>(`/intelligence/events${verification?`?verification=${verification}`:''}`,{items:[]},60000)
  const providers=useData<Obj>('/intelligence/providers',{})
  const ai=useData<Obj>('/ai/status',{})
  const artifacts=useData<{items:Obj[]}>('/ai/artifacts',{items:[]})
  const [artifact,setArtifact]=useState<Obj|null>(null)
  const [busy,setBusy]=useState('');const [error,setError]=useState('');const [message,setMessage]=useState('')
  const [symbol,setSymbol]=useState('BTCUSDT')
  async function run(label:string,action:()=>Promise<Obj>){setBusy(label);setError('');setMessage('');try{const result=await action();setMessage(result.status?`${label}: ${result.status}${result.error?` — ${result.error}`:''}`:`${label} completed`);events.reload();artifacts.reload();ai.reload()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy('')}}
  const items=events.data.items
  return <>
    <PageHeading eyebrow="MARKET INTELLIGENCE" title="News, events and AI interpretation">
      <button className="secondary" disabled={!!busy} onClick={()=>run('News ingestion',()=>request('/intelligence/ingest',{method:'POST'}))}>Ingest news now</button>
    </PageHeading>
    <ErrorBanner text={error||events.error}/>
    {message&&<div className="notice">{message}</div>}
    <div className="metrics">
      <Metric label="Source-verified events" value={items.filter(i=>i.verification_status==='SOURCE_VERIFIED').length} note="Traceable provider reference"/>
      <Metric label="Unverified" value={items.filter(i=>i.verification_status==='UNVERIFIED').length} note="Never treated as fact"/>
      <Metric label="News (Finnhub)" value={providers.data.news?.verification||'—'} note={providers.data.news?.configured?`Economic calendar: ${providers.data.macro_calendar?.verification||'—'}`:'Set FINNHUB_API_KEY on the server'}/>
      <Metric label="Macro series (FRED)" value={providers.data.macro_series?.verification||'—'} note={providers.data.macro_series?.configured?'Point-in-time vintages (ALFRED)':'Set FRED_API_KEY on the server'}/>
      <Metric label="AI provider" value={ai.data.configured?`${ai.data.provider}`:'Not configured'} note={ai.data.configured?ai.data.model:'Set GEMINI_API_KEY'}/>
    </div>
    <div className="grid two">
      <Card eyebrow="STRUCTURED EVENTS" title="Recent news and macro" actions={<label className="field inline"><span>Verification</span><select value={verification} onChange={e=>setVerification(e.target.value)}>{['','SOURCE_VERIFIED','OPERATOR_ENTERED','UNVERIFIED'].map(v=><option key={v} value={v}>{v||'All'}</option>)}</select></label>}>
        <DataTable items={items} empty="No events stored. QTR never invents news." columns={[
          {key:'event_at',label:'TIME',render:when},{key:'event_type',label:'TYPE',render:v=><Status value={v}/>},
          {key:'title',label:'EVENT'},{key:'affected_assets',label:'ASSETS',render:v=>(v||[]).join(', ')},
          {key:'surprise',label:'SURPRISE',render:(v,row)=>row.category==='MACRO'?`${row.actual_value??'—'} vs ${row.expected_value??'—'}${v!=null?` (${v>0?'+':''}${v})`:''}`:'—'},
          {key:'verification_status',label:'SOURCE',render:(v,row)=><span className="stack"><Status value={v}/><small>{row.source}</small></span>},
        ]}/>
      </Card>
      <Card eyebrow="AI RESEARCH ASSISTANT" title="Budget and tasks">
        <div className="status-list">
          <p><span>Spent today</span><b>${num(ai.data.spent_today,4)} / ${num(ai.data.daily_budget,2)}</b></p>
          <p><span>Spent this month</span><b>${num(ai.data.spent_month,4)} / ${num(ai.data.monthly_budget,2)}</b></p>
          <p><span>Requests last hour</span><b>{ai.data.requests_last_hour??0} / {ai.data.max_requests_per_hour??'—'}</b></p>
          <p><span>Role</span><b>Researcher only — never places orders</b></p>
        </div>
        <label className="field"><span>Market</span><input value={symbol} onChange={e=>setSymbol(e.target.value.toUpperCase())}/></label>
        <div className="button-row">
          <button className="primary" disabled={!!busy||!ai.data.configured} onClick={()=>run('Market context',()=>request('/ai/tasks',{method:'POST',body:JSON.stringify({task:'market_context',symbol})}))}>Summarize context</button>
          <button className="secondary" disabled={!!busy||!ai.data.configured} onClick={()=>run('Hypothesis generation',()=>request('/ai/tasks',{method:'POST',body:JSON.stringify({task:'hypotheses',symbol})}))}>Propose hypotheses</button>
        </div>
        {!ai.data.configured&&<p className="fine">AI tasks are disabled until GEMINI_API_KEY is set on the server. Everything else keeps working.</p>}
      </Card>
    </div>
    <Card eyebrow="AI OUTPUT · SEPARATED FROM FACTS" title="Research artifacts" className="spaced">
      <DataTable items={artifacts.data.items} onRow={setArtifact} empty="No AI analyses yet" columns={[
        {key:'created_at',label:'TIME',render:when},{key:'task_type',label:'TASK',render:v=><Status value={v}/>},
        {key:'subject_id',label:'SUBJECT'},{key:'summary',label:'SUMMARY'},
        {key:'verification',label:'FACTS VERIFIED',render:v=>`${v?.facts_verified??0} verified · ${v?.unverified??0} unverified`},
      ]}/>
    </Card>
    {artifact&&<Drawer title={artifact.task_type} close={()=>setArtifact(null)}>
      <p className="reason">{artifact.summary}</p>
      <h4>Claims</h4>
      {(artifact.claims||[]).map((claim:Obj,index:number)=><div key={index} className={`claim ${claim.type?.toLowerCase()}`}><header><Status value={claim.type}/><Status value={claim.status}/></header><p>{claim.text}</p>{claim.note&&<small>{claim.note}</small>}</div>)}
      {(artifact.proposals||[]).length>0&&<><h4>Proposals (become hypotheses only after validation)</h4>{artifact.proposals.map((p:Obj,i:number)=><p key={i} className="fine">• {p.statement||JSON.stringify(p).slice(0,160)}</p>)}</>}
      <p className="fine">Model {artifact.model} · prompt {artifact.prompt_version}</p>
    </Drawer>}
  </>
}

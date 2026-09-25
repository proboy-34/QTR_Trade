import {useState,type FormEvent} from 'react'
import {request} from '../api'
import {num,when,type Obj} from '../format'
import {useData} from '../hooks'
import {Card,DataTable,Drawer,ErrorBanner,Metric,PageHeading,Status} from '../ui'
import './autonomy.css'

const FLOW=['IDEA','RESEARCH','BACKTESTING','OOS_VALIDATION','WALK_FORWARD','ROBUSTNESS','PAPER_TESTING','PAPER_VALIDATED','CANDIDATE','ACTIVE']
const TEMPLATES:Record<string,{label:string,spec:(p:Obj)=>Obj}>={
  ema:{label:'EMA trend change',spec:p=>({entry:[{left:'ema:{fast}',operator:'crosses_above',right:'ema:{slow}'}],exit:[{left:'ema:{fast}',operator:'crosses_below',right:'ema:{slow}'}],parameters:{fast:Number(p.a),slow:Number(p.b)}})},
  breakout:{label:'Volume-confirmed breakout',spec:p=>({entry:[{left:'close',operator:'gt',right:'highest:{lookback}'},{left:'volume_ratio:20',operator:'gt',right:1.5}],exit:[{left:'close',operator:'lt',right:'ema:{exit_ema}'}],parameters:{lookback:Number(p.a),exit_ema:Number(p.b)}})},
  momentum:{label:'Efficient momentum',spec:p=>({entry:[{left:'return:{lookback}',operator:'gt',right:0},{left:'efficiency:20',operator:'gt',right:0.35},{left:'close',operator:'gt',right:'ema:{trend}'}],exit:[{left:'close',operator:'lt',right:'ema:{trend}'}],parameters:{lookback:Number(p.a),trend:Number(p.b)}})},
}

export default function ResearchLabPage(){
  const [stage,setStage]=useState('')
  const dashboard=useData<Obj>('/research/dashboard',{counts:{}},20000)
  const hypotheses=useData<{items:Obj[]}>(`/research/hypotheses${stage?`?stage=${stage}`:''}`,{items:[]},20000)
  const [detail,setDetail]=useState<Obj|null>(null)
  const [creating,setCreating]=useState(false)
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [message,setMessage]=useState('')
  const counts=dashboard.data.counts||{}
  async function act(label:string,fn:()=>Promise<Obj>){setBusy(true);setError('');try{const result=await fn();setMessage(`${label}: ${result.stage||result.created?.length||'done'}`);dashboard.reload();hypotheses.reload();if(detail)open(detail.id)}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  async function open(id:string){try{setDetail(await request<Obj>(`/research/hypotheses/${id}`))}catch(e){setError(e instanceof Error?e.message:String(e))}}
  async function create(e:FormEvent<HTMLFormElement>){e.preventDefault();const f=Object.fromEntries(new FormData(e.currentTarget)) as Obj
    const template=TEMPLATES[f.template]
    const spec={...template.spec(f),timeframes:[f.timeframe],universe:[String(f.symbol).toUpperCase()],risk:{stop_loss_pct:Number(f.stop)/100,take_profit_pct:Number(f.target)/100}}
    await act('Hypothesis created',()=>request('/research/hypotheses',{method:'POST',body:JSON.stringify({statement:f.statement||`${template.label} on ${f.symbol} ${f.timeframe}`,spec,exchange:f.exchange||null})}));setCreating(false)}
  return <>
    <PageHeading eyebrow="RESEARCH ENGINE" title="Research lab">
      <button className="secondary" disabled={busy} onClick={()=>act('Generated from scanner',()=>request('/research/generate',{method:'POST'}))}>Generate from evidence</button>
      <button className="secondary" disabled={busy} onClick={()=>act('Research queue',()=>request('/research/run',{method:'POST'}))}>Run research queue</button>
      <button className="primary" onClick={()=>setCreating(true)}>New hypothesis</button>
    </PageHeading>
    <ErrorBanner text={error||dashboard.error||hypotheses.error}/>
    {message&&<div className="notice">{message}</div>}
    <Card eyebrow="RESEARCH QUEUE" title="Live counts from the research database">
      <div className="queue-grid">
        {[['Hypotheses',counts.hypotheses],['Experiments',counts.experiments],['Running',counts.running_experiments],['In research',counts.in_research],['Rejected',counts.rejected],['Paper testing',counts.paper_testing],['Validated',counts.validated],['Active',counts.active]].map(([label,value])=><div key={String(label)}><span>{label}</span><strong>{value??0}</strong></div>)}
      </div>
    </Card>
    <div className="metrics spaced">
      <Metric label="Candidates awaiting you" value={(dashboard.data.candidates||[]).filter((c:Obj)=>c.status==='ready_for_review').length} note="Promotion is always a human decision"/>
      <Metric label="In paper testing" value={(dashboard.data.candidates||[]).filter((c:Obj)=>c.status==='paper_testing').length} note="Trading paper money only"/>
      <Metric label="Experiments completed" value={counts.experiments_by_status?.COMPLETED??0} note={`${counts.experiments_by_status?.FAILED??0} failed · kept as evidence`}/>
      <Metric label="Rejected ideas" value={counts.rejected??0} note="Remembered, never hidden"/>
    </div>
    <Card eyebrow="HYPOTHESES" title="Pipeline" actions={<label className="field inline"><span>Stage</span><select value={stage} onChange={e=>setStage(e.target.value)}>{['',...FLOW,'REJECTED','FAILED','DEGRADED','ARCHIVED'].map(s=><option key={s} value={s}>{s||'All'}</option>)}</select></label>}>
      <DataTable items={hypotheses.data.items} onRow={row=>open(row.id)} empty="No hypotheses yet" columns={[
        {key:'statement',label:'HYPOTHESIS'},{key:'origin',label:'ORIGIN',render:v=><Status value={v}/>},
        {key:'assets',label:'ASSETS',render:v=>(v||[]).join(', ')},{key:'timeframes',label:'TF',render:v=>(v||[]).join(', ')},
        {key:'stage',label:'STAGE',render:v=><Status value={v}/>},{key:'trials',label:'EXPERIMENTS'},
        {key:'decision_reason',label:'REASON',render:v=>v||'—'},{key:'updated_at',label:'UPDATED',render:when},
      ]}/>
    </Card>
    {detail&&<Drawer title="Hypothesis evidence" close={()=>setDetail(null)}>
      <p className="reason">{detail.statement}</p>
      <div className="stage-flow">{FLOW.map(s=>{const reached=(detail.stage_history||[]).some((h:Obj)=>h.stage===s);return <span key={s} className={detail.stage===s?'current':reached?'done':''}>{s.replaceAll('_',' ')}</span>})}</div>
      {['REJECTED','FAILED','DEGRADED'].includes(detail.stage)&&<div className="error">{detail.stage}: {detail.decision_reason}</div>}
      {['backtest','out_of_sample','walk_forward','robustness'].filter(key=>detail.evidence?.[key]).map(key=><div key={key} className="gate"><header><b>{key.replaceAll('_',' ')}</b><Status value={detail.evidence[key].result}/></header>
        <div className="kv">{Object.entries(detail.evidence[key].checks||{}).map(([name,check])=>{const c=check as Obj;return <p key={name}><span>{name.replaceAll('_',' ')}</span><b className={c.passed?'up':'down'}>{num(c.value,4)} {c.passed?'≥':'✕'} {String(c.threshold)}</b></p>})}</div></div>)}
      {detail.evidence?.paper_validation&&<div className="gate"><header><b>paper validation</b><Status value={detail.evidence.paper_validation.result}/></header><p className="fine">{detail.evidence.paper_validation.paper?.trades??0} paper trades · expectancy {num(detail.evidence.paper_validation.paper?.expectancy_pct,3)}%</p></div>}
      <h4>Experiments ({(detail.experiments||[]).length})</h4>
      {(detail.experiments||[]).map((x:Obj)=><p key={x.id} className="fine"><Status value={x.status}/> {x.name} · {x.runtime_ms??0}ms</p>)}
      <h4>Specification</h4><pre className="spec">{JSON.stringify(detail.spec,null,1)}</pre>
      <div className="button-row">
        <button className="primary" disabled={busy} onClick={()=>act('Advanced',()=>request(`/research/hypotheses/${detail.id}/advance`,{method:'POST'}))}>Advance one stage</button>
        <button className="secondary" disabled={busy} onClick={()=>act('Validated',()=>request(`/research/hypotheses/${detail.id}/advance?run_to_paper=true`,{method:'POST'}))}>Run all research gates</button>
        <button className="secondary" disabled={busy} onClick={()=>act('Rejected',()=>request(`/research/hypotheses/${detail.id}/reject`,{method:'POST',body:JSON.stringify({reason:'Rejected by operator'})}))}>Reject</button>
      </div>
      <p className="fine">Candidates reach paper testing only after every gate passes; activation happens in Strategies by an operator.</p>
    </Drawer>}
    {creating&&<Drawer title="New research hypothesis" close={()=>setCreating(false)}>
      <form onSubmit={create}>
        <label className="field"><span>Template</span><select name="template">{Object.entries(TEMPLATES).map(([key,t])=><option key={key} value={key}>{t.label}</option>)}</select></label>
        <label className="field"><span>Statement</span><input name="statement" placeholder="What do you expect and why?"/></label>
        <div className="form-grid"><label className="field"><span>Symbol</span><input name="symbol" defaultValue="BTCUSDT" required/></label><label className="field"><span>Timeframe</span><select name="timeframe" defaultValue="1h">{['15m','1h','4h','1d'].map(t=><option key={t}>{t}</option>)}</select></label>
        <label className="field"><span>Parameter A</span><input name="a" type="number" defaultValue="10" min="2"/></label><label className="field"><span>Parameter B</span><input name="b" type="number" defaultValue="30" min="2"/></label>
        <label className="field"><span>Stop %</span><input name="stop" type="number" step="0.1" defaultValue="3"/></label><label className="field"><span>Target %</span><input name="target" type="number" step="0.1" defaultValue="6"/></label>
        <label className="field"><span>Data source</span><select name="exchange" defaultValue=""><option value="">Automatic</option><option value="binance">binance</option><option value="paper">paper (demo)</option></select></label></div>
        <button className="primary" disabled={busy}>Create hypothesis</button>
        <p className="fine">Hypotheses are declarative specifications validated by QTR; no code is executed.</p>
      </form>
    </Drawer>}
  </>
}

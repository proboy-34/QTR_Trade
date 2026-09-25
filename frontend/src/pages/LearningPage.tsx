import {useState} from 'react'
import {request} from '../api'
import {num,pct,when,type Obj} from '../format'
import {useData} from '../hooks'
import {Card,DataTable,ErrorBanner,PageHeading,Status} from '../ui'
import './autonomy.css'

export default function LearningPage(){
  const dashboard=useData<Obj>('/learning/dashboard',{observed:{},tested:{},learned:{}},30000)
  const [kind,setKind]=useState('')
  const [query,setQuery]=useState('')
  const knowledge=useData<{items:Obj[],counts:Obj}>(`/memory/knowledge?${new URLSearchParams({...(kind?{kind}:{}),...(query?{q:query}:{})})}`,{items:[],counts:{}})
  const counterfactuals=useData<{items:Obj[]}>('/learning/counterfactuals',{items:[]})
  const analyses=useData<{items:Obj[]}>('/learning/post-trade',{items:[]})
  const discrepancies=useData<{items:Obj[]}>('/learning/discrepancies',{items:[]})
  const [busy,setBusy]=useState(false);const [error,setError]=useState('')
  const d=dashboard.data
  async function run(){setBusy(true);setError('');try{await request('/learning/run',{method:'POST'});dashboard.reload();knowledge.reload();counterfactuals.reload();analyses.reload();discrepancies.reload()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  return <>
    <PageHeading eyebrow="EVIDENCE, NOT MAGIC" title="Learning"><button className="primary" onClick={run} disabled={busy}>{busy?'Analyzing…':'Run learning cycle'}</button></PageHeading>
    <ErrorBanner text={error||dashboard.error}/>
    <div className="learn-flow">
      <section><span>1 · OBSERVED</span><strong>{d.observed?.trades_recorded??0}</strong><p>paper trades recorded</p><small>{d.observed?.counterfactuals??0} counterfactual outcomes · {d.observed?.counterfactuals_pending??0} pending</small></section>
      <i>→</i>
      <section><span>2 · TESTED</span><strong>{d.tested?.successful??0}</strong><p>experiments completed</p><small>{d.tested?.failed??0} failed experiments kept as evidence</small></section>
      <i>→</i>
      <section><span>3 · LEARNED</span><strong>{Object.values(d.learned?.knowledge||{}).reduce((a:number,b)=>a+Number(b),0)}</strong><p>knowledge entries</p><small>{d.learned?.trades_analyzed??0} trades analyzed · {d.learned?.thesis_confirmed??0} theses confirmed / {d.learned?.thesis_refuted??0} refuted</small></section>
    </div>
    <div className="grid two spaced">
      <Card eyebrow="STRATEGY HEALTH" title="Degradation watch">
        {(d.degraded_strategies||[]).length?<DataTable items={d.degraded_strategies} columns={[{key:'name',label:'STRATEGY'},{key:'health_status',label:'HEALTH',render:v=><Status value={v}/>},{key:'reasons',label:'EVIDENCE',render:v=>(v||[]).join('; ')}]}/>:<p className="fine">No strategy is currently flagged. Health changes never modify a strategy automatically.</p>}
        <div className="pill-row">{Object.entries(d.strategy_health||{}).map(([k,v])=><span key={k} className="pill"><Status value={k}/><b>{String(v)}</b></span>)}</div>
      </Card>
      <Card eyebrow="WHY DID WE NOT TRADE?" title="Counterfactual outcomes">
        <div className="pill-row">{Object.entries(d.counterfactual_verdicts||{}).map(([k,v])=><span key={k} className="pill"><Status value={k}/><b>{String(v)}</b></span>)}</div>
        <DataTable items={counterfactuals.data.items.slice(0,12)} empty="No rejected or unacted opportunities evaluated yet" columns={[
          {key:'symbol',label:'ASSET'},{key:'rejection_category',label:'REJECTED BY',render:v=><Status value={v}/>},
          {key:'forward_return_pct',label:'AFTERWARDS',render:v=>v==null?'pending':<span className={v>=0?'up':'down'}>{pct(v)}</span>},
          {key:'verdict',label:'VERDICT',render:v=><Status value={v||'PENDING'}/>},
        ]}/>
        <p className="fine">Counterfactuals are research evidence only; no rule changes because of a single outcome.</p>
      </Card>
    </div>
    <Card eyebrow="KNOWLEDGE BASE" title="Lessons, patterns and failed ideas" className="spaced" actions={<div className="button-row"><label className="field inline"><span>Kind</span><select value={kind} onChange={e=>setKind(e.target.value)}>{['','TRADE_LESSON','STRATEGY_BEHAVIOR','MARKET_PATTERN','FAILED_IDEA','FINDING','COUNTERFACTUAL_FINDING','REGIME_OBSERVATION','NEWS_RELATIONSHIP'].map(k=><option key={k} value={k}>{k||'All'}</option>)}</select></label><label className="field inline"><span>Search</span><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="breakout, SOLUSDT…"/></label></div>}>
      <DataTable items={knowledge.data.items} empty="Nothing learned yet — evidence accumulates from research and paper trades" columns={[
        {key:'kind',label:'KIND',render:v=><Status value={v}/>},{key:'title',label:'ENTRY'},{key:'symbol',label:'MARKET',render:v=>v||'—'},
        {key:'regime',label:'REGIME',render:v=>v?<Status value={v}/>:'—'},{key:'evidence_count',label:'EVIDENCE'},
        {key:'source_type',label:'SOURCE',render:v=><Status value={v}/>},{key:'created_at',label:'RECORDED',render:when},
      ]}/>
    </Card>
    <div className="grid two spaced">
      <Card eyebrow="POST-TRADE ANALYST" title="Recent trade analyses">
        <DataTable items={analyses.data.items.slice(0,12)} empty="No closed paper trades analyzed yet" columns={[
          {key:'thesis_correct',label:'THESIS',render:v=><Status value={v===null?'INCONCLUSIVE':v?'CONFIRMED':'REFUTED'}/>},
          {key:'entry_quality',label:'ENTRY',render:v=><Status value={v}/>},{key:'exit_quality',label:'EXIT',render:v=><Status value={v}/>},
          {key:'factors',label:'FACTORS',render:v=>(v||[]).join(', ')||'—'},
        ]}/>
      </Card>
      <Card eyebrow="BACKTEST VS PAPER" title="Discrepancy reports">
        <DataTable items={discrepancies.data.items} empty="No paper validations yet" columns={[
          {key:'backtest',label:'EXPECTED',render:v=>`${num(v?.expectancy_pct,3)}%`},{key:'paper',label:'PAPER',render:v=>`${num(v?.expectancy_pct,3)}% · ${v?.trades??0} trades`},
          {key:'explanations',label:'EXPLANATIONS',render:v=>(v||[]).join('; ')||'within tolerance'},{key:'created_at',label:'WHEN',render:when},
        ]}/>
      </Card>
    </div>
  </>
}

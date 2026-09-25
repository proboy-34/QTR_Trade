import {useState} from 'react'
import {request} from '../api'
import {num,when,type Obj} from '../format'
import {useData} from '../hooks'
import {Badge,Card,DataTable,Drawer,ErrorBanner,Metric,PageHeading,Status} from '../ui'
import './autonomy.css'

export default function ScannerPage(){
  const [status,setStatus]=useState('')
  const {data,error,reload}=useData<{items:Obj[],note?:string}>(`/scanner/opportunities${status?`?status=${status}`:''}`,{items:[]},30000)
  const [selected,setSelected]=useState<Obj|null>(null)
  const [busy,setBusy]=useState(false);const [message,setMessage]=useState('');const [failure,setFailure]=useState('')
  async function scan(){setBusy(true);setFailure('');setMessage('');try{const result=await request<Obj>('/scanner/run',{method:'POST',body:JSON.stringify({timeframe:'1h'})});setMessage(`Scanned ${result.scanned} markets · ${result.opportunities} with signals`);reload()}catch(e){setFailure(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  const items=data.items
  return <>
    <PageHeading eyebrow="MARKET SCANNER" title="Opportunities">
      <label className="field inline"><span>Status</span><select value={status} onChange={e=>setStatus(e.target.value)}>{['','DETECTED','QUEUED','EXPIRED'].map(s=><option key={s} value={s}>{s||'All'}</option>)}</select></label>
      <button className="primary" onClick={scan} disabled={busy}>{busy?'Scanning…':'Scan now'}</button>
    </PageHeading>
    <ErrorBanner text={failure||error}/>
    {message&&<div className="notice">{message}</div>}
    <div className="notice subtle">The scanner never trades. Opportunities are observations that research and the decision layer may use. Rank is an evidence-based attention priority, not a forecast of return.</div>
    <div className="metrics">
      <Metric label="Open opportunities" value={items.filter(i=>['DETECTED','QUEUED'].includes(i.status)).length} note="Detected or queued for research"/>
      <Metric label="With verified news" value={items.filter(i=>(i.events||[]).some((e:Obj)=>e.verification_status==='SOURCE_VERIFIED')).length} note="Source-linked events"/>
      <Metric label="Assets" value={new Set(items.map(i=>i.symbol)).size} note="Distinct markets"/>
      <Metric label="Top rank" value={items.length?num(Math.max(...items.map(i=>i.rank_score||0)),1):'—'} note="0–100 attention score"/>
    </div>
    <Card eyebrow="QUEUE" title="Detected situations">
      <DataTable items={items} onRow={setSelected} empty="No scanner opportunities yet" columns={[
        {key:'symbol',label:'ASSET',render:v=><b className="symbol">{v}</b>},
        {key:'signals',label:'SIGNALS',render:v=><span className="pill-row compact">{(v||[]).map((s:Obj)=><Badge key={s.code}>{s.code.replaceAll('_',' ')}</Badge>)}</span>},
        {key:'regime',label:'REGIME',render:v=><Status value={v||'UNKNOWN'}/>},
        {key:'rank_breakdown',label:'STRATEGIES',render:v=>(v?.applicable_strategy_ids||[]).length},
        {key:'events',label:'NEWS',render:v=>(v||[]).length?<Badge tone="green">{v.length} linked</Badge>:<span className="fine">none</span>},
        {key:'rank_score',label:'RANK',render:v=><div className="bar"><i style={{width:`${Math.min(100,v||0)}%`}}/><span>{num(v,1)}</span></div>},
        {key:'status',label:'STATUS',render:v=><Status value={v}/>},
        {key:'last_seen_at',label:'LAST SEEN',render:when},
      ]}/>
    </Card>
    {selected&&<Drawer title={`${selected.symbol} · ${selected.timeframe}`} close={()=>setSelected(null)}>
      <h4>Signals</h4>
      <div className="kv">{(selected.signals||[]).map((s:Obj)=><p key={s.code}><span>{s.code}</span><b>{Object.entries(s).filter(([k])=>!['code'].includes(k)).map(([k,v])=>`${k}: ${typeof v==='object'?JSON.stringify(v):v}`).join(' · ')}</b></p>)}</div>
      <h4>Rank components</h4>
      <div className="kv">{Object.entries(selected.rank_breakdown?.components||{}).map(([k,v])=><p key={k}><span>{k.replaceAll('_',' ')}</span><b>{num(v,3)}</b></p>)}</div>
      <h4>Linked events</h4>
      {(selected.events||[]).length?(selected.events as Obj[]).map(e=><p key={e.id} className="event-line"><Status value={e.verification_status}/> {e.title}</p>):<p className="fine">No related events in the ±24h window.</p>}
      <p className="fine">Seen {selected.observations} time(s). Expires {when(selected.expires_at)}.</p>
    </Drawer>}
  </>
}

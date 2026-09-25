import {useState,type FormEvent} from 'react'
import {request} from '../api'
import {num,when,type Obj} from '../format'
import {useData} from '../hooks'
import {Card,DataTable,Drawer,ErrorBanner,Metric,PageHeading,Status} from '../ui'
import './autonomy.css'

const PROMOTION:Record<string,string[]>={ready_for_review:['approved','under_review','retired'],approved:['active','suspended','retired'],paper_testing:['under_review','suspended','retired'],active:['suspended','retired'],suspended:['active','under_review','retired']}

export default function StrategyHealthPage(){
  const {data,error,reload}=useData<{items:Obj[]}>('/strategies/overview',{items:[]},30000)
  const [selected,setSelected]=useState<Obj|null>(null)
  const [busy,setBusy]=useState(false);const [failure,setFailure]=useState('');const [message,setMessage]=useState('')
  const items=data.items
  async function act(label:string,fn:()=>Promise<unknown>){setBusy(true);setFailure('');try{await fn();setMessage(label);reload();setSelected(null)}catch(e){setFailure(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  async function challenger(e:FormEvent<HTMLFormElement>){e.preventDefault();if(!selected)return;const f=new FormData(e.currentTarget);const parameters:Record<string,number>={}
    f.forEach((value,key)=>{if(key.startsWith('p_')&&value!=='')parameters[key.slice(2)]=Number(value)})
    await act('Challenger hypothesis created — see Research lab',()=>request(`/strategies/${selected.id}/challengers`,{method:'POST',body:JSON.stringify({parameters,rationale:String(f.get('rationale')||'')})}))}
  return <>
    <PageHeading eyebrow="STRATEGY REPOSITORY" title="Strategies and health"/>
    <ErrorBanner text={failure||error}/>
    {message&&<div className="notice">{message}</div>}
    <div className="metrics">
      <Metric label="Active" value={items.filter(i=>i.status==='active').length} note="Operator-promoted"/>
      <Metric label="Paper testing" value={items.filter(i=>i.status==='paper_testing').length} note="Research candidates"/>
      <Metric label="Ready for review" value={items.filter(i=>i.status==='ready_for_review').length} note="Awaiting your decision"/>
      <Metric label="Degraded / review" value={items.filter(i=>['DEGRADED','REVIEW_REQUIRED'].includes(i.health_status)).length} note="Flagged, never auto-changed"/>
    </div>
    <Card eyebrow="IMMUTABLE VERSIONS" title="Strategies">
      <DataTable items={items.map(item=>({...item,paper_trades:item.paper_performance?.trades??0,
        paper_expectancy:item.paper_performance?.trades?item.paper_performance.expectancy_pct:null,
        recent_pf:item.recent_performance?.trades?item.recent_performance.profit_factor:null}))} onRow={setSelected} empty="No strategies" columns={[
        {key:'name',label:'STRATEGY'},{key:'version',label:'VER',render:v=>`v${v??'—'}`},{key:'symbol',label:'MARKET'},{key:'timeframe',label:'TF'},
        {key:'origin',label:'ORIGIN',render:v=><Status value={v}/>},{key:'status',label:'STATUS',render:v=><Status value={v}/>},
        {key:'health_status',label:'HEALTH',render:v=><Status value={v}/>},
        {key:'paper_trades',label:'PAPER TRADES'},
        {key:'paper_expectancy',label:'EXPECTANCY',render:v=>v===null?'—':`${num(v,3)}%`},
        {key:'recent_pf',label:'RECENT PF',render:v=>v===null?'—':num(v,2)},
      ]}/>
    </Card>
    {selected&&<Drawer title={selected.name} close={()=>setSelected(null)}>
      <div className="pill-row"><Status value={selected.status}/><Status value={selected.health_status}/><span className="fine">v{selected.version} · {selected.symbol} · {selected.timeframe}</span></div>
      {(selected.health_details?.reasons||[]).length>0&&<p className="reason">{selected.health_details.reasons.join('; ')}</p>}
      <h4>Validation evidence</h4>
      {(selected.validations||[]).map((v:Obj,i:number)=><p key={i} className="fine"><Status value={v.result}/> {v.method.replaceAll('_',' ')} · {when(v.created_at)}</p>)}
      {!(selected.validations||[]).length&&<p className="fine">No validation records for the latest version.</p>}
      <h4>Performance by regime</h4>
      <div className="kv">{Object.entries(selected.regime_performance||{}).map(([regime,stats])=>{const s=stats as Obj;return <p key={regime}><span><Status value={regime}/></span><b>{s.trades} trades · {num(s.expectancy_pct,3)}% · PF {num(s.profit_factor,2)} · {s.evidence}</b></p>})}</div>
      {!Object.keys(selected.regime_performance||{}).length&&<p className="fine">Insufficient paper evidence.</p>}
      {PROMOTION[selected.status]&&<><h4>Human decision</h4><div className="button-row">{PROMOTION[selected.status].map(target=><button key={target} className={target==='active'||target==='approved'?'primary':'secondary'} disabled={busy} onClick={()=>act(`Moved to ${target}`,()=>request(`/strategies/${selected.id}/transition`,{method:'POST',body:JSON.stringify({status:target,reason:'Operator decision from strategy review'})}))}>{target.replaceAll('_',' ')}</button>)}</div>
        <p className="fine">Approval and activation require PASS validation and can only be performed by an operator. Real-money trading stays disabled.</p></>}
      <h4>Propose a challenger</h4>
      <form onSubmit={challenger}>
        <div className="form-grid">{Object.entries(selected.parameters||{}).map(([key,value])=><label key={key} className="field"><span>{key}</span><input name={`p_${key}`} type="number" step="any" placeholder={String(value)}/></label>)}</div>
        <label className="field"><span>Rationale</span><input name="rationale" placeholder="Why might this variation be better?"/></label>
        <button className="secondary" disabled={busy}>Create challenger hypothesis</button>
        <p className="fine">A challenger must beat the unchanged champion out-of-sample; it never replaces it automatically.</p>
      </form>
    </Drawer>}
  </>
}

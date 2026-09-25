import {useState,type FormEvent} from 'react'
import {request} from '../api'
import {num,pct,when,type Obj} from '../format'
import {useData} from '../hooks'
import {Badge,Card,DataTable,ErrorBanner,Metric,PageHeading,Status} from '../ui'
import './autonomy.css'

function cellTone(value:number|null){if(value===null)return 'muted';if(value>=0.7)return 'hot';if(value>=0.4)return 'warm';return 'cool'}

export default function SafetyPage(){
  const safety=useData<Obj>('/safety',{active:[],history:[]},15000)
  const risk=useData<Obj>('/portfolio/risk',{correlation:{},positions:{},limits:{}},30000)
  const providers=useData<Obj>('/system/providers',{scheduler:{}},15000)
  const [busy,setBusy]=useState(false);const [error,setError]=useState('')
  async function act(fn:()=>Promise<unknown>){setBusy(true);setError('');try{await fn();safety.reload()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  async function activate(e:FormEvent<HTMLFormElement>){e.preventDefault();const f=Object.fromEntries(new FormData(e.currentTarget));await act(()=>request('/safety/controls',{method:'POST',body:JSON.stringify(f)}))}
  const s=safety.data,r=risk.data,p=providers.data
  const symbols=Object.keys(r.correlation||{})
  return <>
    <PageHeading eyebrow="SAFETY · RISK · SYSTEM" title="Controls and health"><Badge tone={s.safe_mode?'red':'green'}>{s.safe_mode?'SAFE MODE ACTIVE':'NORMAL OPERATION'}</Badge></PageHeading>
    <ErrorBanner text={error||safety.error||risk.error}/>
    <div className="live-safety"><b>REAL TRADING DISABLED.</b> Trading mode: {s.real_trading?.trading_mode||'paper'} · live trading enabled: {String(s.real_trading?.live_trading_enabled??false)} · live execution adapter installed: {String(s.real_trading?.live_execution_adapter_installed??false)}</div>
    <div className="grid two">
      <Card eyebrow="KILL SWITCH" title="Active controls">
        <DataTable items={s.active||[]} empty="No active controls — new paper orders are allowed" columns={[
          {key:'scope',label:'SCOPE',render:v=><Status value={v}/>},{key:'target',label:'TARGET'},{key:'reason',label:'REASON'},
          {key:'trigger',label:'TRIGGER',render:v=><Status value={v}/>},{key:'triggered_at',label:'SINCE',render:when},
          {key:'id',label:'',render:v=><button className="secondary" disabled={busy} onClick={()=>act(()=>request(`/safety/controls/${v}/clear`,{method:'POST'}))}>Clear</button>},
        ]}/>
        <form onSubmit={activate} className="inline-form">
          <label className="field"><span>Stop</span><select name="scope" defaultValue="NEW_ORDERS"><option value="SYSTEM">System stop</option><option value="PAPER_TRADING">Paper trading stop</option><option value="NEW_ORDERS">New orders stop</option><option value="STRATEGY">Strategy stop</option><option value="ASSET">Asset stop</option></select></label>
          <label className="field"><span>Target</span><input name="target" defaultValue="*" placeholder="* or strategy id / symbol"/></label>
          <label className="field"><span>Reason</span><input name="reason" required minLength={3} placeholder="Why stop?"/></label>
          <button className="primary" disabled={busy}>Activate stop</button>
        </form>
        <p className="fine">Stops block new trades immediately. Protective stop-loss/take-profit exits keep running. Automatic triggers stay active until an operator clears them.</p>
      </Card>
      <Card eyebrow="SYSTEM" title="Providers and services">
        <div className="status-list">
          <p><span>Binance public data</span><b>{p.binance?.live_stream||'—'} · {p.binance?.connected?'connected':'not streaming'}</b></p>
          <p><span>Binance private trading</span><b className="safe-text">{p.binance?.private_trading||'disabled'}</b></p>
          <p><span>Last universe refresh</span><b>{when(p.binance?.last_universe_refresh)}</b></p>
          <p><span>News provider</span><b>{p.news?.news?.configured?p.news.news.provider:'not configured'}</b></p>
          <p><span>Macro provider</span><b>{p.news?.macro?.configured?p.news.macro.provider:'not configured'}</b></p>
          <p><span>AI provider</span><b>{p.ai?.configured?`${p.ai.provider} · ${p.ai.model}`:'not configured'}</b></p>
          <p><span>Database</span><b>{p.database?.status||'—'} · {p.database?.dialect||''}</b></p>
          <p><span>Event bus</span><b>{p.event_bus?.published??0} published · {p.event_bus?.failures??0} failures</b></p>
        </div>
      </Card>
    </div>
    <div className="metrics spaced">
      <Metric label="Gross exposure" value={pct((r.gross_exposure||0)*100)} note={`limit ${pct((r.limits?.max_total_exposure||0)*100,0)}`}/>
      <Metric label="Concentration (HHI)" value={num(r.concentration_hhi,3)} note={`${num(r.effective_bets,2)} effective independent bets`}/>
      <Metric label="Portfolio beta to BTC" value={num(r.portfolio_beta_to_btc,3)} note="Shared market exposure"/>
      <Metric label="Correlated cluster limit" value={pct((r.limits?.max_correlated_exposure||0)*100,0)} note={`ρ ≥ ${r.limits?.correlation_threshold??'—'} counts as one exposure`}/>
    </div>
    <div className="grid two">
      <Card eyebrow="PORTFOLIO INTELLIGENCE" title="Correlation matrix">
        {symbols.length>1?<div className="table-wrap"><table className="matrix"><thead><tr><th/>{symbols.map(c=><th key={c}>{c}</th>)}</tr></thead><tbody>{symbols.map(row=><tr key={row}><th>{row}</th>{symbols.map(col=>{const v=r.correlation[row]?.[col];return <td key={col} className={cellTone(v)}>{v===null||v===undefined?'—':num(v,2)}</td>})}</tr>)}</tbody></table></div>:<p className="fine">Correlation needs at least two assets with stored history and open exposure.</p>}
        <p className="fine">{r.note}</p>
      </Card>
      <Card eyebrow="SCHEDULER" title="Autonomous jobs">
        <DataTable items={Object.entries(p.scheduler||{}).map(([name,state])=>({name,...(state as Obj)}))} empty="Scheduler not running" columns={[
          {key:'name',label:'JOB'},{key:'status',label:'STATE',render:v=><Status value={v==='HEALTHY'?'PASS':v}/>},{key:'runs',label:'RUNS'},
          {key:'last_run',label:'LAST RUN',render:when},{key:'last_error',label:'LAST ERROR',render:v=>v?<span className="down">{String(v).slice(0,80)}</span>:'—'},
        ]}/>
      </Card>
    </div>
    <Card eyebrow="AUDIT" title="Safety history" className="spaced">
      <DataTable items={s.history||[]} empty="No safety events" columns={[
        {key:'triggered_at',label:'TRIGGERED',render:when},{key:'scope',label:'SCOPE'},{key:'target',label:'TARGET'},{key:'reason',label:'REASON'},
        {key:'source',label:'BY'},{key:'active',label:'STATE',render:v=><Badge tone={v?'red':'muted'}>{v?'ACTIVE':'CLEARED'}</Badge>},{key:'cleared_by',label:'CLEARED BY',render:v=>v||'—'},
      ]}/>
    </Card>
  </>
}

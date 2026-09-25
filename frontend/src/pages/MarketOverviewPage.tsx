import {useState} from 'react'
import {request} from '../api'
import {num,pct,when} from '../format'
import {useData} from '../hooks'
import {Badge,Card,DataTable,ErrorBanner,Metric,PageHeading,Status} from '../ui'
import './autonomy.css'

type Overview={exchange:string,timeframe:string,items:Record<string,any>[]}
type Universe={run_id:string|null,evaluated:number,eligible:number,evaluated_at?:string,items:Record<string,any>[],note?:string}

export default function MarketOverviewPage(){
  const [timeframe,setTimeframe]=useState('1h')
  const overview=useData<Overview>(`/market/overview?timeframe=${timeframe}`,{exchange:'',timeframe,items:[]},30000)
  const universe=useData<Universe>('/universe',{run_id:null,evaluated:0,eligible:0,items:[]})
  const [busy,setBusy]=useState(false);const [error,setError]=useState('')
  async function refresh(){setBusy(true);setError('');try{await request('/universe/refresh',{method:'POST'});universe.reload();overview.reload()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  const items=overview.data.items
  const excluded=universe.data.items.filter(item=>!item.eligible)
  const regimes=items.reduce<Record<string,number>>((acc,item)=>{const key=item.regime||'UNKNOWN';acc[key]=(acc[key]||0)+1;return acc},{})
  return <>
    <PageHeading eyebrow="MULTI-ASSET MARKET" title="Market overview">
      <label className="field inline"><span>Timeframe</span><select value={timeframe} onChange={e=>setTimeframe(e.target.value)}>{['15m','1h','4h','1d'].map(t=><option key={t}>{t}</option>)}</select></label>
      <button className="primary" onClick={refresh} disabled={busy}>{busy?'Refreshing…':'Refresh universe'}</button>
    </PageHeading>
    <ErrorBanner text={error||overview.error||universe.error}/>
    {overview.data.exchange==='paper'&&<div className="notice">Showing the local demo feed. Live Binance assets appear after a universe refresh and market-data sync (public data, no key required).</div>}
    <div className="metrics">
      <Metric label="Eligible assets" value={universe.data.eligible} note={universe.data.run_id?`of ${universe.data.evaluated} evaluated`:'Universe not refreshed yet'}/>
      <Metric label="Tracked markets" value={items.length} note={`${overview.data.exchange||'—'} · ${timeframe}`}/>
      <Metric label="Data quality issues" value={items.filter(item=>item.data_quality!=='GOOD').length} note="Stale or failed validation"/>
      <Metric label="Last universe refresh" value={universe.data.evaluated_at?new Date(universe.data.evaluated_at).toLocaleTimeString():'—'} note={universe.data.evaluated_at?when(universe.data.evaluated_at):'Requires public Binance access'}/>
    </div>
    <Card eyebrow="REGIME MIX" title="Where markets are">
      <div className="pill-row">{Object.entries(regimes).map(([regime,count])=><span key={regime} className="pill"><Status value={regime}/><b>{count}</b></span>)}{!items.length&&<span className="fine">No classified markets yet.</span>}</div>
    </Card>
    <Card eyebrow="LIVE STATE" title="Assets" className="spaced">
      <DataTable items={items} empty="No market data stored yet" columns={[
        {key:'symbol',label:'MARKET',render:(v,row)=><b className="symbol">{v}{row.is_demo&&<Badge tone="muted">demo</Badge>}</b>},
        {key:'price',label:'PRICE',render:v=>num(v,6)},
        {key:'change_24h_pct',label:'24H',render:v=><span className={Number(v)>=0?'up':'down'}>{pct(v)}</span>},
        {key:'volume_24h',label:'VOLUME 24H',render:v=>num(v,0)},
        {key:'volatility',label:'VOLATILITY',render:v=>v===null?'—':pct(Number(v)*100,3)},
        {key:'regime',label:'REGIME',render:(v,row)=><span className="stack"><Status value={v||'UNKNOWN'}/>{row.regime_confidence!=null&&<small>{Math.round(row.regime_confidence*100)}% evidence</small>}</span>},
        {key:'data_quality',label:'DATA',render:v=><Status value={v}/>},
        {key:'last_candle_at',label:'LAST CANDLE',render:when},
      ]}/>
    </Card>
    <Card eyebrow="ELIGIBILITY" title="Excluded instruments and why" className="spaced">
      <DataTable items={excluded.slice(0,100).map(item=>({...item,quote_volume:item.metrics?.quote_volume_24h,spread:item.metrics?.spread_bps}))} empty={universe.data.note||'No exclusions recorded'} columns={[
        {key:'symbol',label:'SYMBOL'},{key:'reasons',label:'REASONS',render:v=>(v||[]).join(' · ')},
        {key:'quote_volume',label:'24H QUOTE VOLUME',render:v=>num(v,0)},{key:'spread',label:'SPREAD (BPS)',render:v=>num(v,2)},
      ]}/>
    </Card>
  </>
}

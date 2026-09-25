import {useEffect,useState,type FormEvent} from 'react'
import {fmt,request} from '../api'

type Backfill={id:string;exchange:string;symbol:string;timeframe:string;status:string;progress:number;rows_written:number;last_success_at?:string;failure_reason?:string}

export default function BackfillsPage(){
  const [items,setItems]=useState<Backfill[]>([])
  const [error,setError]=useState('')
  const load=()=>request<{items:Backfill[]}>('/market-data/backfills').then(x=>setItems(x.items)).catch(e=>setError(e.message))
  useEffect(()=>{request<{items:Backfill[]}>('/market-data/backfills').then(x=>setItems(x.items)).catch(e=>setError(e.message))},[])
  async function create(e:FormEvent<HTMLFormElement>){
    e.preventDefault();setError('')
    const f=new FormData(e.currentTarget)
    await request<Backfill>('/market-data/backfills',{method:'POST',body:JSON.stringify({exchange:f.get('exchange'),symbol:f.get('symbol'),timeframe:f.get('timeframe'),start_at:new Date(String(f.get('start'))).toISOString(),end_at:new Date(String(f.get('end'))).toISOString(),batch_limit:500})}).then(job=>request(`/market-data/backfills/${job.id}/resume`,{method:'POST'})).then(load).catch(e=>setError(e.message))
  }
  async function resume(id:string){await request(`/market-data/backfills/${id}/resume`,{method:'POST'}).then(load).catch(e=>setError(e.message))}
  return <><div className="page-heading"><div><p>MARKET DATA OPERATIONS</p><h2>Historical backfills</h2></div></div>{error&&<div className="error">{error}</div>}<section className="card"><header><span>INCREMENTAL INGESTION</span><h3>Start a checkpointed job</h3></header><form onSubmit={create}><div className="form-grid"><label className="field"><span>Provider</span><select name="exchange" defaultValue="paper"><option>paper</option><option>binance</option><option>okx</option><option>bybit</option></select></label><label className="field"><span>Symbol</span><input name="symbol" defaultValue="BTCUSDT"/></label><label className="field"><span>Timeframe</span><select name="timeframe" defaultValue="1h"><option>1m</option><option>5m</option><option>15m</option><option>1h</option><option>4h</option><option>1d</option></select></label><label className="field"><span>Start</span><input name="start" type="datetime-local" required/></label><label className="field"><span>End</span><input name="end" type="datetime-local" required/></label></div><button className="primary">Create and run</button></form></section><section className="card"><header><span>CHECKPOINTS</span><h3>Backfill history</h3></header><div className="table-wrap"><table><thead><tr><th>PROVIDER</th><th>MARKET</th><th>RANGE</th><th>STATUS</th><th>PROGRESS</th><th>ROWS</th><th>LAST CHECKPOINT</th><th>ACTION</th></tr></thead><tbody>{items.map(x=><tr key={x.id}><td>{x.exchange}</td><td>{x.symbol}</td><td>{x.timeframe}</td><td><span className="badge gold">{x.status}</span></td><td>{(Number(x.progress)*100).toFixed(1)}%</td><td>{x.rows_written}</td><td>{fmt.date(x.last_success_at)}</td><td>{x.status==='FAILED'&&<button className="secondary" onClick={()=>resume(x.id)}>Resume</button>}</td></tr>)}</tbody></table></div>{!items.length&&<div className="empty">No backfill jobs yet.</div>}</section></>
}

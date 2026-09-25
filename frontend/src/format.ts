export type Obj=Record<string,any>

export const tone=(value:string|undefined|null)=>{
  const v=String(value||'').toUpperCase()
  if(['PASS','HEALTHY','GOOD','VERIFIED_BY_REFERENCE','SOURCE_VERIFIED','SYSTEM_METRIC_MATCH','CORRECT_AVOIDANCE','ACTIVE','CANDIDATE','PAPER_VALIDATED','COMPLETED','LIVE','TRADING','TRUE','SUPPORTED'].includes(v))return 'green'
  if(['FAIL','FAILED','REJECTED','DEGRADED','UNVERIFIED','STALE','ERROR','MISSED_OPPORTUNITY','PANIC','HIGH','CRITICAL','FALSE'].includes(v))return 'red'
  if(['UNKNOWN','INSUFFICIENT_DATA','NOT_CONFIGURED','ARCHIVED','PENDING','NEUTRAL','EXPIRED'].includes(v))return 'muted'
  return 'gold'
}
export const pct=(v:any,d=2)=>v===null||v===undefined||v===''?'—':`${Number(v).toFixed(d)}%`
export const num=(v:any,d=2)=>v===null||v===undefined||v===''?'—':new Intl.NumberFormat('en-US',{maximumFractionDigits:d}).format(Number(v))
export const when=(v:any)=>v?new Date(String(v).endsWith('Z')||String(v).includes('+')?String(v):`${v}Z`).toLocaleString():'—'

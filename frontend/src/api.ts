export const API='/api/v1'
export async function request<T>(path:string, options?:RequestInit):Promise<T>{
  const response=await fetch(`${API}${path}`,{headers:{'Content-Type':'application/json'},...options})
  const body=await response.json().catch(()=>({error:{message:'Invalid server response'}}))
  if(!response.ok) throw new Error(body.error?.message || `Request failed (${response.status})`)
  return body as T
}
export const fmt={
  money:(v:unknown)=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(Number(v||0)),
  number:(v:unknown,d=2)=>new Intl.NumberFormat('en-US',{maximumFractionDigits:d}).format(Number(v||0)),
  pct:(v:unknown)=>`${(Number(v||0)*100).toFixed(2)}%`,
  date:(v:unknown)=>v?new Date(String(v)).toLocaleString():'—'
}


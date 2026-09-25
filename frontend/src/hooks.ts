import {useCallback,useEffect,useState} from 'react'
import {request} from './api'

export function useData<T>(path:string,initial:T,refreshMs=0){
  const [data,setData]=useState(initial);const [error,setError]=useState('');const [loading,setLoading]=useState(true)
  const reload=useCallback(()=>{setLoading(true);request<T>(path).then(value=>{setData(value);setError('')}).catch(e=>setError(e.message)).finally(()=>setLoading(false))},[path])
  useEffect(()=>{reload();if(!refreshMs)return;const timer=window.setInterval(reload,refreshMs);return()=>window.clearInterval(timer)},[reload,refreshMs])
  return{data,error,loading,reload}
}

import {type ReactNode} from 'react'
import {Orbit} from 'lucide-react'
import {tone,type Obj} from './format'

export function Badge({children,tone='gold'}:{children:ReactNode,tone?:string}){return <span className={`badge ${tone}`}>{children}</span>}
export function Card({title,eyebrow,children,className='',actions}:{title?:string,eyebrow?:string,children:ReactNode,className?:string,actions?:ReactNode}){return <section className={`card ${className}`}>{(title||eyebrow)&&<header className={actions?'with-actions':''}><div>{eyebrow&&<span>{eyebrow}</span>}{title&&<h3>{title}</h3>}</div>{actions}</header>}{children}</section>}
export function ErrorBanner({text}:{text:string}){return text?<div className="error" role="alert">{text}</div>:null}
export function Empty({text='No records yet'}:{text?:string}){return <div className="empty"><Orbit/><p>{text}</p></div>}
export function Metric({label,value,note}:{label:string,value:ReactNode,note?:ReactNode}){return <section className="card metric"><span>{label}</span><strong>{value}</strong>{note!==undefined&&<small>{note}</small>}</section>}
export type Column={key:string,label:string,render?:(value:any,row:Obj)=>ReactNode}
export function DataTable({items,columns,onRow,empty}:{items:Obj[],columns:Column[],onRow?:(row:Obj)=>void,empty?:string}){
  if(!items.length)return <Empty text={empty}/>
  return <div className="table-wrap"><table><thead><tr>{columns.map(c=><th key={c.key}>{c.label}</th>)}</tr></thead><tbody>{items.map((row,i)=><tr key={row.id||i} onClick={onRow?()=>onRow(row):undefined} className={onRow?'clickable':''}>{columns.map(c=><td key={c.key}>{c.render?c.render(row[c.key],row):String(row[c.key]??'—')}</td>)}</tr>)}</tbody></table></div>
}
export function PageHeading({eyebrow,title,children}:{eyebrow:string,title:string,children?:ReactNode}){return <div className="page-heading"><div><p>{eyebrow}</p><h2>{title}</h2></div>{children&&<div className="heading-actions">{children}</div>}</div>}
export function Status({value}:{value:any}){return <Badge tone={tone(value)}>{String(value??'—').replaceAll('_',' ')}</Badge>}
export function Drawer({title,close,children}:{title:string,close:()=>void,children:ReactNode}){return <div className="drawer-back" onClick={close}><aside className="drawer" onClick={e=>e.stopPropagation()} aria-label={title}><header><h2>{title}</h2><button onClick={close} aria-label="Close">×</button></header>{children}</aside></div>}

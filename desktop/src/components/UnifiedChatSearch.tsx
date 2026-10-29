import {useEffect, useRef, useState} from 'react'
import type {GroupMember, GroupMemberPage} from '../types'
import Avatar from './Avatar'
import Icon from './Icon'
import ChatTimeFilter, {timeLabel} from './ChatTimeFilter'
import './UnifiedChatSearch.css'

const memberNames = new Intl.Collator('zh-CN-u-co-pinyin', {numeric: true, sensitivity: 'base'})

const kinds = [{key:'image', label:'图片', icon:'image'}, {key:'video', label:'视频', icon:'video'}, {key:'link', label:'链接', icon:'link'}, {key:'file', label:'文件', icon:'file'}] as const
export default function UnifiedChatSearch({search, onSearch, category, onCategory, members, onMembers, from, to, onFrom, onTo, toNow, onNow, onLocateTime, onRange, isGroup, memberPage, memberLoading, memberError, onOpen, onClear}: {
  search: string; onSearch(value:string):void; category:string; onCategory(value:string):void
  members:GroupMember[]; onMembers(value:GroupMember[]):void; from?:number; to?:number
  toNow:boolean; onNow():void; onLocateTime(value:number):Promise<boolean>; onRange():void
  onFrom(value:number|undefined):void; onTo(value:number|undefined):void; isGroup:boolean
  memberPage:GroupMemberPage|null; memberLoading:boolean; memberError:string; onOpen():void; onClear():void
}) {
  const [open,setOpen]=useState(false)
  const [calendar,setCalendar]=useState<'from'|'to'|'point'|null>(null)
  const [memberQuery,setMemberQuery]=useState('')
  const root=useRef<HTMLDivElement>(null)
  const input=useRef<HTMLInputElement>(null)
  const memberGrid=useRef<HTMLDivElement>(null)
  useEffect(()=>{
    const grid=memberGrid.current
    if(!open||!grid)return
    const reduced=window.matchMedia('(prefers-reduced-motion: reduce)')
    let frame=0,target=grid.scrollTop,position=grid.scrollTop,last=0
    const stop=()=>{cancelAnimationFrame(frame);frame=0;target=grid.scrollTop}
    const tick=(now:number)=>{
      const elapsed=Math.min(64,now-last);last=now
      target=Math.max(0,Math.min(target,grid.scrollHeight-grid.clientHeight))
      const distance=target-position
      if(Math.abs(distance)<1){grid.scrollTop=target;frame=0;return}
      position+=distance*(1-Math.exp(-elapsed/45))
      grid.scrollTop=position
      frame=requestAnimationFrame(tick)
    }
    const wheel=(event:WheelEvent)=>{
      // Preserve touchpad precision, horizontal gestures and browser zoom.
      const stepped=event.deltaMode===1||(event.deltaMode===0&&Math.abs(event.deltaY)>=100&&Number.isInteger(event.deltaY))
      if(event.ctrlKey||event.deltaX||!stepped||reduced.matches){stop();return}
      if(grid.scrollHeight<=grid.clientHeight)return
      event.preventDefault()
      if(!frame){target=grid.scrollTop;position=grid.scrollTop}
      target=Math.max(0,Math.min(target+event.deltaY*(event.deltaMode===1?20:1),grid.scrollHeight-grid.clientHeight))
      if(!frame){last=performance.now();frame=requestAnimationFrame(tick)}
    }
    grid.addEventListener('wheel',wheel,{passive:false})
    grid.addEventListener('pointerdown',stop)
    grid.addEventListener('keydown',stop)
    return()=>{stop();grid.removeEventListener('wheel',wheel);grid.removeEventListener('pointerdown',stop);grid.removeEventListener('keydown',stop)}
  },[open,memberQuery,isGroup])
  const selected=category==='all'?[]:category.split(',')
  function show() { setOpen(true); onOpen() }
  useEffect(()=>{
    const outside=(event:PointerEvent)=>{if(!root.current?.contains(event.target as Node)){setOpen(false);setCalendar(null)}}
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'){if(calendar)setCalendar(null);else setOpen(false);input.current?.focus()}}
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',escape)
    return ()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape)}
  },[calendar])
  const visible=(memberPage?.members||[]).filter(m=>(m.name+' '+m.username).toLocaleLowerCase().includes(memberQuery.trim().toLocaleLowerCase())).sort((a,b)=>memberNames.compare(a.name || a.username,b.name || b.username) || memberNames.compare(a.username,b.username))
  return <div className={`unified-search${open ? ' is-open' : ''}`} ref={root}>
    <div className="unified-search-field">
      <Icon name="search" size={18}/>
      <input ref={input} aria-label="搜索当前会话" aria-expanded={open} value={search} placeholder="搜索当前会话" onClick={show} onChange={e=>{onSearch(e.target.value);show()}} onKeyDown={e=>{if(e.key==='ArrowDown'){e.preventDefault();show()}if(e.key==='Enter'){setOpen(false);setCalendar(null)}}}/>
      <div className="search-chips" aria-label="已选筛选条件">
        {kinds.filter(k=>selected.includes(k.key)).map(k=><button key={k.key} className="search-chip" aria-label={`移除${k.label}筛选`} onClick={()=>onCategory(selected.filter(v=>v!==k.key).join(',')||'all')}>{k.label}<Icon name="close" size={12}/></button>)}
        {members.map(m=><button key={m.username} className="search-chip" title={m.name} aria-label={`移除成员${m.name}`} onClick={()=>onMembers(members.filter(v=>v.username!==m.username))}><span>{m.name}</span><Icon name="close" size={12}/></button>)}
        {from!=null||to!=null?<button className="search-chip time-chip" aria-label="移除时间筛选" title={`${timeLabel(from)||'不限'} — ${toNow?'截至目前':timeLabel(to)||'不限'}`} onClick={()=>{onFrom(undefined);onTo(undefined)}}><span>{timeLabel(from).slice(5)||'不限'} — {toNow?'截至目前':timeLabel(to).slice(5)||'不限'}</span><Icon name="close" size={12}/></button>:null}
      </div>
      {search||selected.length||members.length||from!=null||to!=null?<button className="search-icon-button" aria-label="清空全部筛选" onClick={onClear}><Icon name="close" size={16}/></button>:null}
      <button className={`search-icon-button search-disclosure ${open?'expanded':''}`} aria-label={open?'收起筛选':'展开筛选'} aria-expanded={open} onClick={()=>{if(open){setOpen(false);setCalendar(null)}else show()}}><Icon name="chevron" size={16}/></button>
    </div>
    {open?<div className={`unified-filter-panel${isGroup?'':' direct-chat'}`}>
      <section className="filter-content-time" aria-label="内容和时间">
        <div className="unified-kind-row"><strong>内容类型</strong><div>{kinds.map(k=><button key={k.key} className={selected.includes(k.key)?'selected':''} aria-pressed={selected.includes(k.key)} onClick={()=>onCategory((selected.includes(k.key)?selected.filter(v=>v!==k.key):[...selected,k.key]).join(',')||'all')}><Icon name={k.icon} size={16}/>{k.label}</button>)}</div></div>
        <ChatTimeFilter from={from} to={to} toNow={toNow} onFrom={onFrom} onTo={onTo} onNow={onNow}
          onLocate={onLocateTime} onRange={onRange} calendar={calendar} onCalendar={setCalendar}/>
      </section>
      {isGroup?<section className="filter-members" aria-label="群成员筛选">
        <div className="unified-member-heading"><strong>群成员</strong><label><Icon name="search" size={14}/><input aria-label="搜索成员" placeholder="搜索成员" value={memberQuery} onChange={e=>setMemberQuery(e.target.value)}/></label></div>
        <div className="unified-member-grid" ref={memberGrid}>
          {memberLoading?<p>正在读取成员…</p>:memberError?<p role="status">读取失败 <button onClick={onOpen}>重试</button></p>:memberPage&&!memberPage.available?<p>暂无成员资料</p>:visible.length===0?<p>没有匹配的成员</p>:visible.map(m=>{
            const checked=members.some(v=>v.username===m.username)
            return <button key={m.username} className={`unified-member${checked?' selected':''}`} title={m.name} aria-pressed={checked} onClick={()=>onMembers(checked?members.filter(v=>v.username!==m.username):[...members,m])}><Avatar name={m.name} id={m.username} src={m.avatar_url} size={24}/><span>{m.name}</span><i>{checked?<Icon name="check" size={11}/>:null}</i></button>
          })}
        </div>
      </section>:null}
    </div>:null}
  </div>
}

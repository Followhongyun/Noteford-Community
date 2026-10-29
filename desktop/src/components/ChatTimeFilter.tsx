import {useEffect, useRef, useState} from 'react'
import ChatDatePicker from './ChatDatePicker'
import Icon from './Icon'

export function timeLabel(value?: number) {
  if (value == null) return ''
  const d = new Date(value * 1000)
  return `${d.getFullYear()}/${String(d.getMonth()+1).padStart(2,'0')}/${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:00`
}

export default function ChatTimeFilter({from,to,toNow,onFrom,onTo,onNow,onLocate,onRange,calendar,onCalendar}: {
  from?:number;to?:number;toNow:boolean
  onFrom(value:number|undefined):void;onTo(value:number|undefined):void;onNow():void
  onLocate(value:number):Promise<boolean>;onRange():void
  calendar:'from'|'to'|'point'|null;onCalendar(value:'from'|'to'|'point'|null):void
}) {
  const [mode,setMode]=useState<'range'|'point'>('range')
  const [point,setPoint]=useState<number>()
  const [notice,setNotice]=useState('')
  const request=useRef(0)
  const locate=useRef(onLocate)
  locate.current=onLocate
  useEffect(()=>{
    const token=++request.current
    if(mode!=='point'||point==null){setNotice('');return}
    const timer=window.setTimeout(()=>{
      setNotice('正在定位…')
      void locate.current(point).then(found=>{if(token===request.current)setNotice(found?'已定位，可查看聊天上下文':'未找到消息或暂时无法定位，请换个时间')})
    },240)
    return()=>{window.clearTimeout(timer);request.current++}
  },[mode,point])
  const fields=mode==='range'?(['from','to'] as const):(['point'] as const)
  return <section className="chat-time-filter" aria-label="时间">
    <div className="time-filter-heading"><strong>时间</strong><div className="time-mode" role="group" aria-label="时间使用方式">
      <button aria-pressed={mode==='range'} onClick={()=>{setMode('range');onCalendar(null);onRange()}}>筛选时段</button>
      <button aria-pressed={mode==='point'} onClick={()=>{setMode('point');onCalendar(null)}}>定位时间</button>
    </div></div>
    <div className="unified-time-fields">{fields.map(key=>{
      const label=key==='from'?'开始时间':key==='to'?'结束时间':'指定时间'
      const value=key==='from'?from:key==='to'?to:point
      return <div className="unified-time-row" key={key}><label>{label}</label><div className="unified-date-anchor">
        <button className="unified-date-trigger" aria-label={label} aria-expanded={calendar===key} onClick={()=>onCalendar(calendar===key?null:key)}><span>{key==='to'&&toNow?'截至目前':timeLabel(value)||(key==='point'?'选择日期与时间':'不限')}</span><Icon name="chevron" size={14}/></button>
        {calendar===key?<div className="unified-calendar-popover"><ChatDatePicker label={label} value={value} onChange={key==='from'?onFrom:key==='to'?onTo:setPoint} onNow={key==='to'?()=>{onNow();onCalendar(null)}:undefined}/></div>:null}
      </div></div>
    })}</div>
    {mode==='range'&&from!=null&&to!=null&&from>=to?<p className="unified-time-error" role="alert">结束时间须晚于开始时间</p>:null}
    {mode==='point'?<p className="time-locate-status" role="status">{notice||'从选定时间起，查看第一条消息及上下文'}</p>:null}
  </section>
}

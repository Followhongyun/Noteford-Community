import {useState} from 'react'
import Icon from './Icon'

function dayKey(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

export default function ChatDatePicker({value, onChange, label, onNow}: {value?: number; onChange(value: number | undefined): void; label: string; onNow?(): void}) {
  const today = new Date()
  const selected = value == null ? '' : dayKey(new Date(value * 1000))
  const [month, setMonth] = useState(() => new Date((value == null ? today : new Date(value * 1000)).getFullYear(), (value == null ? today : new Date(value * 1000)).getMonth(), 1))
  const hour = value == null ? '00' : String(new Date(value * 1000).getHours()).padStart(2, '0')
  function choose(date: Date, nextHour = hour) {
    const now = new Date()
    if (new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime() > now.getTime()) return
    const allowedHour = dayKey(date) === dayKey(now) ? Math.min(Number(nextHour), now.getHours()) : Number(nextHour)
    onChange(new Date(date.getFullYear(), date.getMonth(), date.getDate(), allowedHour).getTime() / 1000)
  }
  const offset = (month.getDay() + 6) % 7
  const days = Array.from({length: 42}, (_, index) => new Date(month.getFullYear(), month.getMonth(), index - offset + 1))
  return <div className="chat-calendar" aria-label={`${label}日历`}>
    <div className="calendar-heading">
      <button type="button" aria-label="上个月" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}><Icon name="back" size={16} /></button>
      <strong aria-live="polite">{month.getFullYear()}年{month.getMonth() + 1}月</strong>
      <button type="button" aria-label="下个月" disabled={new Date(month.getFullYear(), month.getMonth()+1, 1).getTime()>today.getTime()} onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}><Icon name="forward" size={16} /></button>
    </div>
    <div className="calendar-week">{['一', '二', '三', '四', '五', '六', '日'].map(day => <span key={day}>{day}</span>)}</div>
    <div className="calendar-days">
      {days.map(date => {
        const key = dayKey(date)
        return <button key={key} type="button" disabled={date.getTime()>today.getTime()} aria-label={key} aria-pressed={key === selected} aria-current={key === dayKey(today) ? 'date' : undefined}
          className={(date.getMonth() !== month.getMonth() ? 'outside-month ' : '') + (key === selected ? 'selected' : '')}
          onClick={() => { choose(date); setMonth(new Date(date.getFullYear(), date.getMonth(), 1)) }}>{date.getDate()}</button>
      })}
    </div>
    <div className="calendar-time">
      {onNow?<button type="button" className="calendar-now" onClick={onNow}>截至目前</button>:null}
      <select aria-label={`${label}小时`} value={hour} onChange={event => choose(value == null ? today : new Date(value * 1000), event.target.value)}>
        {Array.from({length: 24}, (_, index) => String(index).padStart(2, '0')).map(item => <option key={item} value={item} disabled={(value==null||selected===dayKey(today))&&Number(item)>today.getHours()}>{item}:00</option>)}
      </select>
    </div>
    <button className="calendar-clear" type="button" onClick={() => onChange(undefined)}>清除时间</button>
  </div>
}

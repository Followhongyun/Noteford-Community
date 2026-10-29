import {useEffect, useRef, useState} from 'react'
import type {Message} from '../types'
import Icon from './Icon'
export default function MacFileMessage({message, libraryId}: {message: Message; libraryId?: string}) {
  const [media, setMedia] = useState(message.media), [busy, setBusy] = useState(false), [notice, setNotice] = useState('')
  const pending = useRef(false), alive = useRef(true)
  useEffect(() => {alive.current = true; return () => {alive.current = false}}, [])
  useEffect(() => {if (!pending.current) setMedia(message.media)}, [message.media?.url, message.media?.status])
  const ready = media?.status === 'ready' && !!media.url
  async function action() {
    if (pending.current || !libraryId) return
    pending.current = true; setBusy(true); setNotice('')
    try {
      if (ready) {if (await window.noteford.saveMedia?.({url: media!.url!, name: message.title || '附件'})) {if (alive.current) setNotice('已保存')}}
      else {const next = await window.noteford.recheckMedia({library_id: libraryId, uid: message.uid}); if (alive.current) {setMedia(next); if (next?.status !== 'ready') setNotice('未取得原件，请稍后重试')}}
    } catch {if (alive.current) setNotice(ready ? '保存失败，请重试' : '下载失败，请重试')}
    finally {pending.current = false; if (alive.current) setBusy(false)}
  }
  return <div className="media-card file-card"><Icon name="file"/><div><strong>{message.title || '附件'}</strong><span>{ready ? '已下载' : busy ? '正在下载…' : '未下载'}</span><button type="button" disabled={busy || !libraryId} onClick={() => void action()}>{busy ? '处理中…' : ready ? '另存为' : notice ? '重试下载' : '下载附件'}</button>{notice && <p role="status">{notice}</p>}</div></div>
}

import useBackdropDismiss from '../useBackdropDismiss'
import AutoLoadMore from './AutoLoadMore'
import MacContentView from './MacContentView'
import LinkText from './LinkText'
import {useEffect,useRef,useState} from 'react'
import {createPortal} from 'react-dom'
import type {ContentPage,ContentSource,MessagePage} from '../types'
import Icon from './Icon'
import ChatView from './ChatView'
const categories=[['','全部'],['image','图片'],['video','视频'],['article','公众号'],['channel','视频号'],['link','外部链接'],['file','文件']]
const availability:Record<string,string>={ready:'原文件可读',ready_audio:'音频可读',ready_thumbnail:'仅缩略图',thumbnail_only:'仅缩略图',link_only:'链接待研究',message_only:'仅消息记录',encoded_voice:'待解码'}
function MacContentEmpty({error, filtered, onClear, onRetry}: {
 error: string; filtered: boolean; onClear(): void; onRetry(): void
}) {
 return <div className="content-empty" role="status">
  <strong>{error ? '素材暂时无法读取' : filtered ? '没有符合条件的素材' : '还没有可展示的素材'}</strong>
  <p>{error ? '请重试；已有资料仍保留在本机。' : filtered ? '试试其他关键词，或查看全部分类。' : '关注会话并完成同步后，这里会显示图片、链接与文件。'}</p>
  {error ? <button className="content-reset" onClick={onRetry}>重新读取</button> : filtered ? <button className="content-reset" onClick={onClear}>清除筛选</button> : null}
 </div>
}
function LegacyContentView({toolbarHost}: {toolbarHost?: HTMLElement|null}){
 const mac = window.noteford.platform === 'darwin'
 const [category,setCategory]=useState(''),[query,setQuery]=useState(''),[page,setPage]=useState<ContentPage|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[revision,setRevision]=useState(0),[context,setContext]=useState<MessagePage|null>(null),[source,setSource]=useState<ContentSource|null>(null),[contextBusy,setContextBusy]=useState(false)
 const generation=useRef(0),contextGeneration=useRef(0)
 const backdropDismiss = useBackdropDismiss(() => {contextGeneration.current++;setSource(null)}, false)
 useEffect(()=>{if(!source)return;const close=(event:KeyboardEvent)=>{if(event.key==='Escape'){contextGeneration.current++;setSource(null)}};window.addEventListener('keydown',close);return()=>window.removeEventListener('keydown',close)},[source])
 useEffect(()=>{let alive=true;const id=++generation.current;setPage(null);setBusy(true);const timer=setTimeout(()=>{window.noteford.content({category,query}).then(result=>{if(alive&&id===generation.current){setPage(result);setError('')}}).catch(e=>{if(alive)setError(String(e))}).finally(()=>{if(alive)setBusy(false)})},200);return()=>{alive=false;clearTimeout(timer)}},[category,query,revision])
 useEffect(()=>{const timer=setInterval(()=>setRevision(v=>v+1),60000);return()=>{clearInterval(timer);contextGeneration.current++}},[])
 async function more(){if(page?.next_offset==null||busy)return;const id=generation.current;setBusy(true);setError('');try{const next=await window.noteford.content({category,query,offset:page.next_offset});if(id===generation.current)setPage({...next,items:[...page.items,...next.items]})}catch(e){if(id===generation.current)setError(String(e))}finally{if(id===generation.current)setBusy(false)}}
 async function openSource(s:ContentSource){const id=++contextGeneration.current;setSource(s);setContext(null);setContextBusy(true);try{const result=await window.noteford.messages({conversation:s.conversation_id,before:s.timestamp+1,limit:300});if(id===contextGeneration.current){if(!result.messages.some(m=>m.uid===s.message_uid))throw Error('原消息已变化，请刷新后重试');setContext(result)}}catch(e){if(id===contextGeneration.current)setError(String(e))}finally{if(id===contextGeneration.current)setContextBusy(false)}}
 async function older(){if(!context?.next_before||!source||contextBusy)return;const id=contextGeneration.current;setContextBusy(true);try{const next=await window.noteford.messages({conversation:source.conversation_id,before:context.next_before,limit:80});if(id===contextGeneration.current)setContext({...next,messages:[...next.messages,...context.messages]})}catch(e){setError(String(e))}finally{if(id===contextGeneration.current)setContextBusy(false)}}
 const refreshButton = <button className={mac ? 'mac-page-action' : undefined} disabled={busy} onClick={()=>setRevision(v=>v+1)}><Icon name="refresh" size={17}/>刷新</button>
 return <main className="content-screen" aria-labelledby={mac ? 'workspace-page-title' : undefined}>
 {mac ? <>{toolbarHost && createPortal(refreshButton, toolbarHost)}</> : <header className="content-heading"><div><h1>内容筛选</h1><p>提取已关注会话中的素材，保留原消息与转发出处。</p></div>{refreshButton}</header>}
 <div className="content-tools"><div className="content-tabs" aria-label="内容分类">{categories.map(([key,label])=><button key={key} aria-pressed={category===key} onClick={()=>setCategory(key)}>{label}{page&&<span>{key?page.counts[key]||0:Object.values(page.counts).reduce((a,b)=>a+b,0)}</span>}</button>)}</div><label className="content-search"><Icon name="search" size={17}/><input aria-label="搜索素材" placeholder="搜索标题、内容或来源" value={query} onChange={e=>setQuery(e.target.value)}/></label></div>
 {error&&<p role="alert" className="content-error">{error}</p>}
 <div className="content-list">{!page&&busy?<p className="content-empty">正在整理素材…</p>:!page?.items.length?window.noteford.platform === 'darwin' ? <MacContentEmpty error={error} filtered={Boolean(query.trim() || category)} onClear={()=>{setQuery('');setCategory('')}} onRetry={()=>setRevision(v=>v+1)}/> : <p className="content-empty">没有匹配的素材；新消息同步后会自动出现在这里。</p>:page.items.map(item=><article className="content-item" key={item.id}><div className="content-kind"><Icon name={item.category==='image'?'image':item.category==='video'||item.category==='channel'?'video':item.category==='file'?'file':'link'} size={22}/></div><div className="content-copy"><div className="content-meta"><span>{categories.find(([k])=>k===item.category)?.[1]}</span><span>{availability[item.availability]||'暂不可读'}</span></div><h2>{item.title}</h2>{item.text&&item.text!==item.title&&<p><LinkText text={item.text} /></p>}{item.url&&<button className="content-url" title={item.url} onClick={()=>void window.noteford.openExternal({url:item.url!}).catch(e=>setError(String(e)))}>{item.url}</button>}<div className="content-sources"><button onClick={()=>void openSource(item.sources[0])}>{item.sources[0].conversation_name} · {item.sources[0].sender} · {new Date(item.sources[0].timestamp*1000).toLocaleString('zh-CN')} · 查看原消息</button>{item.sources.length>1&&<details><summary><Icon name="chevron" size={14}/>共 {item.sources.length} 处来源</summary>{item.sources.slice(1).map(s=><button key={s.message_uid} onClick={()=>void openSource(s)}>{s.conversation_name} · {s.sender} · {new Date(s.timestamp*1000).toLocaleString('zh-CN')}</button>)}</details>}</div></div></article>)}{page?.next_offset!=null&&<AutoLoadMore key={JSON.stringify([category,query,revision])} cursor={page.next_offset} busy={busy} onLoad={more}/>}</div>
 {source&&<div {...backdropDismiss} className="content-overlay" role="dialog" aria-modal="true" aria-label="原消息与此前讨论"><section><header><strong>原消息与此前讨论</strong><button aria-label="关闭原消息" onClick={()=>{contextGeneration.current++;setSource(null)}}><Icon name="close"/></button></header>{error&&<p role="alert" className="content-error">{error}</p>}<ChatView page={context} loading={!context&&contextBusy} loadingOlder={!!context&&contextBusy} onLoadOlder={()=>void older()}/></section></div>}
 </main>
}

export default function ContentView(props: {toolbarHost?: HTMLElement|null; cache?: Map<string, ContentPage>; viewState?: {category: string; query: string; conversation: string}; onNavigateSource?(source: ContentSource, signal?: AbortSignal): Promise<void>}) {
 return window.noteford.platform === "darwin" ? <MacContentView {...props}/> : <LegacyContentView {...props}/>
}

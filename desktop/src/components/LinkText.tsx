import {useState} from 'react'
import type {ReactNode, MouseEvent} from 'react'
import {splitLinks, webUrl} from '../links'
import './LinkText.css'

export function ExternalLink({url, children}: {url: string; children: ReactNode}) {
  const [failed, setFailed] = useState(false)
  const href = webUrl(url)
  if (!href) return <>{children}</>
  function open(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault()
    event.stopPropagation()
    if (event.type === 'auxclick' && event.button !== 1) return
    setFailed(false)
    void window.noteford.openExternal({url: href!}).catch(() => setFailed(true))
  }
  return <><a className="message-link" href={href} title={href} onClick={open} onAuxClick={open}>{children}</a>{failed ? <span className="message-link-error" role="alert">无法打开链接，请重试</span> : null}</>
}

export default function LinkText({text}: {text: string}) {
  return <>{splitLinks(text.trimEnd()).map((part, index) => part.url
    ? <ExternalLink key={index} url={part.url}>{part.text}</ExternalLink>
    : <span key={index}>{part.text}</span>)}</>
}

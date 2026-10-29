import { useEffect, useRef, useState } from 'react'
import Icon from './Icon'
import Avatar from './Avatar'
import FollowName from './FollowName'
import type { Conversation, SelectionState } from '../types'

type FollowGuide = {name: string; following: number; reading: boolean; onDone(): void; onSkip(): void}

type Category = 'all' | 'groups' | 'official' | 'people'
const categories = [
  { key: 'all', label: '全部对象', icon: 'list' },
  { key: 'groups', label: '群聊', icon: 'group' },
  { key: 'official', label: '公众号', icon: 'official' },
  { key: 'people', label: '联系人', icon: 'person' },
] as const
const states: { key: 'all' | SelectionState; label: string }[] = [
  { key: 'all', label: '全部状态' },
  { key: 'following', label: '已关注' },
  { key: 'paused', label: '已暂停' },
  { key: 'unselected', label: '未关注' },
]
function categoryOf(row: Conversation): Exclude<Category, 'all'> {
  if (window.noteford.platform === 'darwin' && row.contact_kind) return row.contact_kind
  return row.is_group ? 'groups' : row.username.startsWith('gh_') ? 'official' : 'people'
}

export default function ContactDirectory({ rows, search, loading, busyUsername, onSearch, onFollow, onPause, onBatch, guide }: {
  guide?: FollowGuide
  rows: Conversation[]
  search: string
  loading: boolean
  busyUsername: string | null
  onSearch(value: string): void
  onFollow(row: Conversation): void
  onPause(row: Conversation): void
  onBatch(rows: Conversation[], following: boolean, signal: AbortSignal): Promise<string[]>
}) {
  const [category, setCategory] = useState<Category>('all')
  const [state, setState] = useState<'all' | SelectionState>('all')
  const [activeMarquee, setActiveMarquee] = useState<string | null>(null)
  const mac = window.noteford.platform === 'darwin'
  const hoverText = (id: string, hovered: boolean) => {
    setActiveMarquee(current => hovered ? id : current === id ? null : current)
  }
  const categoryRows = rows.filter(row => category === 'all' || categoryOf(row) === category)
  const visible = categoryRows.filter(row => state === 'all' || row.selection_state === state)
    .slice().sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))

  const categoryTabs = <nav className="follow-categories" aria-label="对象分类">
        {categories.map(item => <button key={item.key} type="button" aria-pressed={category === item.key} onClick={() => setCategory(item.key)}><Icon name={item.icon} size={17} />{item.label}<span>{rows.filter(row => item.key === 'all' || categoryOf(row) === item.key).length}</span></button>)}
      </nav>
  const searchField = <label className="follow-search"><Icon name="search" size={17} /><input aria-label="搜索关注对象" value={search} onChange={event => onSearch(event.target.value)} placeholder="搜索名称" spellCheck={false} /></label>

  if (mac) return <MacFollowDirectory rows={rows} search={search} loading={loading} onSearch={onSearch} onBatch={onBatch} guide={guide} />

  return (
    <main className="follow-workspace" aria-labelledby={mac ? 'workspace-page-title' : undefined}>
      {guide && <section className="mac-follow-guide" aria-labelledby="mac-follow-guide-title">
        <div><h2 id="mac-follow-guide-title">先选择你想关注的内容</h2>
          <p role="status">已关注 {guide.following} 个{guide.reading ? ' · 正在同步…' : ''}</p></div>
        <div className="mac-follow-guide-actions"><button className="settings-button settings-save" disabled={!guide.following || busyUsername !== null} onClick={guide.onDone}>进入消息</button>
          <button className="settings-button" onClick={guide.onSkip}>稍后选择</button></div>
      </section>}
      {mac ? <header className="follow-header">{categoryTabs}{searchField}</header> : <>
        <header className="follow-header"><div><h1>关注管理</h1><p>选择你想持续关注的群聊、公众号和联系人</p></div>{searchField}</header>
        {categoryTabs}
      </>}
      <div className="follow-toolbar">
        <div className="follow-states" role="group" aria-label="关注状态">{states.map(item => <button key={item.key} type="button" aria-pressed={state === item.key} onClick={() => setState(item.key)}>{item.label}<span>{categoryRows.filter(row => item.key === 'all' || row.selection_state === item.key).length}</span></button>)}</div>
      </div>
      <div className="follow-list" aria-busy={loading}>
        {visible.length ? <ul>{visible.map(row => {
          const following = row.selection_state === 'following'
          const busy = busyUsername === row.username
          const empty = mac && !row.has_local_messages
          return <li className="follow-row" key={row.username}>
            <Avatar name={row.name} id={row.username} src={row.avatar_url} size={36} />
            <div className={`follow-card-copy${busy || empty ? ' preparing' : ''}`}>
              <FollowName text={row.name} active={!mac && activeMarquee === `name:${row.username}`} onHover={hovered => hoverText(`name:${row.username}`, hovered)} />
              {busy || empty ? <FollowName hint text={busy ? following ? '正在取消关注…' : '准备中，首次关注可能需要一些时间…' : '本机暂无消息'} active={activeMarquee === `hint:${row.username}`} onHover={hovered => hoverText(`hint:${row.username}`, hovered)} /> : null}
            </div>
            <button className={`follow-action${following ? ' checked' : ' start'}`} type="button" disabled={busyUsername !== null} aria-pressed={following} aria-busy={busy} title={busy ? following ? '正在取消关注' : '正在准备关注内容' : following ? '取消关注' : '关注'} aria-label={`${busy ? following ? '取消中' : '准备中' : following ? '取消关注' : '关注'} ${row.name}`} onClick={() => following ? onPause(row) : onFollow(row)}>{busy ? <Icon name="loading" size={18} className="follow-loading" /> : following ? <Icon name="followed" size={22} /> : '关注'}</button>
          </li>
        })}</ul> : <div className="follow-empty"><Icon name="group" size={30} /><h2>{loading ? '正在读取关注对象…' : '没有符合条件的对象'}</h2><p>{!loading && (search.trim() ? '试试其他名称，或切换分类和状态。' : '切换分类或状态，查看其他关注对象。')}</p></div>}
      </div>
      {!mac && <footer className="follow-footer">关注后可在消息页阅读与搜索；取消关注会停止后续索引，已有本地内容保留。</footer>}
    </main>
  )
}


type MacFollowProps = {
  rows: Conversation[]
  search: string
  loading: boolean
  onSearch(value: string): void
  onBatch(rows: Conversation[], following: boolean, signal: AbortSignal): Promise<string[]>
  guide?: FollowGuide
}

function MacFollowDirectory({rows, search, loading, onSearch, onBatch, guide}: MacFollowProps) {
  const [category, setCategory] = useState<Category>('all')
  const [selection, setSelection] = useState<{side: boolean; ids: Set<string>}>({side: false, ids: new Set()})
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const operation = useRef<AbortController | null>(null)
  useEffect(() => () => operation.current?.abort(), [])
  const visible = rows.filter(row => (category === 'all' || categoryOf(row) === category)
    && (!search.trim() || row.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())))
    .slice().sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  const selected = visible.filter(row => selection.ids.has(row.username) && (row.selection_state === 'following') === selection.side)
  function clearSelection() {
    setSelection({side: false, ids: new Set()})
    setNotice('')
  }
  function toggle(row: Conversation) {
    const side = row.selection_state === 'following'
    setNotice('')
    setSelection(current => {
      const ids = new Set(current.side === side ? current.ids : [])
      if (ids.has(row.username)) ids.delete(row.username)
      else ids.add(row.username)
      return {side, ids}
    })
  }
  async function applySelection() {
    if (operation.current || !selected.length || loading) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(true)
    setNotice('')
    try {
      const failed = await onBatch(selected, !selection.side, controller.signal)
      if (!controller.signal.aborted) {
        setSelection({side: selection.side, ids: new Set(failed)})
        setNotice(failed.length ? `${failed.length} 项未完成，请重试` : `已${selection.side ? '取消关注' : '关注'} ${selected.length} 项`)
      }
    } catch {
      if (!controller.signal.aborted) setNotice('操作未完成，请重试')
    } finally {
      operation.current = null
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  const guideStep = guide?.following ? 3 : selected.length && !selection.side ? 2 : 1
  return <main className={`follow-workspace follow-transfer${guide ? ' has-follow-guide' : ''}`} aria-labelledby="workspace-page-title">
    {guide && <section className="follow-onboarding" aria-labelledby="follow-onboarding-title">
      <div className="follow-onboarding-heading">
        <div><h2 id="follow-onboarding-title">先关注你想看的内容</h2>
          <p>选择想让 AI 读取的群聊、联系人或公众号，之后随时调整。</p></div>
        <button className="settings-button settings-save" disabled={!guide.following || busy || loading || selected.length > 0} onClick={guide.onDone}>开始使用<Icon name="forward" size={16}/></button>
      </div>
      <ol className="follow-onboarding-steps" aria-label="首次关注步骤">
        <li aria-current={guideStep === 1 ? 'step' : undefined} className={guideStep > 1 ? 'complete' : ''}><span className="follow-step-number">1</span><div><strong>选择对象</strong></div></li>
        <li aria-current={guideStep === 2 ? 'step' : undefined} className={guideStep > 2 ? 'complete' : ''}><span className="follow-step-number">2</span><div><strong>点击关注</strong></div></li>
        <li aria-current={guideStep === 3 ? 'step' : undefined}><span className="follow-step-number">3</span><div><strong>开始阅读</strong></div></li>
      </ol>
      {window.noteford.platform === 'darwin' && <p>连接已完成。可退出当前微信，重新登录官方微信，并使用截屏快捷键确认功能正常。</p>}
    </section>}
    <header className="follow-transfer-header">
      <nav className="follow-transfer-categories" aria-label="对象分类">
        {(['all', 'groups', 'people', 'official'] as const).map(key => <button type="button" key={key} disabled={busy}
          aria-pressed={category === key} onClick={() => {setCategory(key); clearSelection()}}>
          {{all: '全部', groups: '群聊', people: '联系人', official: '公众号'}[key]}
        </button>)}
      </nav>
      <div className="follow-transfer-actions">
        <label className="follow-search"><Icon name="search" size={16} /><input aria-label="搜索关注对象" value={search} disabled={busy}
          onChange={event => {clearSelection(); onSearch(event.target.value)}} placeholder="搜索名称" spellCheck={false} /></label>
        <button className={`follow-batch-action${guide && guideStep === 2 ? ' guide-target' : ''}`} type="button" disabled={busy || loading || !selected.length} onClick={() => void applySelection()}>
          {busy ? '处理中…' : selection.side && selected.length ? '取消关注' : '关注'}{selected.length ? ` (${selected.length})` : ''}
        </button>
      </div>
    </header>
    {notice && <p className="follow-transfer-notice" role="status">{notice}</p>}
    <div className="follow-transfer-columns" aria-busy={busy || loading}>
      {[true, false].map(side => {
        const items = visible.filter(row => (row.selection_state === 'following') === side)
        const title = side ? '已关注' : '未关注'
        const allSelected = !!items.length && selection.side === side && items.every(row => selection.ids.has(row.username))
        return <section className="follow-transfer-column" key={title} aria-label={title}>
          <header><h2>{title}<span>{items.length}</span></h2>
            <label className="follow-select-all"><input type="checkbox" aria-label={`全选${title}结果`} checked={allSelected} disabled={busy || loading || !items.length}
              onChange={() => {setNotice(''); setSelection({side, ids: new Set(allSelected ? [] : items.map(row => row.username))})}} />全选</label>
          </header>
          <ul className="follow-transfer-list">
            {items.map(row => <li key={row.username}>
              <label className={`follow-transfer-row${selection.side === side && selection.ids.has(row.username) ? ' selected' : ''}`}>
                <input type="checkbox" aria-label={`选择${title}对象 ${row.name}`} checked={selection.side === side && selection.ids.has(row.username)} disabled={busy || loading} onChange={() => toggle(row)} />
                <Avatar name={row.name} id={row.username} src={row.avatar_url} size={32} />
                <span className="follow-transfer-name" title={row.name}>{row.name}</span>
              </label>
            </li>)}
            {!items.length && <li className="follow-transfer-empty">{loading ? '正在读取…' : search.trim() || category !== 'all' ? '没有匹配结果' : side ? '暂无已关注对象' : '暂无未关注对象'}</li>}
          </ul>
        </section>
      })}
    </div>
  </main>
}

import { useEffect, useState } from 'react'
import { applyOnboardingKey, loadingPermissionGuide } from '@noteford/permission-guide'
import Icon from './Icon'
import type { PermissionGuide as PermissionGuideModel } from '../types'
import type { WechatConnectCard } from '../types'

export default function PermissionGuide({
  guide,
  grantingId,
  grantNotice = '',
  grantNoticeId = null,
  onGrant,
  onRestart,
  onContinue,
  step = 'permissions',
  connect = null,
  onStartScan,
  onStep,
}: {
  guide: PermissionGuideModel | null
  grantingId: string | null
  grantNotice?: string
  grantNoticeId?: string | null
  onGrant: (id: string) => void
  onRestart: () => void
  onContinue: () => void
  step?: 'permissions' | 'connect'
  connect?: WechatConnectCard | null
  onStartScan?: () => void
  onStep?: (step: 'permissions' | 'connect') => void
}) {
  const [downloadFailed, setDownloadFailed] = useState(false)
  const page = guide ?? loadingPermissionGuide()
  const onConnect = step === 'connect'
  const canLeave = page.canContinue && !page.loading && (!onConnect || !!connect?.canContinue)

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target
      if (event.key === 'Enter' && target instanceof HTMLElement && target.closest('button, input, textarea, a, select')) return
      const result = applyOnboardingKey({
        platform: 'darwin',
        step: onConnect ? 'connect' : 'permissions',
        key: event.key,
        canContinue: page.canContinue && !page.loading,
        connectCanContinue: !!connect?.canContinue && page.canContinue && !page.loading,
      })
      if (!result.handled) return
      event.preventDefault()
      if (!result.changed) return
      if (result.step === 'permissions') onStep?.('permissions')
      else onContinue()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onContinue, onStep, onConnect, page.canContinue, page.loading, connect?.canContinue])

  const title = onConnect && connect ? connect.pageTitle : page.title
  const intro = onConnect && connect ? connect.intro : page.intro
  const currentIndex = onConnect ? Math.max(page.steps.findIndex(item => item.id === 'connect'), 1) : 0

  return (
    <main className="permission-guide" aria-busy={page.loading || undefined} data-permission-guide={page.loading ? 'loading' : 'ready'} data-onboarding-step={onConnect ? 'connect' : 'permissions'}>
      <div className="permission-drag" aria-hidden="true" />
      <div className="permission-guide-body">
        <div className="permission-mark">
          <Icon name="permission" size={26} />
        </div>
        <h1>{title}</h1>
        <p className="permission-intro">{intro}</p>
        {onConnect && connect ? (
          <div className="permission-cards">
            <article className="permission-card" data-permission="wechat-connect" data-status={connect.status === 'done' ? 'granted' : connect.status}>
              <div className="permission-card-copy">
                <h2>{connect.title}</h2>
                <p>{connect.detail}</p>
                <p className="permission-card-risk">{connect.risk}</p>
                {connect.status !== 'done' && <div className="wechat-download">
                  <span>仅支持微信官网版</span>
                  <a href="https://mac.weixin.qq.com/" onClick={event => {
                    event.preventDefault()
                    setDownloadFailed(false)
                    void window.noteford.openExternal({url: 'https://mac.weixin.qq.com/'}).catch(() => setDownloadFailed(true))
                  }}>前往官网下载 <Icon name="external" size={14}/></a>
                  {downloadFailed && <span role="alert">无法打开浏览器，请访问 mac.weixin.qq.com</span>}
                </div>}
              </div>
              {connect.status === 'done' ? (
                <span className="permission-granted">完成</span>
              ) : connect.canStart ? (
                <button type="button" className="permission-grant" onClick={() => onStartScan?.()} disabled={!connect.canStart}>
                  {connect.actionLabel}
                </button>
              ) : (
                <span className="permission-status" data-scan-status={connect.status}>{connect.actionLabel}</span>
              )}
            </article>
          </div>
        ) : page.cards.length > 0 ? (
          <div className="permission-cards">
            {page.cards.map(card => (
              <article className="permission-card" key={card.id} data-permission={card.id} data-status={card.status}>
                <div className="permission-card-copy">
                  <h2>
                    {card.title}
                    {card.optionalLabel ? <span className="permission-optional">{card.optionalLabel}</span> : null}
                  </h2>
                  <p>{card.detail}</p>
                  {card.restartNote ? <p className="permission-card-note">{card.restartNote}</p> : null}
                  {card.gapNote ? <p className="permission-card-note">{card.gapNote}</p> : null}
                  {card.identityNote ? <p className="permission-card-note">{card.identityNote}</p> : null}
                  {grantNotice && card.id === grantNoticeId ? <p className="permission-card-note">{grantNotice}</p> : null}
                  {card.canRestart && card.status !== 'restart_required' ? (
                    <button type="button" className="permission-grant" onClick={onRestart}>已打开开关？重启 Noteford</button>
                  ) : null}
                </div>
                {card.status === 'granted' ? (
                  <span className="permission-granted">{card.actionLabel}</span>
                ) : (
                  <button
                    type="button"
                    className="permission-grant"
                    onClick={() => card.status === 'restart_required' ? onRestart() : onGrant(card.id)}
                    disabled={grantingId !== null || page.loading}
                    aria-busy={grantingId === card.id || undefined}
                  >
                    {grantingId === card.id ? '正在请求…' : card.actionLabel}
                  </button>
                )}
              </article>
            ))}
          </div>
        ) : null}
      </div>
      <div className="permission-guide-footer">
        <div className="permission-steps" role="group" aria-label="引导步骤">
          {page.steps.map((item, index) => {
            const current = index === currentIndex
            const locked = page.loading || (index > currentIndex && !page.canContinue)
            return (
              <button
                key={item.id}
                type="button"
                className={current ? 'is-current' : ''}
                aria-label={item.label}
                aria-current={current ? 'step' : undefined}
                disabled={locked}
                onClick={() => {
                  if (current || locked) return
                  if (item.id === 'permissions') onStep?.('permissions')
                  else if (item.id === 'connect' && page.canContinue) onContinue()
                }}
              />
            )
          })}
        </div>
        <button
          type="button"
          className="permission-continue"
          disabled={!canLeave}
          title={canLeave ? undefined : onConnect ? '请先完成扫码登录' : '请先允许必需权限'}
          onClick={() => onContinue()}
        >
          {page.continueLabel || '继续'}
          <Icon name="forward" size={16} />
        </button>
      </div>
      <p className="permission-hint">{page.hint}</p>
      <p className="permission-live" aria-live="polite">
        {onConnect && connect
          ? `${connect.title} ${connect.actionLabel}`
          : page.cards.map(card => `${card.title} ${grantingId === card.id ? '正在请求…' : card.actionLabel}${card.gapNote ? ` ${card.gapNote}` : ''}${grantNotice && card.id === grantNoticeId ? ` ${grantNotice}` : ''}`).join('，')}
      </p>
    </main>
  )
}

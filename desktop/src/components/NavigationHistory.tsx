import Icon from './Icon'

export default function NavigationHistory({canBack, canForward, back, forward, disabled = false}: {canBack: boolean; canForward: boolean; back(): void; forward(): void; disabled?: boolean}) {
  return <nav className="navigation-history" aria-label="浏览历史">
    <button type="button" aria-label="上一步" title="上一步" disabled={disabled || !canBack} onClick={back}><Icon name="chevron" size={20} className="navigation-back"/></button>
    <button type="button" aria-label="下一步" title="下一步" disabled={disabled || !canForward} onClick={forward}><Icon name="chevron" size={20}/></button>
  </nav>
}

import Icon from './Icon'
import {ExternalLink} from './LinkText'

const references = [
  {name: 'wx-cli', repo: 'pandorafuture/wx-cli', license: 'MIT', licensePath: 'main/LICENSE', scope: '本地读取架构、消息解码与会话解析思路'},
  {name: 'WeChatBridge', repo: 'freestylefly/WeChatBridge', license: 'MIT', licensePath: 'main/LICENSE', scope: '暂存后原子切换与本地数据可靠性思路'},
]

export default function OpenSourceCredits({compact = false}: {compact?: boolean}) {
  return <section className={compact ? "mac-settings-credits" : "settings-group"} aria-label={compact ? "开源与致谢" : undefined} aria-labelledby={compact ? undefined : "settings-credits-title"}>
    {!compact && <h2 id="settings-credits-title">贡献与致谢</h2>}
    <details className={compact ? "mac-settings-disclosure" : undefined}>
      <summary className="settings-detail-button">开源项目与许可证{compact && <Icon name="chevron" size={15}/>}</summary>
      <div className="open-source-reference-list">
        <h3>思路与研究参考</h3>
        {references.map(item => <div className="preference-row" key={item.repo}>
          <div className="preference-label">
            <h3><ExternalLink url={`https://github.com/${item.repo}`}>{item.name}</ExternalLink></h3>
            <p className="preference-description">{item.repo} · {item.scope}</p>
          </div>
          <div className="preference-control">{item.licensePath ? <ExternalLink url={`https://github.com/${item.repo}/blob/${item.licensePath}`}>{item.license}</ExternalLink> : <span>{item.license}</span>}</div>
        </div>)}
      </div>
    </details>
  </section>
}

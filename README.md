# Noteford Community

**简体中文** · [English](README.en.md)

把本机微信资料整理成可阅读、可搜索、可交给 AI 使用的本地资料库。

[下载 Mac 版 0.1.1](https://github.com/Followhongyun/Noteford-Community/releases/latest) · [安装与 AI 接入](docs/INSTALL.md) · [更新记录](CHANGELOG.md)

## 能做什么

- **阅读与搜索**：浏览关注会话，搜索消息，定位原消息和上下文。
- **资料同步**：手动、定时或由已授权 AI 触发同步；每次有效触发重新计算下次同步时间。
- **媒体与素材**：查看缓存图片、视频和附件，筛选聊天中的素材；部分原件可能已过期或不可获取。
- **折叠聊天提示**：将单独表情、动图、撤回提示和拍一拍合并为可展开的计数提示。
- **Skill 与 MCP**：让支持本机工具调用的 AI 在授权范围内检索微信资料，并引用原消息依据。
- **GitHub 更新**：检查并下载后续更高正式版本，正常退出应用时安装。

开源版不包含「发现」首页，不需要注册洪云产品账号。它与会员版独立维护，不承诺同步会员版后续功能。

## 下载并开始使用

| 项目 | 当前支持 |
| --- | --- |
| 版本 | **0.1.1** |
| 电脑 | Apple 芯片 Mac（M1 及后续） |
| 系统 | macOS 13 或更新版本 |
| 安装包 | `Noteford-0.1.1-macOS.dmg`，已签名并通过 Apple 公证 |
| Intel / Windows | 当前不提供已验收的安装包 |

1. 从 [最新 Release](https://github.com/Followhongyun/Noteford-Community/releases/latest) 下载 DMG，打开后将「Noteford 开源版」拖入 **Applications**。
2. 从 Applications 启动，按提示连接本人微信并确认系统权限。
3. 选择关注会话，首次同步完成后开始阅读、搜索和查看素材。

应用自带运行时，无需另外安装 Node.js 或 Python。资料位于 `~/Noteford Community/Data`；开源版可与会员开发版并存，账号、资料和授权独立。

**已有早期 community 版本？** 先正常退出，再手动安装 0.1.1 替换应用，保留资料目录。旧预推广下载已下架；这次是编号整理，功能没有退回早期版本，也不会自动降级。

## 连接你的 AI

保持 Noteford 开源版运行，把下面这段话交给支持本机命令的 AI：

> 请安装并连接 Noteford 开源版：`npx skills add Followhongyun/Noteford-Community --skill noteford-community`。按照 Skill 完成当前客户端 MCP 注册、首次授权，并实际调用 list_libraries 和 get_status 验证连接。

安装 Skill 需要 Node.js/npm；Codex 注册脚本还需要 Python 3。仅执行安装命令不会自动授予聊天访问权限，首次读取须由你在 Noteford 中亲自确认。

Codex 中的 MCP 名称为 `noteford-community`。配置后若当前对话没有加载工具，使用新对话；其他客户端按其官方 MCP 配置方式接入。本机 stdio 接口不适用于纯云端客户端。详见 [完整接入步骤与排障](docs/INSTALL.md)。

## 更新与联系

设置页「应用更新」可检查 GitHub 更新。发现更高正式版本后自动下载，正常退出时安装；不会跨到会员版渠道。

目前版本固定为 **0.1.1**，解除冻结需维护者明确确认；同版本修订不会触发自动升级。[版本规则](docs/DEVELOPMENT.md#版本号规范2026-10-29-起)

后续更新请联系洪云：在应用设置页底部，将鼠标移到二维码图标上即可查看微信名片。

## 从源码构建

需要 Node.js 24（最低 22.19）、Python 3.11，以及 Mac 构建工具：

```sh
node desktop/scripts/install-dependencies.cjs
npm --prefix desktop run build
npm --prefix desktop run package:mac -- --dir
```

第三方自行构建需配置自己的签名身份；维护者证书不随源码提供。开发入口、测试与构建限制见 [开发说明](docs/DEVELOPMENT.md)。

## 文档与许可

[产品能力](docs/PRODUCT.md) · [架构](docs/ARCHITECTURE.md) · [数据与权限](docs/DATA-AND-SECURITY.md) · [验收记录](docs/STATUS.md) · [贡献指南](CONTRIBUTING.md) · [安全问题](SECURITY.md)

原创源码采用 [AGPL-3.0-only](LICENSE)，第三方组件保留各自许可及 [声明](NOTICE.md)。商业使用与再分发按适用许可证进行，会员服务不额外限制开源许可证授予的权利。

Noteford 与腾讯没有官方关联。仅处理本人有权访问的资料，不向微信发送消息；微信版本、媒体格式与历史资源的可用性存在差异。

# Noteford Community

Noteford 是一款本地微信资料阅读应用：在授权后读取本机资料，浏览会话、搜索消息、查看媒体，并通过 MCP 向 AI 客户端提供受权限控制的读取接口。

本仓库公开 **Mac 0.1.5（2026-10-25）的发布源码快照**，不包含随后加入的「发现」首页。后续会员版在独立私有仓库迭代；本仓库不承诺同步后续功能。当前代码包含社区版独立运行改造。

## 开源版

基于发现页之前的 Mac 0.1.5，已移除 Followhongyun 产品账号、登录注册、官方自动更新和依赖产品账号的 Nexus 联动。设置底部显示「后续更新请联系洪云」本地微信账号连接、同步、阅读、搜索、媒体和 MCP 授权保留。

应用显示为「Noteford 开源版」，Bundle ID 为 `com.followhongyun.noteford.community`；Mac 资料存放在 `~/Noteford Community/Data`，应用缓存、MCP 发现目录及钥匙串服务独立。可与会员开发版并存，不导入或接管其账号、资料或授权。

## 下载安装

在 [GitHub Releases](https://github.com/Followhongyun/Noteford-Community/releases/latest) 下载 `Noteford-0.1.5-community.1-macOS.dmg`，打开后将「Noteford 开源版」拖入 Applications，再从 Applications 启动。

支持 Apple 芯片 Mac（M1 及后续），最低 macOS 13；此安装包不支持 Intel Mac。首次连接微信时按应用提示授权并扫码。安装包自带运行时，使用应用不需要安装 Node.js 或 Python；AI Skill 需在支持本机命令的客户端中安装并完成首次授权。

```sh
npx skills add Followhongyun/Noteford-Community --skill noteford-community
```

将上述命令交给本机 AI 并明确要求“安装并连接”。最新版 Skill 会指导 Codex 注册独立的 `noteford-community` MCP，再验证授权读取；仅在终端运行安装命令只安装 Skill 文件。已经安装 Skill 的用户重新运行上述命令更新即可，无需重装 Mac 应用。其他客户端按自身官方 MCP 配置方式接入。

## 开发与构建

Node.js 24（最低 22.19）和 Python 3.11：

```sh
node desktop/scripts/install-dependencies.cjs
npm --prefix desktop run build
npm --prefix desktop run package:mac -- --dir
```

Mac 打包入口准备独立 Python 环境，冻结服务并收集许可材料。分发签名使用配置的 Developer ID；第三方自行构建时须配置自己的签名身份。源码启动入口和详细测试说明见 [开发说明](docs/DEVELOPMENT.md)。

## 文档

[产品能力](docs/PRODUCT.md) · [架构](docs/ARCHITECTURE.md) · [模块](docs/MODULES.md) · [接口](docs/CONTRACTS.md) · [数据与权限](docs/DATA-AND-SECURITY.md) · [版本状态](docs/STATUS.md) · [第三方声明](NOTICE.md)

## 许可

原创程序源码采用 [AGPL-3.0-only](LICENSE)。第三方组件保留各自许可证和声明。商业使用与再分发按适用许可证进行；会员服务不构成对开源许可证权利的额外限制。

Noteford 与腾讯没有官方关联。只能处理本人有权访问的资料；应用不向微信发送消息。历史实验路径及兼容性限制见相应文档，不保证所有微信版本、媒体格式或云端历史资源可用。

# Noteford Community

Noteford 是一款本地微信资料阅读应用：在授权后读取本机资料，浏览会话、搜索消息、查看媒体，并通过 MCP 向 AI 客户端提供受权限控制的读取接口。

本仓库公开 **Mac 0.1.5（2026-10-25）的发布源码快照**，不包含随后加入的「发现」首页。后续会员版在独立私有仓库迭代；本仓库不承诺同步后续功能。此处发布源码，不提供新制作的安装包。

## 版本边界

- 包含会话阅读、搜索、关注管理、媒体缓存、MCP、可选产品登录和更新实现。
- Mac 基线面向 Apple Silicon、macOS 13+；保留 Windows 源码，但本次不宣称完成 Windows 或 Intel Mac 验收。
- 本地使用不要求登录。官方账号、更新及 Nexus 联动服务不随源码提供，访问这些服务仍受各自授权约束。
- 不包含聊天、数据库、账号配置、令牌、签名私钥、模型权重或私有 Git 历史。

## 开发

使用 Node.js 24（最低 22.19）和 Python 3.11。安装与检查入口见 [开发说明](docs/DEVELOPMENT.md)。

```sh
python3 -m venv .venv-build
.venv-build/bin/python -I Scripts/bootstrap-build-pip.py
.venv-build/bin/python -m pip install -r requirements-build-mac.txt
node desktop/scripts/install-dependencies.cjs
npm --prefix desktop run build
```

历史 Mac 开发运行器和打包流程绑定维护者签名身份；公开源码不包含证书，第三方需配置自己的身份及独立资料目录。前端构建不等于已完成安装包或真机验收。请先阅读 [运行与分发边界](docs/OPERATIONS.md)。

## 文档

[产品能力](docs/PRODUCT.md) · [架构](docs/ARCHITECTURE.md) · [模块](docs/MODULES.md) · [接口](docs/CONTRACTS.md) · [数据与权限](docs/DATA-AND-SECURITY.md) · [版本状态](docs/STATUS.md) · [第三方声明](NOTICE.md)

## 许可

原创程序源码采用 [AGPL-3.0-only](LICENSE)。第三方组件保留各自许可证和声明。商业使用与再分发按适用许可证进行；会员服务不构成对开源许可证权利的额外限制。

Noteford 与腾讯没有官方关联。只能处理本人有权访问的资料；应用不向微信发送消息。历史实验路径及兼容性限制见相应文档，不保证所有微信版本、媒体格式或云端历史资源可用。

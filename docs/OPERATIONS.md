# 开源版运行

将 Noteford 开源版.app 放入 Applications，与现有 Noteford 开发版并存。应用标识、资料目录、缓存、钥匙串及 MCP 发现目录独立。不要手动把会员版 Data 作为开源版资料目录。

开源版从 0.1.5-community.2 起使用本公开仓库的 GitHub Release 自动更新，不能使用会员更新渠道。旧 community.1 须手动迁移安装一次。详见 [安装与接入](INSTALL.md)。无一键清理所有资料的卸载器，删除 App 不会自动删除个人资料。

真实微信首次连接需要用户允许系统权限和扫码；自动测试使用合成数据，不代表真实账号同步验收。

## 发布 GitHub 更新

递增桌面和 Python 版本，运行测试，使用固定 Community Bundle ID 和签名身份构建、公证并附票。包内 `app-update.yml` 固定为本仓库的 GitHub provider，channel 为 latest，只选择正式 Release；不设置 GitHub token。

用同一份已附票 App 制作安装 DMG 和更新 ZIP，最后生成 `latest-mac.yml`（version、files.url/sha512/size、path、sha512、releaseDate）及 SHA256SUMS。只发 arm64 包。每次发布须先上传全部资产、核对实际下载，再用受控 feed 完成原生更新、资料保留和业务验收后标记 Latest。首版无更新器，使用带更新器的受控基线验证，不将它冒充旧首版自动迁移。

不可覆盖已发布版本资产。撤回时先将 GitHub Latest 指向此前有效版本；已安装用户不会自动降级，应发布更高修复版。首版没有更新清单，撤回到首版只会令检查失败，不会卸载新版本。

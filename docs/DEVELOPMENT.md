# 开发与验证

需要 Node.js 24（至少 22.19）、Python 3.11；Mac 原生构建另需 Xcode Command Line Tools。先在独立目录安装依赖，不使用已有其他版本资料。

```sh
python3 -m venv .venv-build
.venv-build/bin/python -I Scripts/bootstrap-build-pip.py
.venv-build/bin/python -m pip install -r requirements-build-mac.txt
node desktop/scripts/install-dependencies.cjs
npm --prefix desktop run build
```

依赖安装入口使用项目内经校验的 CA 策略，并安装锁定的 Electron 运行时。`npm run build` 进行类型检查和前端构建，不制作安装包。

## 检查

```sh
node Scripts/check-private-data.cjs --install
PYTHONPATH=src .venv-build/bin/python -m pytest -q
node --test tests/*.test.cjs desktop/tests/*.test.cjs
npm --prefix desktop run build
```

pytest 为测试依赖，需要在独立环境安装。自动测试使用合成资料；可选原生测试跳过时不计为真实权限验收。

## 运行限制

Mac `dev:mac` 入口会准备独立签名运行器并使用钥匙串身份。公开源码没有维护者的 Developer ID 证书，因此不承诺新电脑直接执行该入口即可运行。独立构建需配置自己的签名、应用标识、私有资料根及更新通道；相关实现位于 `desktop/scripts/`、`desktop/electron/`。Windows 开发入口保留于 Scripts/start-dev.ps1，本次未做 Windows 验收。

不要为通过启动检查删除现有钥匙串、重置用户授权或复用其他版本的数据。真实数据与系统权限测试必须单独授权。更多边界见 OPERATIONS、DATA-AND-SECURITY。

## 版本号规范（2026-10-29 起）

开源版与会员版统一采用 `主版本.功能版本.修订版本`（SemVer）格式，各自独立递增，不要求同号，也不能仅凭版本号判断两版功能多少。版本类型由应用名称、Bundle ID、仓库和更新渠道区分，不放进正式版本号后缀。

- **主版本**：重大不兼容变更，例如需要明确迁移的数据或接口变化。正式进入 1.0 须由维护者明确决定，不能因为规范调整直接跳到 1.0。
- **功能版本**：新增功能或明显的能力扩展，如 `0.1.6 → 0.2.0`；功能位递增时修订位归零。0.x 阶段的不兼容变化至少递增功能位，并说明迁移要求。
- **修订版本**：兼容的错误修复、界面样式调整及小幅优化，如 `0.1.5 → 0.1.6`。
- **正式版**只用三段数字。Git tag 为 `vX.Y.Z`，界面显示“Noteford 开源版 X.Y.Z”或会员版的应用名称加版本号；Python、桌面 package.json、锁文件及实际 App 版本保持一致。源码修改不自动改动版本字段。
- **测试版**仅在明确需要分发测试时使用 `X.Y.Z-beta.N` 或 `X.Y.Z-rc.N`，GitHub 标为预发布，不能进入正式自动更新通道。本机日常开发沿用最近发布号，并通过开发版名称及提交号区分；不为每个提交或 UI 修改制造新产品版本。
- **发布时定号**：按本次累计改动的最高级别选择下一号，核对该版本所属平台、版本类型和渠道的已发布最高版本，候选必须严格更高。修改更新说明不升号；已发布二进制、清单和 tag 不覆盖，修包必须用新号。
- **渠道隔离**：开源版只走公开 GitHub Release；会员版保持其既定会员渠道。不开启自动降级，不跨版本类型接管资料或安装。Mac 与 Windows 若有历史版本差异，分别保证升级序列递增；不得用本次 Mac 规范把已有 Windows 0.3.42 重置到更低版本。

历史 community 编号仅作为发布记录保留；当前开源版编号以以下推广前冻结规则为准。

### 开源版推广前冻结（2026-10-29 最新指示，优先于上述递增规则）

开源版当前统一为 **0.1.1**。在维护者明确确认已发布第一篇 Noteford 的 X 推广前，所有源码迭代和本机替换均保持 0.1.1，不得自行升号、增加 community 后缀或通过内部产品版本号变相升号。不能按日期、计划或推测判断推广已经发布。

本次是从历史 community 编号回调到 0.1.1 的明确例外，功能保持最新，历史已发布包及 tag 不覆盖。已有 community.1/community.2 用户需退出后手动安装 0.1.1，保留资料；不启用自动降级。同版本修订不会被 SemVer 自动更新识别；冻结期后续修改先保留于源码与本机，需要再次分发时单独确定不可变构建资产标识，不能覆盖已发布字节或擅自升号。

冻结状态登记于开源仓库 `desktop/release-policy.json`，构建和打包前校验桌面、锁文件、Python 与候选版本。解除冻结必须有维护者明确确认，之后再按实际累计改动决定版本。会员版继续独立编号，此冻结不将会员版改为 0.1.1。

2026-10-30 维护者要求清理过期发布：已下架两份历史 community Release 及远程标签，仅保留 v0.1.1。此次下架不改写源码历史，也不改变 0.1.1 冻结及现存发布资产不可覆盖的规则。

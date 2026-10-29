# 第三方来源与许可说明

Noteford（原 WeChat Local）使用官方 `modelcontextprotocol/python-sdk` 的 `mcp` 包（MIT License）及其运行依赖。每次正式分发前应以**实际打包的依赖清单**核对许可证和所需声明；本文件不能代替最终安装包的依赖许可清单。

当前界面使用 Lucide 图标（ISC License）。历史版本曾研究并使用 Laya；当前源码、构建依赖及冻结服务分析中未包含 Laya 或模型权重。若未来重新加入，须重新核对权重许可与实际依赖。

设计过程中参考过以下开源项目；“参考”不自动表示复制其代码：

| 项目 | 已知许可 | 参考范围 |
| --- | --- | --- |
| `fanyuantaier/wechatauto-replica` | Apache License 2.0 | 微信 4.x WCDB 配置只读内存读取、SQLCipher 密钥验证、快照与 WAL、媒体目录和图片容器思路 |
| `pandorafuture/wx-cli` | MIT License | 本地读取架构、消息表映射、Zstandard 解码、发送者与会话解析思路 |
| `freestylefly/WeChatBridge` | MIT License | 暂存后原子切换的可靠性思路 |
| Skype SILK SDK | BSD 3-Clause（保留 SDK 文件原始条款） | Mac 构建使用独立命令行适配器解码本机语音；Windows 原有外部解码路径不变 |

Mac 的 `noteford-silk` 仅编译原始 Skype SDK 的 C/H 文件及 Noteford 自己的有界命令行适配器。构建从校验 SHA-256 的 `pilk 0.2.4` 源码包提取 SDK 文件，不复制或链接其 Python 绑定及其他包装代码。随包 `app.asar/licenses/runtime/mac-silk/` 保留 SDK 条款、对应源码和下载地址；不依赖用户安装 Python 或 FFmpeg。

Mac 另带 FFmpeg 8.1.3 的独立图片解码程序（LGPL-2.1-or-later），从 FFmpeg 官方源码按固定 SHA-256 构建，只启用 HEVC/PNG/APNG 图片处理，禁用网络、GPL 和 nonfree。`app.asar/licenses/runtime/mac-image-decoder/` 含完整对应源码、许可、配置及构建脚本；可用兼容自编译程序替换这两个 helper 并重新 ad-hoc 签名应用，不实施运行时哈希锁。Electron 自带的 libffmpeg 及 Windows 分发流程不变。CDN 请求字段和公开协议 RSA 参数参考 betteray/WeChat 的 CdnGetMsgImgTask；WXGF 分区布局参考 sjzar/chatlog 与 sarv.blog 的格式研究，未复制其实现代码。

Mac 本地旧表情的账号派生公式与 AES-CBC 封装规则参考 [CN-Grace/Wechat-Emoticon-Parser](https://github.com/CN-Grace/Wechat-Emoticon-Parser) 的格式说明，并以本机消息 MD5 验证；未复制其 Windows 内存扫描、导出或转换代码。

项目不包含向微信发送消息、界面自动化、进程注入或修改微信源数据的路径。如未来复制、修改或打包第三方代码，需逐项履行相应许可义务。本仓库原创程序源码采用 AGPL-3.0-only（见根目录 LICENSE）；第三方独立许可保持不变。

## 设置中的贡献与致谢

2026-10-11 回查本机可读取的 WeChat Local / Noteford 会话记录、2026-09-27 的历史 NOTICE 内容及当前源码说明，并核对上游许可证。早期三项参考的许可确认如下：

- [wx-cli：MIT](https://github.com/pandorafuture/wx-cli/blob/main/LICENSE)
- [WeChatBridge：MIT](https://github.com/freestylefly/WeChatBridge/blob/main/LICENSE)
- [wechatauto-replica：Apache-2.0](https://github.com/fanyuantaier/wechatauto-replica/blob/main/LICENSE)

后续红包状态研究另参考 [wechat-antirecall 文档](https://github.com/fzlzjerry/wechat-antirecall/blob/main/Docs/red-packet-269624.md)（仓库 MIT）和 [PadChat-SDK 字段文档](https://github.com/AvengersWeChat/PadChat-SDK/blob/master/docs/index.md)（本次未确认许可证，不标作 MIT）。参考范围见 DECISIONS.md；不将研究过的项目列成已集成的运行依赖。

设置页只展示 wx-cli 和 WeChatBridge 两项研究参考及其 MIT 链接，不再展示其他研究项目和开源组件清单。该界面不等于实际随包依赖清单。

## 2026-10-11 分发许可核查

本轮依据生产 npm 锁文件、本机安装包的原始许可文件、PyInstaller `Analysis-00.toc` 和已有冻结服务二进制清单核查。顶层收集范围为 20 项 npm 包、35 项 Python 分发包（含因冻结分析带入的构建/辅助模块，不能只看手写 requirements）。Electron/Chromium 完整原始声明、Python 及标准库声明、内嵌 OpenSSL/zstd/zlib/liblzma 的补充许可一并保留。

三个原生 Python 组件的对应版本 PyPI 源码包经 SHA-256 核对，再按 Cargo.lock 收集了 176 个 Rust 包的保守上界清单与对应许可文件（包含当前及此前版本）。该清单包含构建、测试及其他平台依赖，不宣称全部被链接进 Windows 二进制。`r-efi` 与 `wit-bindgen-rt` 的包内许可缺失已沿 getrandom 0.3.3 的 UEFI / wasm32-WASI 条件依赖核实为非 Windows 运行依赖；条件声明及锁文件随证据保留。双许可项目保留原始文本，可选宽松许可不按强制 LGPL/GPL 判定。

已确认的主要许可包括 MIT、ISC、BSD、Apache-2.0、PSF、Unicode、Zlib 及公有领域声明，允许商业使用但仍须遵守各自条件。PyInstaller 的 GPL 有商业应用打包例外，社区 runtime hooks 为 Apache-2.0；不能仅根据工具顶层 GPL 标签判断应用必须开源。pywin32 仅收集冻结分析实际涉及的 win32 许可，未将不打包的 adodbapi LGPL 组件混为应用依赖。

### 三项分发条件的处理

- `lazy-val`：不再分发上游实现。使用独立编写的本地 `@noteford/lazy 1.0.6`（MIT）替换，保留 electron-updater 的延迟配置读取与更新标识行为。随包验证实际实现和源文件一致，不把上游缺失的授权文本自行补写。
- Electron 44.4.3 / FFmpeg：已核对官方 ZIP 校验值和原样 DLL，PE 导入证明动态链接；对应 Chrome/win/x64 配置的 GPL/NONFREE/VERSION3 均为 0。随包保留完整对应源代码、Electron 补丁、原始配置、许可证和构建/替换说明。最终用户可替换兼容 ffmpeg.dll，并为调试库修改进行逆向工程；运行时没有 DLL 哈希锁。详细说明见安装资源 `FFMPEG-SOURCE-AND-RELINKING.md`。
- Microsoft Distributable Code：保留 Python Windows 原始条款，安装器要求接受 `packaging/DISTRIBUTION-TERMS.txt`；明确该代码的声明保留、平台、商标、用途和下游同意要求，不将 Microsoft 代码专用限制扩展到 LGPL 库及其他开源组件。

这些处理针对已锁定版本的版权许可分发条件；变更组件或再分发方式需重新核对，不构成对一切商业用途的保证。

本机 `D:/ffmpeg` 为启用 GPL/version3 的外部程序，当前安装包规则未内置这个独立 EXE；不要未经对应源码与条款核查将其加入包。历史 SILK 解码器也未内置。本轮没有对微信平台授权、编解码专利或应用整体合法性作保证。

### 自动收集与打包校验

`Scripts/collect-third-party-licenses.py` 生成 `build/third-party-licenses/`，其中 manifest 记录版本、原始许可文件哈希、冻结服务每个文件哈希和未决项。`packaging/license-policy.json` 固定已核查版本与声明内容，升级/新增依赖需重新核查；`packaging/license-review.json` 保存条件处理证据与未决项。公开上游补充原文及下载来源位于 `packaging/license-sources/`，对应源码映射位于 `packaging/redistributable-sources.json`。

Electron builder 在打包前收集。Windows 安装资源携带 `resources/third-party-licenses/`；Mac 新构建改为收入 `Contents/Resources/app.asar` 内的 `licenses/runtime/`，不再单独分发该资源目录。Mac 打包校验逐文件核对归档内容与收集材料一致，并拒绝遗漏、变更或被解包为独立资源的文件。ASAR 可用 Electron 官方 `@electron/asar` 工具提取，包含对应源码的原始归档。打包后逐项验证声明与服务文件、Electron 原始声明、npm 锁文件及应用版本。未决项存在时阻止正常安装包构建完成；`--inventory-only` 仅用于核对隔离目录内容，不能作为发布许可放行。旧版已发布 EXE 不会因修改构建配置自动获得这些文件，新构建安装包需单独验收；不自动发布。

## 微信视频号品牌标志

Mac 素材页的 `desktop/src/assets/weixin-channels.svg` 提取自微信视频号助手官方公开静态资源中的 `icon-logo-medium`，保留原矢量路径，以橙色展示。来源：https://channels.weixin.qq.com/ ；资源：https://res.wx.qq.com/t/wx_fed/finder/helper/finder-helper-web/res/static/js/942.e5dfd487.js （2026-10-19 读取）。该标志用于识别视频号来源，属于腾讯／微信品牌资产，不属于 Lucide ISC 许可范围，不表示官方合作或授权。其余应用功能图标通过共享 Icon 组件使用 Lucide。

## shadcn/ui authentication layout

The desktop account card layout is adapted from shadcn/ui's login-03 block.
Source: https://github.com/shadcn-ui/ui/tree/main/apps/v4/registry/new-york-v4/blocks/login-03
Copyright (c) 2023 shadcn. MIT license retained in desktop/licenses/shadcn-ui-MIT.txt
and shipped inside the desktop application. The authentication backend and account flows remain Noteford's implementation.

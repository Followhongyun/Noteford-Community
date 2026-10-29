# macOS 重签正版微信并取密钥

给洪云在自己的 Mac 上跑。完整步骤、恢复官方签名的方法和待真机项在 [macos-feasibility.md 第 7.9 节](../../docs/macos-feasibility.md)。

保持 SIP 开启。不要加 `sudo`。不要对 App Store 版微信重签。

```bash
cd native/macos/wechat_hook && make
file libnoteford_wechat_hook.dylib
python3 scripts/mac-capture/run.py status --app /Applications/WeChat.app --state "$HOME/.noteford"
python3 scripts/mac-capture/run.py resign \
  --app /Applications/WeChat.app \
  --hook native/macos/wechat_hook/libnoteford_wechat_hook.dylib \
  --state "$HOME/.noteford"
```

恢复官方签名：退出微信，用官网安装包覆盖 `/Applications/WeChat.app`，再删除 `$HOME/.noteford/resign.json`。

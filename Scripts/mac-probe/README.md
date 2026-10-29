# macOS 微信只读探测

给要在自己的 Mac 上核对微信 4.x 数据目录的人。脚本只读，不解密，不提取密钥，不重签名，不关闭 SIP，不改微信目录。

云端环境和 Windows 机器跑不了真机部分。在那些系统上用 `--self-test` 只验证脚本自己的样例。

## 准备

系统自带的 Python 3.9 及以上即可，不必安装本仓库依赖。

```bash
python3 scripts/mac-probe/probe.py --self-test
python3 scripts/mac-probe/probe.py --output ~/Desktop/noteford-mac-probe
```

第二条命令在桌面生成 `report.json` 和 `report.md`。先看 Markdown。报告含账号目录名和数据库文件头的前 16 字节（salt，不是密钥），不含消息正文。分享前自己打开看一遍。

读取 `~/Library/Containers/com.tencent.xinWeChat` 可能被系统拒绝。若报告里出现 `permission_denied`，把「系统设置 → 隐私与安全性 → 完全磁盘访问权限」授给运行脚本的终端，然后再跑一次。

## 附加进程

默认会尝试 `task_for_pid`，用来回答「当前签名下能不能附加」。失败是有效结果，把 `kern_return` 留在报告里即可。目录结果会先写入报告，再尝试附加；若附加阶段进程被系统杀掉，已写出的目录部分仍然保留。

脚本在附加成功时只统计可读内存区数量，不读取、不记录内存内容，随后释放任务端口。不要为了这次探测重签名微信或关闭 SIP。

可选的 root 对照（仍然不读内存内容）：

```bash
sudo python3 scripts/mac-probe/probe.py --output ~/Desktop/noteford-mac-probe-root
```

数据目录不在默认位置时：

```bash
python3 scripts/mac-probe/probe.py --output ~/Desktop/noteford-mac-probe --root /path/to/xwechat_files
```

`--root` 必须是已存在的目录。输出目录不能放在微信数据目录里面。

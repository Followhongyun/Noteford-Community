# 数据与权限

Mac 开源版资料根为 ~/Noteford Community/Data；用户界面缓存与连接发现位于 ~/Library/Application Support/Noteford Community。微信数据库及图片密钥保存在独立钥匙串服务 com.followhongyun.noteford.community。不迁移会员版数据或复用其授权。

微信原始文件只读；Noteford 保存本地副本、索引与媒体。用户授予系统权限后才能连接本机微信；MCP 读取需单独在应用内授权。关闭授权后拒绝后续读取。

不要提交 Data、配置、数据库、聊天、密钥、令牌或缓存。远程 MCP 默认关闭，单独部署需自行验证。

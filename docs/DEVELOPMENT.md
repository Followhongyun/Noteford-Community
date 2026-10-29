# 开发与验证

需要 Node.js 24（至少 22.19）、Python 3.11；Mac 原生构建另需 Xcode Command Line Tools。先在独立目录安装依赖，不使用已有会员版资料。

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

Mac `dev:mac` 历史入口会准备独立签名运行器并使用钥匙串身份。公开源码没有维护者的 Developer ID 证书，因此不承诺新电脑直接执行该入口即可运行。独立构建需配置自己的签名、应用标识、私有资料根及更新通道；相关实现位于 `desktop/scripts/`、`desktop/electron/`。Windows 开发入口保留于 Scripts/start-dev.ps1，本次未做 Windows 验收。

不要为通过启动检查删除现有钥匙串、重置用户授权或复用其他版本的数据。真实数据与系统权限测试必须单独授权。更多边界见 OPERATIONS、DATA-AND-SECURITY。

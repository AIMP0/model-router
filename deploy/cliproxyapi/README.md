# CLIProxyAPI 部署组件

本目录来自原 `AIMP0/cliproxyapi-deploy`，现在与模型路由、余额管理页面统一维护。完整步骤见仓库根目录 [README](../../README.md)。

上游使用 [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的 **v8.0.73 Linux amd64** 发行版，固定 SHA256 校验；本目录不包含上游源码或二进制，不修改上游程序。

从仓库根目录运行：

```sh
python3 deploy/cliproxyapi/install_binary.py
python3 deploy/cliproxyapi/setup_runtime.py
```

初始化交互读取 DeepSeek Key，生成独立代理连接密钥；已有 runtime 时拒绝覆盖。GPT 的设备码登录需在本目录执行 `bin/cli-proxy-api -config runtime/config.json -codex-device-login`。

代理始终监听 `127.0.0.1:8317`，远程管理关闭。用户服务模板位于根目录 `systemd/cliproxyapi.service`，新安装目录为 `~/.local/share/model-router/deploy/cliproxyapi`。原部署不被脚本自动更改。

`runtime/`、`bin/`、真实密钥和 OAuth 凭据不会提交。管理页余额查询单独配置官方余额接口 Key，不读取或展示 OAuth 凭据。

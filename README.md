# Model Router

一个仓库管理模型路由、CLIProxyAPI 部署和账号余额页面。Python 3.10+，运行时仅使用标准库；CLIProxyAPI 使用固定版本的上游发行包。

```text
应用 / Agent → 模型路由 127.0.0.1:8318 → CLIProxyAPI 127.0.0.1:8317 → 模型提供商
浏览器       → 管理页面 服务器IP:8319 → 提供商余额接口
```

管理页面有独立登录令牌，可配置监听 IP、端口、访问地址和余额查询账号。路由、代理和管理页面是三个独立进程。管理端口不提供模型调用接口。

## 余额能查到什么

余额属于**提供商账号**，同一账号下的多个模型共享。页面不会把相同余额按模型重复相加，也不会把查询失败显示为 0。

| 接口类型 | 页面显示 | 所需凭据 |
| --- | --- | --- |
| DeepSeek | 官方账户余额、赠送余额、充值余额，区分 CNY / USD | DeepSeek 官方 API Key |
| OpenRouter 账户 | 总充值减累计使用所得账户余额 | 该接口可能要求 Management API Key |
| OpenRouter Key 额度 | Key 的 `limit_remaining`，单独标注预算额度 | 普通 OpenRouter API Key；未返回预算时不显示金额 |
| GPT / Codex 登录 | 订阅额度查询入口；不显示推测金额 | 无需导入 OAuth 凭据 |
| OpenAI API、Claude、Grok | 明确显示「未接入查询」及官方控制台入口 | 不收集未使用的密钥 |
| 自定义 JSON 接口 | 读取指定 GET 接口的金额字段 | 可选 Bearer Key、字段路径和单位 |

自定义示例：接口返回 `{"data":{"balance":"18.50"}}`，金额路径填写 `data.balance`，单位选择 CNY。远程接口要求 HTTPS，不跟随重定向；请求失败不会回显上游响应或密钥。标准接口的地址固定，不接受静默替换。

提供商配置中的密钥**只用于余额查询**，与模型路由的调用密钥分开。关联模型仅用于展示归属，不修改模型路由规则。余额缓存 60 秒；手动刷新最短间隔 5 秒，单接口超时 8 秒。

## 新机器安装

完整代理部署需要 Linux x86_64、Python 3.10+、Git 和用户级 systemd。余额页面本身可在其他 Python 3.10+ 平台运行。

```sh
git clone https://github.com/HaoGuoo/model-router.git ~/.local/share/model-router
cd ~/.local/share/model-router

# 1. 安装代理；校验固定版本发行包 SHA256
python3 deploy/cliproxyapi/install_binary.py
python3 deploy/cliproxyapi/setup_runtime.py

# 2. 初始化路由；读取上一步的本地连接密钥
python3 setup_runtime.py

# 3. 初始化管理页面；替换成实际服务器 IP
python3 setup_dashboard.py --host 0.0.0.0 --port 8319 --public-url http://192.168.1.20:8319
```

第 3 步交互设置至少 32 字符的管理令牌，回车则自动生成。令牌保存在 `runtime/dashboard-token`；仅在可信本机查看该文件以登录，别粘贴到仓库、URL 或日志。它与 `runtime/client-token`、代理 `runtime/link-key` 相互独立。非交互初始化可增加 `--generate-token`。

GPT 的 Codex OAuth 登录在代理目录执行，认证文件仅保存在私有 runtime：

```sh
cd ~/.local/share/model-router/deploy/cliproxyapi
bin/cli-proxy-api -config runtime/config.json -codex-device-login
```

### 前台试运行

在三个终端中分别从仓库根目录运行：

```sh
deploy/cliproxyapi/bin/cli-proxy-api -config deploy/cliproxyapi/runtime/config.json
python3 -m aimp_router.server --config runtime/config.json --token-file runtime/client-token --credentials-file runtime/credentials.json --port 8318
python3 -m aimp_router.dashboard --runtime-dir runtime
```

打开 `http://服务器IP:8319`，使用管理令牌登录，在「提供商配置」填写 DeepSeek 官方 Key 并刷新余额。初始账号没有示例金额或真实密钥；未填写时显示待配置。管理页面也能独立运行：只执行 `setup_dashboard.py` 和 dashboard 启动命令即可。

### 后台服务

停止对应的前台进程，在仓库根目录执行：

```sh
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cliproxyapi aimp-model-router model-router-dashboard
systemctl --user status model-router-dashboard
```

服务模板使用 `~/.local/share/model-router`，其他目录请先修改路径。管理页「访问配置」保存监听 IP、端口与访问地址后，执行 `systemctl --user restart model-router-dashboard` 生效。对外地址不会自动配置网卡、防火墙或云安全组；访问该 IP 需要允许所选端口。

`127.0.0.1` 仅本机访问，`0.0.0.0` 监听全部 IPv4 网卡。公网访问请使用 HTTPS 反向代理或 SSH 隧道，避免通过明文 HTTP 传输管理令牌和提供商 Key。HTTPS 代理把根路径转发到管理端口，同时在 dashboard 的 ExecStart 增加 `--secure-cookies`，并将访问地址设置为对应 HTTPS 地址；不要暴露 8317 / 8318。原始 HTTP / IP 访问适合可信局域网。

## 路由功能

- OpenAI 风格：`POST /v1/chat/completions`、`GET /v1/models`。
- 状态与预览：`GET /health`、`POST /routing/preview`。
- 所有路由接口均需要 `Authorization: Bearer <client-token>`，仅监听 `127.0.0.1:8318`。
- `model: auto` 按 roles、tiers 或显式 `routing.chain` 选模型；指定具体别名时固定模型。
- 网络故障、408/429/指定 5xx 触发候补与冷却；普通请求错误和认证错误不切换。
- 按 tools、vision、json_schema 能力筛选；流式输出后不重放请求。工具由调用方执行。

编辑 `runtime/config.json` 的 targets、roles、tiers 和 fallback，重启 `aimp-model-router` 生效。密钥使用 `runtime/credentials.json` 中的环境变量名；也可运行 `python3 -m aimp_router.configure --help` 为已有别名配置上游。新增代理模型后同步更新 targets。`router.example.json` 是部署快照，Claude / Grok 初始未启用，可用性以提供商账号为准。

MCP 从仓库根目录或已安装本包的环境运行：

```sh
python3 -m aimp_router.mcp --token-file runtime/client-token --url http://127.0.0.1:8318
```

提供 `generate`、`list_models`、`get_router_status`、`preview_route`；令牌从本地文件读取。项目不包含工作台前端、会话记录或私有记忆。

## 目录与原仓库迁移

```text
aimp_router/           路由、MCP、余额适配器、管理页面
deploy/cliproxyapi/    原 cliproxyapi-deploy 的安装器与示例配置
systemd/              三个用户服务模板
setup_runtime.py      路由运行配置初始化
setup_dashboard.py    IP、端口与管理令牌初始化
test_*.py             本机模拟上游与管理接口测试
```

`cliproxyapi-deploy` 已并入本仓库，后续统一在此维护。源快照：[`cliproxyapi-deploy@7aa74da`](https://github.com/HaoGuoo/cliproxyapi-deploy/tree/7aa74dafd61f91d1bca2d8fa12db2c858697e82d)。原仓库保留其历史并归档；本仓库保留 model-router 原有提交历史。

本次仓库整合不迁移服务器、不复制旧凭据、不自动重启服务。已有部署不要直接重跑初始化脚本；迁移时先备份私有 runtime，调整服务目录和代理 `oauth.auth-dir` 的绝对路径，验证后再停用旧目录。路由初始化仍支持 `--link-key-file /旧目录/runtime/link-key`。

## 验证

```sh
python3 -m unittest test_router test_dashboard -v
# 可选 MCP SDK 互操作测试（建议虚拟环境）
python3 -m pip install -e '.[test]'
python3 -m unittest test_mcp_sdk -v
```

测试使用本机模拟余额和模型接口，不调用付费模型、不读取真实账号密钥。未安装工作台时两项集成测试跳过。运行数据、密钥、OAuth 文件、日志和二进制均由 `.gitignore` 排除。

接口资料：[DeepSeek 余额](https://api-docs.deepseek.com/api/get-user-balance/)、[OpenRouter 账户余额](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)、[OpenRouter Key 额度](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)。

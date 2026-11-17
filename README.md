# AIMP 模型路由

从丽萨服务器正在运行的部署中独立整理的 Python 服务。位于 AIMP Agent 与 CLIProxyAPI 之间，提供模型选择、故障切换和 stdio MCP 接口。运行时仅依赖 Python 标准库。

## 行为

- OpenAI 风格接口：`POST /v1/chat/completions`、`GET /v1/models`。
- 状态与路由预览：`GET /health`、`POST /routing/preview`。
- 以上 HTTP 接口均需要 `Authorization: Bearer <client-token>`，仅监听 `127.0.0.1:8318`。
- `model: auto` 按角色、任务档位或显式 `routing.chain` 选择；指定具体模型时固定该模型。
- 可重试网络故障、408/429/5xx 指定状态触发候补与冷却；普通请求错误和认证错误不切换。
- 根据 tools、vision、json_schema 能力筛选。流式输出只在首个事件之前尝试候补；已输出内容后不重放。
- 工具调用仅透传，实际执行由调用方 Agent 负责。

## 配置与部署

需要 Python 3.10+。将本仓库克隆到 `~/.local/share/aimp-model-router`。先按独立的 `cliproxyapi-deploy` 仓库配置代理，然后在本项目目录执行：

```sh
python3 setup_runtime.py --link-key-file ~/.local/share/cliproxyapi-v8.0.73/runtime/link-key
python3 -m aimp_router.server --config runtime/config.json --token-file runtime/client-token --credentials-file runtime/credentials.json --port 8318
```

首次初始化会创建两层独立密钥的本地存储；已有 runtime 时拒绝覆盖。`router.example.json` 保留当前部署的别名、具体模型、角色和候补配置，不含密钥。Claude、Grok 仍未启用；某些角色首选它们时会按配置落到可用候补。模型列表是部署快照，可用性以提供商账号为准。

后台运行时停止前台实例，然后安装用户服务：

```sh
mkdir -p ~/.local/share/systemd/user/default.target.wants
cp systemd/aimp-model-router.service ~/.local/share/systemd/user/
ln -sfn ../aimp-model-router.service ~/.local/share/systemd/user/default.target.wants/aimp-model-router.service
systemctl --user daemon-reload
systemctl --user start aimp-model-router.service
systemctl --user status aimp-model-router.service
```

服务文件固定使用上述安装路径。原有服务器的密钥留在原运行目录；归档不会旋转密钥或重启原服务。

## 模型与 Agent 配置

编辑 `runtime/config.json`：targets 定义上游模型与能力，roles 定义 Agent 的首选列表，tiers 定义任务档位，fallback 定义候补顺序。修改后重启服务。配置中只保存 `api_key_env` 名称，密钥留在 `runtime/credentials.json`。

请求示例（通过已认证 HTTP 客户端发送）：

```json
{"model":"auto","messages":[{"role":"user","content":"帮我整理任务"}],"routing":{"role":"manager"}}
```

工作台可通过 `/v1/models` 获取模型，用 `routing.chain` 传递已保存的 Agent 路由。该仓库不包含工作台前端、会话记录和 Agent 私有记忆。

## MCP

从已安装本包的 Python 环境运行，或把项目目录设置为工作目录 / PYTHONPATH：

```sh
python3 -m aimp_router.mcp --token-file ~/.local/share/aimp-model-router/runtime/client-token --url http://127.0.0.1:8318
```

使用 JSON-RPC stdio，提供 `generate`、`list_models`、`get_router_status`、`preview_route`。MCP 进程通过本地文件读取令牌，不在配置里明文放令牌。

## 测试

```sh
python3 -m unittest test_router -v
# 可选：官方 MCP SDK 互操作测试（建议在虚拟环境安装）
python3 -m pip install -e '.[test]'
python3 -m unittest test_mcp_sdk -v
```

测试使用本机模拟上游，不调用付费模型。两项工作台集成测试在未安装 `aimp_preview` 时跳过；其余路由测试可独立运行。运行数据、凭据、日志与备份均由 `.gitignore` 排除。

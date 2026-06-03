# GitModel Hub

一个本地运行的**模型 API 网关**，用个人 GitHub Copilot 订阅作为后端，
对外同时暴露 **OpenAI 兼容** 和 **Anthropic 兼容** 接口，可直接给
**Codex**、**Claude Code**、OpenAI / Anthropic SDK、`curl` 等下游客户端使用。
自带一个 **Web 门户**，支持首次 GitHub 登录与各模型的 token 用量统计。

> ⚠️ 在非 IDE 编辑流程外使用 Copilot 订阅违反 GitHub Copilot 服务条款，账号有被封风险。
> 仅供**个人学习**，请勿用于生产或对外服务。

## 功能

- **OpenAI 兼容**：`GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/responses`（含流式）
- **Anthropic 兼容**：`POST /v1/messages`（含流式、工具调用 `tool_use`/`tool_result`、图片）
- **后端**：自动完成 Copilot 两段式鉴权（长期 OAuth token → 短期 API token，内存缓存）
- **Web 门户**：
  1. GitHub Device Flow 首次登录
  2. 按模型 / 按天的 token 用量统计
  3. 可用模型列表
  4. 生成 / 吊销给客户端用的本地 API key

## 安装与运行

```powershell
pip install -r requirements.txt
python -m hub
```

默认监听 `http://127.0.0.1:8088`，浏览器打开即可看到门户。
首次使用点击「开始登录」，按提示完成 GitHub 授权。

## 配置（环境变量 / `.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HUB_HOST` | `127.0.0.1` | 监听地址 |
| `HUB_PORT` | `8088` | 端口 |
| `HUB_DATA_DIR` | `~/.gitmodel_hub` | SQLite 与 token 存放目录 |
| `HUB_REQUIRE_AUTH` | `false` | 为 `true` 时 `/v1/*` 必须带有效 key |
| `HUB_ADMIN_TOKEN` | 空 | 设置后门户管理操作需带 `x-admin-token` |
| `COPILOT_OAUTH_TOKEN` | 空 | 直接注入已有的 `ghu_…` token，跳过登录 |

## 客户端接入

### Codex / OpenAI

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8088/v1"
export OPENAI_API_KEY="<门户生成的 key>"   # 关闭鉴权时可任意填
# 模型: gpt-5.5 / gpt-4.1 / gpt-5.3-codex ...
```

### Claude Code / Anthropic

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8088"
export ANTHROPIC_AUTH_TOKEN="<门户生成的 key>"
# 模型: claude-sonnet-4.6 / claude-opus-4.7 ...
```

> 模型名兼容：直接传 Copilot 的 slug（如 `claude-sonnet-4.6`）即可；
> 也支持官方 Anthropic 模型 ID（如 `claude-3-5-sonnet-20241022`、
> `claude-sonnet-4-5-20250929`），网关会自动映射到对应的 Copilot slug，
> 未识别的名字按原样透传。映射表见 `hub/anthropic_adapter.py` 的 `MODEL_ALIASES`。
> 实际可用的模型 ID 以门户「可用模型列表」/ `GET /api/models` 为准。
>
> 图片输入（多模态）已支持：网关会在检测到图片内容时自动附加
> `Copilot-Vision-Request: true` 头。

### curl 自测

```bash
curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4.1","messages":[{"role":"user","content":"say ok"}]}'
```

## 结构

```
hub/
  config.py             # 环境配置
  store.py              # SQLite：oauth token / api keys / usage
  copilot_client.py     # Copilot 异步客户端 + device flow
  anthropic_adapter.py  # Anthropic <-> OpenAI 翻译（含流式/工具）
  server.py             # FastAPI 应用与所有端点
  static/index.html     # Web 门户
  __main__.py           # python -m hub 入口
```

参考实现：`copilot.py`、`git_model.py`、`llm.py`。

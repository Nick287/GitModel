# GitModel Hub

一个本地运行的**模型 API 网关**：用个人 GitHub Copilot 订阅作为后端，对外同时暴露
**OpenAI 兼容** 与 **Anthropic 兼容** 接口，可直接给 **Codex**、**Claude Code**、
OpenAI / Anthropic SDK、`curl` 等下游客户端使用。自带一个 **Web 门户**，支持
GitHub 登录、token 用量统计与费用估算、API key 管理。

> ⚠️ **免责声明**：在 IDE 正常编辑流程之外使用 Copilot 订阅违反 GitHub Copilot
> 服务条款，账号有被封风险。本项目仅供 **个人学习研究**，请勿用于生产或对外服务。

---

## ✨ 功能

- **OpenAI 兼容**：`GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/responses`（含流式）
- **Anthropic 兼容**：`POST /v1/messages`（含流式、工具调用 `tool_use` / `tool_result`、图片输入）
- **自动鉴权**：完成 Copilot 两段式鉴权（长期 OAuth token → 短期 API token，内存缓存并自动续期）
- **模型名兼容**：官方 Anthropic 模型 ID 自动映射到 Copilot slug，未识别的名字原样透传
- **Web 门户**
  - GitHub Device Flow 登录
  - 用量统计：总量 / 按模型 / 按天趋势 / 最近请求明细
  - **费用估算**：按可配置的模型单价（美元 / 百万 token）换算预估成本
  - 可用模型列表
  - 生成 / 复制 / 吊销下游客户端用的本地 API key
- **安全**
  - 后台账号密码登录（默认 `admin` / `admin`，**首次使用请立即修改**）
  - 登录失败限流（按 IP 锁定，防暴力破解）
  - `/v1/*` 可选 API key 鉴权

---

## 🚀 快速开始

### 本地运行

```powershell
pip install -r requirements.txt
python -m hub
```

默认监听 `http://127.0.0.1:8088`，浏览器打开即可看到门户。

1. 用默认账号 `admin` / `admin` 登录后台（**随后在「设置」里改掉**）。
2. 在「概览」点击「开始登录」，按提示完成 GitHub 授权（Device Flow）。
3. 在「API Keys」生成一个 key 给下游客户端使用。

### Docker

```bash
docker compose up -d --build
```

数据（SQLite，含 Copilot token / 后台凭据 / 用量）持久化到 `./db-container`。

---

## ⚙️ 配置（环境变量 / `.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HUB_HOST` | `127.0.0.1` | 监听地址 |
| `HUB_PORT` | `8088` | 端口 |
| `HUB_DATA_DIR` | 项目下 `db/` | SQLite 与 token 存放目录 |
| `HUB_REQUIRE_AUTH` | `false` | 为 `true` 时 `/v1/*` 必须带有效 key（对外暴露时建议开启） |
| `HUB_ADMIN_TOKEN` | 空 | 可选的后台「万能令牌」，带 `x-admin-token` 即放行，免账号密码 |
| `HUB_LOGIN_MAX_FAILS` | `5` | 同一 IP 连续登录失败多少次后锁定（设 `0` 禁用限流） |
| `HUB_LOGIN_LOCK_SECONDS` | `900` | 锁定时长（秒） |
| `COPILOT_OAUTH_TOKEN` | 空 | 直接注入已有的 `ghu_…` token，跳过登录 |

---

## 🔌 客户端接入

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

> **模型名兼容**：直接传 Copilot 的 slug（如 `claude-sonnet-4.6`）即可；也支持官方
> Anthropic 模型 ID（如 `claude-3-5-sonnet-20241022`、`claude-sonnet-4-5-20250929`），
> 网关会自动映射到对应的 Copilot slug，未识别的名字按原样透传。映射表见
> `hub/anthropic_adapter.py` 的 `MODEL_ALIASES`。实际可用的模型 ID 以门户
> 「可用模型列表」/ `GET /api/models` 为准。
>
> **图片输入（多模态）** 已支持：网关在检测到图片内容时自动附加
> `Copilot-Vision-Request: true` 头。

### curl 自测

```bash
curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4.1","messages":[{"role":"user","content":"say ok"}]}'
```

---

## 📊 用量与费用

门户「用量统计」展示请求数、输入/输出 token、按模型与按天的明细，并给出**预估费用**。

- 费用按「设置 → 模型单价」里的单价表（美元 / 百万 token）换算，默认值取自 Anthropic
  官方公开定价，可自行编辑。
- 流式请求若后端未返回 `prompt_tokens`，网关会用本地估算兜底，并在记录上标注「估算」。
- 由于走的是 Copilot 包月订阅，费用栏是「等价 API 成本」参考，**并非真实扣费**。

---

## ☁️ 部署到 Azure（可选）

`infra/` 提供一套 Terraform，将网关部署到 **Azure Container Apps**：

- 用 user-assigned identity 从既有 ACR 拉镜像（无需 admin 凭据）
- Azure Files 挂载到 `/data` 持久化 SQLite
- 单副本（SQLite 为 WAL 单写者，**切勿多副本**）

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # 填入你的 ACR / 存储账号等
terraform init
terraform apply
```

> **公网暴露注意**：`ingress.external_enabled = true` 会把服务挂到公网。务必
> ① 立即修改后台默认密码；② 保持 `HUB_REQUIRE_AUTH=true`；③ 视情况调低
> `login_max_fails`。`terraform.tfvars` 与 `*.tfstate` 含敏感信息，已被 `.gitignore` 排除，**切勿提交**。

---

## 📁 项目结构

```
hub/
  __main__.py           # python -m hub 入口
  config.py             # 环境变量配置
  store.py              # SQLite：oauth token / api keys / usage / 价格表
  copilot_client.py     # Copilot 异步客户端 + Device Flow 鉴权
  anthropic_adapter.py  # Anthropic <-> OpenAI 翻译（流式 / 工具 / 图片 / 模型别名）
  server.py             # FastAPI 应用与所有端点（含登录限流）
  static/index.html     # Web 门户（单文件）
infra/                  # Terraform：Azure Container Apps 部署
Dockerfile
docker-compose.yml
```

---

## 📜 许可

仅供个人学习研究，使用风险自负。

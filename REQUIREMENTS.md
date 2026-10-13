# OpenCode Zen 代理网关 — 需求与准备工作

> 状态：需求已确认，v1 已实现并通过本地实测（详见文末"落地结果"）
> 目标机器：Linux VPS，Docker + Docker Compose
> 对外地址：`http://<VPS_IP>:8211/v1`

---

## 一、为什么需要这个项目

OpenCode 的免费层（`opencode.ai/zen/v1`）只允许 OpenCode 官方客户端调用。pi 之前
是靠 `models.json` 里写死的请求头冒充客户端，能过；2026-10-13 起服务端加了校验，
写死的会话 ID 失效（详见 `D:\workspace\temp\mitm\` 里的抓包与复现脚本）。

要做的是：**在本机/VPS 上跑一个自己的 OpenAI 兼容网关**，把任意客户端的请求
（pi / OpenCode / Cherry Studio / LobeChat / 各种 SDK）改成"上游认得的形状"再转发，
并用自己的密钥保护这个网关，避免裸奔。

---

## 二、实测的上游规则（2026-10-13，全部有复现脚本）

网关必须同时满足下面 4 条，否则一律返回
`403 {"type":"FreeTierError","message":"OpenCode's free tier can only be used from within OpenCode."}`

| # | 校验项 | 实测结论 | 证据脚本 |
|---|---|---|---|
| 1 | `User-Agent` | 必须以 `opencode/` 开头；`curl/8.x`、`Mozilla/5.0`、空 UA 全部 403 | `mitm/gate2.txt` |
| 2 | `X-Opencode-Session` | 必须是合法形状：`ses_` + 12 位 hex + 14 位 base62，48bit 值 = `ts*4096 + 每毫秒计数器`（按位取反），**计数器必须 < 64**。pi 原来写死的两个 ID 计数器是 3323 / 3701 → 就是它们导致 403 | `mitm/ses_encode.py`、`mitm/final_matrix.py` |
| 3 | body `"stream": true` | 必须显式 `true`；`false` 或不带该字段 → 403 | `mitm/gate2.txt` |
| 4 | body `tools` | 必须同时含名为 `read` 的工具和名为 `bash`/`shell` 的工具（`Sh`、`/bin/sh`、`read_file`、`execute` 等都不行）；形状（chat 的 `{"function":{"name":...}}` 或 responses 的 `{"name":...}`）都认 | `mitm/names 探测`、`mitm/gate_now.txt` |

其他事实：

- 上游端点：`/zen/v1/models`、`/zen/v1/chat/completions`、`/zen/v1/responses`（`/zen/v1` 与
  `/inference/v1` 等价；`/embeddings`、`/messages` 不存在，返回 404 网页）。
- `Authorization: Bearer public` 其实**不是必需**的（去掉也 200），但保留无害。
- 免费模型共 11 个：`big-pickle`、`cat-jam-free`、`jev-1.13-free`、`ling-3.1-flash-free`、
  `longcat-2.5-preview-free`、`mimo-v2.6-flash-free`、`muse-spark-1.2-contributor-free`、
  `muse-spark-1.3-contributor-free`、`nemotron-3.5-lightning-free`、`space-bunny-free`、
  `step-5-preview-free`；`/models` 一共返回 84 个（其余是付费模型，需要真 `OPENCODE_API_KEY`）。
- 免费层有速率限制（实测撞到过 `429`）。
- 这些校验是**启发式**的，opencode 随时可能改；网关要能快速跟进。

---

## 三、系统架构

```
 客户端（pi / OpenCode / Cherry Studio / SDK ...）
     │  Authorization: Bearer <本网关的密钥>
     ▼
┌──────────────────────────────────────────────────────┐
│  opencode-zen 网关容器   :8211                        │
│  1. 鉴权：读密钥文件（可热更新）                       │
│  2. 请求改写：                                        │
│       User-Agent        → opencode/<ver>/cli          │
│       X-Opencode-Session→ 新铸的合法 ID（按键/会话稳定）│
│       Authorization     → Bearer public（或真 key）    │
│       body.stream       → 强制 true                    │
│       body.tools        → 缺失时补 read/bash stub      │
│  3. 响应：SSE 原样透传；客户端要非流式时代理聚合        │
│  4. 日志 / 用量 / 可选 IP 白名单                       │
└──────────────────────────────────────────────────────┘
     │  https://opencode.ai/zen/v1   （出口 IP 需能过 Cloudflare）
     ▼
       OpenCode Zen 免费层
```

### 关键设计点：`stream` 与 `tools` 的处理

- **stream**：上游必须 `true`。客户端要非流式时，网关对上游发 `stream:true`，
  把 SSE 收完聚合成一个标准 `chat.completion` JSON 返回（首字节延迟 = 整段生成时间，
  这是协议要求决定的，无法避免）。
- **tools**：客户端自己带了 `read`+`bash`（pi、OpenCode、Claude Code 类客户端都带）就原样转发；
  没带（纯聊天客户端、curl、Cherry Studio 默认）就注入两个最小 stub。
  注入的 stub 若被模型调用，有两种策略 → 见问题 3。

---

## 四、对外 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 返回免费模型列表（可过滤付费模型） |
| POST | `/v1/chat/completions` | 主要接口，支持流式/非流式 |
| POST | `/v1/responses` | 透传到上游 responses 接口 |
| GET | `/healthz` | 存活检查（不鉴权，只回 `{"status":"ok"}`） |
| GET | `/readyz` | 探测上游可达性 + 免费层是否放行 |

鉴权支持三种写法（任选）：`Authorization: Bearer <key>`、`x-api-key: <key>`、
`?key=<key>`。不通过返回 401 + OpenAI 风格错误体。

---

## 五、密钥文件

- 路径：容器内 `/data/api-keys.txt`（compose 挂载 `./data:/data`，面板/SSH 随时改）。
- 首次启动：文件不存在 → 生成 1 把密钥（`sk-oczen-` + 32 位 hex），
  `chmod 600`，并在**容器日志打印一次**；之后重启不再打印明文。
- 热更新：每次请求按 mtime 判断文件是否变化，改动**立刻生效**，不用重启。
- 文件格式（`#` 开头为注释）：

  ```
  # 名字:密钥   改完直接保存即可生效
  default:sk-oczen-3f9a...e2
  phone:sk-oczen-77c1...a4
  ```

- 可选：`PROXY_API_KEYS` env 变量追加额外密钥（用于临时应急）。
- 可选：`ADMIN_TOKEN` + `POST /admin/keys/rotate` 让网关自己重新生成密钥并写回文件。

---

## 六、配置项（.env）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8211` | 监听端口 |
| `ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | 上游地址 |
| `OPENCODE_API_KEY` | 空 | 填了就用它（付费模型可用）；留空则用 `public`（免费层） |
| `FREE_ONLY` | `true` | 是否只放行 11 个免费模型 |
| `REQUIRE_TOOLS_MODE` | `inject` | `inject` / `passthrough` / `off`（对应问题 3） |
| `KEY_FILE` | `/data/api-keys.txt` | 密钥文件 |
| `IP_ALLOWLIST` | 空 | 逗号分隔 CIDR，空=不限制 |
| `LOG_LEVEL` / `LOG_REQUESTS` | `info` / `true` | 日志与访问日志 |
| `UPSTREAM_TIMEOUT` | `600` | 上游超时秒数（长回答要够大） |

---

## 七、计划目录结构

```
D:\workspace\temp\opencode-zen\
├─ README.md                 部署 + 改密钥 + 客户端接入示例
├─ REQUIREMENTS.md           本文件
├─ docker-compose.yml        ports: 8211:8211，挂载 ./data
├─ Dockerfile                python:3.13-slim + uvicorn，非 root 运行
├─ .env.example
├─ .dockerignore
├─ app/
│  ├─ main.py                路由 / 鉴权中间件
│  ├─ config.py              env 变量读取
│  ├─ keys.py                密钥加载与热更新
│  ├─ zen_session.py         合法 session ID 铸造（复用已实测算法）
│  ├─ gate.py                stream/tools 补丁 + 上游请求头
│  ├─ upstream.py            httpx 客户端 + SSE 透传/聚合
│  └─ logging.py             结构化日志 + 用量 jsonl
├─ scripts/
│  ├─ probe_zen.py           ★VPS 就绪探测（已在本地写好并验证）
│  └─ gen_key.py             手动生成一把密钥
└─ data/                     密钥文件落盘处（不进 git）
```

---

## 八、VPS 准备工作清单

- [ ] Docker Engine ≥ 24 + `docker compose` 插件（`docker compose version` 有输出）
- [ ] 放行 8211：云厂商安全组 + `ufw allow 8211/tcp`
- [ ] **★ 先跑就绪探测**：`python3 scripts/probe_zen.py`
      必须 3 项全 PASS（能解析 opencode.ai、`/models` 200、免费层 200）。
      机房 IP 若被 Cloudflare 拦或上游对数据中心 IP 限流，这一步就会 FAIL，
      换机房/换出口再继续。
- [ ] 确认能出网访问 `opencode.ai:443`（有透明代理/墙的话要配 `HTTPS_PROXY`）
- [ ] 决定对外暴露方式（明文 HTTP 会把密钥暴露在公网链路上，见问题 4）
- [ ] 时间同步（`timedatectl`），日志轮转（compose `logging.options.max-size`）

---

## 九、风险与限制（请先认下这些）

1. **这是绕过 OpenCode 免费层限制**，与其服务条款的意图相悖；只建议个人自用，
   不要公开分享密钥/公网开放注册。
2. 上游校验是启发式的，**opencode 改一次就得跟一次**；可能某天直接封出口 IP。
3. 免费模型只有 11 个且有限速（实测 429），不适合高并发。
4. 非流式请求要聚合完整 SSE，超长回答会占用网关连接与内存（有上限保护）。
5. 注入的 `read`/`bash` stub 是假工具，模型可能真的调用它们。
6. 明文 HTTP + 公网 = 密钥可被中间人看到；强烈建议配 HTTPS 或只在 SSH 隧道内用。

---

## 十、待确认的 4 个问题

### 已确认（2026-10-13）

| 问题 | 决定 |
|---|---|
| 1 技术栈 | **Python 3.13 + FastAPI + httpx + uvicorn**，Docker 镜像 `python:3.13-slim`，非 root 运行 |
| 2 端点范围 | **`/v1/chat/completions` + `/v1/responses` + `/v1/models`**（`/v1/models/{id}` 也实现了） |
| 3 无 tools 请求 | **注入 stub + 剥离调用**：默认 `INJECT_STUB_TOOLS=true` + `STUB_CALL_MODE=text`，另留 `drop` / `passthrough` 两个开关 |
| 4 附加能力 | 这一问未作答，按默认实现：多密钥 + 文件热更新、访问日志 jsonl（含 tokens）、IP 白名单、每密钥限速；HTTPS 未内置，README 给了 Caddy/SSH 隧道两种做法 |

### 落地结果（v1，本地已跑通）

- `docker-compose.yml` / `Dockerfile` / `.env` / `.env.example` / `README.md` 已就绪
- 首启自动生成 `sk-oczen-<32hex>` 写入 `./data/api-keys.txt` 并在日志打印一次
- 实测通过：非流式无 tools（靠 stub 过闸）、流式、`/v1/responses` 流式与非流式、
  自带 read+bash 的客户端原样透传 tool_calls、密钥热更新、付费模型拦截、
  stub 调用被转成文字且 `finish_reason` 由 `tool_calls` 降级为 `stop`、pi 客户端真实接入
- Docker 构建与运行：**已在 GitHub Actions 验证**（`linux/amd64` + `linux/arm64` 双架构推送到
  `ghcr.io/moxuna/opencode-zen:latest`，并真起容器验证"root 属主的挂载卷 → 入口脚本自动修属主并
  降权到 uid 10001 → 首启生成 600 权限密钥 → `/healthz` OK → 未鉴权 401"。匿名 `docker pull` 实测可用）

### v1.1.0：Responses 协议桥接 + 工具镜像

起因：用 Codex CLI（`wire_api = "responses"`）配 `mimo-v2.6-flash-free` 时上游回
`{"type":"error","error":{"type":"ModelProtocolUnsupported","message":"Model does not support this protocol."}}`
——Codex 只会说 Responses，而该模型只支持 chat/completions。

新增 `app/bridge.py`：

- 请求方向：`instructions` + `input`（Codex 用 `role: developer`）→ chat `messages`（developer 合并成一条
  system）；非 `function` 的 Responses 工具（`namespace`、`web_search`）丢弃；`function_call` →
  assistant `tool_calls`；`function_call_output` → `role: tool`；`reasoning.effort` → `reasoning_effort`；
  `store`/`include`/`prompt_cache_key`/`client_metadata` 不转发
- 响应方向：chat SSE 分片 → Responses 事件（`response.created` / `output_item.added` /
  `output_text.delta` / `function_call_arguments.delta` / `response.completed`），usage 映射为
  `input_tokens`/`output_tokens`
- 路由：`RESPONSES_MODE=auto` 时按 `RESPONSES_NATIVE_MODELS` 判断，直连失败且错误是
  `ModelProtocolUnsupported` 时自动回退到桥接

同时修掉一个真实缺陷（由 Codex 实测暴露）：注入通用 `bash` stub 后，模型会去调这个假工具而不是客户端
自己的 `exec_command`，导致 Codex 的工具调用链中断。现在改为**工具镜像**（`STUB_TOOL_MODE=mirror`）：
把客户端自己的 shell/读文件工具定义改名成 `read`/`bash` 放进请求，模型调用后网关再把名字改回
客户端原本的工具名。

实测（本机跑 v1.1.0 + Codex CLI 0.162.1）：

| 场景 | 结果 |
|---|---|
| `mimo-v2.6-flash-free`（chat-only，走桥接）纯文本 | ✅ |
| `mimo-v2.6-flash-free` 工具调用（读文件→执行→回传→回答） | ✅ 1095 tokens 单轮完成 |
| `big-pickle`（chat-only，走桥接） | ✅ |
| `muse-spark-1.3-contributor-free`（原生 Responses）工具调用 | ✅ |
| 离线回归 | ruff + gate 43 项 + api 63 项 + bridge 38 项 全绿 |
| 真实上游冒烟（含限速/白名单/并发/断连） | 47 项全绿 |

### 审查与冒烟（v1.1）

- 静态检查：`ruff check .`（E/F/W/B）全绿，配置见 `pyproject.toml`
- 离线测试：`tests/test_gate.py` 33 项 + `tests/test_api.py` 46 项（mock 上游）全绿
- 真实上游冒烟：`scripts/smoke_live.py` 38 项全绿，覆盖首启生成密钥、鉴权、模型列表、
  流式/非流式、stub 注入与剥离、`/v1/responses`、错误码、密钥热更新、重启不换密钥、
  4 路并发流、客户端中途断连、限速 429、IP 白名单 403、访问日志 UTF-8 干净
- 审查修掉的问题：
  1. 非流式响应丢失 `usage`（`ChatRewriter` 按客户端意愿丢弃了 usage chunk）→ 非流式路径改为保留
  2. `MAX_AGGREGATE_BYTES` 定义了却没用 → 非流式聚合现在真的会中断并回 502
  3. 新增 `MAX_BODY_BYTES`（默认 32 MiB），超大请求回 413
  4. 上游中途返回 `error` 时会把错误当正常分片 → 流式透传、非流式 502、responses 也识别
  5. 客户端断连时 `finally` 里的 `await aclose()` 可能被取消导致漏记日志 → 改成 `suppress` + 保证写日志
  6. 容器以 uid 10001 运行，宿主 `./data` 属主不对时首启会崩 → 启动时给出明确修复命令
  7. 删除死代码（`keys.mask`、`keys.rotate`、`zen_session.random_suffix`、`Settings.host`）、
     私有类型标注 `ipaddress._BaseNetwork`、补齐 `raise ... from exc`

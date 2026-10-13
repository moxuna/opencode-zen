# OpenCode Zen 网关（Docker）

[![Build and publish container image](https://github.com/moxuna/opencode-zen/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/moxuna/opencode-zen/actions/workflows/docker-publish.yml)

把 `opencode.ai` 的免费模型（11 个）变成**你自己的 OpenAI 兼容接口**，任何客户端（pi、Cherry Studio、LobeChat、各种 SDK）都能直接用：

| 项目 | 值 |
|---|---|
| 接口地址 | `http://你的VPS_IP:8211/v1` |
| 密钥 | 首次启动自动生成，写在 `./data/api-keys.txt`，同时打印在日志里 |
| 支持 | `/v1/chat/completions`、`/v1/responses`、`/v1/models` |
| 镜像 | `ghcr.io/moxuna/opencode-zen:latest`（已构建，直接拉取） |

> ⚠️ **只建议个人自用**：这是绕过 OpenCode 免费层限制的用法，与其服务意图相悖；上游校验是启发式的，opencode 改一次就可能失效。详见文末[已知限制](#已知限制)。

---

## 30 秒部署（小白照抄版）

**前提**：一台能上外网的 Linux VPS（Ubuntu / Debian），有 root 或 sudo，且**能访问 `opencode.ai`**（第 ② 步会帮你确认）。

```bash
# ① 装 Docker（已装过会自动跳过）
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

# ② 建目录，先花 30 秒确认这台机器能用（3 项必须全 PASS）
mkdir -p ~/opencode-zen && cd ~/opencode-zen
curl -fsSLO https://raw.githubusercontent.com/moxuna/opencode-zen/main/scripts/probe_zen.py
command -v python3 >/dev/null || (apt-get update && apt-get install -y python3)
python3 probe_zen.py

# ③ 下载 compose 与默认配置，然后启动（会自动拉取镜像）
curl -fsSLO https://raw.githubusercontent.com/moxuna/opencode-zen/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/moxuna/opencode-zen/main/.env.example -o .env
docker compose up -d

# ④ 取出自动生成的密钥（也可以直接 cat data/api-keys.txt）
docker compose logs opencode-zen | grep -m1 sk-oczen-

# ⑤ 自测：返回 JSON 里带 "object":"chat.completion" 就是成功
KEY=$(grep -m1 -o 'sk-oczen-[0-9a-f]*' data/api-keys.txt)
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  http://127.0.0.1:8211/v1/chat/completions \
  -d '{"model":"big-pickle","messages":[{"role":"user","content":"say ok"}]}'
```

就这五步。密钥、日志都在 `~/opencode-zen/data/` 里，删容器不会丢。

### 放行端口（外网客户端必须做）

```bash
ufw allow 8211/tcp        # 系统防火墙；云厂商控制台的"安全组"里也要放行 8211
```

### 客户端怎么填

| 客户端里的一项 | 填什么 |
|---|---|
| API 类型 / 提供商 | `OpenAI`（或"OpenAI 兼容"） |
| Base URL / API 地址 | `http://你的VPS_IP:8211/v1` |
| API Key | 第 ④ 步取到的 `sk-oczen-...` |
| 模型名 | `big-pickle`（或先点"获取模型列表"） |

### 更新 / 停止 / 卸载

```bash
docker compose pull && docker compose up -d      # 更新到最新镜像
docker compose restart                           # 重启
docker compose down                              # 停止并删容器（data/ 保留，密钥还在）
docker compose down && rm -rf ~/opencode-zen     # 彻底删除
```

---

## 这个项目做了什么

`opencode.ai` 的免费层只允许 OpenCode 官方客户端调用，服务端实测有 4 条硬性校验（详见 [`REQUIREMENTS.md`](REQUIREMENTS.md)）：

1. `User-Agent` 必须以 `opencode/` 开头
2. `X-Opencode-Session` 必须是形如 `ses_<12位hex><14位base62>` 且"每毫秒计数器 < 64"的合法会话 ID
3. 请求体里 `"stream": true` 必须显式存在
4. 请求体的 `tools` 里必须同时有名为 `read` 和 `bash`/`shell` 的工具

网关自动把这些补齐，于是**任何客户端**都能用；同时它还是个正经的网关：

- **密钥自管**：首启自动生成随机密钥，可加多把，改文件**立即生效**（不用重启）
- **stub 工具不投毒**：客户端没带 `read`/`bash` 时网关注入最小 stub；模型真去调用它时，网关把调用转成一句文字提示并降级 `finish_reason`，**客户端永远看不到自己没声明过的工具**（也可配置成静默丢弃或原样透传）
- **非流式也能用**：上游只认 `stream:true`，客户端要非流式时网关收完 SSE 再聚合成标准 `chat.completion` / `response` 对象，`usage` 一并带上
- **访问日志**：`data/access.jsonl`，含时间/密钥名/模型/tokens/耗时/状态/是否注入过 stub
- **限速与白名单**：每把密钥每分钟上限、IP CIDR 白名单（默认关闭）
- **健康检查**：`/healthz`（不鉴权，供 docker 用）、`/readyz`（会真去问上游，返回模型数与会话计数器）

---

## 目录结构

```
opencode-zen/
├─ docker-compose.yml        一键部署（默认拉取已发布镜像）
├─ Dockerfile                自己构建时用
├─ docker-entrypoint.py      首启修正挂载目录属主并降权运行（免手工 chown）
├─ .env / .env.example       配置（仓库自带一份默认值，可直接改）
├─ requirements.txt
├─ pyproject.toml            ruff 静态检查配置
├─ .github/workflows/        CI：测试 → 构建推送镜像 → 冒烟验证镜像
├─ app/
│  ├─ main.py                路由、鉴权、SSE 透传与非流式聚合
│  ├─ config.py              env 变量
│  ├─ keys.py                密钥文件加载与热更新
│  ├─ zen_session.py         合法会话 ID 铸造（校验第 2 条）
│  ├─ gate.py                请求形状补丁 + stub 工具剥离（第 3、4 条）
│  ├─ upstream.py            上游 HTTP 客户端（自动带齐请求头）
│  └─ access.py              访问日志 + 每密钥限速
├─ scripts/
│  ├─ probe_zen.py           部署前就绪探测（纯标准库）
│  ├─ smoke_live.py          真实上游端到端冒烟（38 项）
│  └─ gen_key.py             手动生成 / 追加一把密钥
├─ tests/                    离线测试：33 + 46 项断言
└─ data/                     ★ 密钥与日志（挂载卷，别提交到 git）
```

---

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| `403 FreeTierError: can only be used from within OpenCode` | 上游改了校验规则，或这台机器的出口 IP 被拉黑。先 `docker compose exec opencode-zen python scripts/probe_zen.py` 复现，再对照 `REQUIREMENTS.md` 第二节逐条排查 |
| 第 ② 步就 FAIL | 这台 VPS 连不上上游（机房被 Cloudflare 拦 / 需要代理）。换机房，或在 `.env` 里加 `HTTPS_PROXY=http://...` 再启动 |
| `401 invalid_api_key` | 密钥填错；`cat data/api-keys.txt` 核对。注意有些客户端会自动在 Base URL 后补 `/v1`，别重复填 |
| `429` | 一是你在 `.env` 设的 `RATE_LIMIT_PER_MIN`，二是上游免费层限速（等一会儿或换模型） |
| `400 model_not_free` | 该模型需要真实的 `OPENCODE_API_KEY`（填进 `.env` 即可解锁全部 84 个模型） |
| `413 body_too_large` / `502 aggregate_too_large` | 请求体或非流式聚合超了上限，调大 `MAX_BODY_BYTES` / `MAX_AGGREGATE_BYTES`，或改用 `stream:true` |
| `502 upstream_unreachable` | 容器出不了网：检查 DNS、机房是否封 443、是否需要 `HTTPS_PROXY` |
| 外网连不上，本机 `curl` 正常 | 云厂商安全组没放行 8211，或 `ufw` 没放行 |
| 容器起不来，日志说 `Cannot create the key file at /data/api-keys.txt` | 少见（入口脚本已自动处理属主）；如果手改了 `DATA_DIR`，就 `chown -R 10001:10001 data` 再重启 |
| 非流式请求很慢 | 上游只支持流式，网关要等整段生成完才返回；让客户端用 `stream:true` 就快了 |
| 回复里出现 `[zen-gateway] ...` | 模型调用了网关注入的假工具，被转成文字提示。想静默就把 `.env` 里 `STUB_CALL_MODE=drop` |
| 日志里 `injected_tools` 非空 | 这次请求客户端没带 read/bash，网关照例补了 stub，正常现象 |

---

## 手动部署 / 自定义

### 配置项（`.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST_PORT` | `8211` | 对外端口（容器内固定 8211） |
| `OPENCODE_API_KEY` | 空 | 留空=只用 11 个免费模型；填真实密钥=84 个模型全解锁 |
| `INJECT_STUB_TOOLS` | `true` | 客户端没带 read/bash 时自动补（**关掉纯聊天客户端会 403**） |
| `STUB_CALL_MODE` | `text` | 假工具被调用时：`text` 变文字提示 / `drop` 丢弃 / `passthrough` 原样透传 |
| `IP_ALLOWLIST` | 空 | 例如 `1.2.3.4/32,10.0.0.0/8` |
| `RATE_LIMIT_PER_MIN` | `0` | 每把密钥每分钟上限，0=不限 |
| `TRUST_PROXY` | `false` | 在 Caddy/Nginx 后面才改 `true`（否则可被伪造来源 IP） |
| `ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | 上游地址，一般不用改 |
| `MAX_BODY_BYTES` | 32 MiB | 请求体上限，超了回 413 |
| `MAX_AGGREGATE_BYTES` | 8 MiB | 非流式聚合上限，超了回 502 |
| `LOG_LEVEL` / `LOG_REQUESTS` | `info` / `true` | 日志级别 / 是否写访问日志 |

改完 `.env` 后 `docker compose up -d` 重建容器即可生效。

### 用源码自己构建（想改代码时）

```bash
git clone https://github.com/moxuna/opencode-zen.git && cd opencode-zen
vi .env                      # 按需改
# 把 docker-compose.yml 里的 image 注释掉、build 打开
docker compose up -d --build
```

### 不用 Docker 直接跑

```bash
pip install -r requirements.txt
KEY_FILE=./data/api-keys.txt ACCESS_LOG=./data/access.jsonl PORT=8211 \
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8211 --no-access-log
```

---

## 改密钥

文件在宿主机 `./data/api-keys.txt`，格式 `名字:密钥`，`#` 开头是注释：

```
# 名字:key，改完保存即生效
default:sk-oczen-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
phone:sk-oczen-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
```

- **立刻生效**：改完下一次请求就用新内容，不用重启容器
- **加一把**：`docker compose exec opencode-zen python scripts/gen_key.py --name phone --file /data/api-keys.txt`，或直接编辑文件
- **废一把**：删掉那一行
- **给别人用**：给他单独一行，这样访问日志里能看出是谁在用，也方便单独限速/作废
- 建议 `chmod 600 data/api-keys.txt`

---

## 客户端接入

### pi

编辑 `~/.pi/agent/models.json`：

```json
{
  "providers": {
    "zengw": {
      "name": "Zen Gateway",
      "baseUrl": "http://你的VPS:8211/v1",
      "api": "openai-completions",
      "apiKey": "sk-oczen-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "models": [
        { "id": "big-pickle", "name": "big-pickle", "reasoning": true, "input": ["text"], "contextWindow": 200000, "maxTokens": 32000 }
      ]
    }
  }
}
```

（`apiKey` 换成你自己的那串。）然后 `pi --model zengw/big-pickle`，或在 `/model` 里选。

### Cherry Studio / LobeChat / 其它

- 类型：`OpenAI`（OpenAI 兼容）
- API 地址：`http://你的VPS:8211/v1`
- 密钥：`sk-oczen-...`
- 模型：点"获取模型列表"，或手填 `big-pickle`

### curl / SDK

```bash
curl -N http://你的VPS:8211/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"big-pickle","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

鉴权三种写法都认：`Authorization: Bearer <key>`、`x-api-key: <key>`、`?key=<key>`。

### Responses 协议（muse-spark 这类模型）

`POST /v1/responses`，请求/响应都是标准 Responses 形状；客户端要非流式时网关自动聚合。

---

## 安全建议

明文 HTTP + 公网 = 密钥在链路上裸奔，而且扫到端口的人都能试着猜密钥。三选一：

1. **域名 + Caddy 自动 HTTPS**（推荐）：

   ```bash
   # /etc/caddy/Caddyfile
   zen.example.com {
       reverse_proxy 127.0.0.1:8211
   }
   ```

   然后 `.env` 里设 `TRUST_PROXY=true`，`docker compose up -d`，客户端填 `https://zen.example.com/v1`。

2. **只走 SSH 隧道**（最省事，端口不暴露）：

   ```bash
   ssh -N -L 8211:127.0.0.1:8211 user@你的VPS
   # 本地客户端填 http://127.0.0.1:8211/v1
   ```

   这时 compose 里可以把端口映射改成 `127.0.0.1:8211:8211`。

3. **IP 白名单 + 限速**：`.env` 里设 `IP_ALLOWLIST` 与 `RATE_LIMIT_PER_MIN`。

另外：`data/` 别提交到 git、别丢进公开网盘。

---

## 已知限制

1. 这是绕过 OpenCode 免费层限制的用法，与其服务意图相悖；**只建议个人自用**，不要公开分享密钥。
2. 上游校验是启发式的（UA、会话 ID、stream、tools 四条），**opencode 改一次就要跟一次**；也可能某天直接封出口 IP。
3. 免费模型只有 11 个，且有限速，不适合高并发或对外提供公共服务。
4. 非流式请求必须等整段回答生成完，超长回答会占用网关连接。
5. 注入的 `read`/`bash` 是假工具，模型可能真的去调用它们（默认会被转成文字提示）。
6. 上游 `responses` 接口的 stub 剥离是尽力而为：会丢弃对应事件，并在最终 `response.output` 里换成一条文字说明。
7. 限速是**进程内内存**实现：重启即清零，多副本互不共享（单容器自用足够）。
8. 没有请求 ID 透传与 Prometheus 指标；需要统计就自己读 `data/access.jsonl`。

---

## 测试、CI 与开发

```bash
python3 tests/test_gate.py     # 33 项：会话 ID 形状、stub 注入/剥离、非流式聚合（纯离线）
python3 tests/test_api.py      # 46 项：HTTP 全链路，上游用 mock，不联网
python3 scripts/probe_zen.py   # 3 项：机器到 opencode.ai 的连通性与免费层是否放行（联网）
pip install ruff && ruff check .

# 端到端冒烟：本地模式会自己起 3 个实例（含限速/白名单场景）
python3 scripts/smoke_live.py

# 端到端冒烟：远程模式，直接测已经部署好的网关（就是下面这条）
python3 scripts/smoke_live.py --base-url http://你的VPS:8211 --key sk-oczen-...
```

远程模式会跑 31 项：健康/就绪、`/v1/models` 与单模型查询、三种鉴权写法、非流式聚合（含
`usage`）、中文往返、流式 SSE、客户端自带工具透传、注入 stub 被剥离、`/v1/responses`
流式与非流式、400/404/413 错误码、4 路并发、客户端中途断连后仍健康。

`main` 分支每次推送会触发 GitHub Actions：先跑 lint + 两套离线测试，再构建
`linux/amd64` + `linux/arm64` 镜像推到 GHCR，最后真起一个容器验证
"首启自动生成密钥 + `/healthz` + 未鉴权 401"。打 `v*` 标签会额外生成同名版本 tag。

镜像地址：`ghcr.io/moxuna/opencode-zen:latest`（也支持 `:<short-sha>` 与 `:v1.0.0` 形式）。

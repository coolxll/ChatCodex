# ChatCodex

在 ChatGPT 里安全地使用本地 Codex。

ChatCodex 是一个本地网关：它把你的 ChatGPT 对话连接到这台电脑上的官方
Codex，让 ChatGPT 能在你划定的工作区内执行命令、读写文件、应用补丁——
而每一步可能有影响的操作，都要经过你在控制台里点「允许」才会真正执行。

- **你说了算**：默认所有命令、写入、补丁都要先经过审批。
- **限定范围**：Codex 只能在你指定的工作区目录内活动。
- **凭据分开**：登录控制台用的 Token 和 ChatGPT 连接用的密钥互不相同。
- **只用官方 Codex**：不做任何修改，由网关统一管控安全边界。

---

## 快速开始

只需要 Python 3.11+ 和 Node.js。

### 1. 启动网关

```bash
cd backend
uv sync --locked
uv run python -m app.main
```

首次启动会自动生成两个密钥并打印在控制台：

- **Web Access Token** — 用来登录下面的管理控制台。
- **MCP Access Token** — 留给 ChatGPT 连接用（见下文）。

> 没装 `uv`？也可以 `pip install fastapi uvicorn mcp websockets` 后用
> `python -m app.main` 启动；Windows 下直接运行 `backend/start.bat`。

### 2. 构建前端

```bash
cd frontend
npm install
npm run build
```

构建后网关会自动使用 `frontend/dist/` 里的界面；跳过这步也能跑，只是用内置的简化界面。

### 3. 打开控制台

浏览器访问 **http://127.0.0.1:8000/**，粘贴第 1 步打印的 **Web Access Token** 登录。

之后**几乎所有配置都在这个网页里改**，不用再去碰命令行参数。

---

## 让 ChatGPT 能连进来

网关默认只监听本机。要让 ChatGPT 访问，需要给它一个公网地址，两种方式任选：

| 方式 | 适合 | 怎么做 |
|---|---|---|
| **Cloudflare 隧道** | 大多数人 | 控制台 →「公网入口」→ 选 Cloudflare；临时域名调试、固定域名上线 |
| **直接暴露** | 已有公网 IP / 域名 | 把 HTTPS、证书、DNS 指向本机，控制台 →「公网入口」→ 选直接暴露 |

配好后，在 ChatGPT 里添加连接器：

1. 打开 `chatgpt.com` → **Settings → Connectors**。
2. URL 填 `<你的公网地址>/mcp/`（注意末尾斜杠）。
3. 按控制台「设置 → 访问与认证」里选的方式完成鉴权：
   - **OAuth**：浏览器会弹出授权页，输入你在控制台设置的 OAuth 密码。
   - **Token**：把 **MCP Access Token** 作为 Bearer 填入（不是 Web Access Token）。

> ChatGPT Tunnel 是控制台「设置」页里另一条独立的接入通道，不走上面的公网入口，
> 按需二选一或并用即可。

---

## 日常使用

- **开始一个任务**：在 ChatGPT 里让它打开工作区，确认目录、沙箱和审批策略后即可开始。
- **处理审批**：需要执行命令或改动文件时，控制台「审批」页会实时弹出请求，点「允许一次」或「拒绝」。
- **管理 Codex**：控制台「Codex」页可启动 / 重启 / 更新官方 Codex，可让它自动下载，也可指定本地安装包。
- **管理上下文**：「执行上下文」页查看或归档每个对话绑定的工作区。

### 改动何时生效

- 公网入口、ChatGPT Tunnel：在对应页面即时启停。
- 认证类设置（Token / OAuth）：保存后需**重启网关**。
- Codex 连接设置：保存后到「Codex」页点「重启 / 重连」。
- 改了前端代码：重新 `npm run build` 并重启网关。

---

## 常见问题

**健康检查**：`curl http://127.0.0.1:8000/healthz`，返回 `{"ok":true,...}` 即正常。

**忘了 Web Access Token？** 重启网关会在控制台重新打印；也可在启动前用环境变量
`CHATCODEX_WEB_ACCESS_TOKEN` 指定一个固定值。

**登录控制台和连 ChatGPT 用的是同一个密钥吗？** 不是。Web Access Token 只用于登录控制台；
MCP Access Token / OAuth 才用于 ChatGPT 连接，两者独立、互不通用的。

**数据存在哪？** 默认在用户私有目录（Windows：`%LOCALAPPDATA%\ChatCodex\`；
Linux/macOS：`$XDG_STATE_HOME/chatcodex/`），可用 `CHATCODEX_DATABASE_URL` 更换位置。

---

## 进阶：环境变量

所有选项都能用环境变量在启动前覆盖；控制台改的配置优先级更高。常用的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CHATCODEX_HOST` / `CHATCODEX_PORT` | `127.0.0.1:8000` | 监听地址 |
| `CHATCODEX_WEB_ACCESS_TOKEN` | 自动生成 | 控制台登录 Token |
| `CHATCODEX_MCP_ACCESS_TOKEN` | 自动生成 | ChatGPT 连接的静态密钥 |
| `CHATCODEX_MCP_AUTH_MODE` | `token` | `token` / `oauth` / `both` / `noauth` |
| `CHATCODEX_PUBLIC_URL` | `http://127.0.0.1:8000` | 公网地址（OAuth 需要） |
| `CHATCODEX_DATABASE_URL` | 用户私有目录 | 数据库位置（`sqlite:///` 或 `postgresql://`） |
| `CHATCODEX_CODEX_COMMAND` | 自动解析 / 下载 | Codex 可执行文件路径 |
| `CHATCODEX_APPROVAL_TIMEOUT_MS` | `300000` | 审批等待时长（毫秒） |

完整列表与默认值见控制台「设置」页。

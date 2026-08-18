# ChatCodex Step-by-Step 操作手册

> 本手册基于当前仓库实现，面向第一次安装、日常使用和维护 ChatCodex 的使用者。项目的核心作用是：通过本地 Gateway，将 ChatGPT 连接到官方 Codex App Server，并把工作目录、沙箱和审批控制在本机。

## 1. 先理解几个安全边界

ChatCodex 有三个需要分清的边界：

1. **Web Access Token**：只用于登录本地管理控制台。
2. **MCP Access Token / OAuth**：用于保护 `/mcp/`，给 ChatGPT 连接使用。它不能代替 Web Access Token。
3. **执行工作区**：每个 ChatGPT 对话都要先绑定一个目录、沙箱模式和审批策略；文件路径越出工作区会被拒绝。

建议的日常安全组合是：

- 沙箱：`工作区可写`（`workspace-write`）
- 审批：`按需请求`（`on-request`）
- MCP 下游工具：只读工具允许，带副作用的工具设为需审批

`完全访问`（`danger-full-access`）会允许获批命令访问工作区外资源，只在完全信任当前任务时使用。

## 2. 安装前检查

### 2.1 必需软件

- Python **3.11 或更高版本**
- Node.js 和 npm
- `uv`（推荐，用于按 `uv.lock` 安装后端依赖）
- 如果使用公网入口：Cloudflare Tunnel 或已经配置好的 HTTPS、DNS 和端口转发

先在终端检查：

```bash
python3 --version
node --version
npm --version
uv --version
```

当前项目声明的 Python 最低版本是 3.11；如果系统只有 Python 3.9，后端不能按锁定依赖正常运行。

### 2.2 获取项目并进入目录

```bash
cd /path/to/ChatCodex
```

Windows PowerShell 示例：

```powershell
Set-Location C:\path\to\ChatCodex
```

## 3. 第一次安装

### Step 1：安装后端依赖

```bash
cd backend
uv sync --locked
```

`--locked` 会严格使用仓库中的 `uv.lock`。如果锁文件与 `pyproject.toml` 不一致，命令会失败，应先处理依赖变更，不要直接忽略锁文件。

### Step 2：安装并构建前端

```bash
cd ../frontend
npm ci
npm run build
```

构建会生成 `frontend/dist/`，包括管理面板和 ChatGPT 使用的独立 widget。`frontend/dist/` 是构建产物，不需要提交到 Git。

如果暂时不构建前端，Gateway 仍可启动，但根路径只会显示内置的简化提示页，完整控制台和 widget 不可用。

## 4. 启动 Gateway

打开一个终端窗口：

```bash
cd /path/to/ChatCodex/backend
uv run python -m app.main
```

也可以使用脚本：

```bash
bash start.sh       # macOS / Linux
start.bat           # Windows
```

首次启动时，终端会打印：

- `Web Access Token`
- `MCP Access Token`（默认 Token 模式）
- 管理面板地址，默认是 `http://127.0.0.1:18473/`
- MCP 地址，默认是 `http://127.0.0.1:18473/mcp/`

请立即把两个 Token 分开保存到密码管理器。后续启动通常只显示“已配置”，不会回显秘密值。

### Step 3：检查 Gateway 健康状态

另开一个终端执行：

```bash
curl http://127.0.0.1:18473/healthz
```

正常时应看到包含以下字段的 JSON：

```json
{"ok":true,"appserver":true,"healthy":true}
```

如果 Gateway 能打开但 `ok` 为 `false`，说明 Web 管理面仍在运行，但 Codex App Server 尚未可用；继续看第 6 节“管理 Codex Runtime”。

## 5. 登录并完成首次配置

### Step 4：登录管理控制台

1. 浏览器打开 `http://127.0.0.1:18473/`。
2. 粘贴启动日志中的 **Web Access Token**。
3. 点击“进入控制台”。

不要把 MCP Access Token 填入此登录框。

### Step 5：检查“概览”

进入“概览”，确认：

- `Codex Runtime` 是否在线
- 是否有待处理审批
- 是否已经建立 MCP 入口
- Web 与 MCP 的认证模式是否符合预期

## 6. 管理 Codex Runtime

进入左侧“Codex”。

### 6.1 使用内部官方 App Server（默认）

推荐保持：

- 连接方式：`内部服务器（native）`
- Codex 二进制：留空，让项目自动解析或下载官方运行时
- 崩溃自动重启：开启

如果页面显示 Codex 未安装：

1. 在“本地官方完整包”输入框留空，或填入本地官方安装包路径。
2. 点击“安装 / 更新官方 Codex”。
3. 安装完成后点击“重启 / 重连”。
4. 回到“概览”，确认 Runtime 在线。

下载的运行时位于 `native/`，该目录包含下载内容或运行时文件，不应提交到 Git。

### 6.2 连接外挂 WebSocket App Server

只有在你已经有一个兼容的官方 App Server WebSocket 服务时，才选择：

1. “设置” → Codex App Server → 连接方式选择“外挂 WebSocket”。
2. 填写 WebSocket URL，例如 `wss://host/codex`。
3. 填写对应 WebSocket 密钥。
4. 保存后进入“Codex”，点击“重启 / 重连”。

如果切换了 App Server、重启了 App Server 或修改了连接参数，之前的执行上下文可能会失效。重新打开 ChatGPT 的工作区配置并保存一次即可刷新上下文授权版本。

## 7. 配置认证和执行策略

进入“设置”，完成以下配置后点击右上角“保存更改”。

### 7.1 访问与认证

Web Access Token 和 MCP Access Token 必须分别设置：

- `token`：静态 Bearer Token；适合本机或私有 Tunnel。
- `both`：同时接受 Token 和 OAuth；需要公网 OAuth 时最灵活。
- `oauth`：只接受 OAuth；需要稳定、可公网访问的 HTTPS issuer。
- `noauth`：无 MCP 认证，仅允许绑定到 loopback（本机）地址，不能拿来做公网服务。

启用 OAuth 或 `both` 时，还要填写：

- OAuth 授权密码
- 固定公网 URL，例如 `https://codex.example.com`

固定公网 URL 必须是 HTTPS 根地址，不能是 `127.0.0.1`、`localhost`、带路径、query 或 fragment 的地址。

认证设置保存后需要**重启 Gateway**才完全生效。修改 Web Access Token 后，浏览器也需要重新登录。

### 7.2 会话默认值

可在“设置”里设定默认值：

- 工作模式：`Agent` 或 `Plan`
- 审批请求策略：`严格请求`、`按需请求`、`从不请求`
- 默认系统访问：`只读`、`工作区可写`、`完全访问`
- 审批等待：默认 300000 毫秒，即 5 分钟

这些是新执行上下文的默认值；ChatGPT 打开工作区时仍会显示并允许按托管要求调整。

### 7.3 下游 MCP 工具

“设置”页的下方会显示已登记的下游 MCP server 和工具：

- `允许`：直接暴露给 WebChat
- `需审批`：调用时进入审批队列
- `禁止`：不暴露给 WebChat

未配置的工具默认是禁止。建议先只开放明确需要的工具，尤其不要默认开放有写入、删除、网络或外部系统副作用的工具。

## 8. 建立公网 MCP 入口

默认 Gateway 只监听本机，ChatGPT 无法直接访问。全局公网入口会同时影响 Web、REST 和 OAuth issuer；`ChatGPT Tunnel` 是另一条独立的 MCP-only 通道，见第 9 节。

### 8.1 Cloudflare 临时域名（调试）

1. 进入“公网入口”。
2. 选择“Cloudflare”。
3. 模式选择“临时域名（仅调试）”。
4. 点击“启用”。
5. 等待状态变为运行，并复制显示的公网地址。

临时域名每次重启都可能变化。OAuth 客户端可能需要重新连接；生产环境不要依赖临时域名。

### 8.2 Cloudflare 固定域名（生产）

1. 在 Cloudflare 侧创建并配置固定域名 Tunnel。
2. 确认 Tunnel 能把流量转发到本机 Gateway，例如 `http://127.0.0.1:18473`。
3. 进入“公网入口”并选择“Cloudflare”。
4. 模式选择“固定域名（生产）”。
5. 填入 Tunnel Token。
6. 在“设置”中把固定公网 URL 填成实际 HTTPS 根域名。
7. 点击“启用”，确认状态为运行。

如果使用 OAuth，固定公网 URL 必须能从公网访问以下元数据地址：

```text
https://你的域名/.well-known/oauth-protected-resource
https://你的域名/.well-known/oauth-authorization-server
```

### 8.3 直接暴露（高级）

“直接暴露”不会启动代理程序。选择它前，必须自行完成：

- 公网 DNS 指向你的入口
- HTTPS 证书和 TLS 终止
- 防火墙与 NAT/端口转发
- 反向代理或端口映射到 Gateway 的监听地址

完成后在“设置”中填写实际公网 URL，再到“公网入口”选择“直接暴露”并启用。

## 9. 使用 ChatGPT Tunnel（可选）

ChatGPT Tunnel 只把 `/mcp/` 提供给 ChatGPT，不承担全局 Web、REST 或 OAuth issuer 的公网访问。

### 9.1 前置条件

你需要 OpenAI 控制平面提供的：

- Tunnel ID（通常以 `tunnel_` 开头）
- Runtime API Key

### 9.2 启动步骤

1. 进入“设置” → “ChatGPT Tunnel · MCP”。
2. 填入 Tunnel ID。
3. 填入 Runtime API Key。
4. 如需使用本地下载的客户端，点击“下载 / 更新客户端”。
5. 打开“随 Gateway 自动启动”，需要重启后才按自动配置启动。
6. 点击“启动”。
7. 确认状态依次变为进程运行、健康、就绪。
8. 在“概览”复制显示的 Tunnel 地址或 Tunnel 标识，按 ChatGPT 连接器创建流程使用。

Token 模式下，MCP Access Token 只保护 tunnel-client 到本地 MCP 的私有跳转，不要把 Web Access Token 当作 MCP Token 使用。

如果使用 OAuth，仍然必须先配置全局、稳定且可公网访问的 HTTPS URL；Secure MCP Tunnel 不会替你转发授权服务器。

## 10. 在 ChatGPT 中开始任务

### Step 6：添加 MCP 连接器

使用全局公网入口时：


1. 打开 ChatGPT → Settings → Connectors。
2. 新增连接器。
3. URL 填：

   ```text
   https://你的公网地址/mcp/
   ```

4. 根据 MCP 认证模式完成授权：
   - OAuth / Both：按页面提示打开授权页，输入 OAuth 授权密码。
   - Token：使用 MCP Access Token 作为 Bearer Token。

### Step 7：打开执行工作区

在 ChatGPT 中发起需要本地文件或命令的任务，按提示打开“Codex 工作区”：

1. 选择工作目录；可直接输入路径，也可在完整视图中点击“浏览”。
2. 选择工作模式：
   - `Agent`：执行任务、修改代码并验证结果。
   - `Plan`：先分析并形成计划，适合先审阅方案。
3. 选择系统访问：优先使用“工作区可写”。
4. 选择审批策略：优先使用“按需请求”。
5. 如果选择“完全访问”，勾选风险确认。
6. 点击“开始”。

工作区保存后，控制台“执行上下文”中会出现对应的对话、目录、沙箱和审批策略。

## 11. 日常审批流程

### Step 8：审阅并处理操作

以下操作通常需要审批：写文件、执行命令、应用补丁，以及被设置为需审批的下游 MCP 工具。

你可以在两个位置处理：

- ChatGPT 内嵌的审批 / diff widget
- 管理控制台 → “审批”

处理前检查：

1. 操作类型和目标路径是否正确。
2. 命令的工作目录是否在预期工作区内。
3. 写入内容或补丁是否符合预期。
4. 是否真的需要提升权限。

可选决定：

- “允许一次”：只允许当前操作。
- “本会话允许”：如果界面提供，只在当前会话范围内允许。
- “拒绝”：操作不会执行。

审批超时会导致操作失败；可以在“设置”里调整审批等待时间。不要为方便而关闭审批或把所有工具设为允许。

### Step 9：查看和归档执行上下文

进入“执行上下文”：

1. 点击某个上下文查看对话 ID、工作目录、工作区根目录、沙箱和审批策略。
2. 任务结束后点击“归档”。
3. 需要继续使用时点击“恢复”。

若看到 `stale_execution_context`、工作区配置变更或 App Server 重启提示，回到 ChatGPT 重新打开工作区并保存一次。

## 12. 停止、重启和升级

### 12.1 停止 Gateway

在运行 Gateway 的终端按 `Ctrl+C`。Gateway 退出时会停止隧道、App Server 并关闭数据库连接。

### 12.2 只重启 Codex

不需要重启整个 Gateway 时：

1. 打开控制台“Codex”。
2. 点击“重启 / 重连”。
3. 确认状态恢复为在线。

### 12.3 修改认证后重启

修改 Web Token、MCP Token、认证模式、OAuth 密码或固定公网 URL 后：

1. 点击“保存更改”。
2. 停止 Gateway。
3. 重新执行 `uv run python -m app.main`。
4. 用新的 Web Access Token 重新登录。

### 12.4 修改前端后生效

```bash
cd frontend
npm run build
```

然后重启 Gateway，使其重新读取 `frontend/dist/`。

### 12.5 更新依赖或官方运行时

后端依赖：

```bash
cd backend
uv sync --locked
```

前端依赖：

```bash
cd frontend
npm ci
npm run build
```

官方 Codex 运行时：在控制台“Codex”页点击“安装 / 更新官方 Codex”，完成后点击“重启 / 重连”。

## 13. 前端开发与预览

开发前端 widget 或控制台时：

```bash
cd frontend
npm ci
npm run dev
```

然后打开：

```text
http://127.0.0.1:5173/preview.html
```

建议每次改动后执行：

```bash
npx tsc --noEmit
npm run build
```

重点检查 widget 的浅色 / 深色主题，以及内嵌 / 全屏布局。开发服务器只用于预览；正式 Gateway 使用的是 `frontend/dist/`。

## 14. 后端测试和发布前检查

```bash
cd backend
uv run python -m unittest discover -s tests -p "test_*.py"
uv run python tests/auth_http_integration.py
```

再执行前端检查：

```bash
cd ../frontend
npx tsc --noEmit
npm run build
```

跨层修改至少要跑后端 contract tests 和前端 build。涉及认证、OAuth、隧道或工作区边界时，还应运行真实进程的认证集成测试。

## 15. 常见问题排查

### 15.1 页面打不开

```bash
curl http://127.0.0.1:18473/healthz
```

- 连接失败：Gateway 没启动、端口被占用或监听地址已修改。
- 页面是简化提示页：没有运行 `cd frontend && npm run build`，或 Gateway 指向了错误的 `frontend/dist`。
- 返回 401：使用的是错误的 Web Access Token。

### 15.2 Gateway 能打开，但 Codex 离线

1. 打开“Codex”查看最近状态和错误。
2. 确认官方 Codex 已安装，或“Codex 二进制”路径正确。
3. 检查内部 WebSocket 端口 `8765` 是否被占用。
4. 点击“安装 / 更新官方 Codex”。
5. 点击“重启 / 重连”。

### 15.3 ChatGPT 无法连接 MCP

依次检查：

1. 连接器 URL 是否以 `/mcp/` 结尾。
2. 是否误用了 Web Access Token；MCP Token 必须使用 MCP Access Token。
3. 公网入口或 ChatGPT Tunnel 是否处于“运行 / 就绪”。
4. OAuth 模式下，公网 URL 是否为稳定 HTTPS 根地址。
5. Cloudflare 临时域名是否在重启后发生变化。
6. 认证设置改动后是否重启 Gateway。

本地可检查 OAuth 元数据：

```bash
curl https://你的公网地址/.well-known/oauth-protected-resource
curl https://你的公网地址/.well-known/oauth-authorization-server
```

也可以在“设置”右侧查看 OAuth Metadata 自检结果。

### 15.4 文件路径被拒绝

确认 ChatGPT 当前执行上下文的目录和工作区根目录正确。不要用工作区外的绝对路径；修改目录后，重新打开“Codex 工作区”并保存。

### 15.5 审批没有出现或操作超时

1. 打开控制台“审批”，手动刷新。
2. 确认审批策略没有设为“从不请求”。
3. 检查审批等待时间是否过短。
4. 确认 ChatGPT 和控制台没有使用不同的会话或上下文。
5. 如果 App Server 刚重启，重新保存执行工作区。

### 15.6 忘记 Token

首次启动时生成的 Token 应从启动日志保存。为避免把秘密写进仓库，不要把 Token 提交到 Git。

如果仍能登录，可在“设置”中输入新 Token、保存并重启 Gateway。若完全无法登录，优先从受保护的数据库备份恢复，或由管理员按项目的数据库运维流程重置 Web Token；不要随意删除数据库，因为其中还保存了配置、上下文和 OAuth 状态。

## 16. 数据、秘密和备份

默认数据库位置：

- macOS / Linux：`$XDG_STATE_HOME/chatcodex/chatcodex.db`；未设置时通常是 `~/.local/state/chatcodex/chatcodex.db`
- Windows：`%LOCALAPPDATA%\ChatCodex\chatcodex.db`

可通过 `CHATCODEX_DATABASE_URL` 指定 SQLite 路径。当前项目也保留 PostgreSQL 配置入口，但使用前应确认对应驱动和部署方式已经准备好。

不要提交或公开以下内容：

- Web Access Token、MCP Access Token、OAuth 密钥和密码
- `native/` 中的下载运行时或秘密文件
- 数据库文件、Tunnel Token、Runtime API Key
- 公网 URL 对应的私有证书和隧道凭据

备份数据库前先停止 Gateway，复制完成后再启动，以避免 SQLite 写入过程中产生不一致备份。

## 17. 常用命令速查

| 目的 | 命令 |
|---|---|
| 安装后端 | `cd backend && uv sync --locked` |
| 启动 Gateway | `cd backend && uv run python -m app.main` |
| 健康检查 | `curl http://127.0.0.1:18473/healthz` |
| 安装前端 | `cd frontend && npm ci` |
| 构建前端 | `cd frontend && npm run build` |
| 前端开发 | `cd frontend && npm run dev` |
| TypeScript 检查 | `cd frontend && npx tsc --noEmit` |
| 后端 contract tests | `cd backend && uv run python -m unittest discover -s tests -p "test_*.py"` |
| 认证集成测试 | `cd backend && uv run python tests/auth_http_integration.py` |

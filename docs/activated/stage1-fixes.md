# 阶段 1 审查问题与阶段 2 前修复清单

本文记录阶段 1 完成后对现有代码与阶段 2 计划做架构与实现审查时发现的问题。在进入阶段 2 开发前，按本清单先行修复并同步更新 [dev-plan.md](dev-plan.md) 与 [stage2-plan.md](stage2-plan.md)。

## 一、阶段 1 代码与架构问题

### 1. `create_app()` 在应用创建阶段强绑定上游网络请求与分词器加载

- **现状**：`lgw/proxy/__init__.py` 的 `create_app()` 在创建 Flask 实例时立即请求所有供应商的 `GET {base_url}/models`（`load_catalog`），并加载 `tiktoken` 分词器（`load_encoder`）。而 `init-db`、`create-key`、`sweep` 三个数据库 CLI 命令均注册在 `lgw.proxy` 上。
- **问题**：
  - 执行仅依赖本地数据库的 CLI 命令时，也会触发外部网络请求和 4 MB 分词器加载。
  - 当任一上游供应商网络抖动、不可达或返回非 200 时，`init-db`、`create-key` 和阶段 4 的 `sweep` 定时清扫任务都会因 `ConfigurationError` 崩溃退出。
  - 与 `stage2-plan.md` 中「DeepSeek 不可达时 admin 仍能启动，建表继续用 proxy 上的 `init-db`」矛盾。
  - 所有单元测试创建 `app` 时都被迫 mock `/models` 请求并手动关闭 `lgw_http`。
- **修复方向**：将数据库 CLI 命令与 `proxy` 的上游初始化解耦（例如提供独立的 CLI 入口，或将 `catalog` / `encoder` 改为仅在提供转发服务时初始化）。

### 2. 启动时同步拉取 `/models` 构建模型路由表，任一失败即拒绝启动

- **现状**：`lgw/proxy/catalog.py` 不在配置中声明模型，而是依靠进程启动时向每家供应商请求 `GET {base_url}/models` 构建路由表；任一供应商请求失败或格式异常即抛 `ConfigurationError` 阻止进程启动。
- **问题**：
  - 多供应商场景下，任一供应商在扩容或重启时短暂故障，会导致整个网关进程无法启动（`CrashLoopBackOff`），拖垮其他正常供应商。
  - 模型表仅在进程启动时拉取一次，上游增删模型必须重启网关进程。
  - 阶段 3 接入的 AWS Bedrock 原生接口没有 OpenAI 风格的 `GET /models`，该机制无法自然演进到阶段 3。
- **修复方向**：重新审视模型路由来源（如改为配置文件显式声明模型列表，或允许按需刷新/降级），消除进程启动对外部上游网络可用性的硬依赖。

### 3. 流式转发在非正常结束时存在半截事件与错误事件粘包 Bug，且 `sse.py` 存在冗余复杂度

- **现状**：
  - `lgw/proxy/streaming.py` 在 `upstream.iter_bytes()` 循环结束后，若 `buffer` 非空则直接 `yield buffer`，随后若 `not progress.done` 再 `yield _ERROR_EVENT`。
  - `lgw/proxy/sse.py` 手写了支持单 `\r`、`\r\n`、`\n` 混用的字节切分器，且每次收到 chunk 都从 `buffer` 偏移 `0` 重新扫描。
- **问题**：
  - **粘包 Bug**：当上游未抛底层读取异常而是直接 EOF 断开，且 `buffer` 中残留未以空行结尾的半截事件（例如 `b'data: {"choices":['`）时，网关会先把半截字节发给客户端，紧接着直接拼上 `data: {"error": ...}\n\n`。不仅违背了「不向客户端转发半截事件」的设计目标，还会导致错误事件与半截数据粘在同一行，客户端无法解析出错误事件。
  - **复杂度与性能**：对单 `\r` 换行的跨 chunk 保留等待属于无必要的过度防御；`split_events` 在面对长事件（如大体积工具调用参数）按小 chunk 到达时存在 $O(N^2)$ 重复扫描。
- **修复方向**：
  - 仅在 `buffer` 构成合法的结尾事件（如末尾缺空行的 `data: [DONE]`）或补齐换行分隔后再输出，避免半截数据污染后续 `_ERROR_EVENT`。
  - 简化 SSE 切分逻辑，避免从头重复扫描整个 `buffer`。

### 4. 将 DeepSeek 专有的 `user_id` 格式约束耦合为全网关 `users.sub` 主键与登录准入规则

- **现状**：`lgw/keys.py` 的 `validate_user_sub` 要求 `sub` 必须匹配 `[a-zA-Z0-9_-]+` 且长度不超过 512，理由是 DeepSeek 的 `user_id` 字段有此格式要求。`stage2-plan.md` 进一步规定 OIDC 登录回调中若 `sub` 不符合该正则，直接拒绝用户登录（`?error=invalid_sub`）。
- **问题**：将单一上游厂商的字段格式要求侵入到了网关自身的用户表主键定义和 OIDC 登录准入上。若 IdP 的 `sub` 含冒号、`@`、斜杠等合法 OIDC 字符，用户将无法使用本网关的任何模型。
- **修复方向**：网关内部 `users.sub` 只做通用非空与长度校验；发往 DeepSeek 的 `user_id` 在 `providers/deepseek.py` 内部转换（例如当不满足正则或统一使用 `sha256(user_sub).hexdigest()`），把上游专有约束隔离在 provider 内部。

### 5. Provider 抽象边界不清，核心模块硬编码依赖 `providers/deepseek.py`

- **现状**：
  - `catalog.py`、`estimate.py`、`forward.py`、`streaming.py` 均直接 `from lgw.proxy.providers.deepseek import ...`。
  - `forward.py` 通过硬编码 `model.startswith("deepseek")` 决定是否调用 `rewrite_body`，而非根据路由选中的 `provider` 决定。
  - `parse_usage` 对所有供应商无条件按 DeepSeek 的 `prompt_cache_hit_tokens` 解析缓存命中（标准 OpenAI 接口为 `prompt_tokens_details.cached_tokens`）。
  - `forward.py` 中 `_call_upstream` 用返回元组首元素是否为字符串 `"live"` 来区分流式与非流式分支，并在函数内局部导入 `streaming_response`。
- **修复方向**：按 `provider` 组织请求改写与 usage 解析（或兼容标准 OpenAI `prompt_tokens_details.cached_tokens`），理顺 `forward.py` 与 `streaming.py` 的调用控制流。

### 6. 非流式请求在响应头前后的读超时（`ReadTimeout`）状态归因不一致

- **现状**：`lgw/proxy/forward.py` 使用 `client.send(upstream_request, stream=True)` 发送请求：
  - 若在等待响应头阶段发生 `httpx.ReadTimeout`，归为 `504`（`upstream_timeout`），数据库记录 `gateway_error` + `unknown`，Token 记 0。
  - 若上游先返回了 `200 OK` 头（例如 DeepSeek 排队时发送空行 keep-alive），随后在 `upstream.read()` 阶段发生 `httpx.ReadTimeout`，归为 `502`（`upstream_interrupted`），数据库记录 `upstream_interrupted` + `estimated`，并估算输入 Token。
- **问题**：同为上游处理/排队过程中的读超时，仅因上游是否先发出 HTTP 响应头，客户端收到的状态码（504 vs 502）、记录状态（`gateway_error` vs `upstream_interrupted`）以及是否估算输入 Token 完全相反；且 `_send_failure` 对 `ReadTimeout` 返回 `"upstream connection timed out"` 文案不准确。
- **修复方向**：统一并澄清连接超时（`ConnectTimeout` / `PoolTimeout`）与上游读取/响应超时（`ReadTimeout`）的错误码和记录状态语义。

### 7. SQLite 连接 `PRAGMA` 顺序与「先读后写」事务升级隐患

- **现状**：
  - `lgw/db.py` 的 `_set_sqlite_pragmas` 先执行 `PRAGMA journal_mode=WAL`，再执行 `PRAGMA busy_timeout=5000`。
  - `lgw/proxy/recorder.py` 的 `finish_request` 先在同一事务中 `session.get(Request, record_id)`（`SELECT`），修改属性后再 `session.commit()`（`UPDATE`）。
- **问题**：
  - `PRAGMA journal_mode=WAL` 需要获取写锁；若在设置 `busy_timeout` 前执行，并发建立连接遇到写锁时会以超时 0 立即抛出 `database is locked`。
  - SQLite WAL 模式下，事务先读后升级为写锁时，若期间有其他连接提交了写入，SQLite 为避免死锁不会等待 `busy_timeout`，而是立即返回 `SQLITE_BUSY`。
- **修复方向**：
  - `_set_sqlite_pragmas` 中先设置 `PRAGMA busy_timeout`，再设置 `PRAGMA journal_mode=WAL`。
  - `finish_request` 改为直接执行 `update(Request).where(Request.id == record_id).values(...)`，省去前置 `SELECT` 并直接开启写事务。

### 8. 代理路由缺少 `/v1` 前缀兼容

- **现状**：`lgw/proxy/routes.py` 只注册了 `/chat/completions` 和 `/models`。
- **问题**：OpenAI 生态工具和用户常将 `base_url` 配置为带 `/v1` 或自动拼接 `/v1`，请求 `/v1/chat/completions` 时会落入 Flask 默认的 HTML 404。
- **修复方向**：为 `/chat/completions` 和 `/models` 同时注册 `/v1/chat/completions` 和 `/v1/models` 路由别名，并为未知路径补充统一的 OpenAI 格式 404 JSON 错误处理。

---

## 二、阶段 2 计划（`stage2-plan.md`）设计修正项

在开始阶段 2 编码前，需同步修正计划中的以下两项设计：

### 1. 管理员组判定改用路径段匹配，废除子串匹配

- **现状**：`dev-plan.md` 与 `stage2-plan.md` 规定 `is_admin` 采用子串匹配（`oidc.admin_group in group`），目的是同时兼容 Keycloak 的 `lgw_admin` 与完整组路径 `/lgw_admin`。
- **问题**：子串匹配会导致 `lgw_admin_readonly`、`not_lgw_admin`、`/dept/lgw_admin_viewer` 等非管理员组被误判为管理员，存在越权风险。
- **修复方向**：改为精确或路径后缀匹配：`group == admin_group or group.endswith("/" + admin_group)`（配置时去掉 `admin_group` 前导斜杠）。

### 2. 避免将完整 `id_token` 存入 Flask 签名会话 Cookie

- **现状**：`stage2-plan.md` 规定将 `sub`、`is_admin` 和完整的 `id_token` 存入 Flask 签名会话 cookie，供 `/api/auth/logout` 拼接 `id_token_hint`。
- **问题**：Keycloak 签发的 `id_token`（含用户声明与组信息）经过 Flask 会话二次序列化与 Base64 编码后，极易逼近或超过浏览器单张 Cookie 4096 字节的上限，导致浏览器静默丢弃 `Set-Cookie`、登录静默失败。
- **修复方向**：会话 cookie 只保存紧凑字段（如 `sub`、`is_admin`）；登出时要么将 `id_token` 存于服务端（如 `users` 表或服务端会话），要么确认 Keycloak 支持仅凭 `client_id` + `post_logout_redirect_uri` 登出（需带用户确认页或按测试 realm 实测决定）。

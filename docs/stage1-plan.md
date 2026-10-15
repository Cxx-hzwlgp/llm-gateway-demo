# 阶段 1 开发计划：本地转发和记录（SQLite）

总体范围和设计决策见 [dev-plan.md](dev-plan.md)。本文件细化阶段 1 的实现方案，审查通过后再开工。
审查中已确认的决策见文末「已确认问题」。

## 目标

lgw Key 认证通过后，把 OpenAI 兼容请求转发到 DeepSeek，支持流式和非流式；每个请求在
`requests` 表中记录正确的状态、Token 用量和用量来源。

不在本阶段：OIDC 登录、Key 管理 API、前端、集群数据库、部署、Bedrock。

## 新增依赖

| 依赖                 | 用途                          |
| ------------------ | --------------------------- |
| `sqlalchemy~=2.0`  | ORM / engine                |
| `httpx`            | 上游请求，支持流式读取                 |
| `tiktoken`         | 通用分词器，估算所有模型的 Token       |
| `openai`（dev）      | 验收与集成测试中作为真实客户端             |
| `respx`（dev）       | 单元测试中 mock httpx           |

## 配置

在 `config.example.json` 中新增：

```json
{
  "proxy": {
    "max_request_seconds": 1800,
    "upstream_connect_timeout": 10,
    "upstream_read_timeout": 120
  },
  "providers": {
    "deepseek": {
      "base_url": "https://api.deepseek.com",
      "api_key": ""
    }
  }
}
```

- `upstream_read_timeout` 是两次读取之间的最大间隔，不是整个请求的时长。DeepSeek 在排队
  时会发送 keep-alive（流式为 `: keep-alive` 注释，非流式为空行），会重置该计时；排队超过
  10 分钟仍未开始推理时 DeepSeek 会主动断开，按上游中断处理。
- `max_request_seconds` 供超时清扫使用。
- 两个超时先用上述默认值，实际使用中出现问题再调整。

## 模块划分

```
lgw/
├── db.py                    # engine（SQLite 开启 WAL、busy_timeout）、session 工厂
├── models.py                # User / ApiKey / Request，状态与用量来源的枚举
├── keys.py                  # 生成 Key、SHA-256、key_hint
├── cli.py                   # flask 命令：init-db、create-key、sweep
└── proxy/
    ├── __init__.py          # create_app：注册路由、CLI、共享 httpx.Client
    ├── routes.py            # /v1/chat/completions、/v1/models
    ├── auth.py              # 解析 Authorization，按哈希查 Key
    ├── errors.py            # OpenAI 格式错误响应、上游错误映射、SSE 错误事件
    ├── sse.py               # 把上游字节流切分为完整 SSE 事件
    ├── recorder.py          # 请求记录：开始时插入，结束时更新状态和用量
    ├── estimate.py          # 中断时的 Token 估算
    └── providers/
        └── deepseek.py      # 请求改写、usage 解析
```

`httpx.Client` 在 app 内共享，复用连接池；gevent monkey patch 后其阻塞 IO 会让出协程。

## 数据库与 CLI

- 表结构按 dev-plan 数据模型。阶段 1 用 `Base.metadata.create_all` 建表，阶段 3 再引入 Alembic。
- 每次读写使用短生命周期 session（`with Session(engine) as session:`），不跨协程共享。
- SQLite 开启 WAL 和 `busy_timeout`，支持多个 gunicorn worker 并发写入。
- CLI（`flask --app lgw.proxy ...`）：
  - `init-db`：建表。
  - `create-key --user-sub <sub> --username <name> --name <key 名称>`：用户不存在时创建，
    打印完整 Key（只打印这一次）。`sub` 会作为 DeepSeek `user_id` 发送，创建用户时校验其匹配
    `[a-zA-Z0-9\-_]+` 且长度不超过 512。登录功能完成后此命令保留为运维工具。
  - `sweep`：把 `in_progress` 且 `started_at` 超过 `max_request_seconds` 的记录标为
    `gateway_error` + `unknown`。阶段 1 手动执行，阶段 3 用 CronJob。

## 请求处理流程

### 1. 认证

- 从 `Authorization: Bearer lgw-...` 取 Key，SHA-256 后按 `key_hash` 查询，要求状态为启用。
- 缺失、格式错误、不存在或已停用：返回 401，不写 `requests`（没有可关联的 Key）。
- `last_used_at`：距上次更新超过 1 分钟才写，减少 SQLite 写入。

### 2. 请求改写（`providers/deepseek.py`）

- `Authorization` 替换为出站 Key；只转发 `Content-Type` 和 `Accept` 等必要请求头，
  不透传客户端的其他头。
- `user_id` 一律覆盖为 lgw 用户的 `sub`（客户端自带的值被替换），用于 DeepSeek 区分共用
  出站 Key 的不同用户。
- `stream_options` 等其余参数原样转发，不注入 `include_usage`（DeepSeek 总会在最后一个
  chunk 返回 usage）。请求体解析失败返回 400，不访问上游。

### 3. 开始记录

插入 `requests` 记录（`in_progress`，`provider`、`model` 取自请求体），提交后再访问上游。
这样进程崩溃时记录仍在，可被清扫。

### 4. 访问上游并检查状态码

用 `client.send(request, stream=True)` 发送，拿到响应头后、返回给客户端之前先检查状态码：

- 连接失败或连接超时：返回 502 / 504，记录 `gateway_error`（见「错误映射」）。
- 上游返回非 200：按「错误映射」处理，不进入流式。
- 上游返回 200：进入非流式或流式分支。

### 5a. 非流式

读完整响应体，解析 `usage`，更新记录为 `completed` + `reported`，返回原始响应体和状态码。
响应体前可能有 keep-alive 空行，`json.loads` 会忽略首尾空白，无需特殊处理。

usage 字段映射（流式相同）：

| `requests` 列         | DeepSeek `usage` 字段                       |
| ------------------- | ----------------------------------------- |
| `input_tokens`      | `prompt_tokens`（含缓存命中）                    |
| `output_tokens`     | `completion_tokens`（含 `reasoning_tokens`） |
| `cache_hit_tokens`  | `prompt_cache_hit_tokens`                 |

- 上游读取中断（`RemoteProtocolError`、`ReadError`、`ReadTimeout`）：返回 502，记录
  `upstream_interrupted`，按请求估算输入 Token、输出记 0，来源为 `estimated`。
- 非流式响应只在最后写一次，无法感知客户端提前断开；记录以上游结果为准。

### 5b. 流式

返回 `text/event-stream` 响应（`Cache-Control: no-cache`），由生成器逐事件转发：

1. `sse.py` 用 `iter_lines()` 累积行，遇到空行得到一个完整事件；注释事件（`: keep-alive`）
   原样转发。
2. 每个 `data:` 事件：解析 JSON，累积 `delta.content` 与 `delta.reasoning_content`；
   `usage` 不为 `null` 时保存（客户端设置 `include_usage: true` 时其余 chunk 的 `usage` 为
   `null`）。usage 位于 `[DONE]` 前最后一个含 `finish_reason` 的 chunk 上。
3. 事件原样转发，不修改内容；每个事件收到后立即 `yield`，不做合并。
4. 看到 `data: [DONE]` 时标记正常结束。
5. 在 `finally` 中关闭上游响应（客户端断开时也能让上游停止生成），并写入最终状态和用量。

Flask 在视图函数返回后才执行生成器，所需数据（Key、请求体、记录 id）在返回前取出，
生成器内不访问 `flask.request`。

## 状态判定

| 情况                    | 判定依据                                                     | status                 |
| --------------------- | -------------------------------------------------------- | ---------------------- |
| 正常结束                  | 收到 `[DONE]`（流式）或完整响应体（非流式）                               | `completed`            |
| 上游中断                  | 迭代中抛出 `RemoteProtocolError` / `ReadError` / `ReadTimeout`，或流结束但没有 `[DONE]` | `upstream_interrupted` |
| 客户端断开                 | 生成器收到 `GeneratorExit`（gunicorn 写 socket 失败后调用 `close()`） | `client_interrupted`   |
| 上游拒绝                  | 上游返回非 200                                                | `upstream_error`       |
| 网关错误                  | 连接上游失败，或网关自身异常                                           | `gateway_error`        |
| 进程被杀                  | `finally` 未执行，由 `sweep` 处理                                | `gateway_error`        |

用量来源：

- 收到上游 usage：`reported`，即使之后客户端断开（usage 先于 `[DONE]` 到达）。
- 中断且没有 usage：`estimated`（见「估算」）。
- 上游拒绝：Token 记 0，来源 `reported`（上游不计费）。
- 连接上游失败、清扫：Token 记 0，来源 `unknown`。

客户端断开的已知局限（测试中需验证并记录实测结果）：

- 只在下一次写入时感知；上游长时间无输出时感知延后。
- 断开后的第一次写入通常仍会成功（进入内核缓冲区），一般多计 1–2 个事件的输出。
- 客户端无 FIN / RST 地消失（网络中断、节点掉电）时可能长时间感知不到，记录多半为
  `completed`，demo 中接受。

## 错误映射

所有错误响应使用 OpenAI 格式，便于 SDK 抛出对应异常：

```json
{"error": {"message": "...", "type": "...", "code": "..."}}
```

### 开始流式之前

| 情况                          | 返回给客户端                   | 说明                                    |
| --------------------------- | ------------------------ | ------------------------------------- |
| lgw Key 缺失 / 无效 / 已停用        | 401 `invalid_api_key`    | 只有这种情况返回 401                          |
| 请求体不是合法 JSON                 | 400                      | 不访问上游                                 |
| 上游 400 / 422                 | 原样转发状态码和响应体              | 客户端参数问题                               |
| 上游 401 / 403                 | 502 `upstream_auth_error` | 出站 Key 失效或无权限，不能让客户端误以为自己的 Key 失效；记 error 日志 |
| 上游 402                       | 502 `upstream_account_error` | DeepSeek 余额不足，属于网关账户问题；记 error 日志     |
| 上游 429                       | 原样转发（含 `Retry-After`）     | 并发超限，客户端 SDK 会自动重试。DeepSeek 按账户计算并发，所有 lgw 用户共享额度 |
| 上游 5xx                       | 原样转发状态码和响应体              | 上游故障，客户端可重试                           |
| 连接上游失败                       | 502 `upstream_unavailable` |                                       |
| 连接上游超时                       | 504 `upstream_timeout`   |                                       |
| 网关自身异常                       | 500 `gateway_error`      | 记录异常堆栈                                |

转为 502 的错误不透传上游响应体（可能包含出站账户信息），只在网关日志中保存。

### 开始流式之后

状态码已经是 200，改为在流内发送错误事件，然后结束且不发送 `[DONE]`：

```
data: {"error": {"message": "upstream connection interrupted", "type": "upstream_error", "code": "upstream_interrupted"}}

```

- OpenAI Python SDK 遇到带 `error` 字段的事件会抛出 `APIError`（阶段 1 用实际使用的 SDK 版本验证）。
- 缺少 `[DONE]` 作为自行解析流的客户端的第二个判断依据。
- 客户端断开时无需通知。

## 估算（`estimate.py`）

估算只在中断且没有收到 usage 时使用，场景少、对准确度要求不高，因此所有模型（DeepSeek、
以后的 Bedrock 等）共用一个通用分词器，接受与各模型实际计费 Token 存在偏差。

- 分词器：`tiktoken` 的 `o200k_base` 编码，与模型、厂商无关。在 app 创建时加载一次，
  避免首个中断请求承担加载耗时。
- 编码文件（约 4 MB）不提交到仓库。`tiktoken` 首次使用时自动下载并缓存到
  `TIKTOKEN_CACHE_DIR`；内网无法访问下载地址时，事先下载到该目录。镜像构建时预先下载，
  运行时不依赖外网。
- 编码使用 `encode_ordinary`，用户文本中出现 `<|endoftext|>` 等特殊 token 字面量时不会报错。
- 输入：对 messages 的 `role` 与 `content` 拼接后编码，不套用 chat template；`content` 为
  数组（多模态）时只计文本部分。
- 输出：已转发的 `content` + `reasoning_content` 拼接后编码。
- 缓存命中：记 0。
- 估算只在 `finally` 中执行一次。阶段 1 实测长上下文（如 100k Token）的耗时，评估是否阻塞
  同 worker 的其他请求（gevent 约束）。

## `GET /v1/models`

认证 lgw Key 后转发到上游 `/models`，原样返回。不写 `requests`。错误映射同上。

## 测试

### 单元测试（pytest + respx）

- `keys.py`：格式、哈希、`key_hint`。
- `sse.py`：多行事件、注释事件、跨读取边界切分、结尾没有空行。
- `providers/deepseek.py`：`user_id` 覆盖（含客户端自带值）、`stream_options` 原样保留、
  usage 字段映射、`include_usage: true` 时中间 chunk 的 `usage: null` 不覆盖已保存值。
- `errors.py`：每个上游状态码的映射结果。
- `estimate.py`：messages 拼接（含数组形式的 `content`）、特殊 token 字面量、空输出。
  使用真实的 `o200k_base` 编码，测试环境需能下载或已缓存编码文件。

### 集成测试（真实进程）

客户端断开只能在真实 socket 上验证，因此：

- 假上游：一个小 Flask app，用 gunicorn 子进程启动在随机端口，可按请求参数返回正常流、
  中途断开、不发 `[DONE]`、长时间无输出、各种错误状态码。
- lgw proxy：用 gunicorn gevent worker 子进程启动，指向假上游和临时 SQLite 文件。
- 客户端：httpx 和 OpenAI SDK。

覆盖用例：

| 用例                     | 期望记录                                    |
| ---------------------- | --------------------------------------- |
| 非流式正常                  | `completed` + `reported`                |
| 流式正常（`include_usage` 为 `true` / 未设置） | `completed` + `reported`；客户端收到的字节与上游一致 |
| 流式中客户端断开               | `client_interrupted` + `estimated`，上游连接被关闭 |
| 流式中上游断开                | `upstream_interrupted` + `estimated`，客户端收到错误事件 |
| 流结束但没有 `[DONE]`         | `upstream_interrupted`                  |
| 上游读取超时                 | `upstream_interrupted`                  |
| 上游 401 / 402            | 客户端收到 502，记录 `upstream_error`          |
| 上游 400 / 429            | 状态码和响应体原样转发                             |
| lgw Key 无效 / 停用          | 401，不写记录                                 |
| `sweep`                 | 超时的 `in_progress` 被标为 `gateway_error` + `unknown` |

## 验收

1. 用 curl 和 OpenAI Python SDK 通过 lgw 调通 DeepSeek，流式和非流式均可用。
2. 对真实 DeepSeek 手动验证：正常、客户端断开（Ctrl+C）、出站 Key 填错（客户端收到 502
   而不是 401）；抽查一次流式响应，确认 usage 位置与文档一致。
3. 上述集成测试全部通过；`ruff format --check`、`ruff check`、`pylint` 通过。
4. 记录实测结论：SDK 对错误事件的行为、客户端断开的感知延迟、估算耗时。结论回填到本文件。

## 提交拆分

按 Conventional Commits，每次提交单一功能：

1. `feat(db): add models, key utilities and init-db / create-key commands`
2. `feat(proxy): authenticate lgw keys`
3. `feat(proxy): forward non-streaming chat completions to DeepSeek`
4. `feat(proxy): stream chat completions with interruption detection`
5. `feat(proxy): estimate tokens for interrupted requests`
6. `feat(proxy): add models passthrough and sweep command`
7. `test(proxy): add integration tests with fake upstream`

## 已确认问题

1. 是否新增 `requests.status = upstream_error`（上游返回非 200，Token 记 0）。—— 新增
2. 上游 402（余额不足）是否和 401 / 403 一样转为 502。—— 是
3. 分词器文件是否提交到仓库。—— 不提交。需要估算的模型不止 DeepSeek，场景少且准确度要求
   不高，改用通用分词器（`tiktoken` `o200k_base`）估算所有模型，接受可能不准确。
4. `upstream_read_timeout` 默认 120 秒、`max_request_seconds` 默认 30 分钟是否合适。—— 先用
   默认值，出现问题再调整。

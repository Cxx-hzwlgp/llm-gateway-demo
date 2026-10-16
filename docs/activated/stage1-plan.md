# 阶段 1 开发计划：本地转发和记录（SQLite）

总体范围和设计决策见 [dev-plan.md](dev-plan.md)。本文件细化阶段 1 的实现方案。

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

- `providers` 的键名是供应商名。每家只有 `base_url` 和 `api_key`。可以配置多家。
  名称不符合格式、缺少 `base_url` 或 `api_key`、一家都没有时启动失败。
- 启动时请求每家的 `GET {base_url}/models`。模型表的键是模型名，值是供应商名称列表，
  按配置顺序排列。同一模型被多家列出时都保留，转发使用第一家。列表失败或格式不对时进程不启动。
  聊天请求里的 `model` 不在表中时返回 404，不写 `requests`、不访问上游。
  模型名以 `deepseek` 开头时才把 `user_id` 覆盖为用户 `sub`。

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
    ├── catalog.py           # 启动时拉取并索引模型表
    ├── forward.py           # 校验、落库、调用上游、非流式收尾
    ├── streaming.py         # 流式转发、断开检测、流内错误事件
    ├── errors.py            # OpenAI 格式错误响应、上游状态码映射
    ├── sse.py               # 把上游字节流切分为完整 SSE 事件
    ├── recorder.py          # 请求记录：开始时插入，结束时更新状态和用量
    ├── estimate.py          # 没有上游 usage 时的 Token 估算
    └── providers/
        └── deepseek.py      # 请求改写、usage 解析
```

`httpx.Client` 在 app 内共享，复用连接池；gevent monkey patch 后其阻塞 IO 会让出协程。

## 数据库与 CLI

- 表结构按 dev-plan 数据模型。阶段 1 用 `Base.metadata.create_all` 建表，阶段 4 再引入 Alembic。
- 每次读写使用短生命周期 session（`with Session(engine) as session:`），不跨协程共享。
- SQLite 开启 WAL 和 `busy_timeout`，支持多个 gunicorn worker 并发写入。
- CLI（`flask --app lgw.proxy ...`）：
  - `init-db`：建表。
  - `create-key --user-sub <sub> --username <name> --name <key 名称>`：用户不存在时创建，
    打印完整 Key（只打印这一次）。模型名以 `deepseek` 开头时，`sub` 才作为上游 `user_id`
    发送。创建用户时校验其匹配 `[a-zA-Z0-9\-_]+` 且长度不超过 512。登录功能完成后此命令保留为运维工具。
  - `sweep`：把 `in_progress` 且 `started_at` 超过 `max_request_seconds` 的记录标为
    `gateway_error` + `unknown`。阶段 1 手动执行，阶段 4 用 CronJob。

## 请求处理流程

### 1. 认证

- 从 `Authorization: Bearer lgw-...` 取 Key，SHA-256 后按 `key_hash` 查询，要求状态为启用。
- 缺失、格式错误、不存在或已停用：返回 401，不写 `requests`（没有可关联的 Key）。
- 每次请求都查库，不缓存认证结果。
- `last_used_at`：距上次更新超过 1 分钟才写，减少 SQLite 写入。

### 2. 请求改写（`providers/deepseek.py`）

- `Authorization` 替换为出站 Key；只转发 `Content-Type` 和 `Accept` 等必要请求头，
  不透传客户端的其他头。
- 模型名以 `deepseek` 开头时，`user_id` 覆盖为 lgw 用户的 `sub`（客户端自带的值被替换），
  用于 DeepSeek 区分共用出站 Key 的不同用户。其他模型不添加、不改写 `user_id`。
- `stream_options` 等其余参数原样转发，不注入 `include_usage`（DeepSeek 总会在最后一个
  chunk 返回 usage）。请求体解析失败返回 400，不访问上游。
- 上游地址只取自所选供应商配置的 `base_url`，请求中任何地址类字段都不影响目标。
- `model` 缺失、不是字符串或超过 255 字符：返回 400，不访问上游。
- 不做自动重试；上游失败直接按「错误映射」返回。

### 3. 开始记录

插入 `requests` 记录（`in_progress`，`provider` 为所选供应商名，`model` 为请求体中的值），
提交后再访问上游。
这样进程崩溃时记录仍在，可被清扫。

### 4. 访问上游并检查状态码

用 `client.send(request, stream=True)` 发送，拿到响应头后、返回给客户端之前先检查状态码：

- 连接失败或连接超时：返回 502 / 504，记录 `gateway_error`（见「错误映射」）。
- 上游返回非 200：按「错误映射」处理，不进入流式。
- 上游返回 200：进入非流式或流式分支。

### 5a. 非流式

读完整响应体，解析 `usage`，更新记录为 `completed` + `reported`，返回原始响应体和状态码。
响应体前可能有 keep-alive 空行，`json.loads` 会忽略首尾空白，无需特殊处理。
响应体不是合法 JSON 或缺少 `usage` 时仍原样返回，记录为 `completed` + `estimated`，记 warning。
上游完成前不向客户端输出任何内容（包括 keep-alive 空行），长时间排队时客户端或中间代理可能
超时；阶段 1 实测后再决定是否改为边收边转发。

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

1. `sse.py` 把 `iter_bytes()` 读到的数据块（已按 `Content-Encoding` 解压；不向客户端转发
   该头）累积到缓冲区，在原始字节上按空行切出完整事件（行结束符可以是 `\n`、`\r\n` 或 `\r`，
   按 SSE 规范允许混用；连续两个行结束符即事件结束），每个事件同时给出原始字节（含换行符）
   和解析出的字段。不用 `iter_lines()`，它会去掉换行符，转发时无法还原上游字节。注释事件
   （`: keep-alive`）同样转发。上游结束时缓冲区中剩余的不完整事件原样转发，并照常解析
   （结尾缺空行的 `data: [DONE]` 仍算正常结束）。
2. 每个 `data:` 事件：解析 JSON，累积 `delta.content`、`delta.reasoning_content` 以及
   `delta.tool_calls` 中的函数名和参数片段（供估算使用）；`usage` 不为 `null` 时保存（客户端
   设置 `include_usage: true` 时其余 chunk 的 `usage` 为 `null`）。usage 位于 `[DONE]` 前
   最后一个含 `finish_reason` 的 chunk 上。JSON 解析失败或字段结构不符合预期时，事件照常
   转发，只记 warning。
3. 转发每个事件的原始字节，不修改内容；每个事件切出后立即 `yield`，不做合并。按完整事件
   转发，上游中途断开时客户端不会收到半截事件，随后追加的错误事件能被正常解析。
4. 看到 `data: [DONE]` 时标记正常结束。
5. 在 `finally` 中关闭上游响应（客户端断开时也能让上游停止生成），并写入最终状态和用量。

Flask 在视图函数返回后才执行生成器，所需数据（Key、请求体、记录 id）在返回前取出，
生成器内不访问 `flask.request`。

### 6. 写入最终记录

流式与非流式共用。写入失败（如 SQLite 锁超时）时记 error 日志，内容包括记录 id、状态和
用量，便于人工补录；不向客户端抛出异常。记录会保持 `in_progress`，最终被 `sweep` 标为
`gateway_error` + `unknown`，这是已知的误标，靠日志补救。

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
- 正常结束但没有收到 usage 或 usage 无法解析：`estimated`，记 warning（说明上游格式变化或
  解析代码有误，需要排查）。
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
| 插入记录失败，或非流式处理中的未预期异常     | 500 `gateway_error`      | 记录异常堆栈                                |
| 认证时数据库查询失败                  | Flask 默认的 HTML 500     | 未捕获，不写 `requests`。更新 `last_used_at` 失败只记日志，认证仍成功 |

转为 502 的错误不透传上游响应体（可能包含出站账户信息），只在网关日志中保存。

### 开始流式之后

状态码已经是 200，改为在流内发送错误事件，然后结束且不发送 `[DONE]`：

```
data: {"error": {"message": "upstream connection interrupted", "type": "upstream_error", "code": "upstream_interrupted"}}

```

- 流已经开始后，网关自己的未预期异常也发送这条错误事件。记录为 `gateway_error`：已经收到 usage 时来源仍是 `reported`，否则 Token 记 0、来源 `unknown`。
- OpenAI Python SDK 遇到带 `error` 字段的事件会抛出 `APIError`（阶段 1 用实际使用的 SDK 版本验证）。
- 缺少 `[DONE]` 作为自行解析流的客户端的第二个判断依据。
- 客户端断开时无需通知。

## 估算（`estimate.py`）

没有收到可用 usage 时使用，包括请求中断，以及正常结束但 usage 缺失或无法解析。正常结束却要估算时记 warning。场景少、对准确度要求不高，因此所有模型（DeepSeek、
以后的 Bedrock 等）共用一个通用分词器，接受与各模型实际计费 Token 存在偏差。

- 分词器：`tiktoken` 的 `o200k_base` 编码，与模型、厂商无关。在 app 创建时加载一次，
  避免首个中断请求承担加载耗时。
- 编码文件（约 4 MB）不提交到仓库。`tiktoken` 首次使用时自动下载并缓存到
  `TIKTOKEN_CACHE_DIR`；内网无法访问下载地址时，事先下载到该目录。镜像构建时预先下载，
  运行时不依赖外网。
- 编码使用 `encode_ordinary`，用户文本中出现 `<|endoftext|>` 等特殊 token 字面量时不会报错。
- 输入：拼接后编码，不套用 chat template。包括 messages 的 `role`、`content`（数组形式时只计
  文本部分）、`reasoning_content`、assistant 消息中 `tool_calls` 的函数名和参数，以及请求中
  `tools` 定义的 JSON 文本。Agent 客户端（Cline 等）的工具定义和工具调用占输入的很大比例，
  只计 `content` 会严重低估。
- 输出：已转发的 `content` + `reasoning_content` + 工具调用函数名和参数拼接后编码。
- 缓存命中：记 0。
- 每个请求最多估算一次，放在确定最终状态时。流式在生成器的 `finally` 里执行。阶段 1 实测长上下文（如 100k Token）的耗时，评估是否阻塞
  同 worker 的其他请求（gevent 约束）。

## `GET /v1/models`

认证 lgw Key 后返回启动时的模型表，每个模型名只出现一次，不带供应商名和出站 Key。
不写 `requests`，不在这次请求里再访问上游。

## 测试

### 单元测试（pytest + respx）

- `keys.py`：格式、哈希、`key_hint`。
- `sse.py`：多行事件、注释事件、跨读取边界切分（包括换行符本身被拆开，如 `\r` 与 `\n`
  分属两个数据块）、一个数据块含多个事件、`\n` / `\r\n` 换行的原始字节与输入一致、结尾没有
  空行。
- `providers/deepseek.py`：`user_id` 覆盖（含客户端自带值）、`stream_options` 原样保留、
  usage 字段映射、`include_usage: true` 时中间 chunk 的 `usage: null` 不覆盖已保存值、
  `tool_calls` 参数片段累积、非法 JSON 事件和缺失 usage 不抛异常。
- `errors.py`：每个上游状态码的映射结果。
- `estimate.py`：messages 拼接（含数组形式的 `content`、`tool_calls`、`tools` 定义）、特殊
  token 字面量、空输出。
- `recorder.py`：最终写入失败时只记日志、不抛异常。
- 配置校验：至少一家供应商、名称格式、`base_url` 与 `api_key`；模型表拉取失败时启动失败。
  同一模型可对应多家供应商，转发使用第一家。`model` 缺失 / 非字符串 / 过长时返回 400，
  不在表中时返回 404。
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
| 流式工具调用                  | 客户端收到的字节与上游一致；客户端断开时估算包含工具调用参数 |
| 流式正常但没有 usage            | `completed` + `estimated`               |
| 流中出现非法 JSON 的 `data:` 事件 | 事件原样转发，流正常结束                          |
| 上游 401 / 402            | 客户端收到 502，记录 `upstream_error`          |
| 上游 400 / 429 / 5xx      | 状态码和响应体原样转发，记录 `upstream_error`      |
| lgw Key 无效 / 停用          | 401，不写记录                                 |
| `sweep`                 | 超时的 `in_progress` 被标为 `gateway_error` + `unknown` |

## 验收

1. 用 curl 和 OpenAI Python SDK 通过 lgw 调通 DeepSeek，流式和非流式均可用。
2. 对真实 DeepSeek 手动验证：正常、客户端断开（Ctrl+C）、出站 Key 填错（客户端收到 502
   而不是 401）；抽查一次流式响应，确认 usage 位置与文档一致。
3. 上述集成测试全部通过；`ruff format --check`、`ruff check`、`pylint` 通过。
4. 记录实测结论：SDK 对错误事件的行为、客户端断开的感知延迟、估算耗时、非流式长时间排队
   时客户端是否超时。结论回填到本文件。
5. 编写临时手册 `docs/activated/stage1-manual.md`，指导从零开始：安装依赖、填写 `config.json`、
   `init-db`、`create-key`、用 gunicorn 启动 proxy、用 curl / OpenAI SDK 验证、配置 Pi 和
   Cline（OpenAI 兼容 provider 的 base URL、Key、模型名）、用 `sqlite3` 查看 `requests`
   记录、手动制造客户端中断并核对记录。按手册用 Pi 和 Cline 实测（含工具调用和中途取消），
   发现的问题回填到本文件。

## 提交拆分

按 Conventional Commits，每次提交单一功能：

1. `feat(db): add models, key utilities and init-db / create-key commands`
2. `feat(proxy): authenticate lgw keys`
3. `feat(proxy): forward non-streaming chat completions to DeepSeek`
4. `feat(proxy): stream chat completions with interruption detection`
5. `feat(proxy): estimate tokens for interrupted requests`
6. `feat(proxy): add models passthrough and sweep command`
7. `test(proxy): add integration tests with fake upstream`
8. `docs: add stage 1 setup and testing manual`

## 实测记录

2026-10-15，在本机 WSL 上根据自动化测试和一次估算计时记录。

- OpenAI Python SDK 1.109.1：流式事件的 JSON 含 `error` 对象时抛出 `APIError`。
  集成测试用缺少 `[DONE]` 的流让网关追加错误事件，SDK 因此抛出该异常。
- 客户端断开：假上游每 50 ms 继续输出时，客户端读完首个事件后关闭连接，记录变为
  `client_interrupted`，假上游的生成器收到 `GeneratorExit`。测试在 5 秒内观察到
  这个结果，没有再拆更细的延迟。上游一直不输出时，要等到下一次读取或
  `upstream_read_timeout`；读超时会先记成 `upstream_interrupted`。
- 估算：同一次调用里输入、输出各约 5 万 token（合计约 10 万）耗时 6.3 ms。
  demo 流量下，单次估算不会把同一个 gevent worker 堵住。
- 非流式长时间排队时客户端会不会超时：没有对真实 DeepSeek 测量。

2026-10-16，在 Windows 上按 [stage1-manual.md](stage1-manual.md) 用 Pi 和 Cline 调用。
基础功能正常，响应原样透传没有问题。工具调用和中途取消随后复核，表现符合预期。

同日用 curl 和 OpenAI Python SDK 通过 lgw 调用真实 DeepSeek（验收第 1、2 项）。
流式和非流式可用。正常请求、客户端断开、出站 Key 填错时客户端收到 502 而不是 401，
流式 usage 的位置与文档一致。

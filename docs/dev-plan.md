# LLM Gateway Demo 开发计划

需求来源：`/mnt/d/codes/notes/llm-gateway.md` 的「demo 开发方案」。本文件是 demo 范围、
设计决策和阶段验收标准的权威来源，需求变化时先更新这里。

## 范围

- 用户通过 Keycloak OIDC 登录，可创建、停用多个 lgw Key；Key 只在创建时完整展示。
- 管理员（Keycloak groups 包含配置的 `admin_group`）可查看所有用户的 Key 和用量，并停用 Key。
- 转发模块认证 lgw Key 后转发请求，只统计 Token（总输入、总输出、缓存命中），不限额、不计费、
不保存请求内容。
- 上游：DeepSeek `/chat/completions`（第一阶段），AWS Bedrock（第四阶段）。
- 支持查询用量：时间范围、指定一组 Key，两者可复合。

## 已确认的技术决策


| 项目     | 决策                                                                        |
| ------ | ------------------------------------------------------------------------- |
| 仓库     | 单仓库：`backend/`（admin、proxy 两个 Flask app 共享 `lgw` 包）、`frontend/`、`deploy/` |
| 转发     | Flask + gunicorn gevent worker + httpx 流式转发；本地开发（WSL）同样用 gunicorn       |
| 登录     | Authlib + Flask 签名会话 cookie，不依赖 Redis                                     |
| 管理员    | Keycloak groups 包含配置项 `oidc.admin_group`                                  |
| 工具     | uv + `pyproject.toml`，ruff（格式化、PEP8、import 排序、numpy docstring），pylint     |
| 规范     | PEP8，行宽 79，docstring 采用 numpy 格式，提交信息遵循 Conventional Commits              |
| 前端     | Vue 3 + Vite + TypeScript + Element Plus                                  |
| 数据库    | SQLAlchemy 2.0；阶段 1、2 用 SQLite（WAL），阶段 3 切换集群数据库                          |
| Python | 3.12（与 Docker 镜像一致）                                                       |


## 目录结构（目标）

```
backend/
├── pyproject.toml / uv.lock
├── config.example.json      # 配置模板，实际配置为 config.json（不入库）
├── lgw/
│   ├── config.py            # 配置加载（CONFIG_FILE_PATH）
│   ├── db.py                # engine / session
│   ├── models.py            # users / api_keys / requests
│   ├── keys.py              # Key 生成、哈希、掩码展示
│   ├── admin/               # 登录、Key 管理、用量查询 API
│   └── proxy/
│       ├── routes.py        # /v1/chat/completions 等入口
│       ├── providers/       # deepseek.py、bedrock.py：请求改写与 usage 解析
│       └── estimate.py      # 中断时的 Token 估算
└── tests/
frontend/                    # 阶段 2 初始化
deploy/                      # 阶段 3：Dockerfile、k8s 清单
```

不自建 Nginx，也不引入新的网络组件。admin、proxy 各用一个 ClusterIP Service，通过公司已有的
Ingress Controller 暴露，TLS 由 cert-manager 签发：

- admin app 同时托管前端构建产物，`/` 与 `/api/` 同源，避免 CORS 和跨域 cookie 问题。
  开发时由 Vite dev server 把 `/api` 代理到 admin。
- proxy app 通过 Ingress 对外提供 `/v1/`。调用方是 SDK / curl 等非浏览器客户端，
  不涉及 CORS 和 cookie。
- Ingress Controller 为 k8s 社区的 Ingress-Nginx（`ingressClassName: nginx`），仍是 SSE 链路上
  的一层反向代理，proxy 的 Ingress 需要单独配置注解（见阶段 3）。

## 数据模型（相对笔记的调整需确认）


| 表          | 列                                                                                                                                                 |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `users`    | `sub` (PK), `username`, `email`                                                                                                                   |
| `api_keys` | `id` (PK), `user_sub`, `name`, `key_hash` (unique), `key_hint`, `created_at`, `last_used_at`, `status`                                            |
| `requests` | `id` (PK), `key_id`, `provider`, `model`, `started_at`, `ended_at`, `status`, `usage_source`, `input_tokens`, `output_tokens`, `cache_hit_tokens` |


相对笔记新增的列：

- `api_keys.name`、`key_hint`：界面要求展示 Key 名称和掩码文本（如 `lgw-ab12…yz89`）。
- `requests.ended_at`、`provider`、`model`：区分上游和模型，便于排查与按模型统计。
- `requests.usage_source`：`reported` / `estimated` / `unknown`，区分官方 usage 和估算值。

`requests.status`：`in_progress`、`completed`、`upstream_interrupted`、`client_interrupted`、
`upstream_error`（上游返回非 200，Token 记 0）、`gateway_error`。

Key 格式为 `lgw-` + `secrets.token_urlsafe(32)`，库中只存 SHA-256。Key 为高熵随机串，
不需要 bcrypt 这类慢哈希，认证时可直接按哈希索引查询。

## 关键设计点

1. **流式与断开检测**：proxy 生成器逐块转发 SSE，同时累积 delta 文本；在 `finally` 中按
  退出原因写入状态和用量。gevent 下客户端断开在下一次写入时才会被感知，阶段 1 首先验证。
2. **usage 获取**：据 DeepSeek 文档，无论是否设置 `stream_options.include_usage`，`[DONE]`
  前的最后一个 chunk（含 `finish_reason`）都带有整个请求的 `usage`，不存在单独的 usage chunk；
  设置为 `true` 时其余 chunk 的 `usage` 为 `null`。因此网关不注入参数、不修改响应，事件原样转发。
3. **user_id**：所有用户共用同一个出站 DeepSeek Key，请求体中的 `user_id` 一律覆盖为 lgw
  用户的 `sub`，供 DeepSeek 做内容安全、KVCache 隔离和调度隔离。DeepSeek 要求匹配
  `[a-zA-Z0-9\-_]+`、最长 512、不含隐私信息；Keycloak `sub` 为 UUID，满足要求，不能用
  username / email。其余参数原样转发。
4. **估算**：只在中断且没有收到 usage 时使用，场景少、准确度要求不高。所有模型共用通用分词器
  （`tiktoken` 的 `o200k_base`），接受与实际计费的偏差，编码文件不入库。输入 Token 按请求
  messages 计算；输出 Token 按已转发的 `content` + `reasoning_content` 计算；缓存命中记 0。
5. **崩溃恢复**：多 worker / 多副本下，单个进程启动时不能把全部 `in_progress` 标为 unknown，
  否则会误伤其他进程的在途请求。改为定期清扫：`in_progress` 且 `started_at` 超过最大请求时长
   （可配置，默认 30 分钟）的记录标为 `gateway_error` + `unknown`。
6. **Key 失效**：只在请求开始时校验，在途请求不受影响（符合笔记约定）。
7. **gevent 约束**：每个 worker 是单线程多协程，只在 IO 等待时切换。CPU 密集操作（如
  Token 估算）会阻塞同 worker 的所有请求，阶段 1 需实测耗时；数据库驱动必须兼容 monkey
   patch，阶段 3 选用 PyMySQL 或 psycopg 3，避免 `mysqlclient` 等 C 驱动。
8. **错误转发**：proxy 错误响应采用 OpenAI 格式 `{"error": {...}}`，保证 SDK 能正确抛出异常
  （admin API 仍用 `{"message": ...}`）。上游 401 / 403 / 402 是网关出站 Key 或账户的问题，
  不能原样转发，否则客户端会误以为自己的 lgw Key 失效；这类错误转为 502 并记录日志。
  429 原样转发，但要注意 DeepSeek 并发限制按账户计算，所有 lgw 用户共享同一额度。
  详细映射见阶段 1 计划。

## 阶段计划

### 阶段 0：脚手架（当前）

- [x] 目录结构、`pyproject.toml`、ruff / pylint 配置、配置模板、两个 app 的健康检查
- [x] 首次提交

验收：`uv sync`、`ruff format --check`、`ruff check`、`pylint` 通过，两个 app 可通过
gunicorn gevent worker 启动。

### 阶段 1：本地转发和记录（SQLite）

详细计划见 [stage1-plan.md](stage1-plan.md)。

1. `db.py`、`models.py`、`keys.py`，提供 CLI：`flask --app ... init-db`、`create-key`（临时，
  在没有登录前用于发 Key）。
2. proxy：`POST /v1/chat/completions`（流式与非流式）、`GET /v1/models` 透传。
3. DeepSeek provider：请求改写、usage 解析。
4. 中断估算与超时清扫命令。
5. 测试：用本地假上游（返回 SSE、可中途断开）覆盖四种状态；pytest + respx 覆盖解析逻辑。

验收：用 curl 和 OpenAI Python SDK 通过 lgw 调通 DeepSeek；正常、客户端断开、上游断开
三种情况在 `requests` 表中记录正确的状态和用量来源。

### 阶段 2：登录和 Key 管理（SQLite）

1. admin：Authlib OIDC 登录 / 回调 / 登出，首次登录写入 `users`。
2. API（RESTful，错误返回 `{"message": ...}`，与参考项目一致）：
  - `GET/POST /api/v1/keys`、`PATCH /api/v1/keys/<id>`（停用）
  - `GET /api/v1/usage?start=&end=&key_ids=`（按 Key、按天聚合）
  - 管理员：`GET /api/v1/admin/keys`、`GET /api/v1/admin/usage`、停用任意 Key
3. 前端：登录态、Key 列表 / 创建（一次性展示）/ 停用、用量查询、管理员视图。

验收：用测试 Keycloak 登录，完成创建 Key、调用、查询用量全流程；普通用户无法访问
其他用户数据。

### 阶段 3：真实数据库和部署

1. 切换到集群 PostgreSQL 或 MySQL，引入 Alembic 管理表结构。
2. `deploy/`：Dockerfile（多阶段构建：先构建前端，产物打入后端镜像；admin / proxy 同一镜像
  不同启动命令）、gunicorn 配置（`graceful_timeout` 接近最大请求时长，避免滚动更新截断流）、
  k8s Deployment、ClusterIP Service、Ingress、Secret（config.json）、清扫 CronJob。
3. Ingress：`spec.ingressClassName: nginx`，cert-manager 通过 `cert-manager.io/cluster-issuer`
  注解签发 TLS（issuer 名称部署时向集群管理员确认）。admin（`/`、`/api/`）和 proxy（`/v1/`）
  使用两个 Ingress 对象，可共用同一域名，Ingress-Nginx 会合并同一 host 的规则；这样长超时等
  注解只作用于 proxy。proxy 的 Ingress 注解（`nginx.ingress.kubernetes.io/` 前缀）：

  | 注解                       | 值       | 原因                                              |
  | ------------------------ | ------- | ----------------------------------------------- |
  | `proxy-buffering`        | `"off"` | SSE 逐事件送达。Controller 默认已关闭，显式设置防止全局配置被改      |
  | `proxy-read-timeout`     | `"1800"` | 默认 60 秒。非流式请求在上游完成前网关不输出任何内容，必须不小于最大请求时长 |
  | `proxy-send-timeout`     | `"1800"` | 与读超时保持一致                                       |
  | `proxy-body-size`        | `"20m"` | 默认 1 MB，长上下文或 base64 图片会被拒绝（413）                 |

  - 不使用 `configuration-snippet` 等 snippet 注解（新版 Controller 默认禁用）。
  - 客户端断开传递：nginx 默认 `proxy_ignore_client_abort off`，客户端断开时会关闭到后端的
    连接，部署后需实测确认 `client_interrupted` 能被正确记录。
4. 从 HPC 环境用真实客户端验证。

验收：k8s 上部署成功，HPC 通过 lgw 调用 DeepSeek，用量正确入库。

### 阶段 4：AWS Bedrock

先调研再定方案：客户端以何种协议访问（OpenAI 兼容格式由网关翻译为 Converse，或透传
Bedrock 原生接口），以及流式响应中 usage 的获取方式。中断时的估算沿用通用分词器。

## 待确认问题

1. 上述数据模型新增列是否接受。—— 接受
2. 阶段 2 需要一个可用的测试 Keycloak realm / client（本机没有 Docker，无法本地起 Keycloak）。—— 在 wsl 开发，环境已足够
3. 阶段 3 使用 PostgreSQL 还是 MySQL。—— PostgreSQL 
4. Bedrock 客户端侧使用的协议（影响阶段 4 方案）。—— 待定
5. 去掉 Nginx 后，k8s 中 admin / proxy 通过什么方式对外暴露，是否需要 TLS。—— 使用公司已有
   Ingress Controller，两个应用各用 ClusterIP Service，通过 Ingress 暴露，TLS 由 cert-manager
   处理；demo 阶段不引入新的网络组件。
6. 公司 Ingress Controller 的类型（ingress-nginx、Traefik 等），决定关闭缓冲、读超时等
   注解的写法（阶段 3 前确认）。—— k8s 社区的 Ingress-Nginx Controller（非 nginx 社区的
   NGINX Ingress Controller），`spec.ingressClassName: nginx`，按其文档添加注解。


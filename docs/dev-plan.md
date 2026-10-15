# 。LLM Gateway Demo 开发计划

需求来源：`D:\codes\notes\llm-gateway.md` 的「demo 开发方案」。本文件是 demo 范围、
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
| 转发     | Flask + gunicorn gevent worker + httpx 流式转发；本地用 `serve.py`（gevent.pywsgi） |
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
├── serve.py                 # 本地开发启动入口
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
deploy/                      # 阶段 3：Dockerfile、nginx.conf、k8s 清单
```

对外路由由 Nginx 统一到同一域名：`/` 前端静态文件，`/api/` 管理模块，`/v1/` 转发模块。
同源部署避免 CORS 和跨域 cookie 问题。

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
`gateway_error`。

Key 格式为 `lgw-` + `secrets.token_urlsafe(32)`，库中只存 SHA-256。Key 为高熵随机串，
不需要 bcrypt 这类慢哈希，认证时可直接按哈希索引查询。

## 关键设计点

1. **流式与断开检测**：proxy 生成器逐块转发 SSE，同时累积 delta 文本；在 `finally` 中按
  退出原因写入状态和用量。gevent 下客户端断开在下一次写入时才会被感知，阶段 1 首先验证。
2. **usage 获取**：流式请求若客户端未设置 `stream_options.include_usage`，网关注入该参数，
  并在转发时去掉仅含 usage 的最后一个 chunk，保证客户端看到的行为不变。
3. **user 字段**：请求体中的 `user` 覆盖为 lgw 用户标识，其余参数原样转发。
4. **估算**：输入 Token 用 DeepSeek 官方 tokenizer（`tokenizers` 库加载 `tokenizer.json`）
  计算请求 messages；输出 Token 用已转发的 `content` + `reasoning_content` 计算；缓存命中记 0。
5. **崩溃恢复**：多 worker / 多副本下，单个进程启动时不能把全部 `in_progress` 标为 unknown，
  否则会误伤其他进程的在途请求。改为定期清扫：`in_progress` 且 `started_at` 超过最大请求时长
   （可配置，默认 30 分钟）的记录标为 `gateway_error` + `unknown`。
6. **Key 失效**：只在请求开始时校验，在途请求不受影响（符合笔记约定）。
7. **gevent 约束**：每个 worker 是单线程多协程，只在 IO 等待时切换。CPU 密集操作（如
  Token 估算）会阻塞同 worker 的所有请求，阶段 1 需实测耗时；数据库驱动必须兼容 monkey
   patch，阶段 3 选用 PyMySQL 或 psycopg 3，避免 `mysqlclient` 等 C 驱动。

## 阶段计划

### 阶段 0：脚手架（当前）

- [x] 目录结构、`pyproject.toml`、ruff / pylint 配置、配置模板、两个 app 的健康检查
- [ ] 首次提交

验收：`uv sync`、`ruff format --check`、`ruff check`、`pylint` 通过，两个 app 可通过
`serve.py` 启动。

### 阶段 1：本地转发和记录（SQLite）

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
2. `deploy/`：后端 Dockerfile（admin / proxy 同一镜像不同启动命令）、gunicorn 配置、前端构建
  - Nginx 镜像、k8s Deployment / Service / Ingress / Secret（config.json）、清扫 CronJob。
3. 从 HPC 环境用真实客户端验证。

验收：k8s 上部署成功，HPC 通过 lgw 调用 DeepSeek，用量正确入库。

### 阶段 4：AWS Bedrock

先调研再定方案：客户端以何种协议访问（OpenAI 兼容格式由网关翻译为 Converse，或透传
Bedrock 原生接口），以及中断时的估算方式（Bedrock 的 Claude 没有公开 tokenizer）。

## 待确认问题

1. 上述数据模型新增列是否接受。—— 接受
2. 阶段 2 需要一个可用的测试 Keycloak realm / client（本机没有 Docker，无法本地起 Keycloak）。
3. 阶段 3 使用 PostgreSQL 还是 MySQL。—— PostgreSQL 
4. Bedrock 客户端侧使用的协议（影响阶段 4 方案）。—— 待定


# LLM Gateway Demo 开发计划

需求来源：`/mnt/d/codes/notes/llm-gateway.md` 的「demo 开发方案」。本文件是 demo 范围、
设计决策和阶段验收标准的权威来源，需求变化时先更新这里。

## 范围

- 用户通过 Keycloak OIDC 登录，可创建、停用多个 lgw Key；Key 只在创建时完整展示。
- 管理员（Keycloak groups 包含配置的 `admin_group`）可查看所有用户的 Key 和用量，并停用 Key。
- 转发模块认证 lgw Key 后转发请求，只统计 Token（总输入、总输出、缓存命中），不限额、不计费、
不保存请求内容。
- 上游：DeepSeek `/chat/completions`（第一阶段），AWS Bedrock（第三阶段）。
- 支持查询用量：按时间范围、一组 Key、一组供应商、一组模型筛选，条件可任意组合；结果可按天、
  Key、供应商、模型分组。

## 技术决策


| 项目     | 决策                                                                        |
| ------ | ------------------------------------------------------------------------- |
| 仓库     | 单仓库：`backend/`（admin、proxy 两个 Flask app 共享 `lgw` 包）、`frontend/`、`deploy/` |
| 转发     | Flask + gunicorn gevent worker + httpx 流式转发；本地开发（WSL）同样用 gunicorn       |
| 登录     | Authlib + Flask 签名会话 cookie，不依赖 Redis                                     |
| 管理员    | Keycloak groups 包含配置项 `oidc.admin_group`                                  |
| 工具     | uv + `pyproject.toml`，ruff（格式化、PEP8、import 排序、numpy docstring），pylint     |
| 规范     | PEP8，行宽 79，docstring 采用 numpy 格式，提交信息遵循 Conventional Commits              |
| 前端     | Vue 3 + Vite + TypeScript + Element Plus                                  |
| 数据库    | SQLAlchemy 2.0；阶段 1–3 用 SQLite（WAL），阶段 4 切换集群 PostgreSQL（psycopg 3）+ Alembic |
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
deploy/                      # 阶段 4：Dockerfile、k8s 清单
```

不自建 Nginx，也不引入新的网络组件。admin、proxy 各用一个 ClusterIP Service，通过公司已有的
Ingress Controller 暴露，TLS 由 cert-manager 签发：

- admin app 同时托管前端构建产物，`/` 与 `/api/` 同源，避免 CORS 和跨域 cookie 问题。
  开发时由 Vite dev server 把 `/api` 代理到 admin。
- proxy app 通过 Ingress 对外提供 `/v1/`。调用方是 SDK / curl 等非浏览器客户端，
  不涉及 CORS 和 cookie。
- Ingress Controller 为 k8s 社区的 Ingress-Nginx（`ingressClassName: nginx`，不是 nginx 社区的
  NGINX Ingress Controller，注解按前者文档编写），仍是 SSE 链路上的一层反向代理，proxy 的
  Ingress 需要单独配置注解（见阶段 4）。

## 数据模型


| 表          | 列                                                                                                                                                 |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `users`    | `sub` (PK), `username`, `email`                                                                                                                   |
| `api_keys` | `id` (PK), `user_sub`, `name`, `key_hash` (unique), `key_hint`, `created_at`, `last_used_at`, `status`                                            |
| `requests` | `id` (PK), `key_id`, `provider`, `model`, `started_at`, `ended_at`, `status`, `usage_source`, `input_tokens`, `output_tokens`, `cache_hit_tokens` |


相对需求笔记新增的列：

- `api_keys.name`、`key_hint`：界面要求展示 Key 名称和掩码文本（如 `lgw-ab12…yz89`）。
- `requests.ended_at`、`provider`、`model`：区分上游和模型，便于排查与按模型统计。
  - `provider` 存供应商名，即配置中 `providers` 下的键名，由网关按模型表选定，不来自客户端。
    每家供应商只配置 `base_url` 和 `api_key`：
    `"providers": {"deepseek": {"base_url": ..., "api_key": ...}}`。
    进程启动时用各供应商自己的 Key 请求 `GET {base_url}/models`，把 `data[].id` 收成模型表：
    键是模型名，值是列出该模型的供应商名称，按配置顺序排列。同一模型出现在多家时都保留；
    转发时用名单里的第一家，供应商选择以后再做。列表失败或格式不对时进程不启动。
    模型表不写回配置、不落库。对外的 `GET /v1/models` 返回这张表里的模型名，每个名字只出现一次，
    不带供应商名和出站 Key。
    供应商名是用量记录的稳定标识，限定为 `[a-z0-9-]+`、最长 64；改名等于新增一个供应商，
    旧记录保留旧名。不建供应商表，配置文件就是唯一来源。
  - `model` 存客户端请求的模型名，原样保存、区分大小写。上游报错或中断时也能记录，与用户
    在客户端里选择的名字一致。缺失、不是字符串或超过 255 字符时返回 400，不访问上游。
  - 索引：`started_at`，`(key_id, started_at)`。demo 数据量下，供应商和模型筛选不单独建索引。
- `requests.usage_source`：`reported` / `estimated` / `unknown`，区分官方 usage 和估算值。

`requests.status`：`in_progress`、`completed`、`upstream_interrupted`、`client_interrupted`、
`upstream_error`（上游返回非 200，Token 记 0）、`gateway_error`。`status` 描述请求执行结果，
`usage_source` 描述用量是否可信，两者独立：转发成功不代表用量统计成功。

Token 列的语义由 lgw 定义，与上游无关，每个 provider 的解析器负责转换到这一语义，
不直接复制上游字段：

- `input_tokens`：全部输入 Token，包括缓存读取和缓存写入的部分。
- `cache_hit_tokens`：`input_tokens` 中从缓存读取的部分，不会大于 `input_tokens`。
- `output_tokens`：全部输出 Token，包括推理内容和工具调用参数。
- 不记录缓存写入量。不做计费时不需要；以后要与 AWS 账单对账时再增加列。

DeepSeek 的 `prompt_tokens` 已包含缓存命中，可直接作为 `input_tokens`。Bedrock Converse 的
`inputTokens` 据了解不含 `cacheReadInputTokens` / `cacheWriteInputTokens`，需要相加，
阶段 3 按官方文档核实。

Key 格式为 `lgw-` + `secrets.token_urlsafe(32)`，库中只存 SHA-256。Key 为高熵随机串，
不需要 bcrypt 这类慢哈希，认证时可直接按哈希索引查询。

## 关键设计点

1. **流式与断开检测**：proxy 生成器逐块转发 SSE，同时累积 delta 文本；在 `finally` 中按
  退出原因写入状态和用量。gevent 下客户端断开在下一次写入时才会被感知，阶段 1 首先验证。
2. **usage 获取**：据 DeepSeek 文档，无论是否设置 `stream_options.include_usage`，`[DONE]`
  前的最后一个 chunk（含 `finish_reason`）都带有整个请求的 `usage`，不存在单独的 usage chunk；
  设置为 `true` 时其余 chunk 的 `usage` 为 `null`。因此网关不注入参数、不修改响应，事件原样转发。
3. **user_id**：模型名以 `deepseek` 开头时，请求体中的 `user_id` 覆盖为 lgw
  用户的 `sub`，供 DeepSeek 做内容安全、KVCache 隔离和调度隔离。其他模型不添加、不改写
  `user_id`。DeepSeek 要求匹配
  `[a-zA-Z0-9\-_]+`、最长 512、不含隐私信息；Keycloak `sub` 为 UUID，满足要求，不能用
  username / email。其余参数原样转发。
4. **估算**：只在中断且没有收到 usage 时使用，场景少、准确度要求不高。所有模型共用通用分词器
  （`tiktoken` 的 `o200k_base`），接受与实际计费的偏差，编码文件不入库。输入 Token 按请求
  messages 计算；输出 Token 按已转发的 `content` + `reasoning_content` 计算；缓存命中记 0。
5. **崩溃恢复**：多 worker / 多副本下，单个进程启动时不能把全部 `in_progress` 标为 unknown，
  否则会误伤其他进程的在途请求。改为定期清扫：`in_progress` 且 `started_at` 超过最大请求时长
   （可配置，默认 30 分钟）的记录标为 `gateway_error` + `unknown`。
6. **Key 失效**：只在请求开始时校验，在途请求不受影响（符合笔记约定）。每次请求都查数据库，
  不缓存认证结果，停用对新请求立即生效。以后如需引入缓存，先确定可接受的停用生效延迟。
7. **gevent 约束**：每个 worker 是单线程多协程，只在 IO 等待时切换。CPU 密集操作（如
  Token 估算）会阻塞同 worker 的所有请求，阶段 1 需实测耗时；数据库驱动必须兼容 monkey
   patch，阶段 4 选用 psycopg 3，避免 `psycopg2` 这类会阻塞整个 worker 的驱动。
8. **错误转发**：proxy 错误响应采用 OpenAI 格式 `{"error": {...}}`，保证 SDK 能正确抛出异常
  （admin API 仍用 `{"message": ...}`）。上游 401 / 403 / 402 是网关出站 Key 或账户的问题，
  不能原样转发，否则客户端会误以为自己的 lgw Key 失效；这类错误转为 502 并记录日志。
  429 原样转发，但要注意 DeepSeek 并发限制按账户计算，所有 lgw 用户共享同一额度。
  详细映射见阶段 1 计划。
9. **转发边界**：上游地址只来自服务端配置，不接受客户端指定（避免 SSRF 和出站 Key 泄露）；
  用量归属只由 lgw Key 决定。网关不做自动重试和回退，一个 `requests` 记录对应一次上游调用；
  需要重试时由客户端发起，每次重试是一条独立记录。
10. **用量解析不影响转发**：解析 SSE 事件或 usage 失败、写入最终记录失败，都只记日志并降级
  `usage_source`，不能中断或改变发给客户端的响应。

## 阶段计划

### 阶段 0：脚手架（已完成）

- [x] 目录结构、`pyproject.toml`、ruff / pylint 配置、配置模板、两个 app 的健康检查
- [x] 首次提交

验收：`uv sync`、`ruff format --check`、`ruff check`、`pylint` 通过，两个 app 可通过
gunicorn gevent worker 启动。

### 阶段 1：本地转发和记录（SQLite）

详细计划见 [stage1-plan.md](stage1-plan.md)。

1. `db.py`、`models.py`、`keys.py`，提供 CLI：`flask --app ... init-db`、`create-key`（登录
  完成前用于发 Key，之后保留为运维工具）。
2. proxy：`POST /v1/chat/completions`（流式与非流式）。`model` 必须在启动时的模型表里，
  否则 404，不写 `requests`、不访问上游。`GET /v1/models` 返回该表，不在请求时再访问上游。
3. DeepSeek provider：请求改写、usage 解析。
4. 中断估算与超时清扫命令。
5. 测试：用本地假上游（返回 SSE、可中途断开）覆盖四种状态；pytest + respx 覆盖解析逻辑。

验收：用 curl 和 OpenAI Python SDK 通过 lgw 调通 DeepSeek；正常、客户端断开、上游断开
三种情况在 `requests` 表中记录正确的状态和用量来源。

### 阶段 2：登录和 Key 管理（SQLite）

1. admin：Authlib OIDC 登录 / 回调 / 登出，首次登录写入 `users`。
2. API（RESTful，错误返回 `{"message": ...}`，与参考项目一致）：
  - `GET/POST /api/v1/keys`、`PATCH /api/v1/keys/<id>`（停用）
  - `GET /api/v1/usage?start=&end=&key_ids=&providers=&models=&group_by=`：筛选条件可组合，
    多值用逗号分隔，省略表示不限；`group_by` 取 `day`、`key`、`provider`、`model` 的任意组合
    （默认 `day,key`）。每行返回分组字段、请求数和三项 Token 之和，并按 `usage_source` 分别
    给出 `estimated` / `unknown` 的请求数，便于判断数据可信度。
  - `GET /api/v1/usage/filters`：返回可选的供应商和模型。供应商为当前配置与历史记录中出现过的
    名称的并集（已删除的供应商仍可查历史）；模型为 `requests` 中出现过的值。普通用户只看到
    自己 Key 下的模型。
  - 管理员：`GET /api/v1/admin/keys`、`GET /api/v1/admin/usage`（参数同上，`group_by` 另可
    取 `user`）、停用任意 Key
3. 前端：登录态、Key 列表 / 创建（一次性展示）/ 停用、用量查询、管理员视图。

验收：用测试 Keycloak 登录，完成创建 Key、调用、查询用量全流程；普通用户无法访问
其他用户数据。

### 阶段 3：AWS Bedrock

先调研再定方案：客户端以何种协议访问（OpenAI 兼容格式由网关翻译为 Converse，或透传
Bedrock 原生接口），以及流式响应中 usage 的获取方式。中断时的估算沿用通用分词器。

- OpenAI 兼容供应商已按启动时的模型表路由：模型名对应供应商名称列表，当前固定用第一家，
  `requests.provider` 记这一家。客户端不能直接指定供应商。多家之间的选择以后再做。
  Bedrock 不是这套 `/models` + `/chat/completions` 接口，接入方式仍待公司流程结束后再定。
- 优先选择不需要协议翻译的方案。跨协议翻译要处理消息、工具调用、推理内容、结束原因和
  流式事件，不只是字段改名，出错时请求往往仍能成功，只是工具调用或 usage 悄悄出错。
- 访问 Bedrock 原生接口需要 SigV4 签名，交给 AWS SDK 完成，不自行拼接或修改已签名的请求。
- 验收覆盖：普通 / 流式请求、usage 映射（含缓存字段，按上文 Token 语义）、客户端断开、
  上游 400 / 429 / 5xx、工具调用（流式与非流式）、上游长时间无数据。

### 阶段 4：真实数据库和部署

1. 切换到集群 PostgreSQL，引入 Alembic 管理表结构。
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
4. 本机 WSL 手动部署验证：
  给出手动部署操作手册，人工部署验收。
  用本机 Docker 构建镜像，加载到本机 kind 集群，
  在 kind 中安装 Ingress-Nginx、cert-manager（自签名 ClusterIssuer）和 PostgreSQL，
  使用与公司集群相同的清单，只替换镜像地址、域名、issuer 名称等环境相关的值。验证项：
  - Alembic 在空库上建表成功，生成的表结构与 `models.py` 一致。
  - 经 Ingress 完成登录、创建 Key、调用 DeepSeek 与 Bedrock（普通与流式）、查询用量全流程。
  - 流式响应经 Ingress 逐事件送达，不被缓冲；超过 60 秒的请求不被截断。
  - 客户端断开经 Ingress 传递到 proxy，记录为 `client_interrupted`。
  - 滚动更新 proxy 时，在途的流式请求正常完成。
  - 清扫 CronJob 按时运行，超时的 `in_progress` 记录被正确标记。
5. 本机验证通过后，部署到公司 k8s，从 HPC 环境用真实客户端重复上述调用和断开验证。

验收：WSL kind 集群上全部验证项通过；公司 k8s 上部署成功，HPC 通过 lgw 调用 DeepSeek 和
Bedrock，用量正确入库。


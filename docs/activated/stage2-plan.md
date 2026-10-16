# 阶段 2 开发计划：登录和 Key 管理（SQLite）

总体范围、技术选择和数据模型见 [dev-plan.md](dev-plan.md)。本文件细化阶段 2 的实现。
管理员判定的原则在 dev-plan 的技术决策表，匹配细节在本文。会话 cookie 的 Path 在
dev-plan 的部署说明里，本文说明登录流程如何用它。

## 目标

用户通过 Keycloak 登录 admin，创建和停用自己的 lgw Key，并查询自己的用量。管理员可以
查看所有人的 Key 和用量，并停用任意 Key。proxy 仍按阶段 1 转发和记录。

不在本阶段：Bedrock、PostgreSQL、Alembic、镜像和 Kubernetes，以及 proxy 转发行为的改动。
非流式长时间排队要不要边收边转发，仍是阶段 1 未测项。

登录使用已选定的 Authlib 和 Flask 签名会话 cookie，不使用参考项目里的 `flask-oidc`，
也不把令牌放进进程内存。参考项目只提供 Keycloak 侧的行为：组从 userinfo 读，登出要结束
IdP 会话。

## 新增依赖

| 依赖 | 用途 |
| --- | --- |
| `authlib` | 授权码流程、在登录回调中用 access token 请求 userinfo |

前端是独立的 npm 工程，不进 `pyproject.toml`。

## 配置

沿用 `config.example.json` 里已有的 `oidc` 和 `admin`，不新增字段。

| 字段 | 用途 |
| --- | --- |
| `oidc.issuer` | realm 地址，末尾斜杠去掉。发现文档是 `{issuer}/.well-known/openid-configuration`。登出端点是 `{issuer}/protocol/openid-connect/logout` |
| `oidc.client_id` / `client_secret` | confidential client |
| `oidc.redirect_uri` | 浏览器里的回调地址。开发时是 Vite 的源加 `/api/auth/callback` |
| `oidc.admin_group` | 子串匹配用的组名，示例为 `lgw_admin` |
| `admin.secret_key` | 签名会话 cookie。缺失或空字符串时 admin 启动失败 |
| `admin.host_url` | 浏览器访问的源，不含路径。开发时是 `http://localhost:5173` |

`host_url` 以 `https://` 开头时会话 cookie 加 `Secure`，否则不加。本机 HTTP 若带上
`Secure`，浏览器会丢掉这张 cookie。

授权范围是 `openid profile email`。`groups` 不靠额外 scope，靠 realm 里已经加到
userinfo 的 Group Membership mapper。

admin 启动时只离线校验上述 OIDC、`admin` 字段（均须为非空字符串）和 `database.url`：
不读取 `providers`，不请求 Keycloak 发现文档或模型上游，也不加载分词器。Keycloak 或
DeepSeek 不可达时 admin 仍能启动。建表继续用 proxy 上的 `init-db`。

## 进程和会话

本地 admin 用 gunicorn gevent worker 听 `127.0.0.1:8001`。Vite 开发服务器听
`5173`，把 `/api` 代理到 `8001`。proxy 仍在 `8000`。浏览器只访问 Vite 的源，因此
cookie 的主机是 `localhost:5173`，路径仍按浏览器看到的 `/api/...` 判断。

会话 cookie：

- `HttpOnly`、`SameSite=Lax`、不设 `Max-Age`（关掉浏览器即失效）
- Path 为 `/api`，见 dev-plan
- 内容是 `sub`、`is_admin`（布尔值）和登出用的 `id_token`。不放 `access_token`、
  `refresh_token` 和 `groups`

关掉浏览器只清除本机 cookie。Keycloak 里的会话还在，下次打开登录地址仍可能直接进来。
要同时结束 IdP 会话，必须走下面的登出。

### Path 在本流程里怎么起作用

浏览器保存 cookie 时带一个 Path。之后只有请求路径等于这个 Path，或落在它下面，才会附上
这张 cookie。一张 cookie 只能有一个 Path。

`Path=/api` 时：

| 浏览器请求 | 是否附带会话 cookie |
| --- | --- |
| `/api/auth/callback`、`/api/keys`、`/api/usage`、`/api/me` | 附带 |
| `/console` 和 `/console/` 下的页面 | 不附带 |
| `/chat/completions`、`/models` | 不附带 |

页面本身不读 cookie。它加载后请求 `/api/me`，由这张 cookie 辨认登录态。登录回调也在
`/api` 下，`Set-Cookie` 可以写上 `Path=/api`。阶段 4 里 admin 与 proxy 共用域名时，
调用方打到根路径的请求不会带上会话。

## 登录和用户

1. `GET /api/auth/login`：浏览器导航到 Keycloak 的授权端点。Authlib 负责 `state`。
2. `GET /api/auth/callback`：用授权码换令牌，再拿 access token 请求一次 userinfo，读取
   `sub`、`preferred_username`、`email`、`groups`。不解析 ID token 来取这些字段。
3. `sub` 必须通过现有的 `validate_user_sub`（`[a-zA-Z0-9_-]+`，最长 512）。不合格时不写
   `users`、不建立会话，重定向到 `{host_url}/console/?error=invalid_sub`。
4. `sub` 尚不存在则插入。已存在（含先前 `create-key` 建出的用户）则更新 `username` 和
   `email`，Key 不动。`username` 用 `preferred_username`，缺失时用 `sub`。`email` 可以
   为空。`username` 或 `email` 超过列长 255，或发现文档、`state`、换取令牌、请求
   userinfo 失败时，均不写 `users`、不建立会话，重定向到
   `{host_url}/console/?error=login_failed`。URL 和日志里不记录授权码或令牌。
5. 按下节规则算出 `is_admin`，把 `sub`、`is_admin` 和 `id_token` 写入会话，重定向到
   `{host_url}/console/`。

`create-key` 仍留在 proxy 上，作为运维发 Key 的工具。

## 管理员

登录回调时根据 userinfo 的 `groups` 判定是否为管理员，并把布尔结果写入会话。日常业务
请求直接读会话里的 `is_admin`，不再请求 Keycloak，也不刷新令牌。在 Keycloak 里变更组
关系后，该用户下次重新登录时生效。

匹配：

```text
groups = userinfo["groups"] 或 []（缺失、null 都算缺失）
若 groups 是字符串，收成只含该字符串的列表
is_admin = 任一元素满足：oidc.admin_group 是它的子串
```

`lgw_admin` 能命中 `lgw_admin` 和 `/lgw_admin`。不要写成全等，完整路径会落空。也不要匹配
裸的 `admin`，否则任何名字里带 `admin` 的组都会成为管理员。

## 登出

`GET /api/auth/logout`：

1. 从会话取出 id token。会话里没有时，直接重定向到 `{host_url}/console/`。
2. 清除本机会话。
3. 重定向到：

```text
{issuer}/protocol/openid-connect/logout
    ?id_token_hint={id_token}
    &client_id={client_id}
    &post_logout_redirect_uri={host_url}/console/
```

`id_token_hint` 来自登录时保存的令牌，不从 access token 里拆。回跳地址带 `/console/`，
因为页面不在源的根上。日志不记录这条 URL。

Keycloak 客户端的 Valid post logout redirect URIs 必须包含
`{host_url}/console/` 这个完整字符串，否则 Keycloak 拒绝登出，IdP 会话仍在。本机会话
在重定向之前已经清除。

## 当前用户

`GET /api/me`：会话有效时从 `users` 表和会话返回
`{"sub": "...", "username": "...", "email": "...", "is_admin": true|false}`。

未登录访问任何 `/api` 业务接口（`/api/healthz`、`/api/auth/login`、
`/api/auth/callback`、`/api/auth/logout` 除外）返回 401。

## Key

错误体都是 `{"message": "..."}`。文案不作为契约，测试检查状态码，并检查 `message` 为
非空字符串。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| `GET` | `/api/keys` | 当前用户的全部 Key，含已停用 |
| `POST` | `/api/keys` | 为当前用户创建一把 Key。正文 `{"name": "..."}` |
| `PATCH` | `/api/keys/<id>` | 停用。正文只能是 `{"status": "disabled"}` |
| `GET` | `/api/admin/keys` | 所有用户的 Key，需为管理员 |

`name` 去掉首尾空白后长度须在 1 到 255。同名允许重复。

列表响应体为 `{"keys": [...]}`，按 `created_at`、`id` 降序排列。每项含 `id`、`name`、
`key_hint`、`created_at`、`last_used_at`、`status`；管理员列表另加 `user_sub`、
`username`。时间用带 `Z` 的 UTC。不返回 Key 原文。

创建成功返回 201，正文为单条 Key 对象，在列表字段之外多一个 `key`，只此一次。`PATCH`
返回 200 和单条 Key 对象；已停用再停用返回 200 和同一表示，不报错。不能重新启用，没有
删除接口。

`PATCH` 的授权：Key 不存在返回 404。属于当前用户，或当前用户是管理员，则停用；否则返回
403。

## 用量

`GET /api/usage` 只统计当前用户。`GET /api/admin/usage` 统计所有用户，需为管理员。
参数相同，管理员的 `group_by` 可以多一个 `user`。

筛选：

| 参数 | 规则 |
| --- | --- |
| `start`、`end` | 上海日历日 `YYYY-MM-DD`，闭区间（含 `start` 和 `end` 当天）。省略表示该侧不限。格式不对或 `start > end`，返回 400 |
| `key_ids`、`providers`、`models` | 逗号分隔，各项去掉空白。空字符串视为省略。省略表示不限。`key_ids` 各项须为正整数，否则返回 400 |
| `group_by` | `day`、`key`、`provider`、`model` 的组合，默认 `day,key`。未知值返回 400，重复值忽略。普通用户传入 `user` 返回 400 |

不统计 `status = in_progress` 的行。这些行的 token 仍是 0，`usage_source` 已是
`unknown`；若算进去，`unknown` 会把「还在进行」和「无法估算」混在一起。

普通用户的 `key_ids` 必须都属于自己；只要含非本人拥有的 id（无论属于他人还是不存在），
整次请求返回 403，不返回部分结果。管理员传入 `key_ids` 直接按条件过滤，未命中时结果为空，
不报错。省略 `key_ids` 时，普通用户只统计自己的 Key，管理员统计全部。

`providers`、`models` 里出现从未见过的名字时结果为空，不报错。

按天筛选和分组都用 `started_at` 的 Asia/Shanghai 日历日。库存的是 UTC。中国没有夏令时，
换算用固定东八区：后端把 `start` 换算为当天 `00:00:00+08:00`，把 `end` 换算为次日
`00:00:00+08:00`，按 `started_at >= start` 且 `started_at < end` 查询。例如
`2026-10-16T16:30:00Z` 属于 `2026-10-17`，`2026-10-16T15:00:00Z` 属于 `2026-10-16`。
这是查询语义；阶段 4 换 PostgreSQL 时保持同一日历日，不沿用 SQLite 的 `datetime` 修饰符。

每一行包含本次 `group_by` 要求的字段，以及：

| 字段 | 含义 |
| --- | --- |
| `day` | `YYYY-MM-DD` |
| `key_id`、`key_name` | 分组含 `key` 时 |
| `provider`、`model` | 分组含对应维度时 |
| `user_sub`、`username` | 仅管理员且分组含 `user` 时 |
| `request_count` | 纳入统计的请求数 |
| `input_tokens`、`output_tokens`、`cache_hit_tokens` | 三项之和 |
| `estimated_count`、`unknown_count` | 其中 `usage_source` 为这两类的请求数 |

`upstream_error` 的 token 为 0、来源为 `reported`，计入 `request_count`，不计入
`estimated_count` 或 `unknown_count`。响应体为 `{"rows": [...]}`，排序按出现的分组字段：
`day`、`user_sub`、`key_id`、`provider`、`model`。没有行时返回 `{"rows": []}`。

筛选项接口返回 `{"providers": [...], "models": [...]}`：

- `GET /api/usage/filters`：返回当前用户自己的 Key 在 `requests` 表里出现过的
  `providers` 和 `models`（含 `in_progress`，避免长请求期间下拉框里还没有该项）。
- `GET /api/admin/usage/filters`：返回全部用户在 `requests` 表里出现过的 `providers`
  和 `models`（含 `in_progress`），需为管理员。

## 前端

`frontend/`：Vue 3、Vite、TypeScript、Element Plus。`base` 为 `/console/`。开发服务器
把 `/api` 代理到 `127.0.0.1:8001`。界面文案用中文。

页面：

- 未登录时提供进入 `/api/auth/login` 的按钮。登出请求 `/api/auth/logout`。
- Key：名称、掩码、创建时间、最后使用时间、状态；创建时填写名称，成功后的对话框展示
  完整 Key，关闭或刷新后不再出现；停用前确认。
- 用量：按上海日历日选择范围，多选 Key、供应商、模型，勾选分组，表格展示 token 与
  `estimated_count`、`unknown_count`。
- 管理员：`is_admin === true` 时显示入口。Key 表多一列用户，用量可按用户分组。

不为组件的 DOM 结构写测试。接口契约由后端测试保护，页面走验收。

## 错误

| 情况 | 状态 |
| --- | --- |
| 未登录 | 401 |
| 普通用户访问管理员接口，或停用他人的 Key | 403 |
| 普通用户用量查询的 `key_ids` 含非本人的 Key | 403 |
| Key 不存在（`PATCH /api/keys/<id>`） | 404 |
| 参数或正文不合法 | 400 |
| 数据库错误 | 500，正文仍是 `{"message": "..."}`，并记录堆栈 |

不返回 Flask 的 HTML 错误页。

## 测试

单元测试不连接真实 Keycloak：

- 组匹配：`lgw_admin`、`/lgw_admin`、单个字符串、缺失、空列表、名字里含 `admin` 但
  不含配置值的组。最后一种不是管理员。
- 用量：东八区日历日的两侧边界、闭区间含 `end` 当天、排除 `in_progress`、普通用户传入非本人
  `key_ids` 为 403、非法 `key_ids` 格式为 400、管理员可按 `user` 分组。
- Key：创建只在响应里出现原文；普通用户停用他人的 Key 为 403；管理员可停用他人的 Key。

登录回调和登出用进程内的假 OIDC：发现文档、授权码、令牌、userinfo、end-session。
断言回调写入或更新 `users`，登出重定向的 URL 含 `id_token_hint` 和
`post_logout_redirect_uri`。pytest 不依赖公司的 Keycloak。

## 测试 realm

参考项目没有 realm 导出。使用已经能在 userinfo 里返回 `groups` 的那个 realm，为本项目
另建一个 confidential client：

- 标准授权码流程
- Valid redirect URIs 包含 `oidc.redirect_uri`
- Valid post logout redirect URIs 包含 `{host_url}/console/`
- Group Membership mapper：Token Claim Name 为 `groups`，Add to userinfo 打开。
  Add to ID token 和 Add to access token 可以不开。Full group path 可以保持打开

准备两名用户：一名属于 `lgw_admin`，一名不属于。

## 验收

1. 管理员登录后，userinfo 的 `groups` 含 `lgw_admin` 或 `/lgw_admin`，`/api/me` 的
   `is_admin` 为 true。另一名用户为 false，不能打开管理员接口，也不能在用量里统计他人的 Key。
2. 创建 Key、用该 Key 经 proxy 调用、在控制台按上海日历日查到这条用量。完整 Key 只在
   创建时出现。
3. 停用后，proxy 上的新请求返回 401，已在途的请求不受影响（阶段 1 的规则）。
4. 登出后再次打开 `/api/auth/login`，进入 Keycloak 登录页，而不是凭残留的 IdP 会话直接进来。
5. `ruff format --check`、`ruff check`、`pylint`、`pytest` 通过。
6. 编写 `docs/activated/stage2-manual.md`：本机启动 admin 和 Vite、注册客户端时要填的
   地址、两名测试用户的验收步骤。

## 提交拆分

1. `feat(admin): add OIDC login, callback and logout`
2. `feat(admin): add API key management`
3. `feat(admin): add usage query`
4. `feat(frontend): add console for keys and usage`
5. `docs: add stage 2 setup manual`

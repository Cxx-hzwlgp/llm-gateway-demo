# 架构

记录本项目的架构。
范围和决策以 [activated/dev-plan.md](activated/dev-plan.md) 为准。阶段 1 的状态、错误和用量规则以 [activated/stage1-plan.md](activated/stage1-plan.md) 为准。本文不重复那些规则。开发约定见仓库根目录的 [AGENTS.md](../AGENTS.md)。

## 现状和目标

现在能用的是 proxy：认证 lgw Key，把 OpenAI 兼容的聊天请求转到 DeepSeek，并在 SQLite 里记下用量。admin 只有健康检查。前端、`deploy/`、Bedrock 和 PostgreSQL 还没有，分别属于阶段 2、3、4。

两个进程共用 `backend/lgw/`、同一份 `config.json` 和同一个数据库文件。本地和以后的集群都用 gunicorn 的 gevent worker。不自建 Nginx；对外暴露方式见 dev-plan 的阶段 4。

```
客户端 (curl / SDK / Pi / Cline)
        │  POST /chat/completions
        │  GET  /models
        ▼
   lgw.proxy  ──httpx──►  DeepSeek
        │
        ▼
     SQLite（users / api_keys / requests）

   lgw.admin  ── 目前只有 GET /api/healthz
```

## proxy 里的一次聊天请求

`lgw.proxy` 在进程启动时读取配置，用每家供应商的 Key 请求 `{base_url}/models`，得到模型名到供应商的表，并加载估算用的分词器。之后的聊天请求不再访问 `/models`。`init-db`、`create-key`、`sweep` 也是这个应用上的命令，因此同样会加载这张表。

```
认证 Key → 校验 model 是否在启动时的模型表里
        → 写入 in_progress 的 requests 行
        → 按模型表里的第一家供应商转发
        → 非流式：读完上游再返回
          流式：按 SSE 事件原样转发，结束时在生成器的 finally 里写最终记录
```

对应代码：

| 模块 | 职责 |
| --- | --- |
| `lgw/config.py` | 读配置，校验 proxy 超时和供应商 |
| `lgw/db.py`、`lgw/models.py` | SQLite engine、短生命周期 session、三张表 |
| `lgw/keys.py`、`lgw/proxy/auth.py` | 生成并哈希 Key；每个请求查库 |
| `lgw/proxy/catalog.py` | 启动时的模型表 |
| `lgw/proxy/forward.py` | 校验、落库、调用上游、非流式收尾 |
| `lgw/proxy/streaming.py`、`lgw/proxy/sse.py` | 流式转发和断开后的记录 |
| `lgw/proxy/providers/deepseek.py` | 出站 URL、请求头、`user_id`、usage 解析 |
| `lgw/proxy/estimate.py` | 没有上游 usage 时的本地估算 |
| `lgw/proxy/recorder.py` | 插入和完成 `requests` |
| `lgw/cli.py` | `init-db`、`create-key`、`sweep` |

Key 的原文只在 `create-key` 时打印一次，库里保存哈希。出站 Key 只存在于配置和发往上游的 `Authorization` 头。

## 数据

三张表的列定义在 dev-plan。阶段 1–3 用 SQLite，开启 WAL，每次读写使用独立 session。阶段 4 换成 PostgreSQL，并用 Alembic 管理表结构；在那之前用 `Base.metadata.create_all`。

`requests` 在访问上游之前提交，这样进程中途退出后仍能被 `sweep` 找到。清扫只更新仍然是 `in_progress` 的行。

## 尚未落地的部分

- 阶段 2：admin 做 Keycloak 登录、Key 管理和用量查询，并在 `/console` 托管前端。浏览器只访问 admin；`/console` 与 `/api` 同源。proxy 占站点根路径，继续只服务非浏览器客户端。
- 阶段 3：Bedrock。它不走现在的 `/models` + `/chat/completions` 路径，接入方式仍以 dev-plan 里的待定项为准。
- 阶段 4：PostgreSQL、镜像和 Kubernetes。admin 与 proxy 仍是两个进程。

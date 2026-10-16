# 阶段 1 使用手册

在本机把 lgw proxy 跑起来，用 curl 或 OpenAI SDK 转发到 DeepSeek，并在 SQLite 里核对
`requests` 记录。命令都在 `backend/` 目录执行。

真实 DeepSeek、Pi 和 Cline 的联调还没做。下面的 Pi / Cline 只说明要填的字段。

## 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
cd backend
uv sync
```

首次启动会让 `tiktoken` 下载 `o200k_base` 编码（约 4 MB）并缓存。内网机器可事先放到
`TIKTOKEN_CACHE_DIR`。

## 配置

```bash
cp config.example.json config.json
```

至少填写：

- `providers` 里至少一家供应商。键名是供应商名，每家要有 `base_url` 和 `api_key`。
  `api_key` 留空时进程启动失败。
- 加载应用时会用每家的 Key 请求 `{base_url}/models`，收成模型名到供应商名称列表的表。
  列表失败或格式不对时进程不启动。`init-db`、`create-key`、`sweep` 和 gunicorn 都会加载应用，
  因此同样会查这张表。同一个模型出现在多家时都保留，转发使用名单里的第一家。
- `database.url` 默认 `sqlite:///lgw.db`，相对当前工作目录。

`proxy` 里的超时可以先保持模板中的值。`upstream_read_timeout` 是两次读取之间的间隔，
不是整次请求的时长。

## 建库和发 Key

```bash
uv run flask --app lgw.proxy init-db
uv run flask --app lgw.proxy create-key \
  --user-sub user-1 \
  --username alice \
  --name laptop
```

`create-key` 只把完整 Key 打到标准输出一次。模型名以 `deepseek` 开头时，`user-sub`
会作为上游 `user_id`。它必须匹配 `[a-zA-Z0-9_-]+` 且不超过 512 字符。用户已存在时不会改用户名。

把打出来的 `lgw-...` 存好，下面记为 `$LGW_KEY`。

## 启动

```bash
uv run gunicorn -k gevent -w 1 -b 127.0.0.1:8000 'lgw.proxy:create_app()'
```

健康检查：`curl -s http://127.0.0.1:8000/healthz`，返回 `{"status":"ok"}`。

配置文件不在当前目录时设置 `CONFIG_FILE_PATH`。

## curl

非流式：

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $LGW_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"hi"}]}'
```

流式：

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $LGW_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

模型列表来自启动时加载的模型表，不写 `requests`，也不会在这次请求里再访问上游。
表里没有的模型名调用 chat 时返回 404，`code` 为 `model_not_found`，不写 `requests`。

```bash
curl -s http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $LGW_KEY"
```

Key 缺失、格式不对、不存在或已停用都是 401，且不产生 `requests` 行。

## OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="lgw-...")
reply = client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "hi"}],
)
print(reply.choices[0].message.content)

stream = client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "hi"}],
    stream=True,
)
for chunk in stream:
    print(chunk)
```

SDK 1.109.1 遇到带 `error` 字段的流式事件会抛 `APIError`。

## 查看记录

```bash
sqlite3 lgw.db "
select id, model, status, usage_source,
       input_tokens, output_tokens, cache_hit_tokens
from requests
order by id desc
limit 10;"
```

正常结束且上游带回 usage 时，`status` 为 `completed`，`usage_source` 为 `reported`。

## 客户端中断

再开一个流式 curl，在输出过程中按 Ctrl+C。然后看最新一行：

- 上游仍在输出时，期望 `client_interrupted`。用量若还没收到官方 usage，则为
  `estimated`。
- 上游长时间没有任何字节时，网关要等到下一次读取才会发现断开。读超时先到时，
  记录会是 `upstream_interrupted`，而不是 `client_interrupted`。

清扫超时仍为 `in_progress` 的行：

```bash
uv run flask --app lgw.proxy sweep
```

超过 `proxy.max_request_seconds`（默认 1800 秒）的 `in_progress` 会被标成
`gateway_error`，`usage_source` 为 `unknown`。已经结束的行不会被改写。

## Pi 和 Cline

两边都把 OpenAI 兼容入口指到本机 proxy：

| 项        | 值                                  |
| -------- | ---------------------------------- |
| Base URL | `http://127.0.0.1:8000/v1`         |
| API Key  | `create-key` 打印的 lgw Key          |
| 模型名      | `deepseek-flash` 或 `deepseek-v4-pro` |

本机 `pi --help` 提供 `--provider`、`--model` 和 `--api-key`。自定义 base URL
的配置项以当前 Pi 版本文档为准。Cline 选择 OpenAI Compatible provider，填上面三项。

这次没有用 Pi 或 Cline 实际调用，也没有覆盖工具调用和中途取消。

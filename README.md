# llm-gateway-demo

公司内部 LLM 服务平台，职责是 LLM 请求转发和 Token 用量统计。

用 OpenAI 兼容接口调用 DeepSeek。网关按 Key 统计 Token，不保存请求内容，不限额，不计费。

Key 管理界面还没有。Key 由维护者发放，完整内容只在发放时出现一次。

## 调用

Base URL 是主机根。本机默认是 `http://127.0.0.1:8000`。

请求带 `Authorization: Bearer <lgw Key>`。模型名使用服务当前提供的名字，例如 `deepseek-flash` 或 `deepseek-v4-pro`。

- `POST /chat/completions`：聊天。流式时把 JSON 里的 `stream` 设为 `true`。
- `GET /models`：查看当前可用的模型名。

Key 缺失、格式不对或已停用时返回 401。模型名不在当前列表里时返回 404。

```bash
curl -s http://127.0.0.1:8000/chat/completions \
  -H "Authorization: Bearer $LGW_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"hi"}]}'
```

流式请求加上 `-N`，并在 JSON 中加入 `"stream": true`。

OpenAI SDK、Pi 和 Cline 使用同一组配置：上面的 Base URL、lgw Key，以及列表中的模型名。Pi 和 Cline 选择 OpenAI 兼容的提供方。

本机安装、填写配置、发放 Key 和启动服务见 [docs/completed/stage1-manual.md](docs/completed/stage1-manual.md)。

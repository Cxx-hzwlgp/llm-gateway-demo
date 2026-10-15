# llm-gateway-demo

LLM 网关 demo：为 HPC 用户转发 DeepSeek 等模型 API 请求，提供 Key 管理和 Token 用量统计。
范围、设计和阶段计划见 [docs/dev-plan.md](docs/dev-plan.md)。

## 本地开发

```bash
cd backend
uv sync
cp config.example.json config.json   # 按需填写
uv run gunicorn -k gevent -b 127.0.0.1:5000 'lgw.admin:create_app()'
uv run gunicorn -k gevent -b 127.0.0.1:8000 'lgw.proxy:create_app()'
```

配置文件路径可通过环境变量 `CONFIG_FILE_PATH` 指定，默认 `config.json`。

## 代码规范

- PEP8，行宽 79；docstring 使用 numpy 格式。
- 提交前运行：

  ```bash
  uv run ruff format .
  uv run ruff check .
  uv run pylint lgw
  uv run pytest
  ```

- 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，每次提交保持单一功能。
- API 采用 RESTful 风格，错误响应格式为 `{"message": "..."}`。

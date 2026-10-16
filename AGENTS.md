# AGENTS.md

## 文档

同一条规则只在一处维护。需求或验收标准变化时，先改 [docs/activated/dev-plan.md](docs/activated/dev-plan.md)。

| 内容 | 文档 |
| --- | --- |
| 项目定位、使用方式 | [README.md](README.md) |
| 范围、技术选择、数据模型、阶段验收 | [docs/activated/dev-plan.md](docs/activated/dev-plan.md) |
| 阶段 1 的请求流程、错误、估算、实测 | [docs/completed/stage1-plan.md](docs/completed/stage1-plan.md) |
| 阶段 1 审查发现的问题与阶段 2 前待修复项 | [docs/activated/stage1-fixes.md](docs/activated/stage1-fixes.md) |
| 阶段 2 的登录、Key、用量和前端 | [docs/activated/stage2-plan.md](docs/activated/stage2-plan.md) |
| 本机安装、发放 Key、启动和核对记录 | [docs/completed/stage1-manual.md](docs/completed/stage1-manual.md) |
| 进程、模块和请求路径 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |

## 进度

阶段 0 和阶段 1 的实现已经完成。实测记录在 `docs/completed/stage1-plan.md`。阶段 1 审查发现的待修复项记录在 `docs/activated/stage1-fixes.md`，在开发阶段 2 之前先行修复。阶段 2 的计划在 `docs/activated/stage2-plan.md`，尚未实现。阶段 3 及以后（Bedrock、PostgreSQL、部署）留到对应阶段，不提前实现。

## 约定

- 应用代码在 `backend/lgw/`。proxy 与 admin 是两个 Flask app，共享 `lgw` 包。
- 文档用中文。Python 3.12，PEP8，行宽 79，docstring 用 numpy 格式。
- 提交信息用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，一次提交一件事。
- `config.json` 不入库。配置示例只改 `backend/config.example.json`。
- proxy 返回给调用方的错误用 OpenAI 格式 `{"error": {...}}`。阶段 2 的 admin API 用 `{"message": "..."}`。
- 阶段 1 不保存请求正文，不限额，不对上游自动重试。

在 `backend/` 下验证：

```bash
uv run ruff format --check .
uv run ruff check .
uv run pylint lgw
uv run pytest
```

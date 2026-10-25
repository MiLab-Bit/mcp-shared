# mcp-shared（Hermes）

workspace 根级共享模块，被各 mcp-servers 经 sys.path 注入引用。

| 文件 | 职责 |
|---|---|
| db.py | 业务数据适配层：Supabase 优先；创业情报/UTM 有本地 JSON 回退，爬虫数据无回退 |
| supabase_client.py | 零依赖 Supabase REST 客户端；凭证链：环境变量 > .supabase.env > /opt/shared/supabase.env |
| quant_core.py | quant 家族共享核心：TuShare/FRED/AV/Alltick 适配 + sina/em/tx 直连接口；Phase 0 加固（SSRF 主机白名单 _guard_url / 5xx+传输层重试退避 / 输入校验 _check_* / 密钥脱敏 _redact）；Phase 1 观测（出站埋点 _observe + quant_health 聚合）；黄金测试在 mcp-quant 仓库 |
| metrics.py | quant 舰队 in-process 指标（Phase 1）：per-provider 出站 calls/errors/retries/error_rate/缓存 hit/miss/延迟 p50/p95；零依赖、线程安全、静默容错 |
| logging_utils.py | quant 结构化日志（Phase 1）：stdlib logging，JSON 行（ts/event/字段），stderr 输出；QUANT_LOG_LEVEL 可调级别 |
| ta.py | quant 技术指标计算层（Phase 2，纯 Python 零依赖）：sma/ema/macd/rsi/boll/kdj/atr + compute 组合器（吃任意 provider OHLCV）+ parse_indicator_spec；口径同通达信；长度不足输出 None 对齐序列 |
| mcp_utils.py | MCP 共享工具函数：clamp_limit 统一 limit 入参归一化（1-200 截断，非破坏性） |
| mcp_auth.py | 共享 Bearer 鉴权 + per-key 用量计量（crawler A2 范式通用化；news/events 等经此接入，crawler_auth 为薄包装） |
| health.sh / mcp_ping.py | 健康检查辅助脚本 |
| tests/ | 共享模块测试（pytest，Supabase 边界全 monkeypatch） |
| check_venv_invariants.sh | venv-mcp 依赖不变量校验（git 化于 frontend-radar 仓库，此处为入口副本） |

状态（2026-10-24）：Supabase 项目 hermes 已接入（8+ 服务端到端运行中）；
共享层运行版本 mcp 1.30.0（<2 不变量由校验脚本看守）。
测试：`/opt/hermes-mcp/venv-mcp/bin/python -m pytest tests/ -q`。

# mcp-shared（Hermes）

workspace 根级共享模块，被各 mcp-servers 经 sys.path 注入引用。

| 文件 | 职责 |
|---|---|
| db.py | 业务数据适配层：Supabase 优先；创业情报/UTM 有本地 JSON 回退，爬虫数据无回退 |
| supabase_client.py | 零依赖 Supabase REST 客户端；凭证链：环境变量 > .supabase.env > /opt/shared/supabase.env |
| quant_core.py | quant 家族共享核心：TuShare/FRED/AV/Alltick 适配 + sina/em/tx 直连接口；黄金测试在 mcp-quant 仓库 |
| health.sh / mcp_ping.py | 健康检查辅助脚本 |
| tests/ | 共享模块测试（pytest，Supabase 边界全 monkeypatch） |
| check_venv_invariants.sh | venv-mcp 依赖不变量校验（git 化于 frontend-radar 仓库，此处为入口副本） |

状态（2026-10-24）：Supabase 项目 hermes 已接入（8+ 服务端到端运行中）；
共享层运行版本 mcp 1.30.0（<2 不变量由校验脚本看守）。
测试：`/opt/hermes-mcp/venv-mcp/bin/python -m pytest tests/ -q`（18 项）。

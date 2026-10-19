# mcp-shared（MiLab）

workspace 根级共享模块，被各 mcp-servers 经 sys.path 注入引用。

| 文件 | 职责 |
|---|---|
| db.py | 业务数据适配层：Supabase 优先；创业情报有本地 JSON 回退，爬虫数据无回退 |
| supabase_client.py | 零依赖 Supabase REST 客户端；凭证链：环境变量 > .supabase.env > /opt/shared/supabase.env |
| quant_core.py | quant 家族共享核心：TuShare/FRED/AV/AKShare 适配 + sina/em/tx 直连接口 |
| health.sh / mcp_ping.py | 健康检查辅助脚本 |

状态（2026-10-19）：Supabase 新项目凭证待接入（/opt/shared/supabase.env 为空占位），
填入后需 systemctl restart 全部 mcp-* 服务。舰队验证：/opt/milab/scripts/mcp-fleet-probe.py

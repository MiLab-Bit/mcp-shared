#!/bin/bash
# SY Realm 全栈健康检查
# 被 OpenClaw Agent 调用，输出结构化状态

echo "============================================"
echo "  SY REALM HEALTH CHECK - $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"

echo ""
echo "## systemd Services"
for svc in bizatlas-worker bizatlas-api redtrip-worker cardio-worker lifetrip-worker; do
    status=$(systemctl is-active $svc 2>/dev/null)
    pid=$(systemctl show $svc -p MainPID --value 2>/dev/null)
    if [ "$status" = "active" ] && [ "$pid" != "0" ]; then
        echo "  [OK] $svc (PID=$pid)"
    else
        echo "  [FAIL] $svc ($status, PID=$pid)"
    fi
done

echo ""
echo "## Docker Containers"
for c in temporal-temporal-1 temporal-temporal-ui-1 temporal-temporal-postgresql-1 openclaw; do
    status=$(docker inspect -f '{{.State.Status}}' $c 2>/dev/null)
    health=$(docker inspect -f '{{.State.Health.Status}}' $c 2>/dev/null)
    if [ "$status" = "running" ]; then
        if [ -n "$health" ]; then
            echo "  [OK] $c ($status / $health)"
        else
            echo "  [OK] $c ($status)"
        fi
    else
        echo "  [FAIL] $c ($status)"
    fi
done

echo ""
echo "## Temporal Namespaces"
for ns in bizatlas redtrip cardio lifetrip; do
    count=$(docker exec temporal-temporal-1 temporal operator namespace describe --address temporal:7233 $ns 2>/dev/null | wc -l)
    if [ "$count" -gt 0 ]; then
        echo "  [OK] $ns"
    else
        echo "  [FAIL] $ns"
    fi
done

echo ""
echo "## Nginx"
if systemctl is-active nginx >/dev/null 2>&1; then
    echo "  [OK] nginx active"
else
    echo "  [FAIL] nginx down"
fi

echo ""
echo "## Resources"
echo "  Memory: $(free -h | awk '/Mem:/ {print $3 "/" $2 " used (" $7 " avail)"}')"
echo "  Disk: $(df -h / | awk 'NR==2 {print $3 "/" $2 " used (" $5 ")"}')"
echo "  Load: $(uptime | awk -F'load average:' '{print $2}')"
echo ""
echo "============================================"

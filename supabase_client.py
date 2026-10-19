#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiLab 云端数据层 —— 标准库实现的 Supabase REST 客户端（零第三方依赖）
凭证来源（按优先级）：环境变量 > 本目录 .supabase.env > /opt/shared/supabase.env
仅使用 service_role 密钥（服务端写，绕过 RLS）；anon 仅用于只读场景。
"""
import os, json, urllib.request, urllib.parse, urllib.error, ssl

CONFIG_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".supabase.env"),
    "/opt/shared/supabase.env",
]

def _load_cfg():
    cfg = {}
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY", "SUPABASE_PAT"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    for p in CONFIG_PATHS:
        if os.path.exists(p):
            try:
                for line in open(p, encoding="utf-8"):
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    cfg.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except Exception:
                pass
    return cfg

_CFG = _load_cfg()
URL = _CFG.get("SUPABASE_URL", "")
KEY = _CFG.get("SUPABASE_SERVICE_ROLE_KEY") or _CFG.get("SUPABASE_ANON_KEY", "")
PAT = _CFG.get("SUPABASE_PAT", "")
CTX = ssl.create_default_context()


class SupabaseError(Exception):
    pass


def is_available():
    return bool(URL and KEY)


def _req(method, table, body=None, params=None, key=None):
    k = key or KEY
    url = f"{URL}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}
    if method in ("POST", "PATCH"):
        headers["Prefer"] = "return=representation"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=30, context=CTX)
        return json.loads(r.read().decode() or "[]")
    except urllib.error.HTTPError as e:
        raise SupabaseError(e.code, e.read().decode()[:300])
    except Exception as e:
        raise SupabaseError("NET", repr(e))


def select(table, columns="*", eq=None, order=None, limit=None, key=None):
    params = {"select": columns}
    if eq:
        for k, v in eq.items():
            params[k] = f"eq.{v}"
    if order:
        params["order"] = order
    if limit:
        params["limit"] = limit
    return _req("GET", table, params=params, key=key)


def insert(table, rows, key=None):
    rows = rows if isinstance(rows, list) else [rows]
    return _req("POST", table, body=rows, key=key)


def update(table, match, patch, key=None):
    params = {k: f"eq.{v}" for k, v in match.items()}
    return _req("PATCH", table, body=patch, params=params, key=key)


def delete(table, match, key=None):
    params = {k: f"eq.{v}" for k, v in match.items()}
    return _req("DELETE", table, params=params, key=key)


if __name__ == "__main__":
    print("available:", is_available(), "url:", URL[:40] if URL else None)

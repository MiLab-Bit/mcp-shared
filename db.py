#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiLab 业务数据适配层
- 优先读取/写入 Supabase 云端（supabase_client）
- 云端不可用时自动回退本地 JSON，保证 CLI 离线可用
提供给三个系统的领域函数：创业情报 / UTM / 爬虫
"""
import os, sys, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import supabase_client as sc
    _SB = sc.is_available()
except Exception:
    sc = None
    _SB = False

# ---------- 创业情报 ----------
def load_startup_data():
    """返回 (acts, user_projects, hosts)，结构兼容原本地 JSON，便于 CLI 无感切换"""
    if _SB:
        try:
            acts_rows = sc.select("si_activities", columns="id,raw")
            proj_rows = sc.select("si_projects", columns="id,raw")
            host_rows = sc.select("si_hosts", columns="id,raw")
            acts = {r["id"]: r["raw"] for r in acts_rows}
            user_projects = {"projects": [r["raw"] for r in proj_rows]}
            hosts = {r["id"]: r["raw"] for r in host_rows}
            return acts, user_projects, hosts
        except Exception as e:
            sys.stderr.write(f"[db] Supabase 读取失败，回退本地: {e}\n")
    activity_lib = json.load(open(os.path.join(HERE, "startup-intel", "activity_library.json") if os.path.isdir(os.path.join(HERE,"startup-intel")) else os.path.join(HERE, "activity_library.json")))
    user_projects = json.load(open(os.path.join(HERE, "startup-intel", "user_projects.json") if os.path.isdir(os.path.join(HERE,"startup-intel")) else os.path.join(HERE, "user_projects.json")))
    hosts = json.load(open(os.path.join(HERE, "startup-intel", "hosts_registry.json") if os.path.isdir(os.path.join(HERE,"startup-intel")) else os.path.join(HERE, "hosts_registry.json")))
    acts = activity_lib.get("activities", activity_lib)
    hosts_data = hosts.get("hosts", hosts)
    return acts, user_projects, hosts_data


# ---------- UTM ----------
# 与 utm_tracker.PROJECT_URLS 保持一致（静态配置，避免循环 import）
PROJECT_URLS = {
    "bizatlas":      {"url": "https://github.com/MiLab-Bit/BizAtlas",         "name": "商舆 BizAtlas"},
    "redtrip":       {"url": "https://github.com/MiLab-Bit/RedTrip",          "name": "红鸢 RedTrip"},
    "lifetrip":      {"url": "https://github.com/MiLab-Bit/LifeTrip",         "name": "LifeTrip"},
    "cardio":        {"url": "https://github.com/MiLab-Bit/Cardio",           "name": "Cardio"},
    "vesta":         {"url": "https://github.com/MiLab-Bit/Vesta",            "name": "Vesta"},
    "codetoart":     {"url": "https://github.com/MiLab-Bit/CodeToArt",        "name": "代码炼金术"},
    "digitalgrave":  {"url": "https://github.com/MiLab-Bit/DigitalGrave",     "name": "数字墓碑"},
    "bigvoyage":     {"url": "https://github.com/MiLab-Bit/BigVoyage",        "name": "大航海"},
    "starwiki":      {"url": "https://github.com/MiLab-Bit/starwiki",         "name": "starwiki"},
    "web3quant":     {"url": "https://github.com/MiLab-Bit/Web3QuantMaster-Skill", "name": "Web3量化"},
    "openfasttoken": {"url": "https://github.com/MiLab-Bit/OpenFastToken",    "name": "OpenFastToken"},
    "mcreator":      {"url": "https://github.com/MiLab-Bit/MCreator",         "name": "MCreator"},
    "zhice":         {"url": "https://egoc.beer",                              "name": "zhice"},
}

def _reconstruct_utm(row):
    pid = row.get("product_id")
    info = PROJECT_URLS.get(pid, {})
    return {
        "link_id": row.get("id"),
        "project_id": pid,
        "project_name": info.get("name", pid),
        "base_url": info.get("url", ""),
        "utm_url": row.get("full_url"),
        "params": {
            "source": row.get("source") or "",
            "medium": row.get("medium") or "",
            "campaign": row.get("campaign") or "",
            "term": row.get("term") or "",
            "content": row.get("content") or "",
        },
        "created_at": (str(row.get("created_at") or ""))[:10],
        "clicks": 0,
    }

def load_utm_links(project_id=None):
    if _SB:
        try:
            rows = sc.select("utm_links",
                             columns="id,product_id,source,medium,campaign,term,content,full_url,created_at")
            out = []
            for r in rows:
                e = _reconstruct_utm(r)
                if project_id is None or e["project_id"] == project_id:
                    out.append(e)
            return out
        except Exception as e:
            sys.stderr.write(f"[db] Supabase UTM 读取失败，回退本地: {e}\n")
    db = _load_utm_local()
    return [l for l in db["utm_links"] if project_id is None or l["project_id"] == project_id]

def save_utm_link(entry):
    """写入一条 UTM 链接；云端按 full_url 去重，本地回退也按 url 去重"""
    if _SB:
        try:
            url = entry["utm_url"]
            existing = sc.select("utm_links", columns="id", eq={"full_url": url})
            if existing:
                return True
            sc.insert("utm_links", {
                "product_id": entry["project_id"],
                "source": entry["params"]["source"],
                "medium": entry["params"]["medium"],
                "campaign": entry["params"]["campaign"],
                "term": entry["params"]["term"],
                "content": entry["params"]["content"],
                "full_url": entry["utm_url"],
            })
            return True
        except Exception as e:
            sys.stderr.write(f"[db] Supabase UTM 写入失败，回退本地: {e}\n")
    db = _load_utm_local()
    if not any(l["utm_url"] == entry["utm_url"] for l in db["utm_links"]):
        db["utm_links"].append(entry)
        _save_utm_local(db)
    return True

def _load_utm_local():
    p = os.path.join(HERE, "utm-tracker", "utm_database.json") if os.path.isdir(os.path.join(HERE, "utm-tracker")) else os.path.join(HERE, "utm_database.json")
    if not os.path.exists(p):
        return {"version": "1.0", "utm_links": []}
    return json.load(open(p))

def _save_utm_local(db):
    p = os.path.join(HERE, "utm-tracker", "utm_database.json") if os.path.isdir(os.path.join(HERE, "utm-tracker")) else os.path.join(HERE, "utm_database.json")
    json.dump(db, open(p, "w"), indent=2, ensure_ascii=False)


# ---------- 爬虫 ----------
def load_crawl_targets():
    if _SB:
        try:
            return sc.select("cr_targets", columns="*", order="id")
        except Exception as e:
            sys.stderr.write(f"[db] Supabase 爬虫目标读取失败: {e}\n")
    return []

def save_crawl_record(target_id, title, url, content, status="ok"):
    if _SB:
        try:
            sc.insert("cr_records", {"target_id": target_id, "title": title,
                                     "url": url, "content": content, "status": status})
            return True
        except Exception as e:
            sys.stderr.write(f"[db] Supabase 爬虫记录写入失败: {e}\n")
    return False


if __name__ == "__main__":
    print("supabase_available:", _SB)


# ---------- 爬虫目标写入（供 MCP 使用） ----------
def save_crawl_target(target_id, target):
    """写入/跳过一条爬虫目标到 Supabase cr_targets；云端不可用时返回 False（本地回退不持久化目标）"""
    if _SB:
        try:
            body = {
                "name": target.get("name", target_id),
                "url": target.get("url", ""),
                "type": target.get("type"),
                "query": target.get("query"),
                "enabled": target.get("enabled", True),
            }
            existing = sc.select("cr_targets", columns="id", eq={"id": target_id})
            if existing:
                sc.update("cr_targets", {"id": target_id}, body)
            else:
                sc.insert("cr_targets", {"id": target_id, **body})
            return True
        except Exception as e:
            sys.stderr.write(f"[db] Supabase 爬虫目标写入失败: {e}\n")
    return False

"""db.py（mcp-shared 业务数据适配层）测试：云端优先 / 本地回退双分支。

所有 Supabase 边界（sc.select / sc.insert / sc.update / sc.is_available）
均 monkeypatch；本地 JSON 文件读走 open 桩。无网络、无真实云依赖。
"""
import io
import json
import os

import pytest

import db

# ---------- load_startup_data ----------

def test_load_startup_data_cloud(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    tables = {
        "si_activities": [{"id": "a1", "raw": {"title": "act1"}}],
        "si_projects": [{"id": "p1", "raw": {"name": "proj1"}}],
        "si_hosts": [{"id": "h1", "raw": {"host": "egoc.beer"}}],
    }
    monkeypatch.setattr(db.sc, "select",
                        lambda t, **k: tables[t])
    acts, projects, hosts = db.load_startup_data()
    assert acts == {"a1": {"title": "act1"}}
    assert projects == {"projects": [{"name": "proj1"}]}
    assert hosts == {"h1": {"host": "egoc.beer"}}


def test_load_startup_data_cloud_failure_falls_back(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    def boom(t, **k):
        raise ConnectionError("supabase down")
    monkeypatch.setattr(db.sc, "select", boom)
    files = {
        "activity_library.json": json.dumps({"activities": {"a1": {"title": "local"}}}),
        "user_projects.json": json.dumps({"projects": [{"name": "localp"}]}),
        "hosts_registry.json": json.dumps({"hosts": {"h1": {"host": "local"}}}),
    }
    real_open = open
    def fake_open(p, *a, **k):
        name = os.path.basename(str(p))
        if name in files:
            return io.StringIO(files[name])
        return real_open(p, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)
    acts, projects, hosts = db.load_startup_data()
    assert acts == {"a1": {"title": "local"}}
    assert hosts == {"h1": {"host": "local"}}


def test_load_startup_data_no_cloud_uses_local(monkeypatch):
    monkeypatch.setattr(db, "_SB", False)
    files = {
        "activity_library.json": json.dumps({"a9": {"title": "x"}}),
        "user_projects.json": json.dumps({"projects": []}),
        "hosts_registry.json": json.dumps({"hosts": {}}),
    }
    real_open = open
    def fake_open(p, *a, **k):
        name = os.path.basename(str(p))
        if name in files:
            return io.StringIO(files[name])
        return real_open(p, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)
    acts, _, _ = db.load_startup_data()
    assert acts == {"a9": {"title": "x"}}  # 无 activities 键时整体即 acts


# ---------- _reconstruct_utm ----------

def test_reconstruct_utm_known_project():
    row = {"id": "L1", "product_id": "bizatlas", "full_url": "https://x?utm_source=s",
           "source": "s", "medium": "m", "campaign": "c", "term": "t", "content": "k",
           "created_at": "2026-10-24T12:00:00+00:00"}
    e = db._reconstruct_utm(row)
    assert e["project_name"] == "商舆 BizAtlas"
    assert e["base_url"] == "https://github.com/MiLab-Bit/BizAtlas"
    assert e["created_at"] == "2026-10-24"  # 截断到日期
    assert e["params"] == {"source": "s", "medium": "m", "campaign": "c",
                           "term": "t", "content": "k"}


def test_reconstruct_utm_unknown_project_fallback():
    e = db._reconstruct_utm({"product_id": "ghost"})
    assert e["project_name"] == "ghost" and e["base_url"] == ""


# ---------- load_utm_links ----------

def test_load_utm_links_cloud(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    rows = [
        {"id": "L1", "product_id": "redtrip", "full_url": "u1", "source": "s"},
        {"id": "L2", "product_id": "lifetrip", "full_url": "u2", "source": "s"},
    ]
    monkeypatch.setattr(db.sc, "select", lambda t, **k: rows)
    out = db.load_utm_links("redtrip")
    assert len(out) == 1 and out[0]["project_id"] == "redtrip"
    assert db.load_utm_links() == out + [db._reconstruct_utm(rows[1])]


def test_load_utm_links_cloud_failure_local(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    monkeypatch.setattr(db.sc, "select",
                        lambda t, **k: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(db, "_load_utm_local",
                        lambda: {"utm_links": [{"project_id": "cardio", "utm_url": "u"}]})
    out = db.load_utm_links()
    assert out == [{"project_id": "cardio", "utm_url": "u"}]


# ---------- save_utm_link ----------

def test_save_utm_link_cloud_dedup(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    calls = []
    monkeypatch.setattr(db.sc, "select", lambda t, **k: [{"id": "existing"}])
    monkeypatch.setattr(db.sc, "insert", lambda t, body: calls.append(body))
    entry = {"project_id": "vesta", "utm_url": "https://x",
             "params": {"source": "s", "medium": "m", "campaign": "c",
                        "term": "t", "content": "k"}}
    assert db.save_utm_link(entry) is True
    assert calls == []  # 已存在 → 不重复 insert


def test_save_utm_link_cloud_insert(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    inserted = {}
    monkeypatch.setattr(db.sc, "select", lambda t, **k: [])
    monkeypatch.setattr(db.sc, "insert",
                        lambda t, body: inserted.update(body))
    entry = {"project_id": "vesta", "utm_url": "https://x",
             "params": {"source": "s", "medium": "m", "campaign": "c",
                        "term": "t", "content": "k"}}
    db.save_utm_link(entry)
    assert inserted["product_id"] == "vesta" and inserted["full_url"] == "https://x"


def test_save_utm_link_local_fallback(monkeypatch):
    monkeypatch.setattr(db, "_SB", False)
    saved = {}
    monkeypatch.setattr(db, "_load_utm_local",
                        lambda: {"version": "1.0", "utm_links": []})
    monkeypatch.setattr(db, "_save_utm_local", lambda d: saved.update(d))
    entry = {"project_id": "vesta", "utm_url": "https://x", "params": {}}
    assert db.save_utm_link(entry) is True
    assert saved["utm_links"] == [entry]


def test_save_utm_link_local_dedup(monkeypatch):
    monkeypatch.setattr(db, "_SB", False)
    monkeypatch.setattr(db, "_load_utm_local",
                        lambda: {"utm_links": [{"utm_url": "https://x"}]})
    monkeypatch.setattr(db, "_save_utm_local",
                        lambda d: pytest.fail("重复 url 不应写本地"))
    assert db.save_utm_link({"utm_url": "https://x", "params": {}}) is True


# ---------- 爬虫 ----------

def test_load_crawl_targets_cloud(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    monkeypatch.setattr(db.sc, "select", lambda t, **k: [{"id": "t1", "name": "n"}])
    assert db.load_crawl_targets() == [{"id": "t1", "name": "n"}]


def test_load_crawl_targets_cloud_failure_empty(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    monkeypatch.setattr(db.sc, "select",
                        lambda t, **k: (_ for _ in ()).throw(OSError("down")))
    assert db.load_crawl_targets() == []


def test_save_crawl_record_ok(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    inserted = {}
    monkeypatch.setattr(db.sc, "insert", lambda t, body: inserted.update(body))
    assert db.save_crawl_record("t1", "title", "https://x", "body", "ok") is True
    assert inserted == {"target_id": "t1", "title": "title",
                        "url": "https://x", "content": "body", "status": "ok"}


def test_save_crawl_record_no_cloud(monkeypatch):
    monkeypatch.setattr(db, "_SB", False)
    assert db.save_crawl_record("t1", "t", "u", "c") is False


def test_save_crawl_target_insert(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    calls = []
    monkeypatch.setattr(db.sc, "select", lambda t, **k: [])  # 不存在
    monkeypatch.setattr(db.sc, "insert",
                        lambda t, body: calls.append(("insert", body)))
    ok = db.save_crawl_target("t1", {"name": "n", "url": "https://x", "type": "rss"})
    assert ok is True
    assert calls[0][0] == "insert" and calls[0][1]["id"] == "t1"


def test_save_crawl_target_update_existing(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    calls = []
    monkeypatch.setattr(db.sc, "select", lambda t, **k: [{"id": "t1"}])  # 已存在
    monkeypatch.setattr(db.sc, "update",
                        lambda t, eq, body: calls.append(("update", eq, body)))
    monkeypatch.setattr(db.sc, "insert",
                        lambda t, body: calls.append(("insert", body)))
    assert db.save_crawl_target("t1", {"name": "n2"}) is True
    assert calls[0][0] == "update" and calls[0][1] == {"id": "t1"}


def test_save_crawl_target_exception_returns_false(monkeypatch):
    monkeypatch.setattr(db, "_SB", True)
    monkeypatch.setattr(db.sc, "select",
                        lambda t, **k: (_ for _ in ()).throw(OSError("down")))
    assert db.save_crawl_target("t1", {"name": "n"}) is False

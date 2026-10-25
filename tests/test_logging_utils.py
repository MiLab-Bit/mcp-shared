"""logging_utils 单元测试（Phase 1）：JSON 行格式/字段/级别 env/隔离性。"""
import json
import logging

import logging_utils


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _capture():
    h = _Collect()
    logging_utils._LOG.addHandler(h)
    return h


def test_log_event_json_line():
    h = _capture()
    try:
        logging_utils.log_event("smoke", provider="tushare", n=1)
    finally:
        logging_utils._LOG.removeHandler(h)
    assert len(h.lines) == 1
    d = json.loads(h.lines[0])
    assert d["event"] == "smoke"
    assert d["provider"] == "tushare" and d["n"] == 1
    assert "ts" in d


def test_log_adapter_call_fields():
    h = _capture()
    try:
        logging_utils.log_adapter_call("fred", "/fred/series/observations",
                                       cache="miss", duration_ms=42, status=200)
    finally:
        logging_utils._LOG.removeHandler(h)
    d = json.loads(h.lines[0])
    assert d["event"] == "adapter_call"
    assert d["provider"] == "fred"
    assert d["duration_ms"] == 42 and d["status"] == 200
    assert d["cache"] == "miss"
    assert d["error"] == ""


def test_log_cache_event():
    h = _capture()
    try:
        logging_utils.log_cache("tushare", "tushare:daily:{}", True)
    finally:
        logging_utils._LOG.removeHandler(h)
    d = json.loads(h.lines[0])
    assert d["event"] == "cache_lookup" and d["hit"] is True


def test_non_serializable_fields_degrade_not_raise():
    h = _capture()
    try:
        logging_utils.log_event("weird", obj=object())
    finally:
        logging_utils._LOG.removeHandler(h)
    d = json.loads(h.lines[0])
    assert d["event"] == "weird"
    assert "object object" in d["obj"]


def test_logger_isolated_stderr():
    """propagate=False + 自有 handler：不污染 root，不重复输出。"""
    assert logging_utils._LOG.propagate is False
    assert len(logging_utils._LOG.handlers) >= 1


def test_level_env_configurable(monkeypatch):
    import importlib
    monkeypatch.setenv("QUANT_LOG_LEVEL", "ERROR")
    importlib.reload(logging_utils)
    try:
        assert logging_utils._LOG.level == logging.ERROR
    finally:
        monkeypatch.delenv("QUANT_LOG_LEVEL")
        importlib.reload(logging_utils)
    assert logging_utils._LOG.level == logging.INFO

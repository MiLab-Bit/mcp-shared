# mcp-shared — Codex/Agent Review

This is the shared Hermes MCP Python library: authentication middleware, Supabase data adapter, usage tracking, provider routing and quantitative utilities. Review compatibility for downstream service consumers and actual import paths; do not invent a unified platform framework.

Security: bearer authentication for protected requests must fail closed, health exceptions must be scoped, usage accounting may fail without bypassing authorization, provider URLs require SSRF checks and real tokens must never enter logs. Exact financial values/market provenance should be preserved.

Require concrete changed-file evidence, exact SHA, safe repro, expected vs observed result and minimal fix; no forced flaw count. Tests should isolate Supabase/Redis/remote calls. See SECURITY.md. ChatGPT implements, Codex reviews without auto-merge, owner retains authority.

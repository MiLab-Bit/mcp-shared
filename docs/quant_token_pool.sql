-- Phase 5：quant 多 token 轮换池（quant_token_pool）
-- 在 Supabase SQL Editor 执行（openclaw 项目）。service_role 全权（与 quant_providers 一致）。
-- 说明：
--   * 代码侧（quant_core._prov_tokens/_pick_token）已实现：**入池为追加语义**——
--     可用 token = active 池 ∪ quant_providers.fields.token（去重，池内优先）。
--     因此只补充新 token 不会让原 fields.token 失效（不会出现"配额不增反降"）；
--     迁移完成后建议把 fields.token 清空，让 pool 成为该 provider 的唯一真源。
--     本 SQL 不执行也不影响现有链路（空池 → 退化单 token）。
--   * 轮换口径：active 中 last_used_at 最旧（NULL 优先）→ weight 降序；
--     进程内 round-robin 取用，取用后回写 last_used_at（多实例协同）。
--   * 限额提升场景：同一 provider 多把 token（如 alpha_vantage 免费 25次/日 多账号），
--     或 tushare 积分账号主备。token 值等同密码，勿在日志/截图外泄。

create table if not exists quant_token_pool (
    id            bigserial primary key,
    provider      text not null,
    token         text not null,
    status        text not null default 'active',   -- active / disabled
    weight        int  not null default 1,           -- 同 last_used_at 时的优先序
    last_used_at  timestamptz,
    note          text,                              -- 备注（如账号归属/限额）
    created_at    timestamptz not null default now()
);

create index if not exists idx_token_pool_prov_status
    on quant_token_pool(provider, status);

-- 唯一约束：同一 provider 下 token 不重复（避免重复入池双倍轮换）
create unique index if not exists uq_token_pool_prov_token
    on quant_token_pool(provider, token);

alter table quant_token_pool enable row level security;

drop policy if exists "service_role all" on quant_token_pool;
create policy "service_role all" on quant_token_pool
    for all to service_role using (true) with check (true);

-- 入池示例（按需替换 token 值；执行后 30s 内代码侧自动生效）：
-- insert into quant_token_pool (provider, token, status, weight, note)
-- values ('alpha_vantage', '<TOKEN_A>', 'active', 1, '主账号'),
--        ('alpha_vantage', '<TOKEN_B>', 'active', 1, '备用账号');

-- 运维查询：
--   select provider, status, weight, last_used_at, note from quant_token_pool order by provider, weight desc;
--   update quant_token_pool set status='disabled' where id = <id>;   -- 停用某把
--   -- quant_health() 的 tokens 字段会报 {"present": true, "pool": N} 示意池内 active 数

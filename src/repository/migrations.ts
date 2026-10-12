/**
 * 数据库迁移脚本（手写 SQL，轻量 migrator）。
 *
 * 说明：
 *  - 不使用 ORM / Prisma，全部为可审计的显式 DDL；
 *  - 迁移按 {@link MIGRATIONS} 顺序执行，已执行记录写入 `schema_migration` 表，幂等可重入；
 *  - 每个迁移在独立事务中执行，失败即回滚，绝不半途写入。
 */

export interface Migration {
  /** 迁移唯一 ID（建议 001_xxx 形式，按字典序执行） */
  id: string
  /** 迁移 SQL（可含多条语句） */
  sql: string
}

/** 迁移列表：新增迁移只能追加，禁止修改已发布条目 */
export const MIGRATIONS: readonly Migration[] = [
  {
    id: '001_init',
    sql: `
-- 迁移记录表：用于判断某个迁移是否已执行
CREATE TABLE IF NOT EXISTS schema_migration (
  id          TEXT PRIMARY KEY,
  applied_at  BIGINT NOT NULL
);

-- 迭代计划快照表：JSONB 全量载荷 + 冗余摘要列
CREATE TABLE IF NOT EXISTS iteration_plan (
  plan_id                TEXT PRIMARY KEY,
  raw_requirement        TEXT NOT NULL,
  iteration_deadline     TEXT NOT NULL,
  iteration_start_date   TEXT NOT NULL,
  dev_member_count       INTEGER NOT NULL,
  task_count             INTEGER NOT NULL,
  risk_count             INTEGER NOT NULL,
  high_risk_count        INTEGER NOT NULL,
  total_estimated_hours  NUMERIC(12, 2) NOT NULL,
  total_duration_hours   NUMERIC(12, 2) NOT NULL,
  payload                JSONB NOT NULL,
  created_at             BIGINT NOT NULL,
  updated_at             BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_iteration_plan_updated_at ON iteration_plan (updated_at DESC);

-- 审计日志表：全链路工具调用 / DAG 变更 / 重规划动作
CREATE TABLE IF NOT EXISTS iteration_audit (
  record_id    TEXT PRIMARY KEY,
  plan_id      TEXT,
  ts           TIMESTAMPTZ NOT NULL,
  action       TEXT NOT NULL,
  ok           BOOLEAN NOT NULL,
  duration_ms  INTEGER NOT NULL,
  input        JSONB,
  output       JSONB,
  error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_iteration_audit_plan_ts ON iteration_audit (plan_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_iteration_audit_ts ON iteration_audit (ts DESC);
`,
  },
]
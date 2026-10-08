-- 应收/应付按工作空间隔离：未选所属项目时仍可出现在当前空间列表
-- 执行后需将 v_finance_receivables / v_finance_payables 视图中加入主表 namespace_id 列（若视图未自动透传）

ALTER TABLE finance_receivables
  ADD COLUMN namespace_id INT NULL COMMENT '工作空间' AFTER project_id;

ALTER TABLE finance_payables
  ADD COLUMN namespace_id INT NULL COMMENT '工作空间' AFTER project_id;

-- 可选：按项目回填历史数据的 namespace_id（按实际 project_authorize 表名调整）
-- UPDATE finance_receivables r
-- INNER JOIN project_authorize pa ON pa.project_id = r.project_id AND (pa.is_deleted = 0 OR pa.is_deleted IS NULL)
-- SET r.namespace_id = pa.namespace_id
-- WHERE r.namespace_id IS NULL AND r.project_id IS NOT NULL;

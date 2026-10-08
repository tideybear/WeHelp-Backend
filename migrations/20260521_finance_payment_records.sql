-- 收付款明细子表（若库中尚未创建可执行）
CREATE TABLE IF NOT EXISTS `finance_payment_records` (
  `id` int(11) NOT NULL AUTO_INCREMENT COMMENT '付款记录ID',
  `payment_type` enum('receivable','payable') NOT NULL COMMENT '类型：receivable=应收收款，payable=应付付款',
  `parent_id` int(11) NOT NULL COMMENT '关联主表ID（finance_receivables.id 或 finance_payables.id）',
  `payment_no` varchar(50) DEFAULT NULL COMMENT '付款/收款单号（业务系统生成）',
  `amount` decimal(15,2) NOT NULL COMMENT '本次付款/收款金额',
  `payment_date` date NOT NULL COMMENT '实际付款/收款日期',
  `payment_bank` varchar(100) DEFAULT NULL COMMENT '付款/收款银行',
  `payment_account` varchar(50) DEFAULT NULL COMMENT '付款/收款账号/流水号',
  `payment_method` enum('银行转账','现金','支票','承兑汇票','其他') DEFAULT '银行转账' COMMENT '付款/收款方式',
  `voucher_no` varchar(50) DEFAULT NULL COMMENT '凭证号/回单号',
  `handler` varchar(50) DEFAULT NULL COMMENT '经办人',
  `remark` text COMMENT '备注',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `is_deleted` tinyint(1) DEFAULT 0 COMMENT '是否已删除',
  PRIMARY KEY (`id`),
  KEY `idx_payment_parent` (`payment_type`, `parent_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='付款/收款记录子表（多笔付款明细）';

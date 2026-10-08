# -*- coding: utf-8 -*-
"""财务应收/应付收付款明细子表 finance_payment_records。"""
from decimal import Decimal, ROUND_HALF_UP

from flask import current_app

PAYMENT_RECORDS_TABLE = 'finance_payment_records'
PAYMENT_TYPE_RECEIVABLE = 'receivable'
PAYMENT_TYPE_PAYABLE = 'payable'

PAYMENT_METHOD_VALUES = frozenset({
    '银行转账', '现金', '支票', '承兑汇票', '其他',
})

RECORD_WRITABLE_FIELDS = frozenset({
    'payment_type',
    'parent_id',
    'payment_no',
    'amount',
    'payment_date',
    'payment_bank',
    'payment_account',
    'payment_method',
    'voucher_no',
    'handler',
    'remark',
})


def _quantize_money(val):
    if val is None:
        return None
    return val.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _parse_decimal(val, required=False):
    if val is None or str(val).strip() == '':
        if required:
            raise ValueError('金额不能为空')
        return None
    d = Decimal(str(val).replace(',', '').strip())
    if d < 0:
        raise ValueError('金额不能为负数')
    return _quantize_money(d)


def _record_row_is_blank(raw):
    if not isinstance(raw, dict):
        return True
    if str(raw.get('payment_date') or '').strip():
        return False
    if str(raw.get('payment_bank') or '').strip():
        return False
    if str(raw.get('payment_account') or '').strip():
        return False
    if str(raw.get('payment_no') or '').strip():
        return False
    if str(raw.get('voucher_no') or '').strip():
        return False
    if str(raw.get('handler') or '').strip():
        return False
    if str(raw.get('remark') or '').strip():
        return False
    val = raw.get('amount')
    if val is not None and str(val).strip() != '':
        return False
    return True


def _normalize_payment_method(raw):
    val = str(raw or '').strip() or '银行转账'
    if val not in PAYMENT_METHOD_VALUES:
        raise ValueError(f'payment_method 取值无效，允许：{", ".join(sorted(PAYMENT_METHOD_VALUES))}')
    return val


def _parse_one_record(raw, row_no):
    if _record_row_is_blank(raw):
        return None, None

    try:
        amount = _parse_decimal(raw.get('amount'), required=True)
    except ValueError as e:
        return None, str(e)
    if amount is None or amount <= 0:
        return None, f'第 {row_no} 行：请填写有效的收付款金额'

    payment_date = str(raw.get('payment_date') or '').strip()[:10]
    if not payment_date:
        return None, f'第 {row_no} 行：收付款日期不能为空'

    try:
        payment_method = _normalize_payment_method(raw.get('payment_method'))
    except ValueError as e:
        return None, str(e)

    row = {
        'amount': amount,
        'payment_date': payment_date,
        'payment_method': payment_method,
        'payment_no': str(raw.get('payment_no') or '').strip() or None,
        'payment_bank': str(raw.get('payment_bank') or '').strip() or None,
        'payment_account': str(raw.get('payment_account') or '').strip() or None,
        'voucher_no': str(raw.get('voucher_no') or '').strip() or None,
        'handler': str(raw.get('handler') or '').strip() or None,
        'remark': str(raw.get('remark') or '').strip() or None,
    }
    return row, None


def parse_payment_records_from_body(body, *, require_records=False):
    """
    解析请求体 payment_records 数组。
    返回 (rows, total_amount, error_msg)。
    """
    if not isinstance(body, dict):
        body = {}
    raw_list = body.get('payment_records')
    if raw_list is None or (isinstance(raw_list, list) and len(raw_list) == 0):
        if require_records:
            return None, None, '请至少添加一条收付款明细'
        return [], Decimal('0'), None
    if not isinstance(raw_list, list):
        return None, None, 'payment_records 必须为数组'

    parsed = []
    total = Decimal('0')
    row_no = 0
    for raw in raw_list:
        if _record_row_is_blank(raw):
            continue
        row_no += 1
        row, err = _parse_one_record(raw, row_no)
        if err:
            return None, None, err
        if row:
            parsed.append(row)
            total += row['amount']

    if require_records and not parsed:
        return None, None, '请至少添加一条有效的收付款明细'

    total_amount = _quantize_money(total) if parsed else Decimal('0')
    return parsed, total_amount, None


def _record_row_to_json(row):
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif hasattr(v, 'isoformat') and k in ('payment_date',):
            out[k] = v.isoformat()[:10]
    return out


def _insert_records(payment_type, parent_id, rows):
    if not rows:
        return
    out_rows = []
    for item in rows:
        row = {k: item[k] for k in item if k in RECORD_WRITABLE_FIELDS}
        row['payment_type'] = payment_type
        row['parent_id'] = int(parent_id)
        out_rows.append(row)
    current_app.db_manager.insert_many(PAYMENT_RECORDS_TABLE, out_rows)


def insert_payment_records_for_receivable(receivable_id, rows):
    _insert_records(PAYMENT_TYPE_RECEIVABLE, receivable_id, rows)


def insert_payment_records_for_payable(payable_id, rows):
    _insert_records(PAYMENT_TYPE_PAYABLE, payable_id, rows)


def _soft_delete_records(payment_type, parent_id):
    current_app.db_manager.execute_update(
        f"""
        UPDATE {PAYMENT_RECORDS_TABLE}
        SET is_deleted = 1
        WHERE payment_type = :ptype
          AND parent_id = :pid
          AND (is_deleted = 0 OR is_deleted IS NULL)
        """,
        {'ptype': payment_type, 'pid': int(parent_id)},
    )


def replace_payment_records_for_receivable(receivable_id, rows):
    _soft_delete_records(PAYMENT_TYPE_RECEIVABLE, receivable_id)
    insert_payment_records_for_receivable(receivable_id, rows)


def replace_payment_records_for_payable(payable_id, rows):
    _soft_delete_records(PAYMENT_TYPE_PAYABLE, payable_id)
    insert_payment_records_for_payable(payable_id, rows)


def _select_sql(where_clause):
    return f"""
        SELECT
            id, payment_type, parent_id, payment_no, amount, payment_date,
            payment_bank, payment_account, payment_method, voucher_no, handler,
            remark, created_at, updated_at
        FROM {PAYMENT_RECORDS_TABLE}
        WHERE {where_clause}
          AND (is_deleted = 0 OR is_deleted IS NULL)
        ORDER BY id ASC
    """


def list_payment_records_by_receivable(receivable_id):
    sql = _select_sql('payment_type = :ptype AND parent_id = :pid')
    rows = current_app.db_manager.execute_query(
        sql,
        {'ptype': PAYMENT_TYPE_RECEIVABLE, 'pid': int(receivable_id)},
    ) or []
    return [_record_row_to_json(r) for r in rows]


def list_payment_records_by_payable(payable_id):
    sql = _select_sql('payment_type = :ptype AND parent_id = :pid')
    rows = current_app.db_manager.execute_query(
        sql,
        {'ptype': PAYMENT_TYPE_PAYABLE, 'pid': int(payable_id)},
    ) or []
    return [_record_row_to_json(r) for r in rows]


def _group_records_by_parent(rows, parent_field='parent_id'):
    grouped = {}
    for row in rows or []:
        pid = row.get(parent_field)
        if pid is None:
            continue
        key = int(pid)
        grouped.setdefault(key, []).append(_record_row_to_json(row))
    return grouped


def list_payment_records_grouped_by_receivable_ids(receivable_ids):
    ids = [int(i) for i in receivable_ids if i is not None]
    if not ids:
        return {}
    placeholders = ', '.join(f':id{i}' for i in range(len(ids)))
    params = {f'id{i}': vid for i, vid in enumerate(ids)}
    params['ptype'] = PAYMENT_TYPE_RECEIVABLE
    sql = _select_sql(
        f"payment_type = :ptype AND parent_id IN ({placeholders})",
    )
    rows = current_app.db_manager.execute_query(sql, params) or []
    return _group_records_by_parent(rows, 'parent_id')


def list_payment_records_grouped_by_payable_ids(payable_ids):
    ids = [int(i) for i in payable_ids if i is not None]
    if not ids:
        return {}
    placeholders = ', '.join(f':id{i}' for i in range(len(ids)))
    params = {f'id{i}': vid for i, vid in enumerate(ids)}
    params['ptype'] = PAYMENT_TYPE_PAYABLE
    sql = _select_sql(
        f"payment_type = :ptype AND parent_id IN ({placeholders})",
    )
    rows = current_app.db_manager.execute_query(sql, params) or []
    return _group_records_by_parent(rows, 'parent_id')


def strip_main_payment_fields(row_data):
    """收付款明细字段改由子表维护，主表不再写入。"""
    for key in (
        'receipt_date', 'receipt_bank', 'receipt_account',
        'payment_date', 'payment_bank', 'payment_account',
    ):
        row_data.pop(key, None)
    return row_data

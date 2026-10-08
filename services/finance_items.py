# -*- coding: utf-8 -*-
"""财务应收/应付产品明细子表 finance_payable_receivable_items。"""
from decimal import Decimal, ROUND_HALF_UP

from flask import current_app

ITEMS_TABLE = 'finance_payable_receivable_items'
DEFAULT_TAX_RATE = Decimal('13')

_items_table_columns_cache = None


def _items_table_columns():
    global _items_table_columns_cache
    if _items_table_columns_cache is None:
        rows = current_app.db_manager.execute_query(
            """
            SELECT COLUMN_NAME AS col
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table
            """,
            {'table': ITEMS_TABLE},
        )
        _items_table_columns_cache = {
            r['col'] for r in (rows or []) if r.get('col')
        }
    return _items_table_columns_cache


def _receivable_fk_column():
    cols = _items_table_columns()
    if 'receivable_id' in cols:
        return 'receivable_id'
    return 'receivables'


def _item_row_to_json(row):
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, Decimal):
            out[k] = str(v)
    return out

# 不含生成列 tax_amount、total_amount
ITEM_WRITABLE_FIELDS = frozenset({
    'payable_id',
    'receivables',
    'product_name',
    'specification',
    'unit',
    'quantity',
    'unit_price',
    'amount',
    'tax_rate',
    'remark',
    'merged_display_ids',
})


def _quantize_money(val):
    if val is None:
        return None
    return val.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _quantize_qty_price(val):
    if val is None:
        return None
    return val.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def _parse_decimal(val, required=False):
    if val is None or str(val).strip() == '':
        if required:
            raise ValueError('数值不能为空')
        return None
    return Decimal(str(val).replace(',', '').strip())


def _line_amount(quantity, unit_price, amount_raw, *, merged_display_ids=None):
    merged = str(merged_display_ids or '').strip()
    if merged:
        if amount_raw is not None:
            return _quantize_money(amount_raw)
        if quantity is not None and unit_price is not None:
            return _quantize_money(quantity * unit_price)
        return None
    if quantity is not None and unit_price is not None:
        return _quantize_money(quantity * unit_price)
    if amount_raw is not None:
        return _quantize_money(amount_raw)
    return None


def _row_is_blank(raw):
    if not isinstance(raw, dict):
        return True
    if str(raw.get('product_name') or '').strip():
        return False
    for key in ('quantity', 'unit_price', 'amount'):
        val = raw.get(key)
        if val is not None and str(val).strip() != '':
            return False
    return True


def parse_items_from_body(body, *, require_items=False):
    """
    解析请求体中的 items 数组。
    返回 (items_rows, total_amount, error_msg)。
    require_items=False 时允许无明细；空白行自动忽略。
    """
    if not isinstance(body, dict):
        body = {}
    raw_items = body.get('items')
    if raw_items is None or (isinstance(raw_items, list) and len(raw_items) == 0):
        if require_items:
            return None, None, '请至少添加一条产品明细'
        return [], Decimal('0'), None
    if not isinstance(raw_items, list):
        return None, None, 'items 必须为数组'

    parsed = []
    total = Decimal('0')
    merge_groups_counted = set()
    row_no = 0
    for raw in raw_items:
        if not require_items and _row_is_blank(raw):
            continue
        row_no += 1
        if not isinstance(raw, dict):
            return None, None, f'第 {row_no} 行明细格式无效'
        product_name = str(raw.get('product_name') or '').strip()
        if not product_name:
            return None, None, f'第 {row_no} 行：产品/货款名称不能为空'
        specification = str(raw.get('specification') or '').strip() or None
        unit = str(raw.get('unit') or '').strip() or None
        remark = str(raw.get('remark') or '').strip() or None

        try:
            quantity = _parse_decimal(raw.get('quantity'))
            unit_price = _parse_decimal(raw.get('unit_price'))
            amount_raw = _parse_decimal(raw.get('amount'))
            tax_rate = _parse_decimal(raw.get('tax_rate'))
        except ValueError as e:
            return None, None, f'第 {row_no} 行：{e}'

        if quantity is not None:
            quantity = _quantize_qty_price(quantity)
        if unit_price is not None:
            unit_price = _quantize_qty_price(unit_price)

        merged_display_ids = str(raw.get('merged_display_ids') or '').strip() or None
        client_id = str(raw.get('_client_id') or '').strip() or None

        amount = _line_amount(
            quantity,
            unit_price,
            amount_raw,
            merged_display_ids=merged_display_ids,
        )
        if amount is None or amount <= 0:
            return None, None, f'第 {row_no} 行：请填写数量×单价或有效金额'

        if tax_rate is None:
            tax_rate = DEFAULT_TAX_RATE
        else:
            tax_rate = tax_rate.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        row = {
            'product_name': product_name,
            'specification': specification,
            'unit': unit,
            'quantity': quantity,
            'unit_price': unit_price,
            'amount': amount,
            'tax_rate': tax_rate,
            'remark': remark,
            'merged_display_ids': merged_display_ids,
            '_client_id': client_id,
        }
        parsed.append(row)
        if merged_display_ids:
            if merged_display_ids not in merge_groups_counted:
                merge_groups_counted.add(merged_display_ids)
                total += amount
        else:
            total += amount

    if require_items and not parsed:
        return None, None, '请至少添加一条有效的产品明细'

    if not parsed:
        return [], Decimal('0'), None

    total_amount = _quantize_money(total)
    return parsed, total_amount, None


def _resolve_merged_display_ids_after_insert(items_rows, inserted_ids):
    if not items_rows or not inserted_ids:
        return
    client_to_db = {}
    for item, db_id in zip(items_rows, inserted_ids):
        cid = str(item.get('_client_id') or '').strip()
        if cid:
            client_to_db[cid] = int(db_id)

    cols = _items_table_columns()
    if 'merged_display_ids' not in cols:
        return

    for item, db_id in zip(items_rows, inserted_ids):
        raw_merged = str(item.get('merged_display_ids') or '').strip()
        if not raw_merged:
            continue
        parts = [p.strip() for p in raw_merged.split(',') if p.strip()]
        resolved = []
        for p in parts:
            if p.isdigit():
                resolved.append(p)
            elif p in client_to_db:
                resolved.append(str(client_to_db[p]))
        if not resolved:
            continue
        merged_str = ','.join(dict.fromkeys(resolved))
        current_app.db_manager.update_by_id(
            ITEMS_TABLE,
            int(db_id),
            {'merged_display_ids': merged_str},
        )


def _rows_for_items_insert(items_rows, fk_name, fk_value):
    writable = set(ITEM_WRITABLE_FIELDS) | {fk_name}
    rows = []
    for item in items_rows:
        row = {k: item[k] for k in item if k in writable}
        row[fk_name] = int(fk_value)
        rows.append(row)
    return rows


def insert_items_for_receivable(receivable_id, items_rows):
    if not items_rows:
        return
    fk = _receivable_fk_column()
    rows = _rows_for_items_insert(items_rows, fk, receivable_id)
    inserted_ids = current_app.db_manager.insert_many(ITEMS_TABLE, rows)
    _resolve_merged_display_ids_after_insert(items_rows, inserted_ids)


def replace_items_for_receivable(receivable_id, items_rows):
    fk = _receivable_fk_column()
    current_app.db_manager.execute_update(
        f"""
        UPDATE {ITEMS_TABLE}
        SET is_deleted = 1
        WHERE {fk} = :rid
          AND (is_deleted = 0 OR is_deleted IS NULL)
        """,
        {'rid': int(receivable_id)},
    )
    insert_items_for_receivable(receivable_id, items_rows)


def replace_items_for_payable(payable_id, items_rows):
    current_app.db_manager.execute_update(
        f"""
        UPDATE {ITEMS_TABLE}
        SET is_deleted = 1
        WHERE payable_id = :pid
          AND (is_deleted = 0 OR is_deleted IS NULL)
        """,
        {'pid': int(payable_id)},
    )
    insert_items_for_payable(payable_id, items_rows)


def insert_items_for_payable(payable_id, items_rows):
    if not items_rows:
        return
    rows = _rows_for_items_insert(items_rows, 'payable_id', payable_id)
    inserted_ids = current_app.db_manager.insert_many(ITEMS_TABLE, rows)
    _resolve_merged_display_ids_after_insert(items_rows, inserted_ids)


def _select_items_sql(where_clause):
    fk_recv = _receivable_fk_column()
    recv_col = f'{fk_recv} AS receivable_id' if fk_recv != 'receivable_id' else 'receivable_id'
    cols = _items_table_columns()
    merged_col = ', merged_display_ids' if 'merged_display_ids' in cols else ''
    return f"""
        SELECT
            id, payable_id, {recv_col}, product_name, specification, unit,
            quantity, unit_price, amount, tax_rate, tax_amount, total_amount,
            remark{merged_col}, created_at, updated_at
        FROM {ITEMS_TABLE}
        WHERE {where_clause}
          AND (is_deleted = 0 OR is_deleted IS NULL)
        ORDER BY id ASC
    """


def list_items_by_receivable(receivable_id):
    fk = _receivable_fk_column()
    sql = _select_items_sql(f'{fk} = :rid')
    return current_app.db_manager.execute_query(sql, {'rid': int(receivable_id)}) or []


def list_items_by_payable(payable_id):
    sql = _select_items_sql('payable_id = :pid')
    return current_app.db_manager.execute_query(sql, {'pid': int(payable_id)}) or []


def _group_items_by_fk(rows, fk_field):
    grouped = {}
    for row in rows or []:
        rid = row.get(fk_field) or row.get('receivables')
        if rid is None:
            continue
        key = int(rid)
        grouped.setdefault(key, []).append(_item_row_to_json(row))
    return grouped


def list_items_grouped_by_receivable_ids(receivable_ids):
    ids = [int(i) for i in receivable_ids if i is not None]
    if not ids:
        return {}
    fk = _receivable_fk_column()
    placeholders = ', '.join(f':id{i}' for i in range(len(ids)))
    params = {f'id{i}': vid for i, vid in enumerate(ids)}
    sql = _select_items_sql(f'{fk} IN ({placeholders})')
    rows = current_app.db_manager.execute_query(sql, params) or []
    return _group_items_by_fk(rows, 'receivable_id' if fk == 'receivable_id' else fk)


def list_items_grouped_by_payable_ids(payable_ids):
    ids = [int(i) for i in payable_ids if i is not None]
    if not ids:
        return {}
    placeholders = ', '.join(f':id{i}' for i in range(len(ids)))
    params = {f'id{i}': vid for i, vid in enumerate(ids)}
    sql = _select_items_sql(f'payable_id IN ({placeholders})')
    rows = current_app.db_manager.execute_query(sql, params) or []
    return _group_items_by_fk(rows, 'payable_id')


def strip_main_product_fields(row_data):
    """主表产品字段改由明细表维护，创建时剔除。"""
    for key in ('product_name', 'specification', 'unit', 'quantity', 'unit_price'):
        row_data.pop(key, None)
    return row_data

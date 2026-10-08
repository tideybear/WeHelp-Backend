# -*- coding: utf-8 -*-
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request, current_app
import logging

from services.finance_items import (
    insert_items_for_payable,
    list_items_by_payable,
    list_items_grouped_by_payable_ids,
    parse_items_from_body,
    replace_items_for_payable,
    strip_main_product_fields,
)
from services.finance_payment_records import (
    insert_payment_records_for_payable,
    list_payment_records_by_payable,
    list_payment_records_grouped_by_payable_ids,
    parse_payment_records_from_body,
    replace_payment_records_for_payable,
    strip_main_payment_fields,
)
from services.finance_scope import (
    namespace_delete_where_sql,
    namespace_list_filter_clause,
    stamp_namespace_on_row,
)

logger = logging.getLogger(__name__)

finance_payables_app = Blueprint('finance_payables', __name__)

PAYABLES_TABLE = 'finance_payables'
PAYABLES_VIEW = 'v_finance_payables'
PROJECT_AUTHORIZE_TABLE = 'project_authorize'
# MySQL 生成列，不可 INSERT/UPDATE
NON_WRITABLE_COLUMNS = frozenset({'unpaid_amount'})

_NAMESPACE_PROJECT_SUBQUERY = f"""
    SELECT DISTINCT pa.project_id
    FROM {PROJECT_AUTHORIZE_TABLE} pa
    WHERE pa.namespace_id = :namespace_id
      AND (pa.is_deleted = 0 OR pa.is_deleted IS NULL)
"""

INVOICE_STATUS_VALUES = ('未开票', '已开票', '部分开票', '已作废')
PAYMENT_STATUS_VALUES = ('待付款', '部分付款', '已付清', '逾期')

_table_columns_cache = {}


def _get_table_columns(table_name):
    if table_name not in _table_columns_cache:
        rows = current_app.db_manager.execute_query(
            """
            SELECT COLUMN_NAME AS col
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table
            """,
            {'table': table_name},
        )
        _table_columns_cache[table_name] = {
            r['col'] for r in (rows or []) if r.get('col')
        }
    return _table_columns_cache[table_name]


def _pick_row_for_table(table_name, data):
    cols = _get_table_columns(table_name)
    skip = NON_WRITABLE_COLUMNS if table_name == PAYABLES_TABLE else frozenset()
    return {k: v for k, v in data.items() if k in cols and k not in skip}


def _json_safe_row(row):
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, (datetime, date)):
            out[k] = v.strftime('%Y-%m-%d %H:%M:%S') if isinstance(v, datetime) else v.strftime('%Y-%m-%d')
    return out


def _parse_decimal_field(val, field_name, required=False):
    if val is None or str(val).strip() == '':
        if required:
            raise ValueError(f'{field_name} 不能为空')
        return None
    try:
        return Decimal(str(val).replace(',', '').strip())
    except Exception:
        raise ValueError(f'{field_name} 格式无效')


def _build_payables_list_filters():
    clauses = []
    params = {}

    keyword = request.args.get('keyword')
    if keyword is not None and str(keyword).strip():
        kw = f"%{str(keyword).strip()}%"
        clauses.append(
            '(s.customer_name LIKE :keyword OR s.contract_no LIKE :keyword '
            'OR s.product_name LIKE :keyword OR s.invoice_no LIKE :keyword '
            'OR s.project_name LIKE :keyword)',
        )
        params['keyword'] = kw

    customer_name = request.args.get('customer_name')
    if customer_name is not None and str(customer_name).strip():
        clauses.append('s.customer_name LIKE :customer_name')
        params['customer_name'] = f"%{str(customer_name).strip()}%"

    contract_no = request.args.get('contract_no')
    if contract_no is not None and str(contract_no).strip():
        clauses.append('s.contract_no LIKE :contract_no')
        params['contract_no'] = f"%{str(contract_no).strip()}%"

    invoice_no = request.args.get('invoice_no')
    if invoice_no is not None and str(invoice_no).strip():
        clauses.append('s.invoice_no LIKE :invoice_no')
        params['invoice_no'] = f"%{str(invoice_no).strip()}%"

    for field in ('invoice_status', 'status'):
        raw = request.args.get(field)
        if raw is None or str(raw).strip() == '':
            continue
        val = str(raw).strip()
        if field == 'invoice_status' and val not in INVOICE_STATUS_VALUES:
            return None, None, f'invoice_status 取值无效'
        if field == 'status' and val not in PAYMENT_STATUS_VALUES:
            return None, None, f'status 取值无效'
        clauses.append(f's.{field} = :{field}')
        params[field] = val

    namespace_id = request.args.get('namespace_id')
    if namespace_id is not None and str(namespace_id).strip() != '':
        try:
            params['namespace_id'] = int(namespace_id)
        except (TypeError, ValueError):
            return None, None, 'namespace_id 必须为整数'
        clauses.append(
            namespace_list_filter_clause(PAYABLES_VIEW, _NAMESPACE_PROJECT_SUBQUERY),
        )

    project_id = request.args.get('project_id')
    if project_id is not None and str(project_id).strip() != '':
        try:
            params['project_id'] = int(project_id)
        except (TypeError, ValueError):
            return None, None, 'project_id 必须为整数'
        clauses.append('s.project_id = :project_id')

    is_accounting_procedures = request.args.get('is_accounting_procedures')
    if is_accounting_procedures is not None and str(is_accounting_procedures).strip() != '':
        try:
            params['is_accounting_procedures'] = int(is_accounting_procedures)
        except (TypeError, ValueError):
            return None, None, 'is_accounting_procedures 必须为整数'
        clauses.append('s.is_accounting_procedures = :is_accounting_procedures')

    extra = ''
    if clauses:
        extra = ' AND ' + ' AND '.join(clauses)
    return extra, params, None


def _parse_payables_body_fields(body, *, require_customer=False, require_amount=False, partial=False):
    if not isinstance(body, dict):
        body = {}

    row_data = {}

    if 'customer_name' in body or require_customer:
        name = str(body.get('customer_name') or '').strip()
        if require_customer and not name:
            return None, '客户名称不能为空'
        if name:
            row_data['customer_name'] = name

    decimal_fields = (
        'invoice_amount', 'contract_account', 'discount_amount', 'quantity', 'unit_price', 'amount',
        'paid_amount',
    )
    for field in decimal_fields:
        if partial and field not in body:
            continue
        if field in body or (not partial and field in ('amount',)):
            try:
                row_data[field] = _parse_decimal_field(
                    body.get(field),
                    field,
                    required=(require_amount and field == 'amount'),
                )
            except ValueError as e:
                return None, str(e)

    if partial and 'due_days' not in body:
        pass
    elif 'due_days' in body or not partial:
        raw = body.get('due_days')
        if raw is None or str(raw).strip() == '':
            if 'due_days' in body:
                row_data['due_days'] = None
        else:
            try:
                row_data['due_days'] = int(raw)
            except (TypeError, ValueError):
                return None, 'due_days 必须为整数'

    date_fields = (
        'warehouse_date', 'payment_date', 'expected_payment_date',
    )
    for field in date_fields:
        if partial and field not in body:
            continue
        if field in body:
            raw = body.get(field)
            row_data[field] = str(raw).strip()[:10] if raw else None

    enum_fields = ('invoice_status', 'status')
    for field in enum_fields:
        if partial and field not in body:
            continue
        if field in body or not partial:
            if partial and field not in body:
                continue
            val = str(body.get(field) or '').strip()
            if val:
                if field == 'invoice_status' and val not in INVOICE_STATUS_VALUES:
                    return None, f'invoice_status 取值无效'
                if field == 'status' and val not in PAYMENT_STATUS_VALUES:
                    return None, f'status 取值无效'
                row_data[field] = val

    str_fields = (
        'invoice_no', 'contract_no', 'product_name', 'specification', 'unit',
        'payment_bank', 'payment_account', 'remark',
    )
    for field in str_fields:
        if partial and field not in body:
            continue
        if field in body:
            raw = body.get(field)
            row_data[field] = str(raw or '').strip() or None

    if 'project_id' in body or not partial:
        if partial and 'project_id' not in body:
            pass
        else:
            raw = body.get('project_id')
            if raw is not None and str(raw).strip() != '':
                try:
                    row_data['project_id'] = int(raw)
                except (TypeError, ValueError):
                    return None, 'project_id 非法'
            elif 'project_id' in body:
                row_data['project_id'] = None

    if 'is_accounting_procedures' in body:
        raw = body.get('is_accounting_procedures')
        if raw is None or str(raw).strip() == '':
            row_data['is_accounting_procedures'] = None
        else:
            try:
                flag = int(raw)
            except (TypeError, ValueError):
                return None, 'is_accounting_procedures 必须为整数'
            if flag not in (0, 1):
                return None, 'is_accounting_procedures 取值无效'
            row_data['is_accounting_procedures'] = flag

    return row_data, None


def _parse_namespace_id(raw):
    if raw is None or str(raw).strip() == '':
        return None, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, 'namespace_id 必须为整数'


def _project_belongs_to_namespace(project_id, namespace_id):
    rows = current_app.db_manager.execute_query(
        f"""
        SELECT 1 AS ok
        FROM {PROJECT_AUTHORIZE_TABLE} pa
        WHERE pa.namespace_id = :namespace_id
          AND pa.project_id = :project_id
          AND (pa.is_deleted = 0 OR pa.is_deleted IS NULL)
        LIMIT 1
        """,
        {'namespace_id': namespace_id, 'project_id': project_id},
    )
    return bool(rows)


@finance_payables_app.route('/finance/payables/list', methods=['GET'])
def get_payables_list():
    """应付账款列表（v_finance_payables）。"""
    try:
        page = request.args.get('page', default=1, type=int)
        page_size = request.args.get('page_size', default=100, type=int)
        if page < 1:
            return jsonify({'code': 400, 'msg': 'page 必须 >= 1'}), 400
        if page_size < 1 or page_size > 500:
            return jsonify({'code': 400, 'msg': 'page_size 必须在 1~500 之间'}), 400

        filter_extra, filter_params, err = _build_payables_list_filters()
        if err:
            return jsonify({'code': 400, 'msg': err}), 400

        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM {PAYABLES_VIEW} s
            WHERE 1=1
            {filter_extra}
        """
        count_res = current_app.db_manager.execute_query(count_sql, filter_params)
        total = int(count_res[0].get('total', 0)) if count_res else 0

        offset = (page - 1) * page_size
        sql = f"""
            SELECT
                s.id, s.customer_name, s.warehouse_date, s.invoice_status,
                s.invoice_no, s.invoice_amount, s.contract_no, s.contract_account, s.discount_amount,
                s.product_name,
                s.specification, s.unit, s.quantity, s.unit_price, s.amount,
                s.paid_amount, s.unpaid_amount, s.payment_date, s.payment_bank,
                s.payment_account, s.expected_payment_date, s.due_days, s.status,
                s.remark, s.created_at, s.updated_at, s.project_id, s.project_name
            FROM {PAYABLES_VIEW} s
            WHERE 1=1
            {filter_extra}
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT :limit OFFSET :offset
        """
        page_params = {**filter_params, 'limit': page_size, 'offset': offset}
        rows = current_app.db_manager.execute_query(sql, page_params) or []
        safe_rows = [_json_safe_row(r) for r in rows]
        record_ids = [r.get('id') for r in safe_rows if r.get('id') is not None]
        items_map = list_items_grouped_by_payable_ids(record_ids)
        payments_map = list_payment_records_grouped_by_payable_ids(record_ids)
        for row in safe_rows:
            pid = row.get('id')
            row['items'] = items_map.get(int(pid), []) if pid is not None else []
            row['payment_records'] = payments_map.get(int(pid), []) if pid is not None else []

        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': safe_rows,
            'total': total,
            'page': page,
            'page_size': page_size,
        })
    except Exception as e:
        logger.error('获取应付账款列表失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取应付账款列表失败: {str(e)}'}), 500


@finance_payables_app.route('/finance/payables/create', methods=['POST'])
def create_payable():
    """新增应付账款。"""
    try:
        body = request.get_json(silent=True) or {}
        project_id_raw = body.get('project_id')
        project_id = None
        if project_id_raw is not None and str(project_id_raw).strip() != '':
            try:
                project_id = int(project_id_raw)
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'project_id 非法'}), 400

        namespace_id, ns_err = _parse_namespace_id(body.get('namespace_id'))
        if ns_err:
            return jsonify({'code': 400, 'msg': ns_err}), 400
        if (
            project_id is not None
            and namespace_id is not None
            and not _project_belongs_to_namespace(project_id, namespace_id)
        ):
            return jsonify({'code': 400, 'msg': '所选项目不属于当前空间'}), 400

        items_rows, items_total, items_err = parse_items_from_body(body, require_items=False)
        if items_err:
            return jsonify({'code': 400, 'msg': items_err}), 400

        payment_rows, _, payment_err = parse_payment_records_from_body(body, require_records=False)
        if payment_err:
            return jsonify({'code': 400, 'msg': payment_err}), 400

        row_data, err = _parse_payables_body_fields(
            body, require_customer=True, require_amount=False, partial=False,
        )
        if err:
            return jsonify({'code': 400, 'msg': err})

        strip_main_product_fields(row_data)
        strip_main_payment_fields(row_data)
        row_data['amount'] = items_total if items_total is not None else Decimal('0')
        stamp_namespace_on_row(row_data, namespace_id, PAYABLES_TABLE)

        row_data.setdefault('invoice_status', '未开票')
        row_data.setdefault('status', '待付款')
        row_data.setdefault('paid_amount', Decimal('0'))

        row = _pick_row_for_table(PAYABLES_TABLE, row_data)
        if 'customer_name' not in row:
            return jsonify({'code': 500, 'msg': '应付表缺少 customer_name 列'}), 500

        new_id = current_app.db_manager.insert_one(PAYABLES_TABLE, row)
        insert_items_for_payable(new_id, items_rows)
        insert_payment_records_for_payable(new_id, payment_rows)
        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': {'id': new_id},
        })
    except Exception as e:
        logger.error('新增应付账款失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'新增应付账款失败: {str(e)}'}), 500


@finance_payables_app.route('/finance/payables/update', methods=['POST'])
def update_payable():
    """更新应付账款。"""
    try:
        body = request.get_json(silent=True) or {}
        record_id = body.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'})

        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': 'id 非法'})

        exists = current_app.db_manager.execute_query(
            f'SELECT id FROM {PAYABLES_TABLE} WHERE id = :id AND (is_deleted = 0 OR is_deleted IS NULL) LIMIT 1',
            {'id': record_id},
        )
        if not exists:
            return jsonify({'code': 404, 'msg': '未找到应付账款'})

        items_in_body = 'items' in body
        items_rows = []
        if items_in_body:
            items_rows, items_total, items_err = parse_items_from_body(body, require_items=False)
            if items_err:
                return jsonify({'code': 400, 'msg': items_err}), 400

        payments_in_body = 'payment_records' in body
        payment_rows = []
        if payments_in_body:
            payment_rows, _, payment_err = parse_payment_records_from_body(body, require_records=False)
            if payment_err:
                return jsonify({'code': 400, 'msg': payment_err}), 400

        row_data, err = _parse_payables_body_fields(body, partial=True)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400

        if items_in_body:
            from decimal import Decimal as _Dec
            row_data = row_data or {}
            row_data['amount'] = items_total if items_total is not None else _Dec('0')
            strip_main_product_fields(row_data)

        if payments_in_body:
            row_data = row_data or {}
            strip_main_payment_fields(row_data)

        if not row_data and not items_in_body and not payments_in_body:
            return jsonify({'code': 400, 'msg': '没有可更新的字段'}), 400

        if row_data:
            row = _pick_row_for_table(PAYABLES_TABLE, row_data)
            if row:
                current_app.db_manager.update_by_id(PAYABLES_TABLE, record_id, row)

        if items_in_body:
            replace_items_for_payable(record_id, items_rows)

        if payments_in_body:
            replace_payment_records_for_payable(record_id, payment_rows)

        return jsonify({'code': 200, 'msg': 'success', 'data': {'id': record_id}})
    except Exception as e:
        logger.error('更新应付账款失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'更新应付账款失败: {str(e)}'}), 500


@finance_payables_app.route('/finance/payables/delete', methods=['POST'])
def delete_payable():
    """软删除应付账款。"""
    try:
        body = request.get_json(silent=True) or {}
        record_id = body.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'})

        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': 'id 非法'})

        namespace_id, ns_err = _parse_namespace_id(body.get('namespace_id'))
        if ns_err:
            return jsonify({'code': 400, 'msg': ns_err})

        check_params = {'id': record_id}
        check_clauses = ['s.id = :id']
        if namespace_id is not None:
            check_params['namespace_id'] = namespace_id
            check_clauses.append(
                namespace_list_filter_clause(PAYABLES_VIEW, _NAMESPACE_PROJECT_SUBQUERY),
            )

        exists = current_app.db_manager.execute_query(
            f"""
            SELECT id FROM {PAYABLES_VIEW} s
            WHERE {' AND '.join(check_clauses)}
            LIMIT 1
            """,
            check_params,
        )
        if not exists:
            return jsonify({'code': 404, 'msg': '未找到应付账款或无权删除'})

        params = {'id': record_id, 'updated_at': datetime.now()}
        sql = f"""
            UPDATE {PAYABLES_TABLE}
            SET is_deleted = 1, updated_at = :updated_at
            WHERE id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
        """
        if namespace_id is not None:
            params['namespace_id'] = namespace_id
            sql += namespace_delete_where_sql(PAYABLES_TABLE, _NAMESPACE_PROJECT_SUBQUERY)
        affected = current_app.db_manager.execute_update(sql, params)
        if affected <= 0:
            return jsonify({'code': 404, 'msg': '未找到应付账款或无权删除'})
        return jsonify({'code': 200, 'msg': '删除成功'})
    except Exception as e:
        logger.error('删除应付账款失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'删除应付账款失败: {str(e)}'}), 500


@finance_payables_app.route('/finance/payables/detail', methods=['GET'])
def get_payable_detail():
    """单条应付账款详情。"""
    try:
        record_id = request.args.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400

        sql = f"""
            SELECT s.*
            FROM {PAYABLES_VIEW} s
            WHERE s.id = :id
            LIMIT 1
        """
        rows = current_app.db_manager.execute_query(sql, {'id': record_id})
        if not rows:
            return jsonify({'code': 404, 'msg': '未找到应付账款'}), 404
        detail = _json_safe_row(rows[0])
        item_rows = list_items_by_payable(record_id)
        detail['items'] = [_json_safe_row(r) for r in item_rows]
        detail['payment_records'] = list_payment_records_by_payable(record_id)
        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': detail,
        })
    except Exception as e:
        logger.error('获取应付账款详情失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取应付账款详情失败: {str(e)}'}), 500

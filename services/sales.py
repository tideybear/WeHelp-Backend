# -*- coding: utf-8 -*-
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request, current_app
import logging

logger = logging.getLogger(__name__)

sales_app = Blueprint('sales', __name__)

CUSTOMER_TABLE = 'customers'
CUSTOMER_CODE_PREFIX = 'CUS-'

CUSTOMER_STATUS_LABELS = {0: '禁用', 1: '启用', 2: '黑名单'}
CUSTOMER_LEVEL_LABELS = {1: '普通', 2: '重要', 3: 'VIP', 4: '战略合作'}
CUSTOMER_TYPE_LABELS = {1: '企业', 2: '个人', 3: '政府机构', 4: '事业单位'}

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
    return {k: v for k, v in data.items() if k in cols}


def _resolve_user_display_name(user_id):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return ''
    rows = current_app.db_manager.execute_query(
        """
        SELECT name, account
        FROM `user`
        WHERE id = :uid
        LIMIT 1
        """,
        {'uid': uid},
    )
    if not rows:
        return ''
    row = rows[0]
    name = str(row.get('name') or '').strip()
    if name:
        return name
    return str(row.get('account') or '').strip()


def _generate_customer_code():
    year = datetime.now().year
    prefix = f'{CUSTOMER_CODE_PREFIX}{year}-'
    start_pos = len(prefix) + 1
    sql = f"""
        SELECT MAX(CAST(SUBSTRING(customer_code, {start_pos}) AS UNSIGNED)) AS max_seq
        FROM {CUSTOMER_TABLE}
        WHERE customer_code LIKE :prefix_like
    """
    rows = current_app.db_manager.execute_query(sql, {
        'prefix_like': prefix + '%',
    })
    max_seq = 0
    if rows and rows[0].get('max_seq') is not None:
        try:
            max_seq = int(rows[0]['max_seq'])
        except (TypeError, ValueError):
            max_seq = 0
    return f'{prefix}{max_seq + 1:03d}'


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


def _parse_decimal_field(val, field_name):
    if val is None or str(val).strip() == '':
        return None
    try:
        return Decimal(str(val).replace(',', '').strip())
    except Exception:
        raise ValueError(f'{field_name} 格式无效')


def _build_customer_list_filters():
    clauses = []
    params = {}

    keyword = request.args.get('keyword')
    if keyword is not None and str(keyword).strip():
        kw = f"%{str(keyword).strip()}%"
        clauses.append(
            '(s.customer_code LIKE :keyword OR s.customer_name LIKE :keyword '
            'OR s.short_name LIKE :keyword)',
        )
        params['keyword'] = kw

    customer_code = request.args.get('customer_code')
    if customer_code is not None and str(customer_code).strip():
        clauses.append('s.customer_code LIKE :customer_code')
        params['customer_code'] = f"%{str(customer_code).strip()}%"

    customer_name = request.args.get('customer_name')
    if customer_name is not None and str(customer_name).strip():
        clauses.append('s.customer_name LIKE :customer_name')
        params['customer_name'] = f"%{str(customer_name).strip()}%"

    short_name = request.args.get('short_name')
    if short_name is not None and str(short_name).strip():
        clauses.append('s.short_name LIKE :short_name')
        params['short_name'] = f"%{str(short_name).strip()}%"

    contact_person = request.args.get('contact_person')
    if contact_person is not None and str(contact_person).strip():
        clauses.append('s.contact_person LIKE :contact_person')
        params['contact_person'] = f"%{str(contact_person).strip()}%"

    phone = request.args.get('phone')
    if phone is not None and str(phone).strip():
        phone_kw = f"%{str(phone).strip()}%"
        clauses.append('(s.phone LIKE :phone OR s.mobile LIKE :phone)')
        params['phone'] = phone_kw

    for field in ('status', 'customer_level', 'customer_type'):
        raw = request.args.get(field)
        if raw is None or str(raw).strip() == '':
            continue
        try:
            params[field] = int(raw)
        except (TypeError, ValueError):
            return None, None, f'{field} 必须为整数'
        clauses.append(f's.{field} = :{field}')

    namespace_id = request.args.get('namespace_id')
    if namespace_id is not None and str(namespace_id).strip() != '':
        try:
            params['namespace_id'] = int(namespace_id)
        except (TypeError, ValueError):
            return None, None, 'namespace_id 必须为整数'
        clauses.append(
            'EXISTS (SELECT 1 FROM customers c '
            'WHERE c.id = s.id AND c.namespace_id = :namespace_id)',
        )

    extra = ''
    if clauses:
        extra = ' AND ' + ' AND '.join(clauses)
    return extra, params, None


def _parse_customer_body_fields(body, *, require_name=False, partial=False):
    if not isinstance(body, dict):
        body = {}

    row_data = {}

    if 'customer_name' in body or require_name:
        customer_name = str(body.get('customer_name') or '').strip()
        if require_name and not customer_name:
            return None, '客户名称不能为空'
        if customer_name and len(customer_name) > 100:
            return None, '客户名称不能超过 100 个字符'
        if customer_name or require_name:
            row_data['customer_name'] = customer_name

    for field, labels in (
        ('status', CUSTOMER_STATUS_LABELS),
        ('customer_level', CUSTOMER_LEVEL_LABELS),
        ('customer_type', CUSTOMER_TYPE_LABELS),
    ):
        if partial and field not in body:
            continue
        raw = body.get(field, 1)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None, f'{field} 必须为整数'
        if val not in labels:
            return None, f'{field} 取值无效'
        row_data[field] = val

    for field in ('credit_limit', 'used_credit', 'receivable_amount'):
        if partial and field not in body:
            continue
        if field in body or not partial:
            try:
                row_data[field] = _parse_decimal_field(body.get(field), field)
            except ValueError as e:
                return None, str(e)

    if partial and 'follow_up_cycle' not in body:
        pass
    elif 'follow_up_cycle' in body or not partial:
        raw = body.get('follow_up_cycle', 0)
        try:
            row_data['follow_up_cycle'] = int(raw) if raw is not None and str(raw).strip() != '' else 0
        except (TypeError, ValueError):
            return None, 'follow_up_cycle 必须为整数'

    for field in ('last_contact_date', 'next_contact_date'):
        if partial and field not in body:
            continue
        if field in body:
            raw = body.get(field)
            row_data[field] = str(raw).strip()[:10] if raw else None

    str_fields = (
        'short_name', 'contact_person', 'phone', 'mobile', 'fax', 'email',
        'qq', 'wechat', 'province', 'city', 'district', 'address', 'zip_code',
        'bank_name', 'bank_account', 'tax_number', 'payment_terms', 'remark',
        'industry', 'source', 'sales_person',
    )
    for field in str_fields:
        if partial and field not in body:
            continue
        if partial or field in body or not partial:
            if partial and field not in body:
                continue
            raw = body.get(field)
            row_data[field] = str(raw or '').strip() or None

    return row_data, None


@sales_app.route('/sales/customers/list', methods=['GET'])
def get_customers_list():
    """客户列表（数据源 v_customers_info）。"""
    try:
        page = request.args.get('page', default=1, type=int)
        page_size = request.args.get('page_size', default=100, type=int)
        if page < 1:
            return jsonify({'code': 400, 'msg': 'page 必须 >= 1'}), 400
        if page_size < 1 or page_size > 500:
            return jsonify({'code': 400, 'msg': 'page_size 必须在 1~500 之间'}), 400

        filter_extra, filter_params, err = _build_customer_list_filters()
        if err:
            return jsonify({'code': 400, 'msg': err}), 400

        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM v_customers_info s
            WHERE 1=1
            {filter_extra}
        """
        count_res = current_app.db_manager.execute_query(count_sql, filter_params)
        total = int(count_res[0].get('total', 0)) if count_res else 0

        offset = (page - 1) * page_size
        sql = f"""
            SELECT
                s.id, s.customer_code, s.customer_name, s.short_name,
                s.contact_person, s.phone, s.mobile, s.email,
                s.province, s.city, s.district, s.address,
                s.bank_name, s.bank_account, s.tax_number,
                s.status, s.customer_level, s.customer_type,
                s.payment_terms, s.credit_limit, s.used_credit, s.receivable_amount,
                s.industry, s.source, s.sales_person,
                s.follow_up_cycle, s.last_contact_date, s.next_contact_date,
                s.remark, s.created_at, s.created_by, s.updated_at, s.updated_by
            FROM v_customers_info s
            WHERE 1=1
            {filter_extra}
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT :limit OFFSET :offset
        """
        page_params = {**filter_params, 'limit': page_size, 'offset': offset}
        rows = current_app.db_manager.execute_query(sql, page_params) or []
        safe_rows = [_json_safe_row(r) for r in rows]

        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': safe_rows,
            'total': total,
            'page': page,
            'page_size': page_size,
        })
    except Exception as e:
        logger.error('获取客户列表失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取客户列表失败: {str(e)}'}), 500


@sales_app.route('/sales/customers/create', methods=['POST'])
def create_customer():
    """新增客户（编码自动生成 CUS-年份-序号）。"""
    try:
        body = request.get_json(silent=True) or {}

        user_id = body.get('user_id')
        operator = ''
        if user_id is not None and str(user_id).strip() != '':
            try:
                operator = _resolve_user_display_name(int(user_id))
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'user_id 非法'}), 400

        namespace_id = body.get('namespace_id')
        if namespace_id is None or str(namespace_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 namespace_id'}), 400
        try:
            ns_val = int(namespace_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': 'namespace_id 非法'}), 400

        row_data, err = _parse_customer_body_fields(body, require_name=True, partial=False)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400

        customer_code = _generate_customer_code()
        row_data['customer_code'] = customer_code
        row_data['namespace_id'] = ns_val
        row_data['created_by'] = operator or None
        row_data['updated_by'] = operator or None

        row = _pick_row_for_table(CUSTOMER_TABLE, row_data)
        if 'customer_name' not in row:
            return jsonify({'code': 500, 'msg': '客户表缺少 customer_name 列'}), 500

        new_id = current_app.db_manager.insert_one(CUSTOMER_TABLE, row)
        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': {'id': new_id, 'customer_code': customer_code},
        })
    except Exception as e:
        logger.error('新增客户失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'新增客户失败: {str(e)}'}), 500


@sales_app.route('/sales/customers/update', methods=['POST'])
def update_customer():
    """更新客户。"""
    try:
        body = request.get_json(silent=True) or {}
        record_id = body.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400

        exists = current_app.db_manager.execute_query(
            f'SELECT id FROM {CUSTOMER_TABLE} WHERE id = :id LIMIT 1',
            {'id': record_id},
        )
        if not exists:
            return jsonify({'code': 404, 'msg': '未找到客户'}), 404

        user_id = body.get('user_id')
        operator = ''
        if user_id is not None and str(user_id).strip() != '':
            try:
                operator = _resolve_user_display_name(int(user_id))
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'user_id 非法'}), 400

        row_data, err = _parse_customer_body_fields(body, require_name=False, partial=True)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400
        if not row_data:
            return jsonify({'code': 400, 'msg': '没有可更新的字段'}), 400

        if operator:
            row_data['updated_by'] = operator

        row = _pick_row_for_table(CUSTOMER_TABLE, row_data)
        if not row:
            return jsonify({'code': 400, 'msg': '没有可更新的字段'}), 400

        current_app.db_manager.update_by_id(CUSTOMER_TABLE, int(record_id), row)
        return jsonify({'code': 200, 'msg': 'success', 'data': {'id': int(record_id)}})
    except Exception as e:
        logger.error('更新客户失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'更新客户失败: {str(e)}'}), 500


@sales_app.route('/sales/customers/delete', methods=['POST'])
def delete_customer():
    """删除客户（物理删除）。"""
    try:
        body = request.get_json(silent=True) or {}
        record_id = body.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400

        namespace_id = body.get('namespace_id')
        params = {'id': record_id}
        extra = ''
        if namespace_id is not None and str(namespace_id).strip() != '':
            try:
                params['namespace_id'] = int(namespace_id)
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'namespace_id 非法'}), 400
            extra = ' AND namespace_id = :namespace_id'

        sql = f'DELETE FROM {CUSTOMER_TABLE} WHERE id = :id{extra}'
        affected = current_app.db_manager.execute_update(sql, params)
        if affected <= 0:
            return jsonify({'code': 404, 'msg': '未找到客户或无权删除'}), 404
        return jsonify({'code': 200, 'msg': '删除成功'})
    except Exception as e:
        logger.error('删除客户失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'删除客户失败: {str(e)}'}), 500


@sales_app.route('/sales/customers/detail', methods=['GET'])
def get_customer_detail():
    """单条客户详情。"""
    try:
        record_id = request.args.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400

        sql = """
            SELECT s.*, c.namespace_id
            FROM v_customers_info s
            LEFT JOIN customers c ON c.id = s.id
            WHERE s.id = :id
            LIMIT 1
        """
        rows = current_app.db_manager.execute_query(sql, {'id': record_id})
        if not rows:
            return jsonify({'code': 404, 'msg': '未找到客户'}), 404
        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': _json_safe_row(rows[0]),
        })
    except Exception as e:
        logger.error('获取客户详情失败: %s', e, exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取客户详情失败: {str(e)}'}), 500

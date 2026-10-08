# -*- coding: utf-8 -*-
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request, current_app
import logging

logger = logging.getLogger(__name__)

purchase_app = Blueprint('purchase', __name__)

SUPPLIER_TABLE = 'suppliers'
SUPPLIER_CODE_PREFIX = 'SUP-'

SUPPLIER_STATUS_LABELS = {0: '禁用', 1: '启用', 2: '黑名单'}
SUPPLIER_LEVEL_LABELS = {1: '普通', 2: '重要', 3: '核心'}

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


def _table_has_column(table_name, column_name):
    return column_name in _get_table_columns(table_name)


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


def _generate_supplier_code():
    year = datetime.now().year
    prefix = f'{SUPPLIER_CODE_PREFIX}{year}-'
    start_pos = len(prefix) + 1
    sql = f"""
        SELECT MAX(CAST(SUBSTRING(supplier_code, {start_pos}) AS UNSIGNED)) AS max_seq
        FROM {SUPPLIER_TABLE}
        WHERE supplier_code LIKE :prefix_like
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
        elif isinstance(v, datetime):
            out[k] = v.strftime('%Y-%m-%d %H:%M:%S')
    return out


def _build_supplier_list_filters():
    clauses = []
    params = {}

    keyword = request.args.get('keyword')
    if keyword is not None and str(keyword).strip():
        kw = f"%{str(keyword).strip()}%"
        clauses.append(
            '(s.supplier_code LIKE :keyword OR s.supplier_name LIKE :keyword '
            'OR s.short_name LIKE :keyword)',
        )
        params['keyword'] = kw

    supplier_code = request.args.get('supplier_code')
    if supplier_code is not None and str(supplier_code).strip():
        clauses.append('s.supplier_code LIKE :supplier_code')
        params['supplier_code'] = f"%{str(supplier_code).strip()}%"

    supplier_name = request.args.get('supplier_name')
    if supplier_name is not None and str(supplier_name).strip():
        clauses.append('s.supplier_name LIKE :supplier_name')
        params['supplier_name'] = f"%{str(supplier_name).strip()}%"

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
        clauses.append(
            '(s.phone LIKE :phone OR s.mobile LIKE :phone)',
        )
        params['phone'] = phone_kw

    for field in ('status', 'supplier_level'):
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
            'EXISTS (SELECT 1 FROM suppliers sup '
            'WHERE sup.id = s.id AND sup.namespace_id = :namespace_id)',
        )

    extra = ''
    if clauses:
        extra = ' AND ' + ' AND '.join(clauses)
    return extra, params, None


@purchase_app.route('/purchase/suppliers/list', methods=['GET'])
def get_suppliers_list():
    """供应商列表（数据源 v_suppliers_info）。"""
    try:
        page = request.args.get('page', default=1, type=int)
        page_size = request.args.get('page_size', default=100, type=int)
        if page < 1:
            return jsonify({"code": 400, "msg": "page 必须 >= 1"}), 400
        if page_size < 1 or page_size > 500:
            return jsonify({"code": 400, "msg": "page_size 必须在 1~500 之间"}), 400

        filter_extra, filter_params, err = _build_supplier_list_filters()
        if err:
            return jsonify({"code": 400, "msg": err}), 400

        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM v_suppliers_info s
            WHERE 1=1
            {filter_extra}
        """
        count_res = current_app.db_manager.execute_query(count_sql, filter_params)
        total = int(count_res[0].get('total', 0)) if count_res else 0

        offset = (page - 1) * page_size
        sql = f"""
            SELECT
                s.id, s.supplier_code, s.supplier_name, s.short_name,
                s.contact_person, s.phone, s.mobile, s.email,
                s.province, s.city, s.district, s.address,
                s.bank_name, s.bank_account, s.tax_number,
                s.status, s.supplier_level, s.payment_terms, s.credit_limit,
                s.remark, s.created_at, s.created_by, s.updated_at, s.updated_by
            FROM v_suppliers_info s
            WHERE 1=1
            {filter_extra}
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT :limit OFFSET :offset
        """
        page_params = {**filter_params, 'limit': page_size, 'offset': offset}
        rows = current_app.db_manager.execute_query(sql, page_params) or []
        safe_rows = [_json_safe_row(r) for r in rows]

        return jsonify({
            "code": 200,
            "msg": "success",
            "data": safe_rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        })
    except Exception as e:
        logger.error('获取供应商列表失败: %s', e, exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"获取供应商列表失败: {str(e)}",
        }), 500


@purchase_app.route('/purchase/suppliers/create', methods=['POST'])
def create_supplier():
    """新增供应商（写入 suppliers 表，编码自动生成 SUP-年份-序号）。"""
    try:
        body = request.get_json(silent=True) or {}
        supplier_name = str(body.get('supplier_name') or '').strip()
        if not supplier_name:
            return jsonify({'code': 400, 'msg': '供应商名称不能为空'}), 400
        if len(supplier_name) > 200:
            return jsonify({'code': 400, 'msg': '供应商名称不能超过 200 个字符'}), 400

        user_id = body.get('user_id')
        operator = ''
        if user_id is not None and str(user_id).strip() != '':
            try:
                operator = _resolve_user_display_name(int(user_id))
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'user_id 非法'}), 400

        status = body.get('status', 1)
        supplier_level = body.get('supplier_level', 1)
        try:
            status = int(status)
            supplier_level = int(supplier_level)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': 'status / supplier_level 必须为整数'}), 400
        if status not in SUPPLIER_STATUS_LABELS:
            return jsonify({'code': 400, 'msg': 'status 取值无效'}), 400
        if supplier_level not in SUPPLIER_LEVEL_LABELS:
            return jsonify({'code': 400, 'msg': 'supplier_level 取值无效'}), 400

        credit_limit = body.get('credit_limit')
        if credit_limit is not None and str(credit_limit).strip() != '':
            try:
                credit_limit = Decimal(str(credit_limit).replace(',', '').strip())
            except Exception:
                return jsonify({'code': 400, 'msg': 'credit_limit 格式无效'}), 400
        else:
            credit_limit = None

        supplier_code = _generate_supplier_code()
        dup = current_app.db_manager.execute_query(
            f'SELECT id FROM {SUPPLIER_TABLE} WHERE supplier_code = :code LIMIT 1',
            {'code': supplier_code},
        )
        if dup:
            supplier_code = _generate_supplier_code()

        namespace_id = body.get('namespace_id')
        ns_val = None
        if namespace_id is not None and str(namespace_id).strip() != '':
            try:
                ns_val = int(namespace_id)
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'namespace_id 非法'}), 400

        row_data = {
            'supplier_code': supplier_code,
            'supplier_name': supplier_name,
            'short_name': str(body.get('short_name') or '').strip() or None,
            'contact_person': str(body.get('contact_person') or '').strip() or None,
            'phone': str(body.get('phone') or '').strip() or None,
            'mobile': str(body.get('mobile') or '').strip() or None,
            'email': str(body.get('email') or '').strip() or None,
            'province': str(body.get('province') or '').strip() or None,
            'city': str(body.get('city') or '').strip() or None,
            'district': str(body.get('district') or '').strip() or None,
            'address': str(body.get('address') or '').strip() or None,
            'bank_name': str(body.get('bank_name') or '').strip() or None,
            'bank_account': str(body.get('bank_account') or '').strip() or None,
            'tax_number': str(body.get('tax_number') or '').strip() or None,
            'status': status,
            'supplier_level': supplier_level,
            'payment_terms': str(body.get('payment_terms') or '').strip() or None,
            'credit_limit': credit_limit,
            'remark': str(body.get('remark') or '').strip() or None,
            'created_by': operator or None,
            'updated_by': operator or None,
        }
        if ns_val is not None:
            row_data['namespace_id'] = ns_val
        row = _pick_row_for_table(SUPPLIER_TABLE, row_data)
        if 'supplier_name' not in row:
            return jsonify({'code': 500, 'msg': '供应商表缺少 supplier_name 列'}), 500

        new_id = current_app.db_manager.insert_one(SUPPLIER_TABLE, row)
        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': {
                'id': new_id,
                'supplier_code': supplier_code,
            },
        })
    except Exception as e:
        logger.error('新增供应商失败: %s', e, exc_info=True)
        return jsonify({
            'code': 500,
            'msg': f'新增供应商失败: {str(e)}',
        }), 500


def _parse_supplier_body_fields(body, *, require_name=False, partial=False):
    """解析创建/更新请求体中的供应商字段，返回 (row_data, error_msg)。"""
    if not isinstance(body, dict):
        body = {}

    row_data = {}

    if 'supplier_name' in body or require_name:
        supplier_name = str(body.get('supplier_name') or '').strip()
        if require_name and not supplier_name:
            return None, '供应商名称不能为空'
        if supplier_name and len(supplier_name) > 200:
            return None, '供应商名称不能超过 200 个字符'
        if supplier_name or require_name:
            row_data['supplier_name'] = supplier_name

    for field in ('status', 'supplier_level'):
        if partial and field not in body:
            continue
        raw = body.get(field, 1 if field == 'status' else 1)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None, f'{field} 必须为整数'
        labels = SUPPLIER_STATUS_LABELS if field == 'status' else SUPPLIER_LEVEL_LABELS
        if val not in labels:
            return None, f'{field} 取值无效'
        row_data[field] = val

    if 'credit_limit' in body or not partial:
        credit_limit = body.get('credit_limit')
        if credit_limit is not None and str(credit_limit).strip() != '':
            try:
                row_data['credit_limit'] = Decimal(str(credit_limit).replace(',', '').strip())
            except Exception:
                return None, 'credit_limit 格式无效'
        elif 'credit_limit' in body:
            row_data['credit_limit'] = None

    str_fields = (
        'short_name', 'contact_person', 'phone', 'mobile', 'fax', 'email',
        'qq', 'wechat', 'province', 'city', 'district', 'address', 'zip_code',
        'bank_name', 'bank_account', 'tax_number', 'payment_terms', 'remark',
    )
    for field in str_fields:
        if partial and field not in body:
            continue
        raw = body.get(field)
        row_data[field] = str(raw or '').strip() or None

    return row_data, None


@purchase_app.route('/purchase/suppliers/update', methods=['POST'])
def update_supplier():
    """更新供应商（按 id，仅更新请求体中出现的可写字段）。"""
    try:
        body = request.get_json(silent=True) or {}
        record_id = body.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400

        exists = current_app.db_manager.execute_query(
            f'SELECT id FROM {SUPPLIER_TABLE} WHERE id = :id LIMIT 1',
            {'id': record_id},
        )
        if not exists:
            return jsonify({'code': 404, 'msg': '未找到供应商'}), 404

        user_id = body.get('user_id')
        operator = ''
        if user_id is not None and str(user_id).strip() != '':
            try:
                operator = _resolve_user_display_name(int(user_id))
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'user_id 非法'}), 400

        row_data, err = _parse_supplier_body_fields(body, require_name=False, partial=True)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400
        if not row_data:
            return jsonify({'code': 400, 'msg': '没有可更新的字段'}), 400

        if operator:
            row_data['updated_by'] = operator

        row = _pick_row_for_table(SUPPLIER_TABLE, row_data)
        if not row:
            return jsonify({'code': 400, 'msg': '没有可更新的字段'}), 400

        current_app.db_manager.update_by_id(SUPPLIER_TABLE, int(record_id), row)
        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': {'id': int(record_id)},
        })
    except Exception as e:
        logger.error('更新供应商失败: %s', e, exc_info=True)
        return jsonify({
            'code': 500,
            'msg': f'更新供应商失败: {str(e)}',
        }), 500


@purchase_app.route('/purchase/suppliers/delete', methods=['POST'])
def delete_supplier():
    """删除供应商（物理删除）。业务结果统一 HTTP 200 + code 字段，避免前端 axios 误判。"""
    try:
        body = request.get_json(silent=True) or {}
        record_id = body.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'})

        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': 'id 非法'})

        namespace_id = body.get('namespace_id')
        params = {'id': record_id}
        ns_extra = ''
        if namespace_id is not None and str(namespace_id).strip() != '':
            try:
                params['namespace_id'] = int(namespace_id)
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'namespace_id 非法'})
            ns_extra = (
                ' AND EXISTS (SELECT 1 FROM suppliers sup '
                'WHERE sup.id = s.id AND sup.namespace_id = :namespace_id)'
            )

        exists = current_app.db_manager.execute_query(
            f'SELECT s.id FROM v_suppliers_info s WHERE s.id = :id{ns_extra} LIMIT 1',
            params,
        )
        if not exists:
            return jsonify({'code': 404, 'msg': '未找到供应商或无权删除'})

        try:
            current_app.db_manager.execute_update(
                'DELETE FROM suppliers_namespace_authorize WHERE supplier_id = :id',
                {'id': record_id},
            )
        except Exception:
            pass

        del_sql = f'DELETE FROM {SUPPLIER_TABLE} WHERE id = :id'
        del_params = {'id': record_id}
        if 'namespace_id' in params:
            del_sql += ' AND namespace_id = :namespace_id'
            del_params['namespace_id'] = params['namespace_id']

        affected = current_app.db_manager.execute_update(del_sql, del_params)
        if affected <= 0:
            return jsonify({'code': 404, 'msg': '未找到供应商或无权删除'})
        return jsonify({'code': 200, 'msg': '删除成功'})
    except Exception as e:
        logger.error('删除供应商失败: %s', e, exc_info=True)
        return jsonify({
            'code': 500,
            'msg': f'删除供应商失败: {str(e)}',
        }), 500


@purchase_app.route('/purchase/suppliers/detail', methods=['GET'])
def get_supplier_detail():
    """单条供应商（按 id）。"""
    try:
        record_id = request.args.get('id')
        if record_id is None or str(record_id).strip() == '':
            return jsonify({"code": 400, "msg": "缺少必要参数 id"}), 400

        sql = """
            SELECT s.*, sup.namespace_id
            FROM v_suppliers_info s
            LEFT JOIN suppliers sup ON sup.id = s.id
            WHERE s.id = :id
            LIMIT 1
        """
        rows = current_app.db_manager.execute_query(sql, {'id': record_id})
        if not rows:
            return jsonify({"code": 404, "msg": "未找到供应商"}), 404
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": _json_safe_row(rows[0]),
        })
    except Exception as e:
        logger.error('获取供应商详情失败: %s', e, exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"获取供应商详情失败: {str(e)}",
        }), 500

# -*- coding: utf-8 -*-
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import Blueprint, request, jsonify, current_app

logger = logging.getLogger(__name__)

fin_app = Blueprint('fin', __name__, url_prefix='/fin')

# 报销明细：读走视图（已过滤 is_deleted = 0），写走基表
REIMBURSEMENT_VIEW = 'v_fin_reimbursement_detail'
REIMBURSEMENT_TABLE = 'fin_reimbursement_detail'

# 费用类型可选值
REIMBURSEMENT_EXPENSE_TYPE_VALUES = {
    '交通', '餐饮', '住宿', '办公用品', '差旅', '招待', '维修', '活动物料', '其他',
}
# 发票类型可选值
REIMBURSEMENT_INVOICE_TYPE_VALUES = {'增值税专票', '普票', '电子票', '无票'}

# 审批状态
REIMBURSEMENT_APPROVAL_STATUS_VALUES = {'待审批', '已通过', '已驳回', '已支付'}
# 支付状态
REIMBURSEMENT_PAYMENT_STATUS_VALUES = {'待支付', '已支付'}
# 支付方式
REIMBURSEMENT_PAYMENT_METHOD_VALUES = {'银行转账', '现金', '微信', '支付宝'}

PENDING_APPROVAL_STATUS = '待审批'

# 可写入字段白名单（新增）
REIMBURSEMENT_WRITABLE_FIELDS = {
    'namespace_id', 'applicant', 'applicant_dept', 'apply_date',
    'expense_date', 'expense_type', 'expense_amount', 'expense_currency',
    'expense_desc', 'expense_remark', 'tax_rate', 'tax_amount',
    'amount_without_tax', 'invoice_type', 'invoice_no', 'invoice_code',
    'invoice_date', 'is_invoice_received', 'attachment_ids', 'remark',
    'related_flow_no', 'created_by',
}

# 可更新字段白名单（审批/支付/流水号等受控字段不可改）
REIMBURSEMENT_UPDATABLE_FIELDS = {
    'applicant', 'applicant_dept', 'apply_date',
    'expense_date', 'expense_type', 'expense_amount', 'expense_desc',
    'expense_remark', 'invoice_type', 'invoice_no', 'invoice_code',
    'invoice_date', 'is_invoice_received', 'attachment_ids', 'remark',
    'related_flow_no',
}

# 受控字段：即便前端传入也不允许更新
REIMBURSEMENT_LOCKED_FIELDS = {
    'id',
    'detail_no',
    'approver',
    'approval_status',
    'approval_time',
    'approval_remark',
    'payment_status',
    'payment_time',
    'payment_method',
    'payment_account',
    'created_by',
    'created_at',
    'updated_by',
    'updated_at',
    'is_deleted',
}

# 日期字段
REIMBURSEMENT_DATE_FIELDS = ('apply_date', 'expense_date', 'invoice_date')
# 日期时间字段
REIMBURSEMENT_DATETIME_FIELDS = (
    'approval_time', 'payment_time', 'created_at', 'updated_at',
)

# 明细流水号前缀
REIMBURSEMENT_DETAIL_NO_PREFIX = 'RD'


def _normalize_datetime(value, date_only=False):
    """把各种日期格式统一成 MySQL 可接受的格式"""
    if value in (None, ''):
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y-%m-%d') if date_only else value.strftime('%Y-%m-%d %H:%M:%S')

    text = str(value).strip()
    # RFC 2822，如 'Tue, 12 Mar 1985 00:00:00 GMT'
    try:
        parsed = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            return None

    return parsed.strftime('%Y-%m-%d') if date_only else parsed.strftime('%Y-%m-%d %H:%M:%S')


def _parse_date_only_value(raw):
    """解析日期值（date 列），失败返回 None。"""
    if raw in (None, ''):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d'):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).date()
    except ValueError:
        return None


def _normalize_decimal_2(value, default_value='0.00'):
    """统一金额为两位小数字符串，非法值返回 default_value。"""
    if value is None:
        return default_value
    text = str(value).strip().replace(',', '').replace('，', '').replace('￥', '').replace('¥', '')
    if not text:
        return default_value
    try:
        return str(Decimal(text).quantize(Decimal('0.00'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return default_value


def _json_safe_row(row):
    """Decimal / datetime 等类型转为可 JSON 序列化的值。"""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for key, value in list(out.items()):
        if isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(value, date):
            out[key] = value.strftime('%Y-%m-%d')
    return out


def _format_row_datetimes(row):
    """把查询结果里的日期时间字段格式化为字符串。"""
    if not isinstance(row, dict):
        return row
    for field in REIMBURSEMENT_DATE_FIELDS + REIMBURSEMENT_DATETIME_FIELDS:
        if field in row and row[field] is not None:
            row[field] = _normalize_datetime(row[field], date_only=(field in REIMBURSEMENT_DATE_FIELDS))
    return row


def _generate_reimbursement_detail_no():
    """明细流水号：RD + 年月日(8位) + 4位序号。"""
    date_str = datetime.now().strftime('%Y%m%d')
    prefix = f'{REIMBURSEMENT_DETAIL_NO_PREFIX}{date_str}'
    rows = current_app.db_manager.execute_query(
        """
        SELECT MAX(CAST(SUBSTRING(detail_no, 11, 4) AS UNSIGNED)) AS max_seq
        FROM fin_reimbursement_detail
        WHERE detail_no LIKE :prefix_like
          AND CHAR_LENGTH(detail_no) = 14
          AND (is_deleted = 0 OR is_deleted IS NULL)
        """,
        {'prefix_like': prefix + '%'},
    )
    max_seq = 0
    if rows and rows[0].get('max_seq') is not None:
        try:
            max_seq = int(rows[0]['max_seq'])
        except (TypeError, ValueError):
            max_seq = 0

    next_seq = max_seq + 1
    if next_seq > 9999:
        raise ValueError(f'当日报销明细序号已用尽（{date_str}）')
    return f'{prefix}{next_seq:04d}'


def _build_reimbursement_list_filters():
    """列表筛选条件（GET 查询参数）。"""
    params = {}
    clauses = []

    keyword = request.args.get('keyword')
    if keyword and str(keyword).strip():
        params['keyword'] = f"%{str(keyword).strip()}%"
        clauses.append(
            '(applicant LIKE :keyword OR detail_no LIKE :keyword '
            'OR expense_desc LIKE :keyword OR expense_remark LIKE :keyword '
            'OR invoice_no LIKE :keyword OR related_flow_no LIKE :keyword)',
        )

    applicant = request.args.get('applicant')
    if applicant and str(applicant).strip():
        params['applicant'] = f"%{str(applicant).strip()}%"
        clauses.append('applicant LIKE :applicant')

    for field in ('expense_type', 'approval_status', 'payment_status'):
        raw = request.args.get(field)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        clauses.append(f'{field} = :{field}')
        params[field] = value

    related_flow_no = request.args.get('related_flow_no')
    if related_flow_no and str(related_flow_no).strip():
        params['related_flow_no'] = str(related_flow_no).strip()
        clauses.append('related_flow_no = :related_flow_no')

    related_flow_no_empty = request.args.get('related_flow_no_empty')
    if related_flow_no_empty and str(related_flow_no_empty).strip().lower() in ('1', 'true', 'yes'):
        clauses.append("(related_flow_no IS NULL OR TRIM(related_flow_no) = '')")

    expense_from = request.args.get('expense_date_from')
    if expense_from and str(expense_from).strip():
        params['expense_date_from'] = _parse_date_only_value(expense_from)
        clauses.append('expense_date >= :expense_date_from')

    expense_to = request.args.get('expense_date_to')
    if expense_to and str(expense_to).strip():
        params['expense_date_to'] = _parse_date_only_value(expense_to)
        clauses.append('expense_date <= :expense_date_to')

    where_sql = ' AND '.join(clauses) if clauses else ''
    return where_sql, params


# 列表排序：审批 已驳回→待审批→已通过→已支付；支付 待支付→已支付；费用日期降序
REIMBURSEMENT_LIST_ORDER_BY = """
    ORDER BY
        FIELD(approval_status, '已驳回', '待审批', '已通过', '已支付') ASC,
        FIELD(payment_status, '待支付', '已支付') ASC,
        expense_date DESC,
        id DESC
"""


@fin_app.route('/reimbursement-details/list', methods=['GET'])
@fin_app.route('/reimbursement_details/list', methods=['GET'])
def get_reimbursement_detail_list():
    """获取报销明细列表（从视图 v_fin_reimbursement_detail 取数）"""
    namespace_id = request.args.get('namespace_id')
    if not namespace_id:
        return jsonify({'message': '缺少 namespace_id'}), 400

    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 20, type=int)
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 500:
        page_size = 20

    where_sql, filter_params = _build_reimbursement_list_filters()
    params = {'namespace_id': namespace_id, **filter_params}
    where_clause = f' AND {where_sql}' if where_sql else ''

    total = current_app.db_manager.execute_query(
        f"SELECT COUNT(*) as total FROM {REIMBURSEMENT_VIEW} "
        f"WHERE namespace_id = :namespace_id{where_clause}",
        params,
    )[0]['total']

    params['limit'], params['offset'] = page_size, (page - 1) * page_size
    rows = current_app.db_manager.execute_query(
        f"SELECT * FROM {REIMBURSEMENT_VIEW} "
        f"WHERE namespace_id = :namespace_id{where_clause} "
        f"{REIMBURSEMENT_LIST_ORDER_BY.strip()} "
        f"LIMIT :limit OFFSET :offset",
        params,
    )

    safe_rows = [_json_safe_row(_format_row_datetimes(row)) for row in rows]

    return jsonify({
        'result': {
            'list': safe_rows,
            'total': total,
            'page': page,
            'pageSize': page_size,
        }
    })


@fin_app.route('/reimbursement-details/detail/<int:id>', methods=['GET'])
@fin_app.route('/reimbursement_details/get', methods=['GET'])
def get_reimbursement_detail_detail(id=None):
    """获取报销明细详情"""
    if id is None:
        record_id = request.args.get('id')
        if not record_id:
            return jsonify({'message': '缺少 id'}), 400
        id = int(record_id)

    rows = current_app.db_manager.execute_query(
        f"SELECT * FROM {REIMBURSEMENT_VIEW} WHERE id = :id LIMIT 1",
        {'id': id},
    )
    if not rows:
        return jsonify({'message': '报销明细不存在'}), 404
    return jsonify({'result': _json_safe_row(_format_row_datetimes(rows[0]))})


def _normalize_reimbursement_create_data(data):
    """校验并规范化新增报销明细数据，返回 (row, error_message)。"""
    if not isinstance(data, dict):
        return None, '请提供报销明细数据'

    applicant = str(data.get('applicant') or '').strip()
    if not applicant:
        return None, '缺少必填字段: applicant'

    apply_date = _parse_date_only_value(data.get('apply_date'))
    if not apply_date:
        return None, 'apply_date 格式无效'

    expense_date = _parse_date_only_value(data.get('expense_date'))
    if not expense_date:
        return None, 'expense_date 格式无效'

    expense_type = str(data.get('expense_type') or '').strip()
    if expense_type not in REIMBURSEMENT_EXPENSE_TYPE_VALUES:
        return None, 'expense_type 取值不在允许范围内'

    amount_str = _normalize_decimal_2(data.get('expense_amount'), '')
    if not amount_str:
        return None, '缺少必填字段: expense_amount'
    try:
        expense_amount = Decimal(amount_str)
    except (InvalidOperation, ValueError):
        return None, 'expense_amount 格式无效'
    if expense_amount <= 0:
        return None, 'expense_amount 必须大于 0'

    invoice_type = str(data.get('invoice_type') or '').strip()
    if invoice_type and invoice_type not in REIMBURSEMENT_INVOICE_TYPE_VALUES:
        return None, 'invoice_type 取值不在允许范围内'

    invoice_date = _parse_date_only_value(data.get('invoice_date'))
    if data.get('invoice_date') and not invoice_date:
        return None, 'invoice_date 格式无效'

    is_invoice_received = data.get('is_invoice_received')
    if is_invoice_received in (True, 'true', '1', 1):
        is_invoice_received = 1
    else:
        is_invoice_received = 0

    row = {
        'applicant': applicant,
        'apply_date': apply_date,
        'expense_date': expense_date,
        'expense_type': expense_type,
        'expense_amount': expense_amount,
        'expense_currency': str(data.get('expense_currency') or 'CNY').strip() or 'CNY',
        'approval_status': PENDING_APPROVAL_STATUS,
        'payment_status': '待支付',
        'is_invoice_received': is_invoice_received,
        'tax_rate': Decimal(_normalize_decimal_2(data.get('tax_rate'), '0.00')),
        'tax_amount': Decimal(_normalize_decimal_2(data.get('tax_amount'), '0.00')),
        'amount_without_tax': Decimal(_normalize_decimal_2(data.get('amount_without_tax'), '0.00')),
        'created_by': str(data.get('created_by') or applicant).strip() or applicant,
        'is_deleted': 0,
    }

    for key in ('applicant_dept', 'expense_desc', 'expense_remark', 'remark',
                'invoice_type', 'invoice_no', 'invoice_code', 'attachment_ids',
                'related_flow_no'):
        value = data.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            row[key] = value

    if invoice_date:
        row['invoice_date'] = invoice_date

    return row, None


@fin_app.route('/reimbursement-details/create', methods=['POST'])
@fin_app.route('/reimbursement_details/create', methods=['POST'])
def create_reimbursement_detail():
    """新增报销明细"""
    data = request.get_json(silent=True) or {}
    namespace_id = data.get('namespace_id') or request.args.get('namespace_id')
    if not namespace_id:
        return jsonify({'message': '缺少 namespace_id'}), 400

    row, err = _normalize_reimbursement_create_data(data.get('data') or data)
    if err:
        return jsonify({'message': err}), 400

    row['namespace_id'] = namespace_id

    try:
        row['detail_no'] = _generate_reimbursement_detail_no()
    except ValueError as ve:
        return jsonify({'message': str(ve)}), 400

    dup = current_app.db_manager.execute_query(
        f"SELECT id FROM {REIMBURSEMENT_TABLE} "
        f"WHERE detail_no = :detail_no AND (is_deleted = 0 OR is_deleted IS NULL) LIMIT 1",
        {'detail_no': row['detail_no']},
    )
    if dup:
        return jsonify({'message': '明细流水号冲突，请重试'}), 400

    try:
        new_id = current_app.db_manager.insert_one(REIMBURSEMENT_TABLE, row)
    except Exception as exc:
        logger.error('新增报销明细失败: %s', exc, exc_info=True)
        return jsonify({'message': f'新增报销明细失败: {exc}'}), 500

    return jsonify({'result': {'id': new_id, 'detail_no': row['detail_no']}}), 201


def _normalize_reimbursement_update_data(data):
    """校验报销明细可更新字段，返回 (normalized, error_message)。"""
    if not isinstance(data, dict) or not data:
        return None, '没有有效字段需要更新'

    normalized = {}
    for key, value in data.items():
        if key in REIMBURSEMENT_LOCKED_FIELDS:
            return None, f'字段 {key} 不允许更新'
        if key not in REIMBURSEMENT_UPDATABLE_FIELDS:
            return None, f'字段 {key} 不允许更新'
        normalized[key] = value

    if 'applicant' in normalized:
        applicant = str(normalized['applicant'] or '').strip()
        if not applicant:
            return None, 'applicant 不能为空'
        normalized['applicant'] = applicant

    for key in REIMBURSEMENT_DATE_FIELDS:
        if key not in normalized:
            continue
        if normalized[key] in (None, ''):
            if key == 'invoice_date':
                normalized[key] = None
            else:
                return None, f'{key} 不能为空'
            continue
        parsed = _parse_date_only_value(normalized[key])
        if parsed is None:
            return None, f'{key} 格式无效'
        normalized[key] = parsed

    if 'expense_type' in normalized:
        expense_type = str(normalized['expense_type'] or '').strip()
        if expense_type not in REIMBURSEMENT_EXPENSE_TYPE_VALUES:
            return None, 'expense_type 取值不在允许范围内'
        normalized['expense_type'] = expense_type

    if 'expense_amount' in normalized:
        amount_str = _normalize_decimal_2(normalized['expense_amount'], '')
        if not amount_str:
            return None, 'expense_amount 不能为空'
        try:
            expense_amount = Decimal(amount_str)
        except (InvalidOperation, ValueError):
            return None, 'expense_amount 格式无效'
        if expense_amount <= 0:
            return None, 'expense_amount 必须大于 0'
        normalized['expense_amount'] = expense_amount

    if 'invoice_type' in normalized:
        invoice_type = str(normalized['invoice_type'] or '').strip()
        if invoice_type and invoice_type not in REIMBURSEMENT_INVOICE_TYPE_VALUES:
            return None, 'invoice_type 取值不在允许范围内'
        normalized['invoice_type'] = invoice_type or None

    if 'is_invoice_received' in normalized:
        flag = normalized['is_invoice_received']
        normalized['is_invoice_received'] = 1 if flag in (True, 'true', '1', 1) else 0

    for key in ('applicant_dept', 'expense_desc', 'expense_remark', 'remark',
                'invoice_no', 'invoice_code', 'attachment_ids', 'related_flow_no'):
        if key not in normalized:
            continue
        value = normalized[key]
        if value is None or str(value).strip() == '':
            normalized[key] = None
        else:
            normalized[key] = str(value).strip()

    # 未取得发票时清空发票相关字段
    if normalized.get('is_invoice_received') == 0:
        for key in ('invoice_type', 'invoice_no', 'invoice_code', 'invoice_date', 'attachment_ids'):
            normalized[key] = None

    return normalized, None


@fin_app.route('/reimbursement-details/update/<int:id>', methods=['PUT', 'POST'])
@fin_app.route('/reimbursement_details/update', methods=['POST'])
def update_reimbursement_detail(id=None):
    """更新报销明细（禁止改流水号、审批与支付字段）"""
    payload = request.get_json(silent=True) or {}
    if id is None:
        record_id = payload.get('id') or request.args.get('id')
        if record_id in (None, ''):
            return jsonify({'message': '缺少 id'}), 400
        id = int(record_id)

    data = payload.get('data')
    if data is None:
        data = {k: v for k, v in payload.items() if k not in ('id', 'namespace_id')}

    update_data, err = _normalize_reimbursement_update_data(data)
    if err:
        return jsonify({'message': err}), 400

    rows = current_app.db_manager.execute_query(
        f"SELECT id FROM {REIMBURSEMENT_TABLE} "
        f"WHERE id = :id AND (is_deleted = 0 OR is_deleted IS NULL) LIMIT 1",
        {'id': id},
    )
    if not rows:
        return jsonify({'message': '报销明细不存在'}), 404

    set_clauses = [f'{field} = :{field}' for field in update_data]
    params = {**update_data, 'id': id, 'updated_at': datetime.now()}
    set_clauses.append('updated_at = :updated_at')

    try:
        affected_rows = current_app.db_manager.execute_update(
            f"UPDATE {REIMBURSEMENT_TABLE} SET {', '.join(set_clauses)} "
            f"WHERE id = :id AND (is_deleted = 0 OR is_deleted IS NULL)",
            params,
        )
    except Exception as exc:
        logger.error('更新报销明细失败: %s', exc, exc_info=True)
        return jsonify({'message': f'更新报销明细失败: {exc}'}), 500

    if affected_rows == 0:
        return jsonify({'message': '报销明细不存在'}), 404

    return jsonify({'result': {'id': id, 'affected_rows': affected_rows}})


@fin_app.route('/reimbursement-details/delete/<int:id>', methods=['DELETE', 'POST'])
@fin_app.route('/reimbursement_details/delete', methods=['POST'])
def delete_reimbursement_detail(id=None):
    """删除报销明细（软删除）"""
    payload = request.get_json(silent=True) or {}
    if id is None:
        record_id = payload.get('id') or request.args.get('id')
        if record_id in (None, ''):
            return jsonify({'message': '缺少 id'}), 400
        id = int(record_id)

    try:
        rowcount = current_app.db_manager.execute_update(
            f"UPDATE {REIMBURSEMENT_TABLE} SET is_deleted = 1, updated_at = :updated_at "
            f"WHERE id = :id AND (is_deleted = 0 OR is_deleted IS NULL)",
            {'id': id, 'updated_at': datetime.now()},
        )
    except Exception as exc:
        logger.error('删除报销明细失败: %s', exc, exc_info=True)
        return jsonify({'message': f'删除报销明细失败: {exc}'}), 500

    if rowcount == 0:
        return jsonify({'message': '报销明细不存在或已删除'}), 404
    return jsonify({'result': {'id': id}})


__all__ = ['fin_app']

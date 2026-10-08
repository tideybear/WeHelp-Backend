# -*- coding: utf-8 -*-
from flask import Blueprint, request, jsonify, current_app
import logging
import os
import uuid
import csv
from datetime import datetime, date, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from werkzeug.utils import secure_filename

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建蓝图
fin_app = Blueprint('fin', __name__)

BANK_STATEMENT_COLUMNS = [
    'voucher_no',
    'voucher_type',
    'our_account_no',
    'counterparty_account_no',
    'counterparty_bank_no',
    'counterparty_bank_name',
    'counterparty_name',
    'group_sub_account',
    'transaction_time',
    'direction',
    'debit_amount',
    'credit_amount',
    'balance',
    'abstract_text',
    'purpose',
    'personalized_info',
]

BANK_STATEMENT_META_COLUMNS = ['bank_name', 'import_source']

BANK_FORMAT_ICBC = 'icbc'
BANK_FORMAT_WZBANK = 'wzbank'

BANK_FORMAT_LABELS = {
    BANK_FORMAT_ICBC: '工商银行',
    BANK_FORMAT_WZBANK: '温州银行',
}

COLUMN_ALIAS_MAP = {
    'voucher_no': 'voucher_no',
    '凭证号': 'voucher_no',
    '凭证号码': 'voucher_no',
    'voucher_type': 'voucher_type',
    '凭证种类': 'voucher_type',
    'our_account_no': 'our_account_no',
    '本方账号': 'our_account_no',
    'counterparty_account_no': 'counterparty_account_no',
    '对方账号': 'counterparty_account_no',
    'counterparty_bank_no': 'counterparty_bank_no',
    '对方行号': 'counterparty_bank_no',
    'counterparty_bank_name': 'counterparty_bank_name',
    '对方行名': 'counterparty_bank_name',
    'counterparty_name': 'counterparty_name',
    '对方单位名称': 'counterparty_name',
    '对方户名': 'counterparty_name',
    'group_sub_account': 'group_sub_account',
    '集团子账户': 'group_sub_account',
    'transaction_time': 'transaction_time',
    '交易时间': 'transaction_time',
    '交易日期': 'transaction_time',
    'direction': 'direction',
    '借/贷方向': 'direction',
    '借/贷': 'direction',
    '借贷方向': 'direction',
    '借贷标志': 'direction',
    '收支方向': 'direction',
    '方向': 'direction',
    'debit_amount': 'debit_amount',
    '借方发生额(元)': 'debit_amount',
    '借方发生额': 'debit_amount',
    'credit_amount': 'credit_amount',
    '贷方发生额(元)': 'credit_amount',
    '贷方发生额': 'credit_amount',
    'balance': 'balance',
    '余额(元)': 'balance',
    '余额': 'balance',
    'abstract_text': 'abstract_text',
    '摘要': 'abstract_text',
    'purpose': 'purpose',
    '用途': 'purpose',
    'personalized_info': 'personalized_info',
    '个性化信息': 'personalized_info',
}

def _normalize_header_key(key):
    if key is None:
        return ""
    s = str(key).strip()
    # 兼容空格、全角标点、大小写等差异
    s = s.replace(" ", "").replace("\u3000", "")
    s = s.replace("（", "(").replace("）", ")")
    return s.lower()

NORMALIZED_ALIAS_MAP = {
    _normalize_header_key(k): v for k, v in COLUMN_ALIAS_MAP.items()
}


def _normalize_cell_value(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, str):
        v = value.strip()
        return v if v else None
    return value

def _normalize_decimal_2(value, default_value="0.00"):
    v = _normalize_cell_value(value)
    if v is None:
        return default_value

    s = str(v).strip()
    if not s:
        return default_value

    # 兼容千分位/全角逗号/货币符号
    s = s.replace(",", "").replace("，", "").replace("￥", "").replace("¥", "")
    try:
        d = Decimal(s).quantize(Decimal("0.00"), rounding=ROUND_HALF_UP)
        return str(d)
    except (InvalidOperation, ValueError, TypeError):
        return default_value


def _parse_csv_rows(file_path):
    with open(file_path, mode='r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        return list(reader)


def _find_bank_header_row_index(rows):
    """跳过标题行，定位含「本方账号」等列名的表头行。"""
    best_idx = 0
    best_score = 0
    for idx, row in enumerate(rows[:30]):
        if not row:
            continue
        score = 0
        for cell in row:
            if _normalize_header_key(cell) in NORMALIZED_ALIAS_MAP:
                score += 1
        if score > best_score:
            best_score = score
            best_idx = idx
    return best_idx if best_score >= 3 else 0


def _parse_excel_rows(file_path):
    try:
        from openpyxl import load_workbook
    except Exception as e:
        raise RuntimeError("缺少依赖 openpyxl，无法解析 Excel 文件") from e

    wb = load_workbook(filename=file_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        wb.close()
        wb = load_workbook(filename=file_path, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    header_idx = _find_bank_header_row_index(rows)
    header = [str(h).strip() if h is not None else "" for h in rows[header_idx]]
    data_rows = []
    for row in rows[header_idx + 1:]:
        if row is None:
            continue
        if all(cell is None or str(cell).strip() == "" for cell in row):
            continue
        row_dict = {}
        for idx, cell in enumerate(row):
            key = header[idx] if idx < len(header) else f"col_{idx}"
            if not key:
                key = f"col_{idx}"
            row_dict[key] = cell
        data_rows.append(row_dict)
    return data_rows


def _headers_from_raw_rows(raw_rows):
    if not raw_rows:
        return set()
    return {_normalize_header_key(k) for k in (raw_rows[0] or {}).keys()}


def _is_wzbank_headers(headers):
    header_set = headers or set()
    if _normalize_header_key('交易日期') in header_set:
        return True
    wz_keys = {
        _normalize_header_key('交易日期'),
        _normalize_header_key('对方户名'),
        _normalize_header_key('凭证号码'),
    }
    return len(header_set & wz_keys) >= 2


def _resolve_bank_format(raw_rows, bank_type=None):
    """bank_type: auto | icbc | wzbank（及中文别名）。表头优先识别温州银行。"""
    headers = _headers_from_raw_rows(raw_rows)
    if _is_wzbank_headers(headers):
        return BANK_FORMAT_WZBANK

    bt = str(bank_type or 'auto').strip().lower()
    if bt in ('wzbank', 'wz', '温州银行', '温州', 'wenzhou'):
        return BANK_FORMAT_WZBANK
    if bt in ('icbc', '工商银行', '工行', 'gongshang'):
        return BANK_FORMAT_ICBC

    icbc_headers = {
        _normalize_header_key('借/贷方向'),
        _normalize_header_key('对方行号'),
        _normalize_header_key('交易时间'),
    }
    if len(headers & icbc_headers) >= 1:
        return BANK_FORMAT_ICBC
    return BANK_FORMAT_ICBC


def _normalize_raw_bank_row(raw):
    normalized = {}
    for raw_key, raw_value in (raw or {}).items():
        key = _normalize_header_key(raw_key)
        mapped_key = NORMALIZED_ALIAS_MAP.get(key)
        if not mapped_key:
            continue
        normalized[mapped_key] = _normalize_cell_value(raw_value)
    return normalized


def _is_wzbank_blank_row(normalized):
    check_keys = (
        'our_account_no',
        'transaction_time',
        'voucher_no',
        'debit_amount',
        'credit_amount',
        'balance',
        'counterparty_account_no',
        'counterparty_name',
    )
    return not any(normalized.get(k) is not None for k in check_keys)


def _is_bank_statement_end_row(normalized, bank_format):
    """温州：整行空白结束；工行：凭证号为空且整行空白结束（避免温州流水误用工行规则）。"""
    if bank_format == BANK_FORMAT_WZBANK:
        return _is_wzbank_blank_row(normalized)
    if normalized.get('voucher_no') is not None:
        return False
    return _is_wzbank_blank_row(normalized)


def _derive_direction_from_amounts(debit_amount, credit_amount):
    try:
        debit = Decimal(str(debit_amount or '0'))
        credit = Decimal(str(credit_amount or '0'))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if debit > 0 and credit <= 0:
        return '借'
    if credit > 0 and debit <= 0:
        return '贷'
    return None


def _ensure_voucher_no(row, row_index, bank_format):
    voucher = row.get('voucher_no')
    if voucher is not None and str(voucher).strip():
        row['voucher_no'] = str(voucher).strip()
        return
    prefix = 'WZ' if bank_format == BANK_FORMAT_WZBANK else 'ICBC'
    row['voucher_no'] = f'{prefix}-{datetime.now().strftime("%Y%m%d")}-{row_index:05d}'


def _build_insert_rows(namespace_id, raw_rows, bank_format=BANK_FORMAT_ICBC):
    now_time = datetime.now()
    insert_rows = []
    skipped = 0
    invalid_rows = []
    bank_label = BANK_FORMAT_LABELS.get(bank_format, '工商银行')

    for row_index, raw in enumerate(raw_rows, start=1):
        normalized = _normalize_raw_bank_row(raw)

        if bank_format == BANK_FORMAT_WZBANK:
            if _is_wzbank_blank_row(normalized):
                continue
        elif _is_bank_statement_end_row(normalized, bank_format):
            break

        if not any(normalized.get(col) is not None for col in BANK_STATEMENT_COLUMNS):
            skipped += 1
            continue

        row = {
            'namespace_id': namespace_id,
            'data_authorize_lv': 1,
            'is_delete': 0,
            'bank_name': bank_label,
            'import_source': bank_format,
            'created_at': now_time,
            'updated_at': now_time,
        }
        for col in BANK_STATEMENT_COLUMNS:
            row[col] = normalized.get(col)

        row['debit_amount'] = _normalize_decimal_2(row.get('debit_amount'))
        row['credit_amount'] = _normalize_decimal_2(row.get('credit_amount'))
        row['balance'] = _normalize_decimal_2(row.get('balance'))

        if row.get('transaction_time') is not None:
            row['transaction_time'] = _format_datetime_value(row.get('transaction_time'))

        if row.get('direction') is None:
            derived = _derive_direction_from_amounts(row.get('debit_amount'), row.get('credit_amount'))
            if derived:
                row['direction'] = derived

        if row.get('direction') is None:
            skipped += 1
            invalid_rows.append(f'第{row_index}行: 无法确定借/贷方向')
            continue

        _ensure_voucher_no(row, row_index, bank_format)
        insert_rows.append(row)

    return insert_rows, skipped, invalid_rows

def _format_datetime_value(value):
    if value is None:
        return None

    # 兼容时间戳：秒/毫秒
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            # 粗略判断毫秒时间戳（>= 2001-09-09 的毫秒级约 1e12）
            if ts >= 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts).strftime('%Y%m%d %H:%M:%S')
        except Exception:
            return value

    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime('%Y-%m-%d 00:00:00')
    if isinstance(value, time):
        return value.strftime('1970-01-01 %H:%M:%S')

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return value

        # 温州银行等：纯日期 20240105 / 2024.01.05
        digits = s.replace('.', '').replace('-', '').replace('/', '')
        if len(digits) == 8 and digits.isdigit():
            return f'{digits[:4]}-{digits[4:6]}-{digits[6:8]} 00:00:00'

        # 兼容常见 ISO 格式：2026-04-27 12:34:56 / 2026-04-27T12:34:56(.xxx)(Z)
        try:
            normalized = s.replace('Z', '+00:00')
            dt = datetime.fromisoformat(normalized)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass

        # 工商银行导出：20260427 12:34:56
        try:
            if len(s) >= 17 and s[8] == ' ':
                date_part = s[:8]
                time_part = s[9:17]
                if date_part.isdigit():
                    return (
                        f'{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]} '
                        f'{time_part}'
                    )
        except Exception:
            pass

        return value

    return value

def _format_row_datetimes(datetime_fields,row: dict):
    if not isinstance(row, dict):
        return row
    for k in datetime_fields:
        if k in row:
            row[k] = _format_datetime_value(row.get(k))
    return row

BANK_STATEMENT_LIST_SORT_COLUMNS = {
    'transaction_time': 'transaction_time',
    'created_at': 'created_at',
    'id': 'id',
}


def _build_bank_statement_list_filters():
    """银行流水列表筛选（GET 查询参数）。"""
    clauses = []
    params = {}

    keyword = request.args.get('keyword')
    if keyword is not None and str(keyword).strip():
        kw = f'%{str(keyword).strip()}%'
        clauses.append(
            '(voucher_no LIKE :keyword OR flow_no LIKE :keyword OR counterparty_name LIKE :keyword '
            'OR counterparty_bank_name LIKE :keyword OR abstract_text LIKE :keyword OR purpose LIKE :keyword '
            'OR our_account_no LIKE :keyword OR counterparty_account_no LIKE :keyword '
            'OR bank_name LIKE :keyword)',
        )
        params['keyword'] = kw

    voucher_no = request.args.get('voucher_no')
    if voucher_no is not None and str(voucher_no).strip():
        clauses.append('voucher_no LIKE :voucher_no')
        params['voucher_no'] = f'%{str(voucher_no).strip()}%'

    counterparty_name = request.args.get('counterparty_name')
    if counterparty_name is not None and str(counterparty_name).strip():
        clauses.append('counterparty_name LIKE :counterparty_name')
        params['counterparty_name'] = f'%{str(counterparty_name).strip()}%'

    direction = request.args.get('direction')
    if direction is not None and str(direction).strip():
        clauses.append('direction = :direction')
        params['direction'] = str(direction).strip()

    our_account_no = request.args.get('our_account_no')
    if our_account_no is not None and str(our_account_no).strip():
        clauses.append('our_account_no LIKE :our_account_no')
        params['our_account_no'] = f'%{str(our_account_no).strip()}%'

    time_from = request.args.get('transaction_time_from')
    if time_from is not None and str(time_from).strip():
        clauses.append('transaction_time >= :transaction_time_from')
        from_val = str(time_from).strip()
        if len(from_val) <= 10:
            from_val = f'{from_val} 00:00:00'
        params['transaction_time_from'] = from_val

    time_to = request.args.get('transaction_time_to')
    if time_to is not None and str(time_to).strip():
        clauses.append('transaction_time <= :transaction_time_to')
        to_val = str(time_to).strip()
        if len(to_val) <= 10:
            to_val = f'{to_val} 23:59:59'
        params['transaction_time_to'] = to_val

    extra = ''
    if clauses:
        extra = ' AND ' + ' AND '.join(clauses)
    return extra, params


def _bank_statement_list_order_clause():
    """默认按交易时间降序；支持 sort_by + sort_order。"""
    sort_by = str(request.args.get('sort_by') or 'transaction_time').strip()
    col = BANK_STATEMENT_LIST_SORT_COLUMNS.get(sort_by, 'transaction_time')
    sort_order = str(request.args.get('sort_order') or 'desc').strip().lower()
    if sort_order not in ('asc', 'desc'):
        sort_order = 'desc'
    return f' ORDER BY {col} {sort_order.upper()}, id DESC'


# 获取银行流水列表，根据userid筛选
@fin_app.route('/fin/bankstatements/list', methods=['GET'])
def get_bankStatementsList():
    try:
        # 获取namespace_id,data_authorize_lv参数
        namespace_id = request.args.get('namespace_id')
        #data_authorize_lv = request.args.get('data_authorize_lv')
        data_authorize_lv = 1
        page = request.args.get('page', default=1, type=int)
        page_size = request.args.get('page_size', default=100, type=int)
        
        if not namespace_id:
           return jsonify({"code": 400, "msg": "缺少必要参数namespace_id"}), 400
        if not data_authorize_lv:
           return jsonify({"code": 400, "msg": "缺少必要参数data_authorize_lv"}), 400

        if page < 1:
            return jsonify({"code": 400, "msg": "page必须>=1"}), 400
        if page_size < 1 or page_size > 500:
            return jsonify({"code": 400, "msg": "page_size必须在1~500之间"}), 400
           
        offset = (page - 1) * page_size
        
        # 构建参数
        params = {"namespace_id": namespace_id, "data_authorize_lv": data_authorize_lv}
        filter_extra, filter_params = _build_bank_statement_list_filters()
        params.update(filter_params)

        flow_no = request.args.get('flow_no')
        if flow_no is not None and str(flow_no).strip() != '':
            filter_extra += ' AND flow_no = :flow_no'
            params['flow_no'] = str(flow_no).strip()
        id_param = request.args.get('id')
        if id_param is not None and str(id_param).strip() != '':
            filter_extra += ' AND id = :id'
            params['id'] = id_param

        order_clause = _bank_statement_list_order_clause()

        # 先查总数
        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM bank_transaction_records
            WHERE namespace_id=:namespace_id
            AND :data_authorize_lv<=data_authorize_lv
            AND is_delete=0
            {filter_extra}
        """
        count_res = current_app.db_manager.execute_query(count_sql, params)
        total = int(count_res[0].get('total', 0)) if count_res else 0

        # 分页查询
        sql = f"""
            SELECT *
            FROM bank_transaction_records
            WHERE namespace_id=:namespace_id
            AND :data_authorize_lv<=data_authorize_lv
            AND is_delete=0
            {filter_extra}
            {order_clause}
            LIMIT :limit OFFSET :offset
        """
        page_params = {**params, "limit": page_size, "offset": offset}
        
        
        # 执行查询
        result = current_app.db_manager.execute_query(sql, page_params)
        if isinstance(result, list):
            datetime_fields = {'created_at', 'updated_at', 'update_at', 'transaction_time'}
            for row in result:
                _format_row_datetimes(datetime_fields,row)
        #logger.info(f"查询结果数量: {len(result)}")
        #logger.info(f"查询结果数量: {result}")
        # 返回结果
        response = jsonify({
           "code": 200, 
           "msg": "success", 
           "data": result,
           "total": total,
           "page": page,
           "page_size": page_size
        })
        
        # 设置响应头
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
        
    except Exception as e:
        logger.error(f"获取项目列表失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500, 
            "msg": f"获取项目列表失败: {str(e)}"
        }), 500


def _json_safe_private_transaction_row(row):
    """Decimal 等类型便于 JSON 序列化。"""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, Decimal):
            out[k] = str(v)
    return out


PRIVATE_ACCOUNT_BALANCE_FUND_STATUSES = ('已收', '已付', '已核销')


def _private_account_balance_decimal_scale():
    """余额小数位，与库表 balance 列精度一致，可用环境变量 PRIVATE_ACCOUNT_BALANCE_DECIMAL_SCALE 覆盖。"""
    try:
        n = int(os.environ.get('PRIVATE_ACCOUNT_BALANCE_DECIMAL_SCALE', '4'))
        return max(0, min(n, 8))
    except (TypeError, ValueError):
        return 4


def _quantize_private_account_balance(value):
    scale = _private_account_balance_decimal_scale()
    quantum = Decimal('1').scaleb(-scale)
    try:
        d = Decimal(str(value if value is not None else '0').replace(',', ''))
    except (InvalidOperation, ValueError, TypeError):
        d = Decimal('0')
    return d.quantize(quantum, rounding=ROUND_HALF_UP)


def _private_account_row_balance_delta(direction, amount):
    """收入 +amount，支出 -amount。"""
    d = str(direction or '').strip()
    try:
        amt = Decimal(str(amount if amount is not None else '0').replace(',', ''))
    except (InvalidOperation, ValueError, TypeError):
        amt = Decimal('0')
    if d == '收入':
        return amt
    if d == '支出':
        return -amt
    return Decimal('0')


_PRIVATE_ACCOUNT_BALANCE_TRIGGER_NAMES = (
    'trg_pat_ai_recalc_balance',
    'trg_pat_au_recalc_balance',
    'trg_pat_ad_recalc_balance',
)


def ensure_private_account_balance_triggers_dropped():
    """
    删除私账 balance 触发器（与 INSERT/UPDATE 同表写回冲突导致 MySQL 1442）。
    应用启动时调用一次；无 DDL 权限时仅记录警告。
    """
    db = current_app.db_manager
    for name in _PRIVATE_ACCOUNT_BALANCE_TRIGGER_NAMES:
        try:
            db.execute_update(f'DROP TRIGGER IF EXISTS {name}', {})
            logger.info('已删除私账触发器（若存在）: %s', name)
        except Exception as e:
            logger.warning('删除私账触发器 %s 失败: %s', name, e)


def _recalc_private_account_balance(namespace_id):
    """
    按空间重算 balance：仅审批已通过且资金已收/已付/已核销参与累加；
    按 business_date、id 正序累加后写回。
    业务上在「资金状态」变更后调用；也可通过 recalc_balance 接口手动全量重算。
    """
    if namespace_id is None or str(namespace_id).strip() == '':
        return

    params = {'namespace_id': namespace_id}
    db = current_app.db_manager
    bal_scale = _private_account_balance_decimal_scale()
    dec_type = f'DECIMAL(20, {bal_scale})'

    clear_sql = """
        UPDATE private_account_transactions
        SET balance = NULL
        WHERE namespace_id = :namespace_id
          AND (is_deleted = 0 OR is_deleted IS NULL)
          AND NOT (
              TRIM(COALESCE(approval_status, '')) = '已通过'
              AND TRIM(COALESCE(fund_status, '')) IN ('已收', '已付', '已核销')
          )
    """
    try:
        db.execute_update(clear_sql, params)
    except Exception as e:
        if '1442' in str(e):
            logger.error(
                '私账余额重算失败(1442)：请执行 backend/sql/private_account_balance_drop_triggers.sql 删除 balance 触发器'
            )
            return
        raise

    window_sql = f"""
        UPDATE private_account_transactions pat
        INNER JOIN (
            SELECT
                id,
                SUM(
                    CASE
                        WHEN TRIM(COALESCE(direction, '')) = '收入'
                        THEN CAST(COALESCE(amount, 0) AS {dec_type})
                        WHEN TRIM(COALESCE(direction, '')) = '支出'
                        THEN -CAST(COALESCE(amount, 0) AS {dec_type})
                        ELSE CAST(0 AS {dec_type})
                    END
                ) OVER (ORDER BY business_date ASC, id ASC) AS running_balance
            FROM private_account_transactions
            WHERE namespace_id = :namespace_id
              AND (is_deleted = 0 OR is_deleted IS NULL)
              AND TRIM(COALESCE(approval_status, '')) = '已通过'
              AND TRIM(COALESCE(fund_status, '')) IN ('已收', '已付', '已核销')
        ) calc ON pat.id = calc.id
        SET pat.balance = CAST(calc.running_balance AS {dec_type})
        WHERE pat.namespace_id = :namespace_id
    """
    try:
        db.execute_update(window_sql, params)
        return
    except Exception as e:
        err_msg = str(e)
        if '1442' in err_msg:
            logger.error(
                '私账余额重算失败(1442)：请执行 backend/sql/private_account_balance_drop_triggers.sql 后重试。'
            )
            return
        logger.warning('窗口函数重算私账余额失败，改用逐行重算: %s', e)

    select_sql = """
        SELECT id, direction, amount
        FROM private_account_transactions
        WHERE namespace_id = :namespace_id
          AND (is_deleted = 0 OR is_deleted IS NULL)
          AND TRIM(COALESCE(approval_status, '')) = '已通过'
          AND TRIM(COALESCE(fund_status, '')) IN ('已收', '已付', '已核销')
        ORDER BY business_date ASC, id ASC
    """
    rows = db.execute_query(select_sql, params) or []
    running = Decimal('0')
    update_sql = """
        UPDATE private_account_transactions
        SET balance = :balance
        WHERE id = :id AND namespace_id = :namespace_id
    """
    for row in rows:
        running += _private_account_row_balance_delta(
            row.get('direction'),
            row.get('amount'),
        )
        bal = _quantize_private_account_balance(running)
        try:
            db.execute_update(update_sql, {
                'balance': str(bal),
                'id': row['id'],
                'namespace_id': namespace_id,
            })
        except Exception as e:
            if '1442' in str(e):
                logger.error(
                    '私账余额重算失败(1442)：请执行 backend/sql/private_account_balance_drop_triggers.sql 删除 balance 触发器'
                )
                return
            raise


def _build_private_account_tx_list_filters():
    """从 query 解析列表筛选项，返回 (extra_where_sql, params)。"""
    clauses = []
    params = {}
    record_id = request.args.get('id')
    if record_id is not None and str(record_id).strip() != '':
        clauses.append('pat.id = :id')
        params['id'] = record_id
    flow_no = request.args.get('flow_no')
    if flow_no is not None and str(flow_no).strip() != '':
        clauses.append('pat.flow_no = :flow_no')
        params['flow_no'] = str(flow_no).strip()
    related_flow_no = request.args.get('related_flow_no')
    if related_flow_no is not None and str(related_flow_no).strip() != '':
        clauses.append(
            "TRIM(COALESCE(pat.related_flow_no, '')) COLLATE utf8mb4_unicode_ci"
            " = :related_flow_no COLLATE utf8mb4_unicode_ci",
        )
        params['related_flow_no'] = str(related_flow_no).strip()
    related_flow_no_empty = request.args.get('related_flow_no_empty')
    if related_flow_no_empty is not None and str(related_flow_no_empty).strip().lower() in (
        '1', 'true', 'yes',
    ):
        clauses.append(
            "(pat.related_flow_no IS NULL OR TRIM(pat.related_flow_no) = '')",
        )
    fund_status_in = request.args.get('fund_status_in')
    if fund_status_in is not None and str(fund_status_in).strip() != '':
        fund_values = [
            v.strip() for v in str(fund_status_in).split(',') if v.strip()
        ]
        if fund_values:
            placeholders = []
            for idx, val in enumerate(fund_values):
                key = f'fund_status_in_{idx}'
                placeholders.append(f':{key}')
                params[key] = val
            clauses.append(f"pat.fund_status IN ({', '.join(placeholders)})")
    business_desc = request.args.get('business_desc')
    if business_desc is not None and str(business_desc).strip():
        clauses.append('pat.business_desc LIKE :business_desc')
        params['business_desc'] = f"%{str(business_desc).strip()}%"

    counterparty_name = request.args.get('counterparty_name')
    if counterparty_name is not None and str(counterparty_name).strip():
        clauses.append('pat.counterparty_name LIKE :counterparty_name')
        params['counterparty_name'] = f"%{str(counterparty_name).strip()}%"

    biz_from = request.args.get('business_date_from')
    if biz_from is not None and str(biz_from).strip():
        clauses.append('pat.business_date >= :business_date_from')
        from_val = str(biz_from).strip()
        if len(from_val) <= 10:
            from_val = f'{from_val} 00:00:00'
        params['business_date_from'] = from_val

    biz_to = request.args.get('business_date_to')
    if biz_to is not None and str(biz_to).strip():
        clauses.append('pat.business_date <= :business_date_to')
        to_val = str(biz_to).strip()
        if len(to_val) <= 10:
            to_val = f'{to_val} 23:59:59'
        params['business_date_to'] = to_val

    project_id = request.args.get('project_id')
    if project_id is not None and str(project_id).strip() != '':
        try:
            params['project_id'] = int(project_id)
        except (TypeError, ValueError):
            raise ValueError('project_id 必须为整数')
        clauses.append('pat.project_id = :project_id')
    project_id_empty = request.args.get('project_id_empty')
    if project_id_empty is not None and str(project_id_empty).strip().lower() in (
        '1', 'true', 'yes',
    ):
        clauses.append('pat.project_id IS NULL')

    filter_fields = (
        'counterparty_type',
        'direction',
        'business_type',
        'fund_status',
        'approval_status',
    )
    for field in filter_fields:
        raw = request.args.get(field)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        clauses.append(f"pat.{field} = :{field}")
        params[field] = value
    extra = ''
    if clauses:
        extra = ' AND ' + ' AND '.join(clauses)
    return extra, params


@fin_app.route('/fin/private_account_transactions/enums', methods=['GET'])
def get_private_account_transactions_enums():
    """私账往来下拉枚举（与后端校验一致）。"""
    return jsonify({
        'code': 200,
        'msg': 'success',
        'data': {
            'business_types': list(PRIVATE_BUSINESS_TYPE_ACTIVE),
            'directions': ['收入', '支出'],
            'account_types': list(PRIVATE_ACCOUNT_TYPE_VALUES),
            'counterparty_types': list(PRIVATE_COUNTERPARTY_TYPE_VALUES),
        },
    })


# 私账列表排序（段内业务日期降序）：
# 1 待审批且未收/未付/未核销 → 2 已通过且未收/未付/未核销 → 3 已通过且已收/已付/已核销
PRIVATE_ACCOUNT_LIST_ORDER_BY = """
    ORDER BY
        CASE
            WHEN TRIM(COALESCE(pat.approval_status, '')) = '已驳回'
            THEN 1
            WHEN TRIM(COALESCE(pat.approval_status, '')) = '待审批'
                 AND TRIM(COALESCE(pat.fund_status, '')) IN ('未付', '未收', '未核销')
            THEN 1
            WHEN TRIM(COALESCE(pat.approval_status, '')) = '已通过'
                 AND TRIM(COALESCE(pat.fund_status, '')) IN ('未付', '未收', '未核销')
            THEN 2
            WHEN TRIM(COALESCE(pat.approval_status, '')) = '已通过'
                 AND TRIM(COALESCE(pat.fund_status, '')) IN ('已收', '已付', '已核销')
            THEN 3
            ELSE 4
        END ASC,
        pat.business_date DESC,
        pat.id DESC
"""


@fin_app.route('/fin/private_account_transactions/recalc_balance', methods=['POST'])
def recalc_private_account_balance_api():
    """按空间全量重算 balance（改库表精度后或历史数据修复后调用）。"""
    try:
        payload = request.get_json(silent=True) or {}
        namespace_id = payload.get('namespace_id') or request.args.get('namespace_id')
        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400

        _recalc_private_account_balance(namespace_id)
        return jsonify({
            "code": 200,
            "msg": "余额重算完成",
            "data": {
                "namespace_id": namespace_id,
                "decimal_scale": _private_account_balance_decimal_scale(),
            },
        })
    except Exception as e:
        logger.error(f"私账余额重算失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"私账余额重算失败: {str(e)}",
        }), 500


@fin_app.route('/fin/private_account_transactions/list', methods=['GET'])
def get_private_account_transactions_list():
    """私账往来流水列表（按 namespace_id 分页，支持多字段筛选）。"""
    try:
        namespace_id = request.args.get('namespace_id')
        page = request.args.get('page', default=1, type=int)
        page_size = request.args.get('page_size', default=100, type=int)

        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400
        if page < 1:
            return jsonify({"code": 400, "msg": "page 必须 >= 1"}), 400
        if page_size < 1 or page_size > 500:
            return jsonify({"code": 400, "msg": "page_size 必须在 1~500 之间"}), 400

        offset = (page - 1) * page_size
        try:
            filter_extra, filter_params = _build_private_account_tx_list_filters()
        except ValueError as ve:
            return jsonify({"code": 400, "msg": str(ve)}), 400
        params = {"namespace_id": namespace_id, **filter_params}

        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM private_account_transactions pat
            WHERE pat.namespace_id = :namespace_id
              AND (pat.is_deleted = 0 OR pat.is_deleted IS NULL)
              {filter_extra}
        """
        count_res = current_app.db_manager.execute_query(count_sql, params)
        total = int(count_res[0].get('total', 0)) if count_res else 0

        sql = f"""
            SELECT
                pat.id, pat.flow_no, pat.business_date, pat.amount, pat.balance, pat.direction, pat.account_type,
                pat.account_name, pat.account_holder,pat.actual_object, pat.account_bank,
                pat.counterparty_name, pat.counterparty_type, pat.counterparty_account,
                pat.business_type, pat.business_desc, pat.related_doc_no, pat.related_flow_no, pat.company_subject,
                pat.is_bookkept, pat.bookkeep_date, pat.bookkeep_voucher_no,
                pat.operator, pat.operator_dept, pat.approver, pat.approval_status,
                pat.reasons_approval_rejection, pat.approval_time,
                pat.fund_status, pat.confirm_time,
                pat.reconciliation_status, pat.reconciliation_time, pat.reconciliation_remark,
                pat.remark, pat.created_by, pat.created_at, pat.updated_by, pat.updated_at, pat.namespace_id,
                pat.project_id,
                (
                    SELECT m.company_voucher_no
                    FROM private_to_company_mapping m
                    WHERE m.private_flow_no COLLATE utf8mb4_unicode_ci
                        = pat.flow_no COLLATE utf8mb4_unicode_ci
                    ORDER BY m.mapping_date DESC, m.id DESC
                    LIMIT 1
                ) AS mapped_company_voucher_no
            FROM private_account_transactions pat
            WHERE pat.namespace_id = :namespace_id
              AND (pat.is_deleted = 0 OR pat.is_deleted IS NULL)
              {filter_extra}
            {PRIVATE_ACCOUNT_LIST_ORDER_BY.strip()}
            LIMIT :limit OFFSET :offset
        """
        page_params = {**params, "limit": page_size, "offset": offset}
        result = current_app.db_manager.execute_query(sql, page_params)

        datetime_fields = {
            'business_date',
            'bookkeep_date',
            'approval_time',
            'confirm_time',
            'reconciliation_time',
            'created_at',
            'updated_at',
        }
        safe_rows = []
        if isinstance(result, list):
            for row in result:
                _format_row_datetimes(datetime_fields, row)
                safe_rows.append(_json_safe_private_transaction_row(row))

        response = jsonify({
            "code": 200,
            "msg": "success",
            "data": safe_rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        })
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
    except Exception as e:
        logger.error(f"获取私账往来列表失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"获取私账往来列表失败: {str(e)}",
        }), 500


def _query_private_account_amount_summary(namespace_id, filter_extra, filter_params):
    """在列表筛选项范围内汇总：预计=已通过；实际=已通过且资金已收/已付/已核销。"""
    params = {"namespace_id": namespace_id, **filter_params}
    sql = f"""
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN TRIM(COALESCE(pat.approval_status, '')) = '已通过'
                         AND TRIM(COALESCE(pat.direction, '')) = '收入'
                    THEN pat.amount
                    WHEN TRIM(COALESCE(pat.approval_status, '')) = '已通过'
                         AND TRIM(COALESCE(pat.direction, '')) = '支出'
                    THEN -pat.amount
                    ELSE 0
                END
            ), 0) AS expected_signed_total,
            COALESCE(SUM(
                CASE
                    WHEN TRIM(COALESCE(pat.approval_status, '')) = '已通过'
                         AND TRIM(COALESCE(pat.fund_status, '')) IN ('已收', '已付', '已核销')
                         AND TRIM(COALESCE(pat.direction, '')) = '收入'
                    THEN pat.amount
                    WHEN TRIM(COALESCE(pat.approval_status, '')) = '已通过'
                         AND TRIM(COALESCE(pat.fund_status, '')) IN ('已收', '已付', '已核销')
                         AND TRIM(COALESCE(pat.direction, '')) = '支出'
                    THEN -pat.amount
                    ELSE 0
                END
            ), 0) AS actual_signed_total
        FROM private_account_transactions pat
        WHERE pat.namespace_id = :namespace_id
          AND (pat.is_deleted = 0 OR pat.is_deleted IS NULL)
          {filter_extra}
    """
    result = current_app.db_manager.execute_query(sql, params)
    expected_signed_total = 0.0
    actual_signed_total = 0.0
    if isinstance(result, list) and result:
        expected_signed_total = float(result[0].get('expected_signed_total') or 0)
        actual_signed_total = float(result[0].get('actual_signed_total') or 0)
    return expected_signed_total, actual_signed_total


@fin_app.route('/fin/private_account_transactions/amount_summary', methods=['GET'])
def get_private_account_amount_summary():
    """当前列表筛选项下：预计=审批已通过合计；实际=已通过且资金已收/已付/已核销合计。"""
    try:
        namespace_id = request.args.get('namespace_id')
        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400

        try:
            filter_extra, filter_params = _build_private_account_tx_list_filters()
        except ValueError as ve:
            return jsonify({"code": 400, "msg": str(ve)}), 400
        expected_signed_total, actual_signed_total = _query_private_account_amount_summary(
            namespace_id, filter_extra, filter_params,
        )

        return jsonify({
            "code": 200,
            "msg": "success",
            "data": {
                "expected_signed_total": expected_signed_total,
                "actual_signed_total": actual_signed_total,
            },
        })
    except Exception as e:
        logger.error(f"获取私账金额合计失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"获取私账金额合计失败: {str(e)}",
        }), 500


@fin_app.route('/fin/private_account_transactions/settled_total', methods=['GET'])
def get_private_account_settled_amount_total():
    """兼容旧接口：返回 actual_signed_total（字段名 signed_total）。"""
    try:
        namespace_id = request.args.get('namespace_id')
        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400

        try:
            filter_extra, filter_params = _build_private_account_tx_list_filters()
        except ValueError as ve:
            return jsonify({"code": 400, "msg": str(ve)}), 400
        _, actual_signed_total = _query_private_account_amount_summary(
            namespace_id, filter_extra, filter_params,
        )

        return jsonify({
            "code": 200,
            "msg": "success",
            "data": {
                "signed_total": actual_signed_total,
            },
        })
    except Exception as e:
        logger.error(f"获取私账已完结金额合计失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"获取私账已完结金额合计失败: {str(e)}",
        }), 500


@fin_app.route('/fin/private_account_transactions/delete', methods=['POST'])
def delete_private_account_transaction():
    """私账往来软删除：将 is_deleted 置为 1（列表仅展示 is_deleted 为 0 或 NULL 的记录）。"""
    try:
        payload = request.get_json(silent=True) or {}
        namespace_id = payload.get('namespace_id')
        record_id = payload.get('id')

        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400
        if record_id is None or record_id == "":
            return jsonify({"code": 400, "msg": "缺少必要参数 id"}), 400

        sql = """
            UPDATE private_account_transactions
            SET is_deleted = 1
            WHERE namespace_id = :namespace_id
              AND id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
        """
        affected_rows = current_app.db_manager.execute_update(sql, {
            "namespace_id": namespace_id,
            "id": record_id,
        })

        if affected_rows <= 0:
            return jsonify({
                "code": 404,
                "msg": "未找到可删除的数据或已删除",
            }), 404

        return jsonify({
            "code": 200,
            "msg": "删除成功",
            "affected_rows": affected_rows,
        })
    except Exception as e:
        logger.error(f"删除私账往来失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"删除私账往来失败: {str(e)}",
        }), 500


def _project_belongs_to_namespace(project_id, namespace_id):
    rows = current_app.db_manager.execute_query(
        """
        SELECT 1 AS ok
        FROM project_authorize pa
        WHERE pa.namespace_id = :namespace_id
          AND pa.project_id = :project_id
          AND (pa.is_deleted = 0 OR pa.is_deleted IS NULL)
        LIMIT 1
        """,
        {'namespace_id': int(namespace_id), 'project_id': int(project_id)},
    )
    return bool(rows)


PRIVATE_ACCOUNT_TX_UPDATE_FIELDS = {
    'flow_no',
    'business_date',
    'amount',
    'direction',
    'account_type',
    'account_name',
    'account_holder',
    'actual_object',
    'account_bank',
    'counterparty_name',
    'counterparty_type',
    'counterparty_account',
    'business_type',
    'business_desc',
    'related_doc_no',
    'related_flow_no',
    'company_subject',
    'is_bookkept',
    'bookkeep_date',
    'bookkeep_voucher_no',
    'operator',
    'operator_dept',
    'approver',
    'approval_status',
    'reasons_approval_rejection',
    'approval_time',
    'fund_status',
    'confirm_time',
    'reconciliation_status',
    'reconciliation_time',
    'reconciliation_remark',
    'remark',
    'project_id',
}

PRIVATE_ACCOUNT_TYPE_VALUES = {'微信', '支付宝', '银行卡', '现金', '其他'}
PRIVATE_COUNTERPARTY_TYPE_VALUES = {'客户', '供应商', '员工', '股东', '其他'}
# 新建/下拉可选
PRIVATE_BUSINESS_TYPE_ACTIVE = (
    '货款', '报销', '借贷', '工资', '备用金', '代收代付', '其他',
)
# 历史库内数据兼容（更新时仍允许，不可用于新建）
PRIVATE_BUSINESS_TYPE_LEGACY = frozenset({'借款', '还款'})
PRIVATE_BUSINESS_TYPE_VALUES = frozenset(PRIVATE_BUSINESS_TYPE_ACTIVE) | PRIVATE_BUSINESS_TYPE_LEGACY


def _validate_business_type(value, *, for_create=False):
    bt = str(value or '').strip()
    if not bt:
        return None, 'business_type 不能为空'
    if for_create:
        if bt not in PRIVATE_BUSINESS_TYPE_ACTIVE:
            return None, 'business_type 取值不在允许范围内'
    elif bt not in PRIVATE_BUSINESS_TYPE_VALUES:
        return None, 'business_type 取值不在允许范围内'
    return bt, None
PRIVATE_APPROVAL_STATUS_VALUES = {'待审批', '已通过', '已驳回'}
PRIVATE_FUND_STATUS_DB_VALUES = {'待收', '已收', '待付', '已付', '已核销', '未核销'}
PRIVATE_FUND_STATUS_NORMAL_CREATE = {'已收', '未收', '已付', '未付'}
PRIVATE_FUND_STATUS_REIMBURSEMENT_CREATE = {'已核销', '未核销'}
REIMBURSEMENT_BUSINESS_TYPE = '报销'
FUND_STATUS_LOCKED_UPDATE = {'已收', '已付', '已核销'}
REVOKE_PENDING_FUND_STATUS = {'未收', '未付', '未核销'}
PENDING_APPROVAL_STATUS = '待审批'
BUSINESS_TYPE_FIXED_RULES = {
    '报销': ('支出', '未核销'),
    '工资': ('支出', '未付'),
    '借款': ('收入', '未收'),
    '还款': ('支出', '未付'),
}


def _fund_status_by_direction(direction):
    d = str(direction or '').strip()
    return '未收' if d == '收入' else '未付'


def _approval_is_passed(status):
    return str(status or '').strip() == '已通过'


def _approval_is_decided(status):
    """审批状态为已通过或已驳回。"""
    return str(status or '').strip() in ('已通过', '已驳回')


def _linked_reimbursement_has_decided_state(flow_no):
    """本流水是否存在已审批/已支付的关联报销明细（与私账审批状态可能不一致）。"""
    flow_no = str(flow_no or '').strip()
    if not flow_no:
        return False
    sql = """
        SELECT 1
        FROM reimbursement_details
        WHERE TRIM(COALESCE(related_flow_no, '')) COLLATE utf8mb4_unicode_ci
              = :flow_no COLLATE utf8mb4_unicode_ci
          AND (is_deleted = 0 OR is_deleted IS NULL)
          AND (
            TRIM(COALESCE(approval_status, '')) IN ('已通过', '已驳回')
            OR TRIM(COALESCE(payment_status, '')) = '已支付'
            OR (approver IS NOT NULL AND TRIM(approver) <> '')
          )
        LIMIT 1
    """
    rows = current_app.db_manager.execute_query(sql, {'flow_no': flow_no})
    return bool(rows)


def _should_reset_linked_on_pat_pending(cur_approval, update_data, flow_no):
    """私账将变为/保持待审批且需同步清空关联报销明细时返回 True。"""
    if str(update_data.get('approval_status', '')).strip() != PENDING_APPROVAL_STATUS:
        return False
    if _approval_is_decided(cur_approval):
        return True
    return _linked_reimbursement_has_decided_state(flow_no)


def _apply_pat_pending_approval_clear(update_data, revoke_approval_update=False):
    """回退待审批：清空私账审批字段（approver 列 NOT NULL，使用空字符串）。"""
    update_data['approver'] = ''
    update_data.pop('approval_time', None)
    if not revoke_approval_update:
        update_data['reasons_approval_rejection'] = None


def _is_fund_status_side_effect_update(update_data):
    """资金状态随业务类型/收支联动变更（非单独修改资金状态）。"""
    if 'fund_status' not in update_data:
        return False
    return 'business_type' in update_data or 'direction' in update_data


def _is_approval_status_only_payload(update_data):
    """仅更新审批状态（通过/驳回及驳回备注、审批人、审批时间），不触发其它业务规则。"""
    keys = set(update_data.keys())
    allowed = {'approval_status', 'reasons_approval_rejection', 'approver', 'approval_time'}
    if keys <= allowed and 'approval_status' in keys:
        ast = str(update_data.get('approval_status', '')).strip()
        return ast in ('已通过', '已驳回')
    return False


def _is_related_flow_only_payload(update_data):
    """仅更新关联银行流水号。"""
    return set(update_data.keys()) <= {'related_flow_no'}


def _is_project_id_only_payload(update_data):
    """仅更新项目关联 project_id（资金已收/已付/已核销时仍允许关联或解绑）。"""
    return set(update_data.keys()) <= {'project_id'}


def _is_reconciliation_only_payload(update_data):
    """仅更新对账相关字段。"""
    allowed = {'reconciliation_status', 'reconciliation_time', 'reconciliation_remark'}
    return (
        'reconciliation_status' in update_data
        and set(update_data.keys()) <= allowed
    )


def _allowed_fund_status_values_for_approved(business_type, direction):
    """审批通过后允许单独修改的资金状态取值。"""
    bt = str(business_type or '').strip()
    if bt == REIMBURSEMENT_BUSINESS_TYPE:
        return PRIVATE_FUND_STATUS_REIMBURSEMENT_CREATE
    d = str(direction or '').strip()
    if d == '收入':
        return {'未收', '已收'}
    return {'未付', '已付'}


def _apply_business_type_fixed_rules(update_data, business_type):
    """固定业务类型下强制收支与资金状态。"""
    bt = str(business_type or '').strip()
    if bt not in BUSINESS_TYPE_FIXED_RULES:
        return None
    direction, fund_status = BUSINESS_TYPE_FIXED_RULES[bt]
    update_data['direction'] = direction
    update_data['fund_status'] = fund_status
    return bt


def _apply_business_type_change_rules(update_data, business_type):
    """业务类型变更：固定类型走固定规则，其它类型默认支出/未付。"""
    bt = str(business_type or '').strip()
    if bt in BUSINESS_TYPE_FIXED_RULES:
        return _apply_business_type_fixed_rules(update_data, bt)
    update_data['direction'] = '支出'
    update_data['fund_status'] = '未付'
    return bt

PRIVATE_ACCOUNT_TX_CREATE_FIELDS = {
    'business_date',
    'amount',
    'direction',
    'account_type',
    'account_name',
    'account_holder',
    'actual_object',
    'account_bank',
    'counterparty_name',
    'counterparty_type',
    'counterparty_account',
    'business_type',
    'business_desc',
    'related_doc_no',
    'company_subject',
    'operator',
    'operator_dept',
    'approver',
    'approval_status',
    'fund_status',
    'remark',
    'created_by',
}


def _generate_private_flow_no(namespace_id):
    """
    流水号：PR + 当前年月日(8位) + 4位序号。
    查询当前日期、同项目空间下已有最大 4 位序号，新记录序号 +1。
    """
    date_str = datetime.now().strftime('%Y%m%d')
    prefix = f'PR{date_str}'
    # PR(2) + YYYYMMDD(8) + seq(4) = 14
    sql = """
        SELECT MAX(CAST(SUBSTRING(flow_no, 11, 4) AS UNSIGNED)) AS max_seq
        FROM private_account_transactions
        WHERE namespace_id = :namespace_id
          AND flow_no LIKE :prefix_like
          AND CHAR_LENGTH(flow_no) = 14
    """
    rows = current_app.db_manager.execute_query(sql, {
        'namespace_id': namespace_id,
        'prefix_like': prefix + '%',
    })
    max_seq = 0
    if rows and rows[0].get('max_seq') is not None:
        try:
            max_seq = int(rows[0]['max_seq'])
        except (TypeError, ValueError):
            max_seq = 0

    next_seq = max_seq + 1
    if next_seq > 9999:
        raise ValueError(f'当日流水号序号已用尽（{date_str}）')
    return f'{prefix}{next_seq:04d}'


def _generate_bank_flow_no(namespace_id):
    """银行流水号：GL + 当前年月日(8位) + 4位序号。"""
    date_str = datetime.now().strftime('%Y%m%d')
    prefix = f'GL{date_str}'
    sql = """
        SELECT MAX(CAST(SUBSTRING(flow_no, 11, 4) AS UNSIGNED)) AS max_seq
        FROM bank_transaction_records
        WHERE namespace_id = :namespace_id
          AND flow_no LIKE :prefix_like
          AND CHAR_LENGTH(flow_no) = 14
    """
    rows = current_app.db_manager.execute_query(sql, {
        'namespace_id': namespace_id,
        'prefix_like': prefix + '%',
    })
    max_seq = 0
    if rows and rows[0].get('max_seq') is not None:
        try:
            max_seq = int(rows[0]['max_seq'])
        except (TypeError, ValueError):
            max_seq = 0

    next_seq = max_seq + 1
    if next_seq > 9999:
        raise ValueError(f'当日银行流水号序号已用尽（{date_str}）')
    return f'{prefix}{next_seq:04d}'


def _assign_bank_flow_nos(namespace_id, insert_rows):
    """为待入库银行流水批量生成 GL 流水号。"""
    if not insert_rows:
        return
    date_str = datetime.now().strftime('%Y%m%d')
    prefix = f'GL{date_str}'
    sql = """
        SELECT MAX(CAST(SUBSTRING(flow_no, 11, 4) AS UNSIGNED)) AS max_seq
        FROM bank_transaction_records
        WHERE namespace_id = :namespace_id
          AND flow_no LIKE :prefix_like
          AND CHAR_LENGTH(flow_no) = 14
    """
    rows = current_app.db_manager.execute_query(sql, {
        'namespace_id': namespace_id,
        'prefix_like': prefix + '%',
    })
    max_seq = 0
    if rows and rows[0].get('max_seq') is not None:
        try:
            max_seq = int(rows[0]['max_seq'])
        except (TypeError, ValueError):
            max_seq = 0
    for row in insert_rows:
        max_seq += 1
        if max_seq > 9999:
            raise ValueError(f'当日银行流水号序号已用尽（{date_str}）')
        row['flow_no'] = f'{prefix}{max_seq:04d}'


def _preserve_business_date_from_row(update_data, lock_rows):
    """
    未在请求中修改 business_date 时，UPDATE 显式写回原值。
    避免列定义为 ON UPDATE CURRENT_TIMESTAMP 时在改其它字段后被刷成当前时间。
    """
    if not lock_rows:
        return
    cur_biz = lock_rows[0].get('business_date')
    if cur_biz is None:
        return
    if isinstance(cur_biz, datetime):
        update_data['business_date'] = (
            cur_biz.replace(tzinfo=None) if cur_biz.tzinfo else cur_biz
        )
        return
    parsed = _parse_business_date_value(cur_biz)
    if parsed:
        update_data['business_date'] = parsed


def _parse_business_date_value(raw):
    """解析业务日期时间为 datetime（库表 business_date 为 TIMESTAMP）。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return datetime.combine(raw, time.min)
    if isinstance(raw, (int, float)):
        try:
            ts = float(raw)
            if ts >= 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        normalized = s.replace('Z', '+00:00')
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt, size in (('%Y-%m-%d %H:%M:%S', 19), ('%Y-%m-%d %H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            return datetime.strptime(s[:size], fmt)
        except ValueError:
            continue
    return None


def _normalize_private_account_create_data(data):
    """校验并规范化新增私账字段，返回 (row_dict, error_msg)。"""
    if not isinstance(data, dict):
        return None, 'data 必须为对象'

    row = {}
    for key in PRIVATE_ACCOUNT_TX_CREATE_FIELDS:
        if key not in data:
            continue
        val = data[key]
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            return None, f'字段 {key} 类型不支持'
        if isinstance(val, str):
            val = val.strip()
            if val == '':
                continue
        row[key] = val

    required_str = (
        'business_date', 'amount', 'direction', 'account_type',
        'actual_object', 'counterparty_name',
        'counterparty_account', 'business_type', 'operator', 'approver',
    )
    for field in required_str:
        if field not in row or row[field] in (None, ''):
            return None, f'缺少必填字段 {field}'

    biz_dt = _parse_business_date_value(row.pop('business_date', None))
    if not biz_dt:
        return None, 'business_date 格式无效，请使用 YYYY-MM-DD HH:mm:ss 或时间戳'
    row['business_date'] = biz_dt

    d = str(row['direction']).strip()
    if d not in ('收入', '支出'):
        return None, 'direction 必须为「收入」或「支出」'
    row['direction'] = d

    at = str(row['account_type']).strip()
    if at not in PRIVATE_ACCOUNT_TYPE_VALUES:
        return None, 'account_type 取值不在允许范围内'
    row['account_type'] = at

    ct = str(row.get('counterparty_type', '其他')).strip() or '其他'
    if ct not in PRIVATE_COUNTERPARTY_TYPE_VALUES:
        return None, 'counterparty_type 取值不在允许范围内'
    row['counterparty_type'] = ct

    bt, bt_err = _validate_business_type(row.get('business_type'), for_create=True)
    if bt_err:
        return None, bt_err
    row['business_type'] = bt

    if bt == REIMBURSEMENT_BUSINESS_TYPE:
        d = str(row.get('direction', '')).strip()
        if d != '支出':
            return None, '报销业务类型收支必须为「支出」'
        row['direction'] = '支出'
        amount_str = _normalize_decimal_2(row.get('amount', '0'))
        if Decimal(amount_str) != 0:
            return None, '报销业务类型金额必须为 0'
        row['amount'] = amount_str
        fs = str(row.get('fund_status', '未核销')).strip() or '未核销'
        if fs != '未核销':
            return None, '报销业务资金状态必须为「未核销」'
        row['fund_status'] = '未核销'
    else:
        amount_str = _normalize_decimal_2(row.get('amount'))
        if Decimal(amount_str) <= 0:
            return None, 'amount 必须大于 0'
        row['amount'] = amount_str
        expected_fs = '未付' if row['direction'] == '支出' else '未收'
        fs = str(row.get('fund_status', expected_fs)).strip() or expected_fs
        if fs != expected_fs:
            return None, f'收支为「{row["direction"]}」时资金状态必须为「{expected_fs}」'
        row['fund_status'] = expected_fs

    ast = str(row.get('approval_status', '待审批')).strip() or '待审批'
    if ast not in PRIVATE_APPROVAL_STATUS_VALUES:
        return None, 'approval_status 取值不在允许范围内'
    row['approval_status'] = ast

    created_by = str(row.get('created_by', '系统')).strip() or '系统'
    row['created_by'] = created_by

    for text_key in (
        'actual_object', 'counterparty_name',
        'counterparty_account', 'operator', 'approver',
    ):
        row[text_key] = str(row[text_key]).strip()

    # account_name 非表单必填，写入时与对方账号保持一致
    row['account_name'] = row['counterparty_account']

    optional_keys = (
        'account_bank', 'business_desc',
        'related_doc_no', 'company_subject', 'operator_dept', 'remark',
    )
    for key in optional_keys:
        if key in row and row[key] is not None:
            row[key] = str(row[key]).strip() or None

    return row, None


@fin_app.route('/fin/private_account_transactions/create', methods=['POST'])
def create_private_account_transaction():
    """新增私账往来流水。"""
    try:
        payload = request.get_json(silent=True) or {}
        namespace_id = payload.get('namespace_id')
        data = payload.get('data')

        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400

        row, err = _normalize_private_account_create_data(data)
        if err:
            return jsonify({"code": 400, "msg": err}), 400

        try:
            row['flow_no'] = _generate_private_flow_no(namespace_id)
        except ValueError as ve:
            return jsonify({"code": 400, "msg": str(ve)}), 400

        dup_sql = """
            SELECT id FROM private_account_transactions
            WHERE namespace_id = :namespace_id AND flow_no = :flow_no
            LIMIT 1
        """
        dup = current_app.db_manager.execute_query(dup_sql, {
            'namespace_id': namespace_id,
            'flow_no': row['flow_no'],
        })
        if dup:
            return jsonify({"code": 400, "msg": "流水号冲突，请重试"}), 400

        row['namespace_id'] = namespace_id
        row['is_deleted'] = 0
        row['is_bookkept'] = 0
        row['reconciliation_status'] = '未对账'

        new_id = current_app.db_manager.insert_one(
            'private_account_transactions',
            row,
        )

        return jsonify({
            "code": 200,
            "msg": "新增成功",
            "data": {"id": new_id, "flow_no": row['flow_no']},
        })
    except Exception as e:
        logger.error(f"新增私账往来失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"新增私账往来失败: {str(e)}",
        }), 500


@fin_app.route('/fin/private_account_transactions/update', methods=['POST'])
def update_private_account_transaction():
    """更新私账往来：支持 data 多字段；兼容仅传 direction 的旧请求。"""
    try:
        payload = request.get_json(silent=True) or {}
        namespace_id = payload.get('namespace_id')
        record_id = payload.get('id')
        data = payload.get('data')

        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400
        if record_id is None or record_id == "":
            return jsonify({"code": 400, "msg": "缺少必要参数 id"}), 400

        if not isinstance(data, dict) or not data:
            legacy_dir = payload.get('direction')
            if legacy_dir is not None and str(legacy_dir).strip() != "":
                data = {'direction': legacy_dir}
            else:
                return jsonify({"code": 400, "msg": "缺少必要参数 data 或 data 格式错误"}), 400

        explicit_business_date_update = 'business_date' in data

        update_data = {}
        for key, value in data.items():
            if key not in PRIVATE_ACCOUNT_TX_UPDATE_FIELDS:
                continue
            if key == 'is_bookkept':
                if value in (True, 'true', '1', 1, '是'):
                    update_data[key] = 1
                elif value in (False, 'false', '0', 0, '否'):
                    update_data[key] = 0
                else:
                    return jsonify({"code": 400, "msg": "is_bookkept 必须为 0/1 或 是/否"}), 400
                continue
            if value is None:
                update_data[key] = None
            elif isinstance(value, (dict, list)):
                return jsonify({"code": 400, "msg": f"字段 {key} 类型不支持"}), 400
            else:
                update_data[key] = value

        if 'direction' in update_data:
            d = str(update_data['direction']).strip()
            if d not in ("收入", "支出"):
                return jsonify({"code": 400, "msg": "direction 必须为「收入」或「支出」"}), 400
            update_data['direction'] = d

        if 'fund_status' in update_data and update_data['fund_status'] is not None:
            fs = str(update_data['fund_status']).strip()
            allowed_fund = {
                '已收', '未收', '已付', '未付', '为付',
                '待收', '待付', '已核销', '未核销',
            }
            if fs not in allowed_fund:
                return jsonify({
                    "code": 400,
                    "msg": "fund_status 取值不在允许范围内",
                }), 400
            update_data['fund_status'] = fs

        if 'account_type' in update_data and update_data['account_type'] is not None:
            at = str(update_data['account_type']).strip()
            if at not in PRIVATE_ACCOUNT_TYPE_VALUES:
                return jsonify({"code": 400, "msg": "account_type 取值不在允许范围内"}), 400
            update_data['account_type'] = at

        if 'counterparty_type' in update_data and update_data['counterparty_type'] is not None:
            ct = str(update_data['counterparty_type']).strip()
            if ct not in PRIVATE_COUNTERPARTY_TYPE_VALUES:
                return jsonify({"code": 400, "msg": "counterparty_type 取值不在允许范围内"}), 400
            update_data['counterparty_type'] = ct

        if 'related_flow_no' in update_data:
            flow_val = update_data['related_flow_no']
            if flow_val is None or str(flow_val).strip() == '':
                update_data['related_flow_no'] = None
            else:
                update_data['related_flow_no'] = str(flow_val).strip()

        if 'project_id' in update_data:
            raw_pid = update_data['project_id']
            if raw_pid is None or str(raw_pid).strip() == '':
                update_data['project_id'] = None
            else:
                try:
                    pid_val = int(raw_pid)
                except (TypeError, ValueError):
                    return jsonify({"code": 400, "msg": "project_id 非法"}), 400
                if not _project_belongs_to_namespace(pid_val, namespace_id):
                    return jsonify({"code": 400, "msg": "项目不属于当前工作空间"}), 400
                update_data['project_id'] = pid_val

        if 'reconciliation_status' in update_data and update_data['reconciliation_status'] is not None:
            rs = str(update_data['reconciliation_status']).strip()
            if rs not in ('未对账', '已对账'):
                return jsonify({
                    "code": 400,
                    "msg": "reconciliation_status 必须为「未对账」或「已对账」",
                }), 400
            update_data['reconciliation_status'] = rs
            if rs == '已对账' and 'reconciliation_time' not in update_data:
                update_data['reconciliation_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        leaving_reimbursement = False
        leaving_reimbursement_flow_no = ''
        if 'business_type' in update_data and update_data['business_type'] is not None:
            bt, bt_err = _validate_business_type(update_data['business_type'], for_create=False)
            if bt_err:
                return jsonify({"code": 400, "msg": bt_err}), 400
            update_data['business_type'] = bt
            prev_bt_sql = """
                SELECT business_type, flow_no
                FROM private_account_transactions
                WHERE namespace_id = :namespace_id
                  AND id = :id
                  AND (is_deleted = 0 OR is_deleted IS NULL)
                LIMIT 1
            """
            prev_bt_rows = current_app.db_manager.execute_query(prev_bt_sql, {
                'namespace_id': namespace_id,
                'id': record_id,
            })
            if prev_bt_rows:
                prev_bt = str(prev_bt_rows[0].get('business_type') or '').strip()
                leaving_reimbursement_flow_no = str(
                    prev_bt_rows[0].get('flow_no') or '',
                ).strip()
                leaving_reimbursement = (
                    prev_bt == REIMBURSEMENT_BUSINESS_TYPE
                    and bt != REIMBURSEMENT_BUSINESS_TYPE
                )
            _apply_business_type_change_rules(update_data, bt)

        effective_bt = update_data.get('business_type')
        if effective_bt is None:
            bt_sql = """
                SELECT business_type
                FROM private_account_transactions
                WHERE namespace_id = :namespace_id
                  AND id = :id
                  AND (is_deleted = 0 OR is_deleted IS NULL)
                LIMIT 1
            """
            bt_rows = current_app.db_manager.execute_query(bt_sql, {
                'namespace_id': namespace_id,
                'id': record_id,
            })
            if bt_rows:
                effective_bt = str(bt_rows[0].get('business_type') or '').strip()

        fund_status_only_update = (
            'fund_status' in update_data
            and set(update_data.keys()) <= {'fund_status', 'approval_status', 'business_date'}
            and not explicit_business_date_update
        )
        approval_only_update = _is_approval_status_only_payload(update_data)
        approval_with_fund_update = (
            set(update_data.keys()) == {'approval_status', 'fund_status'}
            and _approval_is_passed(update_data.get('approval_status'))
        )
        revoke_approval_update = (
            set(update_data.keys())
            <= {'approval_status', 'fund_status', 'reasons_approval_rejection'}
            and str(update_data.get('approval_status', '')).strip() == PENDING_APPROVAL_STATUS
            and str(update_data.get('fund_status', '')).strip() in REVOKE_PENDING_FUND_STATUS
        )

        lock_sql = """
            SELECT fund_status, approval_status, business_type, direction, flow_no, business_date
            FROM private_account_transactions
            WHERE namespace_id = :namespace_id
              AND id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
            LIMIT 1
        """
        lock_rows = current_app.db_manager.execute_query(lock_sql, {
            'namespace_id': namespace_id,
            'id': record_id,
        })
        cur_approval = ''
        cur_flow_no = ''
        if lock_rows:
            cur_approval = str(lock_rows[0].get('approval_status') or '').strip()
            cur_flow_no = str(lock_rows[0].get('flow_no') or '').strip()

        post_approval_fund_update = False
        if 'fund_status' in update_data:
            post_approval_fund_update = (
                _approval_is_passed(cur_approval)
                or _approval_is_passed(update_data.get('approval_status'))
                or approval_with_fund_update
            )

        business_date_only_update = (
            explicit_business_date_update and len(update_data) == 1
        )
        reimbursement_business_date_only_update = (
            business_date_only_update
            and effective_bt == REIMBURSEMENT_BUSINESS_TYPE
            and _approval_is_passed(cur_approval)
        )
        reimbursement_amount_only_update = (
            len(update_data) == 1
            and 'amount' in update_data
            and effective_bt == REIMBURSEMENT_BUSINESS_TYPE
        )

        if effective_bt == REIMBURSEMENT_BUSINESS_TYPE and 'amount' in update_data:
            try:
                amount_str = _normalize_decimal_2(update_data.get('amount'), '0')
                if Decimal(amount_str) < Decimal('0.00'):
                    return jsonify({
                        "code": 400,
                        "msg": "报销类型金额不能为负数",
                    }), 400
                update_data['amount'] = amount_str
            except (InvalidOperation, ValueError):
                return jsonify({"code": 400, "msg": "金额格式无效"}), 400

        if post_approval_fund_update and 'fund_status' in update_data:
            fs = str(update_data['fund_status']).strip()
            row_bt = str(
                effective_bt
                or ((lock_rows[0].get('business_type') if lock_rows else '') or ''),
            ).strip()
            row_dir = str(
                (lock_rows[0].get('direction') if lock_rows else '')
                or update_data.get('direction')
                or '',
            ).strip()
            if not row_dir and row_bt in BUSINESS_TYPE_FIXED_RULES:
                row_dir = BUSINESS_TYPE_FIXED_RULES[row_bt][0]
            allowed_fs = _allowed_fund_status_values_for_approved(row_bt, row_dir)
            if fs not in allowed_fs:
                return jsonify({
                    "code": 400,
                    "msg": '资金状态取值不在允许范围内',
                }), 400
        elif (
            effective_bt in BUSINESS_TYPE_FIXED_RULES
            and not approval_only_update
            and not revoke_approval_update
            and not post_approval_fund_update
            and (
                'business_type' in update_data
                or 'direction' in update_data
                or 'fund_status' in update_data
            )
        ):
            expected_dir, expected_fs = BUSINESS_TYPE_FIXED_RULES[effective_bt]
            if 'direction' in update_data:
                d = str(update_data['direction']).strip()
                if d != expected_dir:
                    return jsonify({
                        "code": 400,
                        "msg": f"业务类型「{effective_bt}」收支必须为「{expected_dir}」",
                    }), 400
            if 'fund_status' in update_data:
                fs = str(update_data['fund_status']).strip()
                if fs != expected_fs:
                    return jsonify({
                        "code": 400,
                        "msg": f"业务类型「{effective_bt}」资金状态必须为「{expected_fs}」",
                    }), 400
            # business_type 变更已在 _apply_business_type_change_rules 中处理
            if 'business_type' not in update_data and (
                'direction' in update_data or 'fund_status' in update_data
            ):
                _apply_business_type_fixed_rules(update_data, effective_bt)
        elif 'direction' in update_data:
            update_data['fund_status'] = _fund_status_by_direction(update_data['direction'])

        if 'business_date' in update_data:
            biz_dt = _parse_business_date_value(update_data['business_date'])
            if not biz_dt:
                return jsonify({
                    "code": 400,
                    "msg": "business_date 格式无效，请使用 YYYY-MM-DD HH:mm:ss 或时间戳",
                }), 400
            update_data['business_date'] = biz_dt

        if 'approval_time' in update_data:
            at_dt = _parse_business_date_value(update_data['approval_time'])
            if not at_dt:
                return jsonify({
                    "code": 400,
                    "msg": "approval_time 格式无效，请使用 YYYY-MM-DD HH:mm:ss 或时间戳",
                }), 400
            update_data['approval_time'] = at_dt

        if 'approver' in update_data and update_data['approver'] is not None:
            approver = str(update_data['approver']).strip()
            if not approver:
                if str(update_data.get('approval_status', '')).strip() != PENDING_APPROVAL_STATUS:
                    return jsonify({"code": 400, "msg": "approver 不能为空"}), 400
                update_data['approver'] = ''
            else:
                update_data['approver'] = approver

        project_id_only_update = _is_project_id_only_payload(update_data)
        if not explicit_business_date_update and not project_id_only_update:
            _preserve_business_date_from_row(update_data, lock_rows)

        if not update_data:
            return jsonify({"code": 400, "msg": "没有可更新的字段"}), 400

        if 'approval_status' in update_data and update_data['approval_status'] is not None:
            ast = str(update_data['approval_status']).strip()
            if ast not in PRIVATE_APPROVAL_STATUS_VALUES:
                return jsonify({
                    "code": 400,
                    "msg": "approval_status 取值不在允许范围内",
                }), 400
            update_data['approval_status'] = ast
            if ast == '已通过':
                update_data['reasons_approval_rejection'] = None
            elif ast == '已驳回':
                reason = update_data.get('reasons_approval_rejection')
                if not str(reason or '').strip():
                    return jsonify({
                        "code": 400,
                        "msg": "驳回须填写备注",
                    }), 400
                update_data['reasons_approval_rejection'] = str(reason).strip()

        if 'reasons_approval_rejection' in update_data and 'approval_status' not in update_data:
            val = update_data['reasons_approval_rejection']
            update_data['reasons_approval_rejection'] = (
                None if val is None or str(val).strip() == '' else str(val).strip()
            )

        if revoke_approval_update:
            update_data['reasons_approval_rejection'] = None

        if (
            'fund_status' in update_data
            and not post_approval_fund_update
            and not leaving_reimbursement
            and not _is_fund_status_side_effect_update(update_data)
        ):
            cur_fs = (
                str(lock_rows[0].get('fund_status') or '').strip()
                if lock_rows else ''
            )
            new_fs = str(update_data.get('fund_status') or '').strip()
            if new_fs != cur_fs:
                return jsonify({
                    "code": 400,
                    "msg": "仅审批通过后可修改资金状态",
                }), 400
            update_data.pop('fund_status', None)

        if revoke_approval_update:
            if cur_approval != '已通过':
                return jsonify({
                    "code": 400,
                    "msg": "仅审批已通过且资金已完结的记录可撤销",
                }), 400
            cur_fs = str(lock_rows[0].get('fund_status') or '').strip() if lock_rows else ''
            if cur_fs not in FUND_STATUS_LOCKED_UPDATE:
                return jsonify({
                    "code": 400,
                    "msg": "当前资金状态无需撤销",
                }), 400
            fs = str(update_data['fund_status']).strip()
            row_bt = str(
                effective_bt
                or ((lock_rows[0].get('business_type') if lock_rows else '') or ''),
            ).strip()
            row_dir = str((lock_rows[0].get('direction') if lock_rows else '') or '').strip()
            if not row_dir and row_bt in BUSINESS_TYPE_FIXED_RULES:
                row_dir = BUSINESS_TYPE_FIXED_RULES[row_bt][0]
            allowed_fs = _allowed_fund_status_values_for_approved(row_bt, row_dir)
            if fs not in allowed_fs:
                return jsonify({
                    "code": 400,
                    "msg": '撤销后资金状态与业务类型不匹配',
                }), 400

        related_flow_only_update = _is_related_flow_only_payload(update_data)
        reconciliation_only_update = _is_reconciliation_only_payload(update_data)

        if lock_rows:
            cur_fs = str(lock_rows[0].get('fund_status') or '').strip()
            if (
                cur_fs in FUND_STATUS_LOCKED_UPDATE
                and not approval_only_update
                and not fund_status_only_update
                and not post_approval_fund_update
                and not revoke_approval_update
                and not business_date_only_update
                and not reimbursement_amount_only_update
                and not related_flow_only_update
                and not project_id_only_update
                and not reconciliation_only_update
            ):
                return jsonify({
                    "code": 400,
                    "msg": "资金状态为已收/已付/已核销时不可修改",
                }), 400

        if (
            any(k != 'approval_status' for k in update_data)
            and not fund_status_only_update
            and not post_approval_fund_update
            and not approval_only_update
            and not revoke_approval_update
            and not business_date_only_update
            and not reimbursement_amount_only_update
            and not related_flow_only_update
            and not project_id_only_update
            and not reconciliation_only_update
        ):
            update_data['approval_status'] = PENDING_APPROVAL_STATUS

        should_reset_linked = _should_reset_linked_on_pat_pending(
            cur_approval, update_data, cur_flow_no,
        )
        if should_reset_linked:
            _apply_pat_pending_approval_clear(update_data, revoke_approval_update)

        should_recalc_balance = False
        if lock_rows and 'fund_status' in update_data:
            old_fs = str(lock_rows[0].get('fund_status') or '').strip()
            new_fs = str(update_data.get('fund_status') or '').strip()
            should_recalc_balance = old_fs != new_fs

        unlinked_reimbursement_count = 0
        if leaving_reimbursement and leaving_reimbursement_flow_no:
            unlink_count, unlink_err = _reset_and_unlink_linked_reimbursements(
                leaving_reimbursement_flow_no,
            )
            if unlink_err:
                logger.warning(
                    '业务类型切出报销但解除关联报销明细失败 flow_no=%s: %s',
                    leaving_reimbursement_flow_no,
                    unlink_err,
                )
            else:
                unlinked_reimbursement_count = unlink_count

        set_clauses = []
        params = {
            "namespace_id": namespace_id,
            "id": record_id,
            "updated_at": datetime.now(),
        }
        for field, value in update_data.items():
            set_clauses.append(f"{field} = :{field}")
            params[field] = value
        set_clauses.append("updated_at = :updated_at")

        sql = f"""
            UPDATE private_account_transactions
            SET {', '.join(set_clauses)}
            WHERE namespace_id = :namespace_id
              AND id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
        """
        affected_rows = current_app.db_manager.execute_update(sql, params)

        if affected_rows <= 0:
            return jsonify({
                "code": 404,
                "msg": "未找到可更新的数据或已删除",
            }), 404

        resp_data = {'affected_rows': affected_rows}
        sync_reimbursement_count = 0
        if unlinked_reimbursement_count:
            resp_data['unlinked_reimbursement_count'] = unlinked_reimbursement_count
        if leaving_reimbursement and leaving_reimbursement_flow_no:
            sync_amount, amount_err = _sync_private_account_reimbursement_amount(
                leaving_reimbursement_flow_no,
            )
            if amount_err:
                logger.warning(
                    '业务类型切出报销后同步私账金额失败 flow_no=%s: %s',
                    leaving_reimbursement_flow_no,
                    amount_err,
                )
            elif sync_amount is not None:
                resp_data['amount'] = sync_amount
        if (
            approval_only_update
            and str(update_data.get('approval_status', '')).strip() in ('已通过', '已驳回')
        ):
            sync_reimbursement_count, sync_err, sync_amount = (
                _sync_pat_approval_to_linked_reimbursements(
                    namespace_id, record_id, update_data,
                )
            )
            if sync_err:
                logger.warning(
                    '私账审批已保存但同步报销明细审批失败 record_id=%s: %s',
                    record_id,
                    sync_err,
                )
            if sync_amount is not None:
                resp_data['amount'] = sync_amount

        if post_approval_fund_update and effective_bt == REIMBURSEMENT_BUSINESS_TYPE:
            flow_sql = """
                SELECT flow_no, fund_status, business_date, account_type, counterparty_account
                FROM private_account_transactions
                WHERE namespace_id = :namespace_id
                  AND id = :id
                  AND (is_deleted = 0 OR is_deleted IS NULL)
                LIMIT 1
            """
            flow_rows = current_app.db_manager.execute_query(flow_sql, {
                'namespace_id': namespace_id,
                'id': record_id,
            })
            if flow_rows:
                pat_flow_row = flow_rows[0]
                flow_no = str(pat_flow_row.get('flow_no') or '').strip()
                cur_fs = str(pat_flow_row.get('fund_status') or '').strip()
                if flow_no:
                    sync_amount, amount_err = _sync_private_account_reimbursement_amount(flow_no)
                    if amount_err:
                        logger.warning(
                            '资金状态已更新但同步私账金额失败 flow_no=%s: %s',
                            flow_no,
                            amount_err,
                        )
                    elif sync_amount is not None:
                        resp_data['amount'] = sync_amount
                    if cur_fs == '已核销':
                        pay_count, pay_err = _sync_linked_reimbursement_payment_on_verified(
                            flow_no,
                            pat_flow_row,
                        )
                        if pay_err:
                            logger.warning(
                                '资金状态已核销但同步报销支付状态失败 flow_no=%s: %s',
                                flow_no,
                                pay_err,
                            )
                        elif pay_count:
                            resp_data['synced_payment_count'] = pay_count

        if should_reset_linked and cur_flow_no:
            reset_count, reset_err = _reset_linked_reimbursement_approval(cur_flow_no)
            if reset_err:
                logger.warning(
                    '私账审批回退待审批但重置关联报销明细失败 flow_no=%s: %s',
                    cur_flow_no,
                    reset_err,
                )
            else:
                sync_reimbursement_count = reset_count
                if reset_count:
                    logger.info(
                        '私账审批回退待审批，已重置 %s 条关联报销明细 flow_no=%s',
                        reset_count,
                        cur_flow_no,
                    )

        if sync_reimbursement_count:
            resp_data['synced_reimbursement_count'] = sync_reimbursement_count

        if should_recalc_balance:
            _recalc_private_account_balance(namespace_id)

        return jsonify({
            "code": 200,
            "msg": "更新成功",
            "data": resp_data,
            "affected_rows": affected_rows,
        })
    except Exception as e:
        logger.error(f"更新私账往来失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"更新私账往来失败: {str(e)}",
        }), 500


@fin_app.route('/fin/bankstatements/delete', methods=['POST'])
def deleteBankStatementAPI():
    try:
        payload = request.get_json(silent=True) or {}
        namespace_id = payload.get('namespace_id')
        record_id = payload.get('id')

        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数namespace_id"}), 400
        if record_id is None:
            return jsonify({"code": 400, "msg": "缺少必要参数id"}), 400

        sql = """
            UPDATE bank_transaction_records
            SET is_delete = 1
            WHERE namespace_id = :namespace_id
            AND id = :id
        """
        affected_rows = current_app.db_manager.execute_update(sql, {
            "namespace_id": namespace_id,
            "id": record_id
        })

        if affected_rows <= 0:
            return jsonify({
                "code": 404,
                "msg": "未找到可删除的数据"
            }), 404

        return jsonify({
            "code": 200,
            "msg": "删除成功",
            "affected_rows": affected_rows
        })
    except Exception as e:
        logger.error(f"删除银行流水失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"删除银行流水失败: {str(e)}"
        }), 500




@fin_app.route('/fin/bankstatements/update', methods=['POST'])
def editBankStatementAPI():
    try:
        payload = request.get_json(silent=True) or {}
        namespace_id = payload.get('namespace_id')
        record_id = payload.get('id')
        data = payload.get('data')

        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数namespace_id"}), 400
        if record_id is None:
            return jsonify({"code": 400, "msg": "缺少必要参数id"}), 400
        if not isinstance(data, dict) or not data:
            return jsonify({"code": 400, "msg": "缺少必要参数data或data格式错误"}), 400

        allowed_fields = {
            'voucher_no',
            'voucher_type',
            'our_account_no',
            'counterparty_account_no',
            'counterparty_bank_no',
            'counterparty_bank_name',
            'counterparty_name',
            'group_sub_account',
            'transaction_time',
            'direction',
            'debit_amount',
            'credit_amount',
            'balance',
            'abstract_text',
            'purpose',
            'personalized_info',
            'bank_name',
            'created_at',
        }

        update_data = {k: v for k, v in data.items() if k in allowed_fields}
        if not update_data:
            return jsonify({"code": 400, "msg": "没有可更新的字段"}), 400

        set_clauses = []
        params = {
            "namespace_id": namespace_id,
            "id": record_id,
            "updated_at": datetime.now()
        }
        for field, value in update_data.items():
            set_clauses.append(f"{field} = :{field}")
            params[field] = value
        set_clauses.append("updated_at = :updated_at")

        sql = f"""
            UPDATE bank_transaction_records
            SET {', '.join(set_clauses)}
            WHERE namespace_id = :namespace_id
            AND id = :id
        """

        affected_rows = current_app.db_manager.execute_update(sql, params)

        if affected_rows <= 0:
            return jsonify({
                "code": 404,
                "msg": "未找到可更新的数据"
            }), 404

        return jsonify({
            "code": 200,
            "msg": "更新成功",
            "affected_rows": affected_rows
        })
    except Exception as e:
        logger.error(f"更新银行流水失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"更新银行流水失败: {str(e)}"
        }), 500

@fin_app.route('/fin/bankstatements/upload', methods=['POST'])
def uploadBankStatementAPI():
    try:
        namespace_id = request.form.get('namespace_id')
        file = request.files.get('file')

        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数namespace_id"}), 400
        if not file or not file.filename:
            return jsonify({"code": 400, "msg": "缺少上传文件"}), 400

        allowed_suffix = {'.csv', '.xls', '.xlsx'}
        original_filename = file.filename or ''
        _, ext = os.path.splitext(original_filename.lower())
        if ext not in allowed_suffix:
            return jsonify({"code": 400, "msg": "仅支持 csv/xls/xlsx 文件"}), 400

        upload_dir = os.path.join(current_app.root_path, 'uploads', 'bank_statements')
        os.makedirs(upload_dir, exist_ok=True)

        stored_name = f"{namespace_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
        stored_path = os.path.join(upload_dir, stored_name)
        file.save(stored_path)

        file_size = os.path.getsize(stored_path) if os.path.isfile(stored_path) else 0
        if file_size < 64:
            return jsonify({
                'code': 400,
                'msg': '上传文件为空或过小，请重新选择文件',
                'data': {'file_size': file_size, 'original_filename': original_filename},
            }), 400

        try:
            if ext == '.csv':
                raw_rows = _parse_csv_rows(stored_path)
            else:
                raw_rows = _parse_excel_rows(stored_path)
        except RuntimeError as parse_err:
            return jsonify({
                'code': 400,
                'msg': str(parse_err),
                'data': {'original_filename': original_filename, 'file_size': file_size},
            }), 400

        bank_type = request.form.get('bank_type') or request.form.get('import_source')
        bank_format = _resolve_bank_format(raw_rows, bank_type)
        insert_rows, skipped_rows, invalid_rows = _build_insert_rows(
            namespace_id, raw_rows, bank_format,
        )
        _assign_bank_flow_nos(namespace_id, insert_rows)
        logger.info(
            "银行流水入库数据构建完成: bank_format=%s insert_rows=%s skipped_rows=%s preview=%s",
            bank_format,
            len(insert_rows),
            skipped_rows,
            insert_rows[:5],
        )
        if not insert_rows:
            headers = _headers_from_raw_rows(raw_rows)
            sample_keys = list((raw_rows[0] or {}).keys())[:12] if raw_rows else []
            hint = None
            if not raw_rows:
                hint = (
                    '未读取到任何数据行。常见原因：上传时 Content-Type 设置错误导致文件体丢失，'
                    '或 Excel 格式不受支持。请重新上传并确认已安装 openpyxl。'
                )
            elif _is_wzbank_headers(headers) and bank_format == BANK_FORMAT_ICBC:
                hint = '文件为温州银行表头，请勿选择「工商银行」，请使用「温州银行」或「自动识别」'
            elif invalid_rows:
                hint = '；'.join(invalid_rows[:3])
            else:
                hint = f'已读取 {len(raw_rows)} 行但未生成入库记录，请检查表头是否与模板一致'
            return jsonify({
                "code": 400,
                "msg": "文件中未解析到可入库数据",
                "data": {
                    "bank_format": bank_format,
                    "bank_name": BANK_FORMAT_LABELS.get(bank_format),
                    "total_rows": len(raw_rows),
                    "skipped_rows": skipped_rows,
                    "invalid_reasons": invalid_rows[:20],
                    "hint": hint,
                    "header_keys_sample": sample_keys,
                    "file_size": file_size,
                }
            }), 400

        inserted_ids = current_app.db_manager.insert_many('bank_transaction_records', insert_rows)

        return jsonify({
            "code": 200,
            "msg": "上传成功",
            "data": {
                "namespace_id": namespace_id,
                "bank_format": bank_format,
                "bank_name": BANK_FORMAT_LABELS.get(bank_format),
                "original_filename": file.filename,
                "stored_filename": stored_name,
                "total_rows": len(raw_rows),
                "inserted_rows": len(insert_rows),
                "skipped_rows": skipped_rows,
                "inserted_ids_count": len(inserted_ids),
                "invalid_reasons": invalid_rows[:20]
            }
        })
    except Exception as e:
        logger.error(f"上传银行流水失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"上传银行流水失败: {str(e)}"
        }), 500

@fin_app.route('/fin/filter', methods=['GET'])
def get_finFilter():
    try:
        namespace_id = request.args.get('namespace_id')
        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数namespace_id"}), 400

        allowed_filter_fields = {
            'transaction_time',
            'direction'
        }
        where_clauses = [
            "namespace_id = :namespace_id",
            ":data_authorize_lv <= data_authorize_lv",
            "is_delete = 0"
        ]
        base_params = {
            "namespace_id": namespace_id,
            "data_authorize_lv": 1
        }

        # transaction_time 区间过滤模板：
        # 仅支持传入 "start,end"（逗号分隔），拆出 start/end 后拼接区间条件
        transaction_time = request.args.get('transaction_time')
        if transaction_time is not None and str(transaction_time).strip() != '':
            time_value = str(transaction_time).strip()
            if ',' in time_value:
                start_time, end_time = [x.strip() for x in time_value.split(',', 1)]
                if start_time and end_time:
                    base_params["filter_transaction_time_start"] = start_time
                    base_params["filter_transaction_time_end"] = end_time
                    where_clauses.append(
                        "transaction_time >= :filter_transaction_time_start AND transaction_time <= :filter_transaction_time_end"
                    )
        
        where_sql = " AND ".join(where_clauses)

        query_sql = f"""
            SELECT *
            FROM bank_transaction_records
            WHERE {where_sql}
            ORDER BY transaction_time DESC, id DESC
        """
        result = current_app.db_manager.execute_query(query_sql, base_params)

        if isinstance(result, list):
            datetime_fields = {'created_at', 'updated_at', 'update_at', 'transaction_time'}
            for row in result:
                _format_row_datetimes(datetime_fields, row)

        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        })
    except Exception as e:
        logger.error(f"获取金融筛选数据失败: {str(e)}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"获取金融筛选数据失败: {str(e)}"
        }), 500


# ---------- 报销明细 reimbursement_details ----------

REIMBURSEMENT_EXPENSE_TYPE_VALUES = {
    '交通', '餐饮', '住宿', '办公用品', '差旅', '招待', '维修', '活动物料', '其他',
}
REIMBURSEMENT_APPROVAL_STATUS_VALUES = {'待审批', '已通过', '已驳回', '已支付'}
REIMBURSEMENT_PAYMENT_STATUS_VALUES = {'待支付', '已支付'}
REIMBURSEMENT_PAYMENT_METHOD_VALUES = {'银行转账', '现金', '微信', '支付宝'}
REIMBURSEMENT_INVOICE_TYPE_VALUES = {'增值税专票', '普票', '电子票', '无票'}

REIMBURSEMENT_DETAIL_LIST_SELECT = """
    rd.id, rd.detail_no, rd.related_flow_no, rd.applicant, rd.applicant_dept, rd.apply_date,
    rd.approver, rd.approval_status, rd.approval_time, rd.approval_remark,
    rd.payment_status, rd.payment_time, rd.payment_method, rd.payment_account,
    rd.expense_date, rd.expense_type, rd.expense_amount, rd.expense_currency,
    rd.expense_desc, rd.expense_remark,
    rd.tax_rate, rd.tax_amount, rd.amount_without_tax,
    rd.invoice_type, rd.invoice_no, rd.invoice_code, rd.invoice_date,
    rd.is_invoice_received, rd.attachment_ids, rd.remark,
    rd.created_by, rd.created_at, rd.updated_by, rd.updated_at
"""

# 列表排序：审批 已驳回→待审批→已通过→已支付；支付 待支付→已支付；费用日期降序
REIMBURSEMENT_DETAIL_LIST_ORDER_BY = """
    ORDER BY
        FIELD(rd.approval_status, '已驳回', '待审批', '已通过', '已支付') ASC,
        FIELD(rd.payment_status, '待支付', '已支付') ASC,
        rd.expense_date DESC,
        rd.id DESC
"""


def _json_safe_reimbursement_row(row):
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, Decimal):
            out[k] = str(v)
    return out


def _parse_date_only_value(raw):
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    s = str(raw).strip()
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d'):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).date()
    except ValueError:
        return None


def _generate_reimbursement_detail_no():
    """明细流水号：RD + 年月日(8位) + 4位序号。"""
    date_str = datetime.now().strftime('%Y%m%d')
    prefix = f'RD{date_str}'
    sql = """
        SELECT MAX(CAST(SUBSTRING(detail_no, 11, 4) AS UNSIGNED)) AS max_seq
        FROM reimbursement_details
        WHERE detail_no LIKE :prefix_like
          AND CHAR_LENGTH(detail_no) = 14
          AND (is_deleted = 0 OR is_deleted IS NULL)
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
    next_seq = max_seq + 1
    if next_seq > 9999:
        raise ValueError(f'当日报销明细序号已用尽（{date_str}）')
    return f'{prefix}{next_seq:04d}'


def _build_reimbursement_detail_list_filters():
    clauses = []
    params = {}
    applicant = request.args.get('applicant')
    if applicant is not None and str(applicant).strip():
        clauses.append('rd.applicant LIKE :applicant')
        params['applicant'] = f"%{str(applicant).strip()}%"
    for field in ('expense_type', 'approval_status', 'payment_status'):
        raw = request.args.get(field)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        clauses.append(f'rd.{field} = :{field}')
        params[field] = value
    related_flow_no = request.args.get('related_flow_no')
    if related_flow_no is not None and str(related_flow_no).strip():
        clauses.append('rd.related_flow_no = :related_flow_no')
        params['related_flow_no'] = str(related_flow_no).strip()
    related_flow_no_empty = request.args.get('related_flow_no_empty')
    if related_flow_no_empty is not None and str(related_flow_no_empty).strip().lower() in (
        '1', 'true', 'yes',
    ):
        clauses.append(
            "(rd.related_flow_no IS NULL OR TRIM(rd.related_flow_no) = '')",
        )
    related_flow_no_not_empty = request.args.get('related_flow_no_not_empty')
    if related_flow_no_not_empty is not None and str(related_flow_no_not_empty).strip().lower() in (
        '1', 'true', 'yes',
    ):
        clauses.append(
            "(rd.related_flow_no IS NOT NULL AND TRIM(rd.related_flow_no) != '')",
        )
    extra = ''
    if clauses:
        extra = ' AND ' + ' AND '.join(clauses)
    return extra, params


@fin_app.route('/fin/reimbursement_details/list', methods=['GET'])
def get_reimbursement_details_list():
    """报销明细列表（分页，按 namespace_id 隔离）。"""
    try:
        namespace_id = request.args.get('namespace_id')
        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400

        page = request.args.get('page', default=1, type=int)
        page_size = request.args.get('page_size', default=100, type=int)
        if page < 1:
            return jsonify({"code": 400, "msg": "page 必须 >= 1"}), 400
        if page_size < 1 or page_size > 500:
            return jsonify({"code": 400, "msg": "page_size 必须在 1~500 之间"}), 400

        offset = (page - 1) * page_size
        filter_extra, filter_params = _build_reimbursement_detail_list_filters()
        params = {"namespace_id": namespace_id, **filter_params}

        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM reimbursement_details rd
            WHERE rd.namespace_id = :namespace_id
              AND (rd.is_deleted = 0 OR rd.is_deleted IS NULL)
            {filter_extra}
        """
        count_res = current_app.db_manager.execute_query(count_sql, params)
        total = int(count_res[0].get('total', 0)) if count_res else 0

        sql = f"""
            SELECT {REIMBURSEMENT_DETAIL_LIST_SELECT.strip()}
            FROM reimbursement_details rd
            WHERE rd.namespace_id = :namespace_id
              AND (rd.is_deleted = 0 OR rd.is_deleted IS NULL)
            {filter_extra}
            {REIMBURSEMENT_DETAIL_LIST_ORDER_BY.strip()}
            LIMIT :limit OFFSET :offset
        """
        page_params = {**params, 'limit': page_size, 'offset': offset}
        result = current_app.db_manager.execute_query(sql, page_params)

        datetime_fields = {
            'apply_date', 'approval_time', 'payment_time',
            'expense_date', 'invoice_date', 'created_at', 'updated_at',
        }
        safe_rows = []
        if isinstance(result, list):
            for row in result:
                _format_row_datetimes(datetime_fields, row)
                safe_rows.append(_json_safe_reimbursement_row(row))

        response = jsonify({
            'code': 200,
            'msg': 'success',
            'data': safe_rows,
            'total': total,
            'page': page,
            'page_size': page_size,
        })
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
    except Exception as e:
        logger.error(f'获取报销明细列表失败: {str(e)}', exc_info=True)
        return jsonify({
            'code': 500,
            'msg': f'获取报销明细列表失败: {str(e)}',
        }), 500


@fin_app.route('/fin/reimbursement_details/get', methods=['GET'])
def get_reimbursement_detail():
    """按 id 获取单条报销明细。"""
    try:
        record_id = request.args.get('id')
        if not record_id:
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400

        sql = f"""
            SELECT {REIMBURSEMENT_DETAIL_LIST_SELECT.strip()}
            FROM reimbursement_details rd
            WHERE rd.id = :id
              AND (rd.is_deleted = 0 OR rd.is_deleted IS NULL)
            LIMIT 1
        """
        rows = current_app.db_manager.execute_query(sql, {'id': record_id})
        if not rows:
            return jsonify({'code': 404, 'msg': '记录不存在'}), 404

        datetime_fields = {
            'apply_date', 'approval_time', 'payment_time',
            'expense_date', 'invoice_date', 'created_at', 'updated_at',
        }
        row = _json_safe_reimbursement_row(_format_row_datetimes(datetime_fields, rows[0]))
        return jsonify({'code': 200, 'msg': 'success', 'data': row})
    except Exception as e:
        logger.error(f'获取报销明细失败: {str(e)}', exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取报销明细失败: {str(e)}'}), 500


def _normalize_reimbursement_create_data(data):
    """校验并规范化报销明细新增数据。"""
    if not isinstance(data, dict):
        return None, 'data 必须为对象'

    applicant = str(data.get('applicant') or '').strip()
    if not applicant:
        return None, '请填写申请人'

    apply_date = _parse_date_only_value(data.get('apply_date'))
    if not apply_date:
        return None, '申请日期格式无效'

    expense_date = _parse_date_only_value(data.get('expense_date'))
    if not expense_date:
        return None, '费用发生日期格式无效'

    expense_type = str(data.get('expense_type') or '').strip()
    if expense_type not in REIMBURSEMENT_EXPENSE_TYPE_VALUES:
        return None, '费用类型取值不在允许范围内'

    try:
        amount_str = _normalize_decimal_2(data.get('expense_amount'), '')
        if not amount_str:
            return None, '请填写费用金额'
        expense_amount = Decimal(amount_str)
        if expense_amount <= 0:
            return None, '费用金额须大于 0'
    except (InvalidOperation, ValueError):
        return None, '费用金额格式无效'

    created_by = str(data.get('created_by') or applicant).strip() or applicant

    row = {
        'applicant': applicant,
        'apply_date': apply_date,
        'expense_date': expense_date,
        'expense_type': expense_type,
        'expense_amount': expense_amount,
        'expense_currency': 'CNY',
        'approval_status': PENDING_APPROVAL_STATUS,
        'payment_status': '待支付',
        'is_invoice_received': 0,
        'tax_rate': Decimal('0.00'),
        'tax_amount': Decimal('0.00'),
        'amount_without_tax': Decimal('0.00'),
        'created_by': created_by,
        'is_deleted': 0,
    }

    expense_desc = str(data.get('expense_desc') or '').strip()
    if expense_desc:
        row['expense_desc'] = expense_desc

    expense_remark = str(data.get('expense_remark') or '').strip()
    if expense_remark:
        row['expense_remark'] = expense_remark

    applicant_dept = str(data.get('applicant_dept') or '').strip()
    if applicant_dept:
        row['applicant_dept'] = applicant_dept

    return row, None


@fin_app.route('/fin/reimbursement_details/create', methods=['POST'])
def create_reimbursement_detail():
    """新增报销明细。"""
    try:
        payload = request.get_json(silent=True) or {}
        namespace_id = payload.get('namespace_id')
        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400

        data = payload.get('data')

        row, err = _normalize_reimbursement_create_data(data)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400

        row['namespace_id'] = namespace_id

        try:
            row['detail_no'] = _generate_reimbursement_detail_no()
        except ValueError as ve:
            return jsonify({'code': 400, 'msg': str(ve)}), 400

        dup_sql = """
            SELECT id FROM reimbursement_details
            WHERE detail_no = :detail_no
              AND (is_deleted = 0 OR is_deleted IS NULL)
            LIMIT 1
        """
        dup = current_app.db_manager.execute_query(dup_sql, {
            'detail_no': row['detail_no'],
        })
        if dup:
            return jsonify({'code': 400, 'msg': '明细流水号冲突，请重试'}), 400

        new_id = current_app.db_manager.insert_one('reimbursement_details', row)

        return jsonify({
            'code': 200,
            'msg': '新增成功',
            'data': {'id': new_id, 'detail_no': row['detail_no']},
        })
    except Exception as e:
        logger.error(f'新增报销明细失败: {str(e)}', exc_info=True)
        return jsonify({'code': 500, 'msg': f'新增报销明细失败: {str(e)}'}), 500


REIMBURSEMENT_DETAIL_UPDATE_FIELDS = {
    'applicant',
    'applicant_dept',
    'apply_date',
    'expense_date',
    'expense_type',
    'expense_amount',
    'expense_desc',
    'expense_remark',
    'is_invoice_received',
    'invoice_type',
    'invoice_no',
    'invoice_code',
    'invoice_date',
    'attachment_ids',
    'remark',
    'related_flow_no',
}

REIMBURSEMENT_DETAIL_LOCKED_FIELDS = {
    'detail_no',
    'approver',
    'approval_status',
    'approval_time',
    'approval_remark',
    'payment_status',
    'payment_time',
    'payment_method',
    'payment_account',
}


def _clear_reimbursement_invoice_fields(update_data):
    for key in (
        'invoice_type',
        'invoice_no',
        'invoice_code',
        'invoice_date',
        'attachment_ids',
    ):
        update_data[key] = None


def _normalize_reimbursement_update_data(update_data):
    """校验报销明细可更新字段。"""
    if not isinstance(update_data, dict) or not update_data:
        return None, '没有可更新的字段'

    normalized = {}
    for key, raw in update_data.items():
        if key not in REIMBURSEMENT_DETAIL_UPDATE_FIELDS:
            return None, f'字段 {key} 不允许更新'
        normalized[key] = raw

    if 'applicant' in normalized:
        applicant = str(normalized['applicant'] or '').strip()
        if not applicant:
            return None, '申请人不能为空'
        normalized['applicant'] = applicant

    for date_key in ('apply_date', 'expense_date', 'invoice_date'):
        if date_key not in normalized:
            continue
        if normalized[date_key] is None or str(normalized[date_key]).strip() == '':
            if date_key == 'invoice_date':
                normalized[date_key] = None
            else:
                return None, f'{date_key} 不能为空'
        else:
            parsed = _parse_date_only_value(normalized[date_key])
            if not parsed and normalized[date_key] is not None:
                return None, f'{date_key} 格式无效'
            normalized[date_key] = parsed

    if 'expense_type' in normalized:
        et = str(normalized['expense_type'] or '').strip()
        if et not in REIMBURSEMENT_EXPENSE_TYPE_VALUES:
            return None, '费用类型取值不在允许范围内'
        normalized['expense_type'] = et

    if 'expense_amount' in normalized:
        try:
            amount_str = _normalize_decimal_2(normalized['expense_amount'], '')
            if not amount_str:
                return None, '费用金额不能为空'
            expense_amount = Decimal(amount_str)
            if expense_amount <= 0:
                return None, '费用金额须大于 0'
            normalized['expense_amount'] = expense_amount
        except (InvalidOperation, ValueError):
            return None, '费用金额格式无效'

    if 'is_invoice_received' in normalized:
        flag = normalized['is_invoice_received']
        if flag in (True, 'true', '1', 1):
            normalized['is_invoice_received'] = 1
        else:
            normalized['is_invoice_received'] = 0
            _clear_reimbursement_invoice_fields(normalized)

    if normalized.get('is_invoice_received') == 0:
        _clear_reimbursement_invoice_fields(normalized)

    if 'invoice_type' in normalized and normalized['invoice_type'] is not None:
        it = str(normalized['invoice_type']).strip()
        if it and it not in REIMBURSEMENT_INVOICE_TYPE_VALUES:
            return None, '发票类型取值不在允许范围内'
        normalized['invoice_type'] = it or None

    for text_key in (
        'expense_desc', 'expense_remark', 'remark',
        'invoice_no', 'invoice_code', 'attachment_ids', 'applicant_dept',
    ):
        if text_key not in normalized:
            continue
        val = normalized[text_key]
        if val is None or str(val).strip() == '':
            normalized[text_key] = None
        else:
            normalized[text_key] = str(val).strip()

    if 'related_flow_no' in normalized:
        flow_val = normalized['related_flow_no']
        if flow_val is None or str(flow_val).strip() == '':
            normalized['related_flow_no'] = None
        else:
            normalized['related_flow_no'] = str(flow_val).strip()

    if normalized.get('is_invoice_received') == 1:
        for req in ('invoice_type',):
            if req in normalized and not normalized.get(req):
                pass
    return normalized, None


@fin_app.route('/fin/reimbursement_details/update', methods=['POST'])
def update_reimbursement_detail():
    """更新报销明细（禁止改流水号、审批与支付字段）。"""
    try:
        payload = request.get_json(silent=True) or {}
        record_id = payload.get('id')
        data = payload.get('data')

        if record_id is None or record_id == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400
        if not isinstance(data, dict) or not data:
            return jsonify({'code': 400, 'msg': '缺少可更新字段 data'}), 400

        for locked in REIMBURSEMENT_DETAIL_LOCKED_FIELDS:
            if locked in data:
                return jsonify({
                    'code': 400,
                    'msg': f'字段 {locked} 不允许更新',
                }), 400

        lock_sql = """
            SELECT payment_status
            FROM reimbursement_details
            WHERE id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
            LIMIT 1
        """
        lock_rows = current_app.db_manager.execute_query(lock_sql, {'id': record_id})
        if not lock_rows:
            return jsonify({'code': 404, 'msg': '记录不存在'}), 404
        if str(lock_rows[0].get('payment_status') or '').strip() == '已支付':
            return jsonify({'code': 400, 'msg': '已支付记录不可修改'}), 400

        update_data, err = _normalize_reimbursement_update_data(data)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400

        set_clauses = []
        params = {'id': record_id, 'updated_at': datetime.now()}
        for field, value in update_data.items():
            set_clauses.append(f'{field} = :{field}')
            params[field] = value
        set_clauses.append('updated_at = :updated_at')

        sql = f"""
            UPDATE reimbursement_details
            SET {', '.join(set_clauses)}
            WHERE id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
        """
        affected_rows = current_app.db_manager.execute_update(sql, params)
        if affected_rows <= 0:
            return jsonify({'code': 404, 'msg': '未找到可更新的数据'}), 404

        return jsonify({'code': 200, 'msg': '更新成功', 'affected_rows': affected_rows})
    except Exception as e:
        logger.error(f'更新报销明细失败: {str(e)}', exc_info=True)
        return jsonify({'code': 500, 'msg': f'更新报销明细失败: {str(e)}'}), 500


def _map_pat_account_type_to_payment_method(account_type):
    """私账账户类型 → 报销明细支付方式。"""
    at = str(account_type or '').strip()
    mapping = {
        '微信': '微信',
        '支付宝': '支付宝',
        '银行卡': '银行转账',
        '现金': '现金',
    }
    method = mapping.get(at)
    if method and method in REIMBURSEMENT_PAYMENT_METHOD_VALUES:
        return method
    return '银行转账'


def _resolve_pat_payment_sync_fields(pat_row):
    """从私账记录解析需同步至报销明细的支付字段。"""
    row = pat_row or {}
    payment_time = row.get('business_date')
    if payment_time is None:
        payment_time = datetime.now()
    elif not isinstance(payment_time, datetime):
        payment_time = _parse_business_date_value(payment_time) or datetime.now()

    payment_method = _map_pat_account_type_to_payment_method(row.get('account_type'))
    payment_account = row.get('counterparty_account')
    if payment_account is not None:
        payment_account = str(payment_account).strip() or None
    return payment_time, payment_method, payment_account


def _sync_linked_reimbursement_payment_on_verified(flow_no, pat_row=None):
    """私账资金状态为已核销时，将业务日期/账户类型/对方账号及已支付同步至全部已关联报销明细。"""
    flow_no = str(flow_no or '').strip()
    if not flow_no:
        return 0, '流水号为空'

    row = pat_row
    if not row:
        pat_sql = """
            SELECT business_date, account_type, counterparty_account
            FROM private_account_transactions
            WHERE flow_no = :flow_no
              AND (is_deleted = 0 OR is_deleted IS NULL)
            LIMIT 1
        """
        pat_rows = current_app.db_manager.execute_query(pat_sql, {'flow_no': flow_no})
        if not pat_rows:
            return 0, '私账记录不存在'
        row = pat_rows[0]

    payment_time, payment_method, payment_account = _resolve_pat_payment_sync_fields(row)
    now = datetime.now()
    sql = """
        UPDATE reimbursement_details
        SET payment_status = :payment_status,
            payment_time = :payment_time,
            payment_method = :payment_method,
            payment_account = :payment_account,
            updated_at = :updated_at
        WHERE related_flow_no = :flow_no
          AND (is_deleted = 0 OR is_deleted IS NULL)
    """
    affected_rows = current_app.db_manager.execute_update(sql, {
        'payment_status': '已支付',
        'payment_time': payment_time,
        'payment_method': payment_method,
        'payment_account': payment_account,
        'flow_no': flow_no,
        'updated_at': now,
    })
    return affected_rows, None


def _reset_linked_reimbursement_approval(flow_no):
    """私账审批回退待审批：清空本流水已关联报销明细的审批/支付字段。"""
    flow_no = str(flow_no or '').strip()
    if not flow_no:
        return 0, '流水号为空'

    sql = """
        UPDATE reimbursement_details
        SET approver = NULL,
            approval_status = :approval_status,
            approval_time = NULL,
            approval_remark = NULL,
            payment_status = :payment_status,
            payment_time = NULL,
            payment_method = NULL,
            payment_account = NULL,
            updated_at = :updated_at
        WHERE TRIM(COALESCE(related_flow_no, '')) COLLATE utf8mb4_unicode_ci
              = :flow_no COLLATE utf8mb4_unicode_ci
          AND (is_deleted = 0 OR is_deleted IS NULL)
    """
    affected_rows = current_app.db_manager.execute_update(sql, {
        'approval_status': PENDING_APPROVAL_STATUS,
        'payment_status': '待支付',
        'flow_no': flow_no,
        'updated_at': datetime.now(),
    })
    return affected_rows, None


def _reset_and_unlink_linked_reimbursements(flow_no):
    """业务类型由报销切出：清空关联明细审批/支付字段并解除本流水关联。"""
    flow_no = str(flow_no or '').strip()
    if not flow_no:
        return 0, '流水号为空'

    sql = """
        UPDATE reimbursement_details
        SET related_flow_no = NULL,
            approver = NULL,
            approval_status = :approval_status,
            approval_time = NULL,
            approval_remark = NULL,
            payment_status = :payment_status,
            payment_time = NULL,
            payment_method = NULL,
            payment_account = NULL,
            updated_at = :updated_at
        WHERE TRIM(COALESCE(related_flow_no, '')) COLLATE utf8mb4_unicode_ci
              = :flow_no COLLATE utf8mb4_unicode_ci
          AND (is_deleted = 0 OR is_deleted IS NULL)
    """
    affected_rows = current_app.db_manager.execute_update(sql, {
        'approval_status': PENDING_APPROVAL_STATUS,
        'payment_status': '待支付',
        'flow_no': flow_no,
        'updated_at': datetime.now(),
    })
    return affected_rows, None


def _resolve_pat_approval_sync_fields(pat_row, update_data):
    """从私账更新结果解析需同步至报销明细的审批字段。"""
    row = pat_row or {}
    data = update_data or {}
    ast = str(row.get('approval_status') or data.get('approval_status') or '').strip()
    approver = str(row.get('approver') or data.get('approver') or '').strip()
    approval_time = row.get('approval_time')
    if approval_time is None:
        approval_time = data.get('approval_time')
    if ast == '已通过':
        remark = '同意申请'
    elif ast == '已驳回':
        remark = str(
            data.get('reasons_approval_rejection')
            or row.get('reasons_approval_rejection')
            or '',
        ).strip()
    else:
        remark = ''
    return ast, approver, approval_time, remark


def _sync_linked_reimbursement_approval(flow_no, approver, approval_status, approval_time, approval_remark):
    """将私账审批结果同步到全部已关联报销明细（related_flow_no = flow_no）。"""
    flow_no = str(flow_no or '').strip()
    if not flow_no:
        return 0, '流水号为空'
    ast = str(approval_status or '').strip()
    if ast not in ('已通过', '已驳回'):
        return 0, '审批状态无效'
    approver_val = str(approver or '').strip()
    if not approver_val:
        return 0, '审批人为空'
    remark = str(approval_remark or '').strip()
    if ast == '已通过':
        remark = remark or '同意申请'
    elif not remark:
        return 0, '驳回须填写审批意见'

    at_dt = approval_time
    if at_dt is None:
        at_dt = datetime.now()
    elif not isinstance(at_dt, datetime):
        at_dt = _parse_business_date_value(at_dt) or datetime.now()

    sql = """
        UPDATE reimbursement_details
        SET approver = :approver,
            approval_status = :approval_status,
            approval_time = :approval_time,
            approval_remark = :approval_remark,
            updated_at = :updated_at
        WHERE related_flow_no = :flow_no
          AND (is_deleted = 0 OR is_deleted IS NULL)
    """
    affected_rows = current_app.db_manager.execute_update(sql, {
        'approver': approver_val,
        'approval_status': ast,
        'approval_time': at_dt,
        'approval_remark': remark,
        'flow_no': flow_no,
        'updated_at': datetime.now(),
    })
    return affected_rows, None


def _sync_pat_approval_to_linked_reimbursements(namespace_id, record_id, update_data):
    """私账审批通过/驳回后，批量同步至已关联报销明细。"""
    pat_row_sql = """
        SELECT flow_no, approver, approval_status, approval_time,
               reasons_approval_rejection
        FROM private_account_transactions
        WHERE namespace_id = :namespace_id
          AND id = :id
          AND (is_deleted = 0 OR is_deleted IS NULL)
        LIMIT 1
    """
    pat_rows = current_app.db_manager.execute_query(pat_row_sql, {
        'namespace_id': namespace_id,
        'id': record_id,
    })
    if not pat_rows:
        return 0, '私账记录不存在', None

    pat_row = pat_rows[0]
    flow_no = str(pat_row.get('flow_no') or '').strip()
    if not flow_no:
        return 0, '流水号为空', None

    ast, approver, approval_time, remark = _resolve_pat_approval_sync_fields(
        pat_row, update_data,
    )
    if ast not in ('已通过', '已驳回'):
        return 0, None, None

    count, err = _sync_linked_reimbursement_approval(
        flow_no, approver, ast, approval_time, remark,
    )
    amount_str = None
    if not err:
        amount_str, _ = _sync_private_account_reimbursement_amount(flow_no)
    return count, err, amount_str


def _sum_linked_reimbursement_amount(flow_no):
    """按私账流水号汇总已关联报销明细费用金额。"""
    flow_no = str(flow_no or '').strip()
    if not flow_no:
        return Decimal('0.00')
    sql = """
        SELECT COALESCE(SUM(expense_amount), 0) AS total
        FROM reimbursement_details
        WHERE related_flow_no = :flow_no
          AND (is_deleted = 0 OR is_deleted IS NULL)
    """
    rows = current_app.db_manager.execute_query(sql, {'flow_no': flow_no})
    if not rows:
        return Decimal('0.00')
    return Decimal(str(rows[0].get('total') or 0))


def _sync_private_account_reimbursement_amount(flow_no):
    """将已关联报销明细金额合计写入私账往来 amount。"""
    flow_no = str(flow_no or '').strip()
    if not flow_no:
        return None, '流水号为空'
    amount_str = _normalize_decimal_2(_sum_linked_reimbursement_amount(flow_no), '0')
    sql = """
        UPDATE private_account_transactions
        SET amount = :amount,
            updated_at = :updated_at
        WHERE flow_no = :flow_no
          AND business_type = :business_type
          AND (is_deleted = 0 OR is_deleted IS NULL)
    """
    affected_rows = current_app.db_manager.execute_update(sql, {
        'amount': Decimal(amount_str),
        'flow_no': flow_no,
        'business_type': REIMBURSEMENT_BUSINESS_TYPE,
        'updated_at': datetime.now(),
    })
    if affected_rows <= 0:
        return None, '未找到对应报销私账流水'
    return amount_str, None


@fin_app.route('/fin/reimbursement_details/link_flow', methods=['POST'])
def link_reimbursement_detail_flow():
    """将报销明细关联到私账流水号（仅更新 related_flow_no）。"""
    try:
        payload = request.get_json(silent=True) or {}
        record_id = payload.get('id')
        related_flow_no = payload.get('related_flow_no')

        if record_id is None or record_id == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400
        flow_no = str(related_flow_no or '').strip()
        if not flow_no:
            return jsonify({'code': 400, 'msg': '缺少必要参数 related_flow_no'}), 400

        check_sql = """
            SELECT id, related_flow_no
            FROM reimbursement_details
            WHERE id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
            LIMIT 1
        """
        rows = current_app.db_manager.execute_query(check_sql, {'id': record_id})
        if not rows:
            return jsonify({'code': 404, 'msg': '记录不存在'}), 404

        existing = str(rows[0].get('related_flow_no') or '').strip()
        if existing and existing != flow_no:
            return jsonify({
                'code': 400,
                'msg': f'该明细已关联流水「{existing}」，请先移除后再关联',
            }), 400

        sql = """
            UPDATE reimbursement_details
            SET related_flow_no = :related_flow_no,
                updated_at = :updated_at
            WHERE id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
        """
        affected_rows = current_app.db_manager.execute_update(sql, {
            'id': record_id,
            'related_flow_no': flow_no,
            'updated_at': datetime.now(),
        })
        if affected_rows <= 0:
            return jsonify({'code': 404, 'msg': '未找到可更新的数据'}), 404

        sync_amount, sync_err = _sync_private_account_reimbursement_amount(flow_no)
        if sync_err:
            logger.warning('关联成功但同步私账金额失败 flow_no=%s: %s', flow_no, sync_err)

        return jsonify({
            'code': 200,
            'msg': '关联成功',
            'affected_rows': affected_rows,
            'data': {'amount': sync_amount, 'related_flow_no': flow_no},
        })
    except Exception as e:
        logger.error(f'关联报销明细流水失败: {str(e)}', exc_info=True)
        return jsonify({'code': 500, 'msg': f'关联失败: {str(e)}'}), 500


@fin_app.route('/fin/reimbursement_details/unlink_flow', methods=['POST'])
def unlink_reimbursement_detail_flow():
    """清除报销明细的 related_flow_no（仅解除关联）。"""
    try:
        payload = request.get_json(silent=True) or {}
        record_id = payload.get('id')
        related_flow_no = payload.get('related_flow_no')

        if record_id is None or record_id == '':
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400

        check_sql = """
            SELECT id, related_flow_no
            FROM reimbursement_details
            WHERE id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
            LIMIT 1
        """
        rows = current_app.db_manager.execute_query(check_sql, {'id': record_id})
        if not rows:
            return jsonify({'code': 404, 'msg': '记录不存在'}), 404

        existing = str(rows[0].get('related_flow_no') or '').strip()
        if not existing:
            return jsonify({'code': 200, 'msg': '已是未关联状态', 'affected_rows': 0})

        if related_flow_no is not None and str(related_flow_no).strip():
            expect = str(related_flow_no).strip()
            if existing != expect:
                return jsonify({
                    'code': 400,
                    'msg': f'该明细关联的是流水「{existing}」，与当前流水不一致',
                }), 400

        sql = """
            UPDATE reimbursement_details
            SET related_flow_no = NULL,
                updated_at = :updated_at
            WHERE id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
        """
        affected_rows = current_app.db_manager.execute_update(sql, {
            'id': record_id,
            'updated_at': datetime.now(),
        })
        if affected_rows <= 0:
            return jsonify({'code': 404, 'msg': '未找到可更新的数据'}), 404

        sync_flow = str(related_flow_no or '').strip() or existing
        sync_amount, sync_err = _sync_private_account_reimbursement_amount(sync_flow)
        if sync_err:
            logger.warning('解除关联成功但同步私账金额失败 flow_no=%s: %s', sync_flow, sync_err)

        return jsonify({
            'code': 200,
            'msg': '已解除关联',
            'affected_rows': affected_rows,
            'data': {'amount': sync_amount, 'related_flow_no': sync_flow},
        })
    except Exception as e:
        logger.error(f'解除报销明细流水关联失败: {str(e)}', exc_info=True)
        return jsonify({'code': 500, 'msg': f'解除关联失败: {str(e)}'}), 500


@fin_app.route('/fin/reimbursement_details/delete', methods=['POST'])
def delete_reimbursement_detail():
    """报销明细软删除。"""
    try:
        payload = request.get_json(silent=True) or {}
        record_id = payload.get('id')
        if not record_id:
            return jsonify({'code': 400, 'msg': '缺少必要参数 id'}), 400

        sql = """
            UPDATE reimbursement_details
            SET is_deleted = 1, updated_at = :updated_at
            WHERE id = :id
              AND (is_deleted = 0 OR is_deleted IS NULL)
        """
        affected_rows = current_app.db_manager.execute_update(sql, {
            'id': record_id,
            'updated_at': datetime.now(),
        })
        if affected_rows <= 0:
            return jsonify({'code': 404, 'msg': '未找到可删除的数据或已删除'}), 404
        return jsonify({'code': 200, 'msg': '删除成功'})
    except Exception as e:
        logger.error(f'删除报销明细失败: {str(e)}', exc_info=True)
        return jsonify({'code': 500, 'msg': f'删除报销明细失败: {str(e)}'}), 500


# 导出所有路由函数
__all__ = ['fin_app', 'get_bankStatementsList', 'deleteBankStatementAPI','editBankStatementAPI', 'uploadBankStatementAPI']
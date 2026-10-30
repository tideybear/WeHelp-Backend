import json
import logging
from datetime import datetime, date, timezone
from email.utils import parsedate_to_datetime

from flask import Blueprint, request, jsonify, current_app

from utils.deepseek_analysis import chat, fetch_webpage, search_web

logger = logging.getLogger(__name__)

# CRM 客户深度分析统一使用的模型，只调 deepseek-flash
DEEPSEEK_MODEL = 'deepseek-flash'

crm_app = Blueprint('crm', __name__, url_prefix='/crm')

# 联系人可更新字段白名单
CONTACT_UPDATABLE_FIELDS = {
    'customer_id', 'name', 'title', 'department', 'gender', 'phone', 'mobile',
    'email', 'wechat', 'whatsapp', 'linkedin', 'language', 'is_primary',
    'birthday', 'notes',
}

# 联系人可写入字段白名单（新增时额外允许 namespace_id）
CONTACT_WRITABLE_FIELDS = CONTACT_UPDATABLE_FIELDS | {'namespace_id'}

# 跟进记录可写入字段白名单（对应 crm_follow_up_records 基表 / v_crm_follow_up_records 视图）
FOLLOW_UP_WRITABLE_FIELDS = {
    'namespace_id', 'customer_id', 'contact_id', 'follow_up_at', 'follow_up_type_id',
    'content', 'result', 'follow_up_status_id', 'next_follow_up_at', 'owner_id',
    'inquiry_id', 'direction', 'channel', 'subject', 'message_body', 'language',
    'related_product_id', 'related_quote_id', 'priority', 'next_action',
    'external_message_id',
}

# 跟进记录可更新字段白名单（namespace_id 不允许变更，避免跨空间串数据）
FOLLOW_UP_UPDATABLE_FIELDS = FOLLOW_UP_WRITABLE_FIELDS - {'namespace_id'}

# 跟进记录 datetime 字段
FOLLOW_UP_DATETIME_FIELDS = ('follow_up_at', 'next_follow_up_at')

# 客户产品/需求可写入字段白名单
CUSTOMER_PRODUCT_WRITABLE_FIELDS = {
    'namespace_id', 'customer_id', 'product_name', 'product_category', 'spec',
    'demand_desc', 'quantity', 'unit', 'target_price', 'currency', 'stage',
    'remark',
}

# 客户产品/需求可更新字段白名单
CUSTOMER_PRODUCT_UPDATABLE_FIELDS = CUSTOMER_PRODUCT_WRITABLE_FIELDS - {'namespace_id'}


def _normalize_datetime(value, date_only=False):
    """把各种日期格式统一成 MySQL 可接受的格式"""
    if value in (None, ''):
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y-%m-%d') if date_only else value.strftime('%Y-%m-%d %H:%M:%S')

    text = str(value).strip()
    # RFC 2822，如 'Tue, 12 Mar 1985 00:00:00 GMT'
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed.strftime('%Y-%m-%d') if date_only else parsed.strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        pass

    # ISO 8601 等
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime('%Y-%m-%d') if date_only else parsed.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue

    return text


@crm_app.route('/list', methods=['GET'])
def get_customer_list():
    """获取客户列表（从视图 v_crm_master_data 取数）"""
    namespace_id = request.args.get('namespace_id')
    if not namespace_id:
        return jsonify({'message': '缺少 namespace_id'}), 400

    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 20, type=int)
    sort_field = request.args.get('sortField', 'created_at')
    sort_order = 'DESC' if request.args.get('sortOrder', 'desc') == 'desc' else 'ASC'

    params = {'namespace_id': namespace_id}
    where = "namespace_id = :namespace_id AND is_deleted = 0"

    # 客户名称
    customer_name = request.args.get('customer_name')
    if customer_name:
        params['customer_name'] = f"%{customer_name}%"
        where += " AND company_name LIKE :customer_name"

    # 客户编码
    customer_code = request.args.get('customer_code')
    if customer_code:
        params['customer_code'] = f"%{customer_code}%"
        where += " AND customer_no LIKE :customer_code"

    # 联系人
    contact_person = request.args.get('contact_person')
    if contact_person:
        params['contact_person'] = f"%{contact_person}%"
        where += " AND customer_basic_info LIKE :contact_person"

    # 关键字（综合搜索）
    keyword = request.args.get('keyword')
    if keyword:
        params['keyword'] = f"%{keyword}%"
        where += " AND (company_name LIKE :keyword OR company_name_en LIKE :keyword OR short_name LIKE :keyword OR customer_no LIKE :keyword OR customer_basic_info LIKE :keyword)"

    # 筛选条件
    for key in ['status', 'owner_id', 'region_id', 'industry_id', 'is_followed_up']:
        val = request.args.get(key)
        if val:
            params[key] = val
            where += f" AND {key} = :{key}"

    # 总数
    total = current_app.db_manager.execute_query(
        f"SELECT COUNT(*) as total FROM v_crm_master_data WHERE {where}", params
    )[0]['total']

    # 列表
    params['limit'], params['offset'] = page_size, (page - 1) * page_size
    rows = current_app.db_manager.execute_query(
        f"SELECT * FROM v_crm_master_data WHERE {where} ORDER BY {sort_field} {sort_order} LIMIT :limit OFFSET :offset",
        params
    )

    return jsonify({
        'result': {
            'list': rows,
            'total': total,
            'page': page,
            'pageSize': page_size,
        }
    })


@crm_app.route('/detail/<int:id>', methods=['GET'])
def get_customer_detail(id):
    """获取客户详情"""
    rows = current_app.db_manager.execute_query(
        "SELECT * FROM v_crm_master_data WHERE id = :id LIMIT 1",
        {'id': id}
    )
    if not rows:
        return jsonify({'message': '客户不存在'}), 404
    return jsonify({'result': rows[0]})


@crm_app.route('/delete/<int:id>', methods=['DELETE'])
def delete_customer(id):
    """删除客户（软删除）"""
    rowcount = current_app.db_manager.execute_update(
        "UPDATE crm_master_data SET is_deleted = 1 WHERE id = :id",
        {'id': id}
    )
    if rowcount == 0:
        return jsonify({'message': '客户不存在'}), 404
    return jsonify({'result': {'id': id}})


@crm_app.route('/update/<int:id>', methods=['PUT'])
def update_customer(id):
    """更新客户"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供更新数据'}), 400

    # 禁止修改的字段
    for field in ['id', 'created_at', 'is_deleted']:
        data.pop(field, None)

    if not data:
        return jsonify({'message': '没有有效字段需要更新'}), 400

    success = current_app.db_manager.update_by_id('crm_master_data', id, data)
    if not success:
        return jsonify({'message': '客户不存在'}), 404
    return jsonify({'result': {'id': id}})


@crm_app.route('/contacts', methods=['GET'])
def get_customer_contacts():
    """获取客户联系人列表（namespace_id、customer_id 可选，传了才过滤）"""
    namespace_id = request.args.get('namespace_id')
    customer_id = request.args.get('customer_id')

    params = {}
    # crm_contacts 自带 namespace_id，直接过滤；不能关联客户主数据，
    # 因为存在 customer_id 为 NULL 的联系人（INNER JOIN 会漏掉）
    where = ["c.is_deleted = 0"]

    if customer_id:
        params['customer_id'] = customer_id
        where.append("c.customer_id = :customer_id")
    if namespace_id:
        params['namespace_id'] = namespace_id
        where.append("c.namespace_id = :namespace_id")

    rows = current_app.db_manager.execute_query(
        f"""
        SELECT c.*
        FROM v_crm_contacts c
        WHERE {' AND '.join(where)}
        ORDER BY c.is_primary DESC, c.id
        """,
        params
    )
    return jsonify({'result': {'list': rows, 'total': len(rows)}})


@crm_app.route('/contacts/create', methods=['POST'])
def create_customer_contact():
    """新增客户联系人"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供联系人数据'}), 400

    # 只保留可写字段
    data = {k: v for k, v in data.items() if k in CONTACT_WRITABLE_FIELDS}
    if not data:
        return jsonify({'message': '没有有效字段需要写入'}), 400

    if not data.get('name'):
        return jsonify({'message': '缺少必填字段: name'}), 400

    # 日期标准化
    if 'birthday' in data:
        data['birthday'] = _normalize_datetime(data['birthday'], date_only=True)

    # namespace_id：优先取传入值，否则按客户反查
    namespace_id = data.pop('namespace_id', None)
    if not namespace_id and data.get('customer_id'):
        rows = current_app.db_manager.execute_query(
            "SELECT namespace_id FROM v_crm_master_data WHERE id = :customer_id LIMIT 1",
            {'customer_id': data['customer_id']}
        )
        if rows:
            namespace_id = rows[0].get('namespace_id')
    if namespace_id:
        data['namespace_id'] = namespace_id

    data['is_deleted'] = 0
    new_id = current_app.db_manager.insert_one('crm_contacts', data)
    return jsonify({'result': {'id': new_id}}), 201


@crm_app.route('/contacts/delete/<int:id>', methods=['DELETE'])
def delete_customer_contact(id):
    """删除客户联系人（软删除）"""
    rowcount = current_app.db_manager.execute_update(
        "UPDATE crm_contacts SET is_deleted = 1 WHERE id = :id",
        {'id': id}
    )
    if rowcount == 0:
        return jsonify({'message': '联系人不存在'}), 404
    return jsonify({'result': {'id': id}})


@crm_app.route('/contacts/update/<int:id>', methods=['PUT'])
def update_customer_contact(id):
    """更新客户联系人"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供更新数据'}), 400

    # 只允许更新白名单字段
    data = {k: v for k, v in data.items() if k in CONTACT_UPDATABLE_FIELDS}
    if not data:
        return jsonify({'message': '没有有效字段需要更新'}), 400

    # 日期字段标准化（前端可能传 RFC 2822 / ISO 格式）
    if 'birthday' in data:
        data['birthday'] = _normalize_datetime(data['birthday'], date_only=True)

    success = current_app.db_manager.update_by_id('crm_contacts', id, data)
    if not success:
        return jsonify({'message': '联系人不存在'}), 404
    return jsonify({'result': {'id': id}})


@crm_app.route('/follow-up-records', methods=['GET'])
def get_follow_up_records():
    """获取跟进记录列表（namespace_id、customer_id、contact_id 可选，传了才过滤）"""
    namespace_id = request.args.get('namespace_id')
    customer_id = request.args.get('customer_id')
    contact_id = request.args.get('contact_id')
    owner_id = request.args.get('owner_id')
    follow_up_status_id = request.args.get('follow_up_status_id')
    follow_up_type_id = request.args.get('follow_up_type_id')
    channel = request.args.get('channel')

    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 20, type=int)

    params = {}
    where = ["is_deleted = 0"]

    if customer_id:
        params['customer_id'] = customer_id
        where.append("customer_id = :customer_id")
    if namespace_id:
        params['namespace_id'] = namespace_id
        where.append("namespace_id = :namespace_id")
    if contact_id:
        params['contact_id'] = contact_id
        where.append("contact_id = :contact_id")
    if owner_id:
        params['owner_id'] = owner_id
        where.append("owner_id = :owner_id")
    if follow_up_status_id:
        params['follow_up_status_id'] = follow_up_status_id
        where.append("follow_up_status_id = :follow_up_status_id")
    if follow_up_type_id:
        params['follow_up_type_id'] = follow_up_type_id
        where.append("follow_up_type_id = :follow_up_type_id")
    if channel:
        params['channel'] = channel
        where.append("channel = :channel")

    where_sql = ' AND '.join(where)

    total = current_app.db_manager.execute_query(
        f"SELECT COUNT(*) as total FROM v_crm_follow_up_records WHERE {where_sql}", params
    )[0]['total']

    params['limit'], params['offset'] = page_size, (page - 1) * page_size
    rows = current_app.db_manager.execute_query(
        f"""
        SELECT *
        FROM v_crm_follow_up_records
        WHERE {where_sql}
        ORDER BY follow_up_at DESC, id DESC
        LIMIT :limit OFFSET :offset
        """,
        params
    )

    return jsonify({
        'result': {
            'list': rows,
            'total': total,
            'page': page,
            'pageSize': page_size,
        }
    })


@crm_app.route('/follow-up-records/<int:id>', methods=['GET'])
def get_follow_up_record_detail(id):
    """获取跟进记录详情"""
    rows = current_app.db_manager.execute_query(
        "SELECT * FROM v_crm_follow_up_records WHERE id = :id AND is_deleted = 0 LIMIT 1",
        {'id': id}
    )
    if not rows:
        return jsonify({'message': '跟进记录不存在'}), 404
    return jsonify({'result': rows[0]})


@crm_app.route('/follow-up-records/create', methods=['POST'])
def create_follow_up_record():
    """创建跟进记录"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供跟进记录数据'}), 400

    # 只保留可写字段
    data = {k: v for k, v in data.items() if k in FOLLOW_UP_WRITABLE_FIELDS}
    if not data:
        return jsonify({'message': '没有有效字段需要写入'}), 400

    for field in ['customer_id', 'follow_up_at']:
        if not data.get(field):
            return jsonify({'message': f'缺少必填字段: {field}'}), 400

    # 日期字段标准化（前端可能传 RFC 2822 / ISO 格式）
    for field in FOLLOW_UP_DATETIME_FIELDS:
        if field in data:
            data[field] = _normalize_datetime(data[field])

    # namespace_id：优先取传入值，否则按客户反查
    namespace_id = data.pop('namespace_id', None)
    if not namespace_id:
        rows = current_app.db_manager.execute_query(
            "SELECT namespace_id FROM v_crm_master_data WHERE id = :customer_id LIMIT 1",
            {'customer_id': data['customer_id']}
        )
        if rows:
            namespace_id = rows[0].get('namespace_id')
    if namespace_id:
        data['namespace_id'] = namespace_id

    data['is_deleted'] = 0
    new_id = current_app.db_manager.insert_one('crm_follow_up_records', data)
    return jsonify({'result': {'id': new_id}}), 201


@crm_app.route('/follow-up-records/update/<int:id>', methods=['PUT'])
def update_follow_up_record(id):
    """更新跟进记录"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供更新数据'}), 400

    # 只允许更新白名单字段
    data = {k: v for k, v in data.items() if k in FOLLOW_UP_UPDATABLE_FIELDS}
    if not data:
        return jsonify({'message': '没有有效字段需要更新'}), 400

    # 日期字段标准化
    for field in FOLLOW_UP_DATETIME_FIELDS:
        if field in data:
            data[field] = _normalize_datetime(data[field])

    success = current_app.db_manager.update_by_id('crm_follow_up_records', id, data)
    if not success:
        return jsonify({'message': '跟进记录不存在'}), 404
    return jsonify({'result': {'id': id}})


@crm_app.route('/follow-up-records/delete/<int:id>', methods=['DELETE'])
def delete_follow_up_record(id):
    """删除跟进记录（软删除）"""
    rowcount = current_app.db_manager.execute_update(
        "UPDATE crm_follow_up_records SET is_deleted = 1 WHERE id = :id",
        {'id': id}
    )
    if rowcount == 0:
        return jsonify({'message': '跟进记录不存在'}), 404
    return jsonify({'result': {'id': id}})


@crm_app.route('/customer/products', methods=['GET'])
def get_customer_products():
    """获取客户产品/需求列表（namespace_id、customer_id 可选，传了才过滤）"""
    namespace_id = request.args.get('namespace_id')
    customer_id = request.args.get('customer_id')

    params = {}
    where = ["is_deleted = 0"]

    if customer_id:
        params['customer_id'] = customer_id
        where.append("customer_id = :customer_id")
    if namespace_id:
        params['namespace_id'] = namespace_id
        where.append("namespace_id = :namespace_id")

    where_sql = ' AND '.join(where)

    total = current_app.db_manager.execute_query(
        f"SELECT COUNT(*) as total FROM v_crm_customer_products WHERE {where_sql}", params
    )[0]['total']

    rows = current_app.db_manager.execute_query(
        f"SELECT * FROM v_crm_customer_products WHERE {where_sql} ORDER BY id DESC",
        params
    )
    return jsonify({'result': {'list': rows, 'total': total}})


@crm_app.route('/customer/products/create', methods=['POST'])
def create_customer_product():
    """创建客户产品/需求"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供产品需求数据'}), 400

    data = {k: v for k, v in data.items() if k in CUSTOMER_PRODUCT_WRITABLE_FIELDS}
    if not data:
        return jsonify({'message': '没有有效字段需要写入'}), 400

    for field in ['customer_id', 'product_name']:
        if not data.get(field):
            return jsonify({'message': f'缺少必填字段: {field}'}), 400

    # namespace_id：优先取传入值，否则按客户反查
    namespace_id = data.pop('namespace_id', None)
    if not namespace_id:
        rows = current_app.db_manager.execute_query(
            "SELECT namespace_id FROM v_crm_master_data WHERE id = :customer_id LIMIT 1",
            {'customer_id': data['customer_id']}
        )
        if rows:
            namespace_id = rows[0].get('namespace_id')
    if namespace_id:
        data['namespace_id'] = namespace_id

    data['is_deleted'] = 0
    new_id = current_app.db_manager.insert_one('crm_customer_products', data)
    return jsonify({'result': {'id': new_id}}), 201


@crm_app.route('/customer/products/update/<int:id>', methods=['PUT'])
def update_customer_product(id):
    """更新客户产品/需求"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供更新数据'}), 400

    data = {k: v for k, v in data.items() if k in CUSTOMER_PRODUCT_UPDATABLE_FIELDS}
    if not data:
        return jsonify({'message': '没有有效字段需要更新'}), 400

    success = current_app.db_manager.update_by_id('crm_customer_products', id, data)
    if not success:
        return jsonify({'message': '客户产品不存在'}), 404
    return jsonify({'result': {'id': id}})


@crm_app.route('/customer/products/delete/<int:id>', methods=['DELETE'])
def delete_customer_product(id):
    """删除客户产品/需求（软删除）"""
    rowcount = current_app.db_manager.execute_update(
        "UPDATE crm_customer_products SET is_deleted = 1 WHERE id = :id",
        {'id': id}
    )
    if rowcount == 0:
        return jsonify({'message': '客户产品不存在'}), 404
    return jsonify({'result': {'id': id}})


# 深度分析输出的字段定义（不再完全按 crm_master_data 表结构）
# 每项为 (输出 key, 中文说明, English description, 取值要求)
ANALYSIS_FIELDS = (
    ('company_name', '公司名称', 'Company name', '公司的完整法定名称，用注册地通用写法，这里能用中文就用中文，和英文名称区分开来'),
    ('company_name_en', '公司英文名称', 'Company name in English', '英文全称'),
    ('short_name', '公司简称', 'Company short name', '常用简称或品牌名'),
    ('company_type', '企业类型', 'Company type', '如生产型企业/贸易型企业/服务型企业/政府机构/个人等'),
    ('website', '公司官网', 'Official website', '必须是真实存在的官网地址，以搜索结果为准'),
    ('country', '国家/地区', 'Country or region', '公司总部所在的国家或地区，写英文国名'),
    ('country_cn', '国家/地区（中文）', 'Country or region in Chinese', '同一国家的中文名，如 英国'),
    ('region', '所属地区', 'region', '东亚、东南亚、南亚、西欧等划分'),
    ('address', '总部地址', 'Headquarters address', '公司注册地址或总部办公地址'),
    ('founded_year', '成立年份', 'Year founded', '公司成立年份，只写 4 位数字'),
    ('industry', '所属行业', 'Industry', '主营业务所属行业，如电气设备批发、建筑材料等'),
    ('industry_cn', '所属行业（中文）', 'Industry in Chinese', '同上的中文表述'),
    ('business_scope', '主营业务介绍', 'Business scope', '公司主要经营什么，面向哪些客户群体'),
    ('main_products', '主要产品/服务', 'Main products or services', '列举具体产品线或服务内容'),
    ('company_size', '企业规模', 'Company size', '具体员工规模数量区间优先，如果没有则小型/中型/大型/特大型大概区间'),
    ('employee_count', '员工人数', 'Number of employees', '大致员工数量，没有确切数据时写区间'),
    ('company_revenue', '年营业额', 'Annual revenue', '最近可查的年度营收金额，具体到个位数返回，如果类似$1.27B这种，则写1270000000'),
    ('revenue_currency', '营收币种', 'Revenue currency', '营收金额的币种，如 GBP、CNY、USD'),
    ('parent_company', '母公司/所属集团', 'Parent company or group', '公司所属的母公司或集团，没有则留空'),
    ('listing_status', '上市情况', 'Listing status', '是否上市、交易所与股票代码，没有则留空'),
    ('export_markets', '主要市场/出口地区', 'Main markets or export regions', '公司产品主要销售或出口到哪些国家地区'),
    ('certifications', '资质与认证', 'Certifications', '如 ISO、CE、UL 等认证，没有则留空'),
    ('customer_needs', '采购需求分析', 'Procurement needs', '该公司作为潜在客户可能采购什么产品或服务'),
    ('competitive_advantages', '竞争优势', 'Competitive advantages', '该公司相对同行具备哪些优势'),
    ('key_contacts', '关键联系人', 'Key contacts', '姓名、职位、邮箱或电话，能查到的都写'),
    ('follow_up_strategy', '跟进策略', 'Follow-up strategy', '针对该客户的建议跟进方式与切入点'),
    ('remark', '备注', 'Remarks', '其他值得记录的补充信息，或信息缺口说明'),
    ('background_check', '背景调查', 'Background Check', '背景调查结,客户相关的背景'),
)

# 输出结果里保留的 key（与 ANALYSIS_FIELDS 对应）
ANALYSIS_FIELD_KEYS = tuple(item[0] for item in ANALYSIS_FIELDS)


@crm_app.route('/deep-analysis', methods=['GET', 'POST'])
def get_customer_deep_analysis(data=None):
    """客户深度分析

    入参 data 为要交给 DeepSeek 分析的原始消息内容（字符串或字典）。
    GET 时从 query string 的 data 参数取（JSON 字符串），
    POST 时从 JSON body 取（对象或 {data: ...}）。
    分析结果按 crm_master_data 的字段结构返回，模型没有给出的字段保留但为空。
    """
    if data is None:
        # POST：优先读 JSON body
        if request.method == 'POST':
            body = request.get_json(silent=True)
            if isinstance(body, dict):
                if 'data' in body:
                    data = body['data']
                else:
                    data = body
            elif body is not None:
                data = body
        # GET：读 query string
        if data is None:
            raw_data = request.args.get('data')
            if raw_data:
                data = raw_data
            else:
                data = {k: v for k, v in request.args.items() if k != 'data'}

    if data in (None, '', {}, []):
        return jsonify({'message': '缺少分析数据'}), 400

    # 字符串尝试按 JSON 解析，失败则当作纯文本消息
    input_data = {}
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                input_data = parsed
        except (TypeError, ValueError):
            input_data = {}
    elif isinstance(data, dict):
        input_data = data
    else:
        return jsonify({'message': '参数格式不正确'}), 400

    # 1) 先根据公司名搜索官网（不依赖模型，模型给的域名可能是幻觉）
    company_name = _pick_company_name(input_data)
    website = ''
    page = None
    search_results = []
    if company_name:
        website, search_results = _search_company_website(input_data, with_content=True) or ('', [])
        if website:
            page = _fetch_website_content(website)

    # 2) 把官网内容与传入数据一起交给模型，单轮分析输出
    merged = dict(input_data)
    if website:
        merged['website'] = website
    if page:
        merged['_website_content'] = page
    site_pages = _collect_site_pages(search_results, website)
    if site_pages:
        merged['_site_pages'] = site_pages

    try:
        raw = chat(_build_deep_analysis_messages(merged), model=DEEPSEEK_MODEL, thinking='disabled')
    except Exception as exc:
        logger.error('客户深度分析调用 DeepSeek 失败: %s', exc, exc_info=True)
        return jsonify({'message': f'深度分析生成失败: {exc}'}), 500

    analysis = _parse_analysis_json(raw)

    # 3) 以传入数据为基线，模型结果覆盖；website 已由搜索确定，不允许模型改写
    result = {key: '' for key in ANALYSIS_FIELD_KEYS}
    for key in ANALYSIS_FIELD_KEYS:
        if key in input_data:
            result[key] = input_data[key]
    for key in ANALYSIS_FIELD_KEYS:
        if key == 'website':
            continue
        value = analysis.get(key)
        if value not in (None, '', [], {}):
            result[key] = value
    if website:
        result['website'] = website

    return jsonify({'result': result})


def _pick_company_name(input_data):
    """从入参里取公司名，用于搜索官网。"""
    for key in ('company_name', 'company_name_en', 'short_name'):
        value = input_data.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ''


def _collect_site_pages(search_results, website, max_pages=4, max_chars=4000):
    """收集官网域名下各子页的正文，供模型分析。

    官网首页常是 JS 渲染页，直接抓取往往只拿到标题；而搜索结果里的
    子页（公司简介、市场与产品页）已带回了正文，信息更完整。

    Args:
        search_results: search_web 返回的结果列表
        website: 官网地址，用于筛选同域名的页面
        max_pages: 最多收集几页
        max_chars: 每页正文最多保留字符数

    Returns:
        页面列表，每项为 {'url': ..., 'title': ..., 'content': ...}。
    """
    if not search_results or not website:
        return []

    def domain_of(url):
        host = str(url).split('://')[-1].split('/')[0].lower().split('?')[0]
        return host[4:] if host.startswith('www.') else host

    site_domain = domain_of(website)
    pages = []
    for item in search_results:
        url = (item.get('url') or '').strip()
        if not url or domain_of(url) != site_domain:
            continue
        text = (item.get('content') or item.get('summary') or '').strip()
        if not text:
            continue
        pages.append({
            'url': url,
            'title': item.get('title') or '',
            'content': text[:max_chars],
        })
        if len(pages) >= max_pages:
            break
    return pages


def _translate_company_name(company_name):
    """让模型把中文公司名译成英文，用于搜索英文官网。

    失败时返回 None，不影响主流程。
    """
    try:
        raw = chat(
            f'把下面的公司名称翻译成英文，只输出英文名称本身，不要任何解释。\n\n{company_name}',
            model=DEEPSEEK_MODEL,
            thinking='disabled',
        )
    except Exception as exc:
        logger.warning('公司名翻译失败: %s', exc)
        return None

    text = (raw or '').strip().strip('"\'')
    # 只取第一行，去掉可能附带的说明
    text = text.splitlines()[0].strip() if text else ''
    if not text or len(text) > 120:
        return None
    logger.info('公司名翻译结果: %s -> %s', company_name, text)
    return text


def _search_company_website(result, with_content=False):
    """联网搜索公司官网。

    只依据真实搜索结果返回官网地址，不采信模型给出的域名（模型可能编造）。

    Args:
        result: 入参数据，取公司名作为搜索关键词
        with_content: 是否同时返回搜索到的网页正文（供模型分析用）

    Returns:
        with_content=False 时返回官网地址字符串或 None；
        with_content=True 时返回 (官网地址, 搜索结果列表) 元组。
    """
    company_name = _pick_company_name(result)
    if not company_name:
        logger.info('缺少公司名，跳过官网搜索')
        return (None, []) if with_content else None

    # 只做一次联网搜索：入参给了英文名就用英文搜，否则用原名搜，避免重复调用接口
    english_name = str(result.get('company_name_en') or '').strip()
    if english_name and english_name.lower() != company_name.lower():
        query = f'{english_name} official website'
    else:
        query = f'{company_name} 官网'

    logger.info('联网搜索官网: query=%s', query)
    try:
        # 带上正文返回，官网首页常是 JS 渲染页抓不到正文，搜索结果里的子页正文更全
        found = search_web(query, count=5, need_content=with_content)
    except Exception as exc:
        logger.warning('官网搜索失败: query=%s, error=%s', query, exc)
        found = []

    if not found:
        logger.info('官网搜索无结果: query=%s', query)
        return (None, []) if with_content else None

    for item in found:
        item.setdefault('_query', query)
    results = found

    url = _pick_website_from_results(results, company_name, english_name)
    if url:
        logger.info('官网搜索命中: url=%s', url)
    else:
        logger.info('官网搜索未找到可用链接: company=%s', company_name)
    return (url, results) if with_content else url


def _pick_website_from_results(results, company_name, english_name=None):
    """从搜索结果中挑出最像官网首页的链接。

    优先级：域名匹配公司名 > site_name/标题匹配公司名 >
    主域名在结果中反复出现 > 标题/站点名包含公司名 > 首条结果。
    """
    name = str(company_name).strip().lower()
    # 域名里的核心词（去掉后缀、常见后缀）
    core = name.split()[0] if name else name
    # 英文名的核心词，中文公司名的官网域名通常用英文（如 宁德时代 -> catl.com）
    en_name = str(english_name or '').strip().lower()
    en_core = en_name.split()[0] if en_name else ''

    def domain_of(url):
        host = url.split('://')[-1].split('/')[0].lower()
        return host[4:] if host.startswith('www.') else host

    def looks_like_news_or_listing(url, title):
        """排除新闻页、列表页、百科等非官网首页的链接。"""
        low = (url + ' ' + (title or '')).lower()
        markers = ('/news', '/blog', '/article', '/news/', '/press', '/media',
                   '/wiki', '/baike', '/zhihu', '/wikipedia', '/list', '/page/')
        return any(m in low for m in markers)

    # 第一优先级：域名本身包含公司名（取该域名下的首页，而非站内子页）
    for item in results:
        url = (item.get('url') or '').strip()
        if not url:
            continue
        host = domain_of(url)
        if core and core in host:
            return _to_homepage(url)
        # 英文名核心词命中域名，如 宁德时代 -> CATL -> catl.com
        if en_core and len(en_core) >= 3 and en_core in host:
            return _to_homepage(url)

    # 第二优先级：site_name / 标题 精确等于公司名 —— 这是最直接的官网信号
    for item in results:
        url = (item.get('url') or '').strip()
        if not url:
            continue
        site_name = (item.get('site_name') or '').strip().lower()
        title = (item.get('title') or '').strip().lower()
        if not site_name and not title:
            continue
        matched = (
            site_name == name or title == name
            or title.startswith(f'{name} |') or title.endswith(f'| {name}')
        )
        if not matched and en_name:
            # 英文名同样精确匹配，如 site_name == 'CATL'
            matched = (
                site_name == en_name or title == en_name
                or title.startswith(f'{en_name} |') or title.endswith(f'| {en_name}')
            )
        if not matched:
            continue
        if _is_third_party(url) or looks_like_news_or_listing(url, item.get('title')):
            continue
        return _to_homepage(url)

    # 第三优先级：同一主域名在多条结果里反复出现 —— 官网通常如此，
    # 而行业媒体、百科这类第三方站点一般只出现一次。
    # 用主域名计数，把 tsmc.com / cld.tsmc.com / investor.tsmc.com 视为同一站点。
    domain_counts = {}
    for item in results:
        url = (item.get('url') or '').strip()
        if not url:
            continue
        domain_counts[_root_domain(domain_of(url))] = \
            domain_counts.get(_root_domain(domain_of(url)), 0) + 1
    for item in results:
        url = (item.get('url') or '').strip()
        if not url:
            continue
        domain = domain_of(url)
        if domain_counts.get(_root_domain(domain), 0) < 2:
            continue
        if _is_third_party(url) or looks_like_news_or_listing(url, item.get('title')):
            continue
        return _to_homepage(url)

    # 第三优先级：标题或站点名包含公司名
    for item in results:
        url = (item.get('url') or '').strip()
        if not url:
            continue
        title = item.get('title') or ''
        site_name = item.get('site_name') or ''
        if not (name and (name in title.lower() or name in site_name.lower())):
            continue
        # 排除百科、新闻等第三方站点，它们不是官网
        if looks_like_news_or_listing(url, title) or _is_third_party(url):
            continue
        return _to_homepage(url) if _is_same_domain(url, results) else url

    # 第四优先级：首条非新闻/列表页的结果
    for item in results:
        url = (item.get('url') or '').strip()
        if not url:
            continue
        if not looks_like_news_or_listing(url, item.get('title')):
            return url

    # 兜底：首条结果
    first_url = (results[0].get('url') or '').strip()
    return first_url or None


def _root_domain(host):
    """取主域名，去掉子域前缀，用于把同一站点的多个子域合并计数。

    例：cld.tsmc.com / investor.tsmc.com -> tsmc.com
    """
    parts = host.split('.')
    if len(parts) <= 2:
        return host
    # 末两段是公共后缀时（com.cn、co.uk 等），保留三段
    if parts[-2] in ('com', 'co', 'net', 'org', 'gov', 'edu') and len(parts[-1]) == 2:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def _to_homepage(url):
    """把站内子页还原成该域名首页。

    子域（如 cld.tsmc.com）会还原到主域首页（tsmc.com），
    因为主域首页才是公司官网入口。
    """
    scheme, _, rest = url.partition('://')
    if not rest:
        return url
    host = rest.split('/')[0]
    root = _root_domain(host.lower())
    if root and root != host.lower():
        # 子域还原到主域；原 host 带 www. 时保留，其余子域不带前缀
        prefix = 'www.' if host.lower().startswith('www.') else ''
        return f'{scheme}://{prefix}{root}/'
    return f'{scheme}://{host}/'


def _is_same_domain(url, results):
    """判断 url 的域名是否出现在结果集中（用于判断它可能是官方站内页）。"""
    def domain_of(u):
        host = u.split('://')[-1].split('/')[0].lower()
        return host[4:] if host.startswith('www.') else host

    target = domain_of(url)
    return any(domain_of((r.get('url') or '')) == target for r in results)


def _is_third_party(url):
    """判断是否属于百科/新闻/社交等第三方站点（非公司官网）。"""
    host = url.split('://')[-1].split('/')[0].lower()
    third_party = ('wikipedia.org', 'baike.baidu.com', 'zhihu.com', 'sohu.com',
                   '163.com', 'qq.com', 'sina.com.cn', 'douyin.com', 'bilibili.com',
                   'linkedin.com', 'facebook.com', 'twitter.com', 'x.com',
                   'youtube.com', 'instagram.com', 'wikipedia', 'medium.com',
                   'crunchbase.com', 'gitee.com', 'csdn.net', 'cnki.net')
    return any(m in host for m in third_party)


def _fetch_website_content(website):
    """抓取官网内容，失败时返回 None（不影响第一轮结果）。"""
    url = str(website).strip()
    if not url:
        return None
    # 补全协议，DeepSeek 抓取接口需要完整 URL
    if not url.startswith(('http://', 'https://')):
        url = f'https://{url}'
    try:
        page = fetch_webpage(url)
    except Exception as exc:
        logger.warning('抓取官网内容失败: url=%s, error=%s', url, exc)
        return None
    if not page or not page.get('content'):
        logger.warning('官网内容为空: url=%s', url)
        return None
    return page


def _build_deep_analysis_messages(data):
    """构造传给 chat 的 messages：system 指定输出结构，user 带原始数据与官网内容。"""
    fields_desc = '\n'.join(
        f'- {key}：{cn}（{en}）—— {rule}' for key, cn, en, rule in ANALYSIS_FIELDS
    )
    content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str, indent=2)
    system = f"""你是一名资深 CRM 客户分析顾问。请基于用户提供的客户资料和官网内容，
做一次全面的客户分析，并输出一个 JSON 对象。

必须输出的字段：
{fields_desc}

要求：
1. 优先依据 _website_content 和 _site_pages 中的官网信息来填写各字段，这是最可靠的信源；
   其次才是用户提供的资料；两者都没有依据的字段才留空字符串。
2. website 字段必须原样照抄输入中的 website，绝不允许自己猜测或拼接域名。
3. 不要编造事实。官网内容和输入资料里没有的信息，字段值留空字符串 ""，
   不要用"推测""可能"之类的内容填充。
4. 国家、行业、成立年份、员工人数、规模、主营产品这类基础字段必须尽力填写 ——
   官网简介页通常会写明公司所在地、成立时间、员工数量和主营业务，请从中提取。
5. 需要中英文的字段（带 _cn 后缀的、以及 company_name / company_name_en），
   英文用英文写，中文用中文写。
6. 只输出 JSON，不要输出任何解释文字或 markdown 代码块。
"""
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': content},
    ]


def _parse_analysis_json(raw):
    """解析模型返回的 JSON，兼容 ```json 代码块与前后多余文字。"""
    if not raw:
        return {}
    text = raw.strip()
    if '```' in text:
        parts = text.split('```')
        for part in parts:
            part = part.strip()
            if part.startswith('json'):
                part = part[4:].strip()
            if part.startswith('{'):
                text = part
                break
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        logger.warning('深度分析结果不是合法 JSON，已按空结果处理')
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = ['crm_app']

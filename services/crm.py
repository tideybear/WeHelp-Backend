from datetime import datetime, date, timezone
from email.utils import parsedate_to_datetime

from flask import Blueprint, request, jsonify, current_app

crm_app = Blueprint('crm', __name__, url_prefix='/crm')

# 联系人可更新字段白名单
CONTACT_UPDATABLE_FIELDS = {
    'customer_id', 'name', 'title', 'department', 'gender', 'phone', 'mobile',
    'email', 'wechat', 'whatsapp', 'linkedin', 'language', 'is_primary',
    'birthday', 'notes',
}


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
    """获取跟进记录列表"""
    customer_id = request.args.get('customer_id')
    if not customer_id:
        return jsonify({'message': '缺少 customer_id'}), 400

    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 20, type=int)

    total = current_app.db_manager.execute_query(
        "SELECT COUNT(*) as total FROM v_crm_follow_up_records WHERE customer_id = :customer_id AND is_deleted = 0",
        {'customer_id': customer_id}
    )[0]['total']

    rows = current_app.db_manager.execute_query(
        "SELECT * FROM v_crm_follow_up_records WHERE customer_id = :customer_id AND is_deleted = 0 ORDER BY follow_up_at DESC LIMIT :limit OFFSET :offset",
        {'customer_id': customer_id, 'limit': page_size, 'offset': (page - 1) * page_size}
    )

    return jsonify({'result': {'list': rows, 'total': total, 'page': page, 'pageSize': page_size}})


@crm_app.route('/follow-up-records', methods=['POST'])
def create_follow_up_record():
    """创建跟进记录"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供跟进记录数据'}), 400

    for field in ['customer_id', 'follow_up_at']:
        if not data.get(field):
            return jsonify({'message': f'缺少必填字段: {field}'}), 400

    data.setdefault('is_deleted', 0)
    new_id = current_app.db_manager.insert_one('crm_follow_up_records', data)
    return jsonify({'result': {'id': new_id}}), 201


@crm_app.route('/customer/products', methods=['GET'])
def get_customer_products():
    """获取客户产品/需求列表"""
    customer_id = request.args.get('customer_id')
    if not customer_id:
        return jsonify({'message': '缺少 customer_id'}), 400

    rows = current_app.db_manager.execute_query(
        "SELECT * FROM v_crm_customer_products WHERE customer_id = :customer_id AND is_deleted = 0 ORDER BY id DESC",
        {'customer_id': customer_id}
    )
    return jsonify({'result': {'list': rows, 'total': len(rows)}})


@crm_app.route('/customer/products', methods=['POST'])
def create_customer_product():
    """创建客户产品/需求"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供产品需求数据'}), 400

    for field in ['customer_id', 'product_name']:
        if not data.get(field):
            return jsonify({'message': f'缺少必填字段: {field}'}), 400

    data.setdefault('is_deleted', 0)
    new_id = current_app.db_manager.insert_one('v_crm_customer_products', data)
    return jsonify({'result': {'id': new_id}}), 201


__all__ = ['crm_app']

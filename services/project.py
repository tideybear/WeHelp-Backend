# -*- coding: utf-8 -*-
from datetime import date, datetime
from flask import Blueprint, request, jsonify, current_app
import logging

logger = logging.getLogger(__name__)

project_app = Blueprint('project', __name__, url_prefix='/project')

# 物理表名（v_project_master_data 视图对应基表）
PROJECT_TABLE = 'project_master_data'
# 项目-用户-空间授权表（视图 join 的关联表）
PROJECT_AUTHORIZE_TABLE = 'user_namespace_project_authorize'
# 取数视图
PROJECT_VIEW = 'v_project_master_data'

# 项目可写入字段白名单（对应 project_master_data 基表）
PROJECT_WRITABLE_FIELDS = {
    'namespace_id', 'project_code', 'project_name', 'cover_image', 'status',
    'category_id', 'planned_start_date', 'planned_end_date', 'actual_start_date',
    'actual_end_date', 'project_manager', 'department', 'budget', 'actual_cost',
    'progress_percent', 'priority', 'description', 'current_step_id',
}

# 项目可更新字段白名单（namespace_id 不允许变更，避免跨空间串数据）
PROJECT_UPDATABLE_FIELDS = PROJECT_WRITABLE_FIELDS - {'namespace_id'}

# 日期字段（写入前统一格式）
PROJECT_DATE_FIELDS = (
    'planned_start_date', 'planned_end_date', 'actual_start_date', 'actual_end_date',
)

# 列表可筛选字段
PROJECT_FILTER_FIELDS = ('status', 'priority', 'category_id', 'project_manager', 'department')


def _normalize_datetime(value, date_only=False):
    """把各种日期格式统一成 MySQL 可接受的格式"""
    if value in (None, ''):
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y-%m-%d') if date_only else value.strftime('%Y-%m-%d %H:%M:%S')

    text = str(value).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime('%Y-%m-%d') if date_only else parsed.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue

    return text


def _project_authorized(project_id, namespace_id, user_id):
    """校验项目是否在指定空间与用户的授权范围内"""
    params = {'project_id': project_id}
    where = ["project_id = :project_id", "(is_deleted = 0 OR is_deleted IS NULL)"]
    if namespace_id:
        params['namespace_id'] = namespace_id
        where.append("namespace_id = :namespace_id")
    if user_id:
        params['user_id'] = user_id
        where.append("user_id = :user_id")

    rows = current_app.db_manager.execute_query(
        f"SELECT id FROM {PROJECT_AUTHORIZE_TABLE} WHERE {' AND '.join(where)} LIMIT 1",
        params
    )
    return bool(rows)


@project_app.route('/list', methods=['GET'])
def get_project_list():
    """获取项目列表（从视图 v_project_master_data 取数）"""
    namespace_id = request.args.get('namespace_id')
    user_id = request.args.get('user_id')
    if not namespace_id:
        return jsonify({'message': '缺少 namespace_id'}), 400

    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 20, type=int)
    sort_field = request.args.get('sortField', 'created_at')
    sort_order = 'DESC' if request.args.get('sortOrder', 'desc') == 'desc' else 'ASC'

    params = {'namespace_id': namespace_id}
    where = ["namespace_id = :namespace_id", "(is_delete = 0 OR is_delete IS NULL)"]

    if user_id:
        params['user_id'] = user_id
        where.append("user_id = :user_id")

    # 项目名称
    project_name = request.args.get('project_name')
    if project_name:
        params['project_name'] = f"%{project_name}%"
        where.append("project_name LIKE :project_name")

    # 项目编号
    project_code = request.args.get('project_code')
    if project_code:
        params['project_code'] = f"%{project_code}%"
        where.append("project_code LIKE :project_code")

    # 关键字（综合搜索）
    keyword = request.args.get('keyword')
    if keyword:
        params['keyword'] = f"%{keyword}%"
        where.append("(project_name LIKE :keyword OR project_code LIKE :keyword "
                     "OR project_manager LIKE :keyword OR description LIKE :keyword)")

    # 筛选条件
    for key in PROJECT_FILTER_FIELDS:
        val = request.args.get(key)
        if val:
            params[key] = val
            where.append(f"{key} = :{key}")

    where_sql = ' AND '.join(where)

    # 总数
    total = current_app.db_manager.execute_query(
        f"SELECT COUNT(DISTINCT id) as total FROM {PROJECT_VIEW} WHERE {where_sql}", params
    )[0]['total']

    # 列表
    params['limit'], params['offset'] = page_size, (page - 1) * page_size
    rows = current_app.db_manager.execute_query(
        f"SELECT DISTINCT * FROM {PROJECT_VIEW} WHERE {where_sql} "
        f"ORDER BY id ASC LIMIT :limit OFFSET :offset",
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


@project_app.route('/detail/<int:id>', methods=['GET'])
def get_project_detail(id):
    """获取项目详情"""
    namespace_id = request.args.get('namespace_id')
    user_id = request.args.get('user_id')
    if not _project_authorized(id, namespace_id, user_id):
        return jsonify({'message': '项目不存在'}), 404

    rows = current_app.db_manager.execute_query(
        f"SELECT * FROM {PROJECT_VIEW} WHERE id = :id LIMIT 1", {'id': id}
    )
    if not rows:
        return jsonify({'message': '项目不存在'}), 404
    return jsonify({'result': rows[0]})


@project_app.route('/create', methods=['POST'])
def create_project():
    """新增项目"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供项目数据'}), 400

    # 只保留可写字段
    user_id = data.get('user_id')
    data = {k: v for k, v in data.items() if k in PROJECT_WRITABLE_FIELDS}
    if not data:
        return jsonify({'message': '没有有效字段需要写入'}), 400

    if not data.get('project_name'):
        return jsonify({'message': '缺少必填字段: project_name'}), 400
    if not data.get('namespace_id'):
        return jsonify({'message': '缺少必填字段: namespace_id'}), 400

    # 日期字段标准化
    for field in PROJECT_DATE_FIELDS:
        if field in data:
            data[field] = _normalize_datetime(data[field], date_only=True)

    data['is_delete'] = 0
    new_id = current_app.db_manager.insert_one(PROJECT_TABLE, data)

    # 写入用户-空间授权记录
    if user_id:
        current_app.db_manager.insert_one(PROJECT_AUTHORIZE_TABLE, {
            'user_id': user_id,
            'namespace_id': data['namespace_id'],
            'project_id': new_id,
            'is_deleted': 0,
        })

    return jsonify({'result': {'id': new_id}}), 201


@project_app.route('/update/<int:id>', methods=['PUT'])
def update_project(id):
    """更新项目"""
    data = request.get_json()
    if not data:
        return jsonify({'message': '请提供更新数据'}), 400

    namespace_id = request.args.get('namespace_id')
    user_id = request.args.get('user_id')
    if not _project_authorized(id, namespace_id, user_id):
        return jsonify({'message': '项目不存在'}), 404

    # 只允许更新白名单字段
    data = {k: v for k, v in data.items() if k in PROJECT_UPDATABLE_FIELDS}
    if not data:
        return jsonify({'message': '没有有效字段需要更新'}), 400

    # 日期字段标准化
    for field in PROJECT_DATE_FIELDS:
        if field in data:
            data[field] = _normalize_datetime(data[field], date_only=True)

    success = current_app.db_manager.update_by_id(PROJECT_TABLE, id, data)
    if not success:
        return jsonify({'message': '项目不存在'}), 404
    return jsonify({'result': {'id': id}})


@project_app.route('/delete/<int:id>', methods=['DELETE'])
def delete_project(id):
    """删除项目（软删除）"""
    namespace_id = request.args.get('namespace_id')
    user_id = request.args.get('user_id')
    if not _project_authorized(id, namespace_id, user_id):
        return jsonify({'message': '项目不存在'}), 404

    rowcount = current_app.db_manager.execute_update(
        f"UPDATE {PROJECT_TABLE} SET is_delete = 1 WHERE id = :id", {'id': id}
    )
    if rowcount == 0:
        return jsonify({'message': '项目不存在'}), 404

    # 同步移除授权记录
    current_app.db_manager.execute_update(
        f"UPDATE {PROJECT_AUTHORIZE_TABLE} SET is_deleted = 1 "
        f"WHERE project_id = :id AND (is_deleted = 0 OR is_deleted IS NULL)",
        {'id': id}
    )
    return jsonify({'result': {'id': id}})


__all__ = ['project_app']

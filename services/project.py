# -*- coding: utf-8 -*-
from datetime import date
from flask import Blueprint, request, jsonify, current_app
import json
import logging

logger = logging.getLogger(__name__)

project_app = Blueprint('project', __name__)

# 物理表名（与 v_projects_info 对应基表一致时可用；不一致请在库中调整或改此常量）
PROJECT_TABLE = 'projects'
PROJECT_AUTHORIZE_TABLE = 'project_authorize'
PROJECT_TASKS_TABLE = 'v_project_tasks'
PROJECT_TASK_COMMENTS_TABLE = 'v_project_task_comments'
PROJECT_TASK_COMMENTS_PHYSICAL_TABLE = 'project_task_comments'
# 写入任务行使用物理表（视图通常不可 INSERT）；若环境仅有视图请改为可写基表名
PROJECT_TASKS_PHYSICAL_TABLE = 'project_tasks'

PROJECT_CODE_PREFIX = 'PRJ-'


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
    """仅保留物理表存在的列，避免 Unknown column 导致插入失败或静默丢字段。"""
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


def _fetch_project_row(project_id):
    rows = current_app.db_manager.execute_query(
        f"""
        SELECT id, project_name, project_code, project_manager, description, status,
               namespace_id
        FROM v_projects_info
        WHERE id = :project_id
        LIMIT 1
        """,
        {'project_id': int(project_id)},
    )
    return rows[0] if rows else None


def _user_can_manage_project(project_id, operator_user_id):
    """系统管理员 user_id=1 或项目负责人（project_manager 与操作者姓名/账号一致）。"""
    try:
        op_id = int(operator_user_id)
        pid = int(project_id)
    except (TypeError, ValueError):
        return False
    if op_id == 1:
        return True
    row = _fetch_project_row(pid)
    if not row:
        return False
    manager = str(row.get('project_manager') or '').strip()
    if not manager:
        return False
    op_name = _resolve_user_display_name(op_id)
    return bool(op_name and manager == op_name)


def _parse_operator_user_id(body):
    raw = body.get('operator_user_id')
    if raw is None or str(raw).strip() == '':
        return None, '缺少必要参数 operator_user_id'
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, 'operator_user_id 非法'


def _require_project_manage_permission(body):
    op_id, err = _parse_operator_user_id(body)
    if err:
        return err
    try:
        project_id = int(body.get('project_id'))
    except (TypeError, ValueError):
        return '缺少或非法参数 project_id'
    if not _user_can_manage_project(project_id, op_id):
        return '无权修改该项目'
    return None


def _project_auth_meta(project_id):
    """取授权表所需的 namespace_id、project_code。"""
    row = _fetch_project_row(project_id)
    if not row:
        return None, None
    namespace_id = row.get('namespace_id')
    project_code = row.get('project_code')
    if namespace_id is None:
        auth_rows = current_app.db_manager.execute_query(
            f"""
            SELECT namespace_id, project_code
            FROM {PROJECT_AUTHORIZE_TABLE}
            WHERE project_id = :project_id
              AND (is_deleted = 0 OR is_deleted IS NULL)
            LIMIT 1
            """,
            {'project_id': int(project_id)},
        )
        if auth_rows:
            namespace_id = auth_rows[0].get('namespace_id')
            project_code = auth_rows[0].get('project_code') or project_code
    try:
        namespace_id = int(namespace_id) if namespace_id is not None else None
    except (TypeError, ValueError):
        namespace_id = None
    return namespace_id, project_code


def _sync_project_authorize(project_id, namespace_id, project_code, user_ids):
    """同步项目成员：始终保留 user_id=1；其余以传入列表为准。"""
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return 'project_id 非法'

    target = {1}
    if isinstance(user_ids, list):
        for x in user_ids:
            try:
                target.add(int(x))
            except (TypeError, ValueError):
                continue

    if namespace_id is not None:
        allowed_ns = _namespace_authorized_user_ids(int(namespace_id))
        for uid in list(target):
            if uid != 1 and uid not in allowed_ns:
                return f'所选用户不在当前空间: {uid}'

    current_rows = current_app.db_manager.execute_query(
        f"""
        SELECT id, user_id, is_deleted
        FROM {PROJECT_AUTHORIZE_TABLE}
        WHERE project_id = :project_id
        """,
        {'project_id': pid},
    ) or []

    by_user = {}
    for r in current_rows:
        try:
            uid = int(r.get('user_id'))
        except (TypeError, ValueError):
            continue
        by_user[uid] = r

    base_row = _pick_row_for_table(PROJECT_AUTHORIZE_TABLE, {
        'project_id': pid,
        'namespace_id': namespace_id,
        'project_code': project_code,
    })

    for uid, row in by_user.items():
        if uid in target:
            if _table_has_column(PROJECT_AUTHORIZE_TABLE, 'is_deleted'):
                if row.get('is_deleted') not in (0, None):
                    current_app.db_manager.update_by_id(
                        PROJECT_AUTHORIZE_TABLE,
                        int(row['id']),
                        {'is_deleted': 0},
                    )
            continue
        if _table_has_column(PROJECT_AUTHORIZE_TABLE, 'is_deleted'):
            current_app.db_manager.update_by_id(
                PROJECT_AUTHORIZE_TABLE,
                int(row['id']),
                {'is_deleted': 1},
            )
        else:
            current_app.db_manager.execute_update(
                f'DELETE FROM {PROJECT_AUTHORIZE_TABLE} WHERE id = :id',
                {'id': int(row['id'])},
            )

    for uid in target:
        if uid in by_user:
            continue
        insert_row = {**base_row, 'user_id': uid}
        if _table_has_column(PROJECT_AUTHORIZE_TABLE, 'is_deleted'):
            insert_row['is_deleted'] = 0
        current_app.db_manager.insert_one(PROJECT_AUTHORIZE_TABLE, insert_row)

    return None


def _max_project_code_seq_in_table(table_name):
    """取单表中 PRJ- 数字后缀的最大序号（无匹配行则 0）。"""
    if not _table_has_column(table_name, 'project_code'):
        return 0
    prefix = PROJECT_CODE_PREFIX
    start_pos = len(prefix) + 1
    sql = f"""
        SELECT MAX(
            CAST(SUBSTRING(UPPER(TRIM(project_code)), {start_pos}) AS UNSIGNED)
        ) AS max_seq
        FROM {table_name}
        WHERE UPPER(TRIM(project_code)) LIKE :prefix_like
    """
    rows = current_app.db_manager.execute_query(sql, {
        'prefix_like': prefix.upper() + '%',
    })
    if not rows or rows[0].get('max_seq') is None:
        return 0
    try:
        return int(rows[0]['max_seq'])
    except (TypeError, ValueError):
        return 0


def _generate_project_code():
    """项目编号 PRJ-XXX：两表 PRJ- 后缀最大值 +1；XXX 至少 3 位，超过 999 自动扩位。"""
    max_seq = max(
        _max_project_code_seq_in_table(PROJECT_TABLE),
        _max_project_code_seq_in_table(PROJECT_AUTHORIZE_TABLE),
    )
    next_seq = max_seq + 1
    if next_seq > 999999:
        raise ValueError('项目编号序号已用尽')
    width = max(3, len(str(next_seq)))
    return f'{PROJECT_CODE_PREFIX}{next_seq:0{width}d}'


@project_app.route('/project/list', methods=['GET'])
def get_projectList():
    """
    按 namespace_id、user_id 查询 v_projects_info（均需显式传入，无默认值）。
    Query: namespace_id, user_id（整数）
    """
    try:
        namespace_id = request.args.get('namespace_id', type=int)
        user_id = request.args.get('user_id', type=int)

        if namespace_id is None or user_id is None:
            return jsonify({'code': 400, 'msg': '缺少必要参数 namespace_id 或 user_id'}), 400

        sql = """
            SELECT 
             id
                ,project_name
                ,project_code
                ,cover_image
                ,status
                ,category_name
                ,user_id
                ,namespace_id
            FROM v_projects_info
            WHERE namespace_id = :namespace_id
            AND user_id = :user_id
        """
        params = {'namespace_id': namespace_id, 'user_id': user_id}

        result = current_app.db_manager.execute_query(sql, params)
        resp = jsonify({'code': 200, 'msg': 'success', 'data': result})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp

    except Exception as e:
        logger.error('获取项目列表失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取项目列表失败: {str(e)}'}), 500


def _namespace_authorized_user_ids(namespace_id: int):
    rows = current_app.db_manager.execute_query(
        """
        SELECT user_id FROM namespace_authorize
        WHERE namespace_id = :namespace_id AND is_deleted = 0
        """,
        {'namespace_id': namespace_id},
    )
    out = set()
    for r in rows or []:
        try:
            out.add(int(r.get('user_id')))
        except (TypeError, ValueError):
            continue
    return out


@project_app.route('/project/create', methods=['POST'])
def create_project():
    """
    JSON: name（必填，写入列 project_name）, namespace_id, user_id（创建人）
    可选 authorize_user_ids: 与创建人一并写入 project_authorize 的其他用户 id 列表；
    服务端始终再写入 user_id=1 与创建人（去重）。
    """
    try:
        body = request.get_json(silent=True) or {}
        name = (body.get('name') or '').strip()
        namespace_id = body.get('namespace_id')
        user_id = body.get('user_id')
        raw_extra = body.get('authorize_user_ids')

        if not name:
            return jsonify({'code': 400, 'msg': '项目名称不能为空'}), 400

        try:
            namespace_id = int(namespace_id)
            user_id = int(user_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 namespace_id / user_id'}), 400

        extra_ids = []
        if isinstance(raw_extra, list):
            for x in raw_extra:
                try:
                    extra_ids.append(int(x))
                except (TypeError, ValueError):
                    continue

        allowed_ns = _namespace_authorized_user_ids(namespace_id)
        for uid in extra_ids:
            if uid not in allowed_ns:
                return jsonify({'code': 400, 'msg': f'所选用户不在当前空间: {uid}'}), 400

        try:
            project_code = _generate_project_code()
        except ValueError as ve:
            return jsonify({'code': 400, 'msg': str(ve)}), 400

        project_manager = _resolve_user_display_name(user_id)
        body_manager = str(body.get('project_manager') or '').strip()
        if body_manager:
            project_manager = body_manager

        create_row = {
            'project_name': name,
            'project_code': project_code,
            'project_manager': project_manager or None,
            'status': '未开始',
        }
        cat_err = _merge_category_id_update(body, create_row)
        if cat_err:
            return jsonify({'code': 400, 'msg': cat_err}), 400

        # 与列表视图字段对齐；status 与前端筛选「未开始」一致，避免 NOT NULL 无默认值插入失败
        row = _pick_row_for_table(PROJECT_TABLE, create_row)
        if 'project_name' not in row:
            return jsonify({'code': 500, 'msg': '项目表缺少 project_name 列'}), 500

        new_id = current_app.db_manager.insert_one(PROJECT_TABLE, row)

        ensure_updates = {}
        if _table_has_column(PROJECT_TABLE, 'project_code'):
            ensure_updates['project_code'] = project_code
        if project_manager and _table_has_column(PROJECT_TABLE, 'project_manager'):
            ensure_updates['project_manager'] = project_manager
        if ensure_updates:
            current_app.db_manager.update_by_id(PROJECT_TABLE, new_id, ensure_updates)

        authorize_set = {user_id, 1}
        authorize_set.update(extra_ids)
        authorize_row = _pick_row_for_table(PROJECT_AUTHORIZE_TABLE, {
            'project_id': new_id,
            'namespace_id': namespace_id,
            'project_code': project_code,
        })
        for uid in authorize_set:
            current_app.db_manager.insert_one(
                PROJECT_AUTHORIZE_TABLE,
                {
                    **authorize_row,
                    'user_id': int(uid),
                },
            )

        resp = jsonify({
            'code': 200,
            'msg': 'success',
            'data': {
                'id': new_id,
                'project_code': project_code,
                'project_manager': project_manager,
            },
        })
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp

    except Exception as e:
        logger.error('创建项目失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'创建项目失败: {str(e)}'}), 500


@project_app.route('/project/detail', methods=['GET'])
def get_projectDetail():
    """
    按 namespace_id、user_id 查询 v_projects_info（均需显式传入，无默认值）。
    Query: namespace_id, user_id（整数）
    """
    try:
        project_id = request.args.get('project_id', type=int)

        if project_id is None:
            return jsonify({'code': 400, 'msg': '缺少必要参数 project_id'}), 400

        sql = """
            SELECT 
             id
                ,project_name
                ,cover_image
                ,project_code
                ,status
                ,category_id
                ,planned_start_date
                ,planned_end_date
                ,actual_start_date
                ,actual_end_date
                ,project_manager
                ,department
                ,budget
                ,priority
                ,description
                ,category_name
                ,processes
                ,user_id
                ,namespace_id
            FROM v_projects_info
            WHERE id = :project_id
        """
        params = {'project_id': project_id}

        result = current_app.db_manager.execute_query(sql, params)
        resp = jsonify({'code': 200, 'msg': 'success', 'data': result})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp

    except Exception as e:
        logger.error('获取项目列表失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取项目列表失败: {str(e)}'}), 500


ALLOWED_PROJECT_STATUS = frozenset({'进行中', '未开始', '已结束'})
PROJECT_DATE_FIELDS = frozenset(
    {'planned_start_date', 'planned_end_date', 'actual_start_date', 'actual_end_date'}
)

PROJECT_INFO_FIELDS = frozenset({'project_code', 'project_name', 'project_manager', 'description'})


def _categories_lookup_table():
    """项目类型下拉：优先物理表 project_categories，否则视图 v_project_categories。"""
    if _table_has_column('project_categories', 'category_id'):
        return 'project_categories'
    if _table_has_column('v_project_categories', 'category_id'):
        return 'v_project_categories'
    return None


def _category_id_exists(category_id):
    try:
        cid = int(category_id)
    except (TypeError, ValueError):
        return False
    tbl = _categories_lookup_table()
    if not tbl:
        return False
    rows = current_app.db_manager.execute_query(
        f"SELECT 1 AS ok FROM `{tbl}` WHERE category_id = :cid LIMIT 1",
        {'cid': cid},
    )
    return bool(rows)


def _merge_category_id_update(body, updates):
    """
    从 body 解析 category_id 写入 updates（整数或 NULL）。
    未传 category_id 时不修改；传 null/空字符串则置空（表有该列时）。
    返回错误文案或 None。
    """
    if 'category_id' not in body:
        return None
    if not _table_has_column(PROJECT_TABLE, 'category_id'):
        return None
    raw = body.get('category_id')
    if raw is None or (isinstance(raw, str) and raw.strip() == ''):
        updates['category_id'] = None
        return None
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return '非法 category_id'
    if not _category_id_exists(cid):
        return '项目类型不存在'
    updates['category_id'] = cid
    return None


@project_app.route('/project/info', methods=['POST'])
def update_project_info():
    """JSON: project_id（整数）, 以及可选 project_code / project_name / project_manager / description（至少一项）"""
    try:
        body = request.get_json(silent=True) or {}
        project_id = body.get('project_id')

        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 project_id'}), 400

        updates = {}
        for k in PROJECT_INFO_FIELDS:
            if k not in body:
                continue
            v = body.get(k)
            if v is None:
                updates[k] = None
            else:
                updates[k] = str(v).strip()

        if 'project_name' in updates and not updates['project_name']:
            return jsonify({'code': 400, 'msg': '项目名称不能为空'}), 400

        cat_err = _merge_category_id_update(body, updates)
        if cat_err:
            return jsonify({'code': 400, 'msg': cat_err}), 400

        if not updates:
            return jsonify({'code': 400, 'msg': '没有可更新的信息字段'}), 400

        perm_err = _require_project_manage_permission(body)
        if perm_err:
            return jsonify({'code': 403, 'msg': perm_err}), 403

        row = _pick_row_for_table(PROJECT_TABLE, updates)
        ok = current_app.db_manager.update_by_id(PROJECT_TABLE, project_id, row)
        if not ok:
            return jsonify({'code': 404, 'msg': '项目不存在或未更新'}), 404

        resp = jsonify({'code': 200, 'msg': 'success', 'data': row})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('更新项目信息失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'更新项目信息失败: {str(e)}'}), 500


@project_app.route('/project/manage', methods=['POST'])
def save_project_manage():
    """
    项目管理保存：基本信息 + 成员授权（需项目负责人或 user_id=1）。
    JSON: project_id, operator_user_id, namespace_id（可选，用于校验成员空间）,
          project_code, project_name, project_manager, description,
          authorize_user_ids（数组，不含 1，服务端自动保留管理员）
    """
    try:
        body = request.get_json(silent=True) or {}
        perm_err = _require_project_manage_permission(body)
        if perm_err:
            return jsonify({'code': 403, 'msg': perm_err}), 403

        try:
            project_id = int(body.get('project_id'))
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 project_id'}), 400

        updates = {}
        for k in PROJECT_INFO_FIELDS:
            if k not in body:
                continue
            v = body.get(k)
            if v is None:
                updates[k] = None
            else:
                updates[k] = str(v).strip()

        if 'project_name' in updates and not updates['project_name']:
            return jsonify({'code': 400, 'msg': '项目名称不能为空'}), 400

        cat_err = _merge_category_id_update(body, updates)
        if cat_err:
            return jsonify({'code': 400, 'msg': cat_err}), 400

        if updates:
            row = _pick_row_for_table(PROJECT_TABLE, updates)
            if row:
                ok = current_app.db_manager.update_by_id(PROJECT_TABLE, project_id, row)
                if not ok:
                    return jsonify({'code': 404, 'msg': '项目不存在或未更新'}), 404

        namespace_id = body.get('namespace_id')
        try:
            ns_val = int(namespace_id) if namespace_id is not None else None
        except (TypeError, ValueError):
            ns_val = None
        if ns_val is None:
            ns_val, _ = _project_auth_meta(project_id)

        _, project_code = _project_auth_meta(project_id)
        if 'project_code' in updates:
            project_code = updates['project_code']

        sync_err = _sync_project_authorize(
            project_id,
            ns_val,
            project_code,
            body.get('authorize_user_ids'),
        )
        if sync_err:
            return jsonify({'code': 400, 'msg': sync_err}), 400

        resp = jsonify({'code': 200, 'msg': 'success', 'data': {'id': project_id}})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('项目管理保存失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'项目管理保存失败: {str(e)}'}), 500


@project_app.route('/project/manage-permission', methods=['GET'])
def get_project_manage_permission():
    """Query: project_id, operator_user_id → { can_manage: bool }"""
    try:
        project_id = request.args.get('project_id', type=int)
        operator_user_id = request.args.get('operator_user_id', type=int)
        if project_id is None or operator_user_id is None:
            return jsonify({'code': 400, 'msg': '缺少 project_id 或 operator_user_id'}), 400
        can_manage = _user_can_manage_project(project_id, operator_user_id)
        return jsonify({
            'code': 200,
            'msg': 'success',
            'data': {'can_manage': can_manage},
        })
    except Exception as e:
        logger.error('查询项目管理权限失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'查询项目管理权限失败: {str(e)}'}), 500


@project_app.route('/project/status', methods=['POST'])
def update_project_status():
    """JSON: project_id（整数）, status（进行中|未开始|已结束）"""
    try:
        body = request.get_json(silent=True) or {}
        project_id = body.get('project_id')
        status = (body.get('status') or '').strip()

        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 project_id'}), 400

        if status not in ALLOWED_PROJECT_STATUS:
            return jsonify({'code': 400, 'msg': '非法状态值'}), 400

        perm_err = _require_project_manage_permission(body)
        if perm_err:
            return jsonify({'code': 403, 'msg': perm_err}), 403

        ok = current_app.db_manager.update_by_id(PROJECT_TABLE, project_id, {'status': status})
        if not ok:
            return jsonify({'code': 404, 'msg': '项目不存在或未更新'}), 404

        resp = jsonify({'code': 200, 'msg': 'success', 'data': {'status': status}})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('更新项目状态失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'更新项目状态失败: {str(e)}'}), 500


@project_app.route('/project/categories', methods=['GET'])
def list_project_categories():
    """项目类型下拉：读取 project_categories（或 v_project_categories）去重列表。"""
    try:
        tbl = _categories_lookup_table()
        if not tbl:
            return jsonify({'code': 200, 'msg': 'success', 'data': []})

        sql = f"""
            SELECT category_id
                 , MAX(category_name) AS category_name
            FROM `{tbl}`
            GROUP BY category_id
            ORDER BY category_id ASC
        """
        result = current_app.db_manager.execute_query(sql, {})
        resp = jsonify({'code': 200, 'msg': 'success', 'data': result or []})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('获取项目类型列表失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取项目类型列表失败: {str(e)}'}), 500


@project_app.route('/project/category', methods=['GET'])
def get_project_category():
    """
    查询 v_project_categories：Query category_id（整数）
    返回该大类下各步骤行（含 category_name，多行 step 相同 category_name）。
    """
    try:
        category_id = request.args.get('category_id', type=int)
        if category_id is None:
            return jsonify({'code': 400, 'msg': '缺少必要参数 category_id'}), 400

        sql = """
            SELECT category_id
                 , category_name
                 , step_id
                 , step_name
                 , step_order
            FROM v_project_categories
            WHERE category_id = :category_id
            ORDER BY category_id, step_order ASC
        """
        result = current_app.db_manager.execute_query(sql, {'category_id': category_id})
        resp = jsonify({'code': 200, 'msg': 'success', 'data': result or []})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('获取项目分类失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取项目分类失败: {str(e)}'}), 500


@project_app.route('/project/tasks/detail', methods=['GET'])
def get_project_tasks_detail():
    """
    查询 project_tasks：Query project_id（整数）
    返回行字段与表列一致，由前端按 phase_id / parent_task_id 组树。
    """
    try:
        task_id = request.args.get('task_id', type=int)
        if task_id is None:
            return jsonify({'code': 400, 'msg': '缺少必要参数 task_id'}), 400

        sql = f"""
            SELECT id
              ,  project_id
              ,  project_category_id
              ,  processes_id
              ,  parent_task_id
              ,  milestone_id
              ,  task_name
              ,  task_description
              ,  task_type
              ,  priority
              ,  is_critical
              ,  planned_start_date
              ,  planned_end_date
              ,  planned_duration
              ,  actual_start_date
              ,  actual_end_date
              ,  actual_duration
              ,  progress_percent
              ,  current_task_progress
              ,  task_status
              ,  assignee_id
              ,  assignee_name
              ,  reviewer_id
              ,  reviewer_name
              ,  participant_ids
              ,  depends_on
              ,  dependency_type
              ,  lag_days
              ,  estimated_hours
              ,  actual_hours
              ,  estimated_cost
              ,  sort_order
            FROM {PROJECT_TASKS_TABLE}
            WHERE id = :task_id
            {_project_task_active_by_id_sql()}
            ORDER BY 
                     (parent_task_id IS NULL) DESC,
                     parent_task_id ASC,
                     sort_order ASC,
                     id ASC
        """
        result = current_app.db_manager.execute_query(sql, {'task_id': task_id})
        resp = jsonify({'code': 200, 'msg': 'success', 'data': result or []})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('获取项目任务失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取项目任务失败: {str(e)}'}), 500


@project_app.route('/project/tasks/comments', methods=['GET'])
def get_project_task_comments():
    """
    任务评论时间线：Query task_id（整数），按 created_at 升序。
    数据来源 v_project_task_comments（已过滤 is_deleted = 0）。
    """
    try:
        task_id = request.args.get('task_id', type=int)
        if task_id is None:
            return jsonify({'code': 400, 'msg': '缺少必要参数 task_id'}), 400

        sql = f"""
            SELECT id
                 , task_id
                 , project_id
                 , user_id
                 , content
                 , created_at
            FROM {PROJECT_TASK_COMMENTS_TABLE}
            WHERE task_id = :task_id
            ORDER BY created_at ASC, id ASC
        """
        result = current_app.db_manager.execute_query(sql, {'task_id': task_id})
        resp = jsonify({'code': 200, 'msg': 'success', 'data': result or []})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('获取任务评论失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取任务评论失败: {str(e)}'}), 500


@project_app.route('/project/tasks/comments', methods=['POST'])
def create_project_task_comment():
    """
    发表评论：JSON 需含 task_id、project_id、user_id、content；
    可选 mention_user_ids、mention_user_names（英文逗号分隔）。
    """
    try:
        body = request.get_json(silent=True) or {}
        try:
            task_id = int(body.get('task_id'))
            project_id = int(body.get('project_id'))
            user_id = int(body.get('user_id'))
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 task_id / project_id / user_id'}), 400

        content = str(body.get('content') or '').strip()
        if not content:
            return jsonify({'code': 400, 'msg': '评论内容不能为空'}), 400

        task_rows = current_app.db_manager.execute_query(
            f"""
            SELECT id FROM {PROJECT_TASKS_TABLE}
            WHERE id = :task_id AND project_id = :project_id
            LIMIT 1
            """,
            {'task_id': task_id, 'project_id': project_id},
        )
        if not task_rows:
            return jsonify({'code': 404, 'msg': '任务不存在或不属于该项目'}), 404

        mention_user_ids = body.get('mention_user_ids')
        mention_user_names = body.get('mention_user_names')
        mids = str(mention_user_ids).strip() if mention_user_ids is not None else ''
        mnames = str(mention_user_names).strip() if mention_user_names is not None else ''

        row = {
            'task_id': task_id,
            'project_id': project_id,
            'user_id': user_id,
            'content': content,
            'mention_user_ids': mids or None,
            'mention_user_names': mnames or None,
        }
        new_id = current_app.db_manager.insert_one(PROJECT_TASK_COMMENTS_PHYSICAL_TABLE, row)
        resp = jsonify({'code': 200, 'msg': 'success', 'data': {'id': new_id, **row}})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('发表评论失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'发表评论失败: {str(e)}'}), 500


@project_app.route('/project/tasks/comments/delete', methods=['POST'])
def delete_project_task_comment():
    """软删除评论：仅评论作者可删（user_id 与库中一致）。"""
    try:
        body = request.get_json(silent=True) or {}
        try:
            comment_id = int(body.get('comment_id'))
            user_id = int(body.get('user_id'))
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 comment_id / user_id'}), 400

        rows = current_app.db_manager.execute_query(
            f"""
            SELECT id, user_id, is_deleted
            FROM {PROJECT_TASK_COMMENTS_PHYSICAL_TABLE}
            WHERE id = :comment_id
            LIMIT 1
            """,
            {'comment_id': comment_id},
        )
        if not rows:
            return jsonify({'code': 404, 'msg': '评论不存在'}), 404
        row = rows[0]
        if int(row.get('is_deleted') or 0) == 1:
            return jsonify({'code': 400, 'msg': '评论已删除'}), 400
        if int(row.get('user_id')) != user_id:
            return jsonify({'code': 403, 'msg': '仅可删除本人发表的评论'}), 403

        current_app.db_manager.execute_update(
            f"""
            UPDATE {PROJECT_TASK_COMMENTS_PHYSICAL_TABLE}
            SET is_deleted = 1
            WHERE id = :comment_id AND user_id = :user_id
            """,
            {'comment_id': comment_id, 'user_id': user_id},
        )
        resp = jsonify({'code': 200, 'msg': 'success', 'data': {'id': comment_id}})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('删除任务评论失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'删除任务评论失败: {str(e)}'}), 500


@project_app.route('/project/user/authorize', methods=['GET'])
def get_project_user_authorize():
    """
    查询 project_tasks：Query project_id（整数）
    返回行字段与表列一致，由前端按 phase_id / parent_task_id 组树。
    """
    try:
        project_id = request.args.get('project_id', type=int)
        if project_id is None:
            return jsonify({'code': 400, 'msg': '缺少必要参数 task_id'}), 400

        sql = f"""
            SELECT project_id,user_id,name
            FROM v_project_authorize_user
            WHERE project_id = :project_id
        """
        result = current_app.db_manager.execute_query(sql, {'project_id': project_id})
        resp = jsonify({'code': 200, 'msg': 'success', 'data': result or []})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('获取项目任务失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取项目任务失败: {str(e)}'}), 500


@project_app.route('/project/tasks', methods=['GET'])
def get_project_tasks():
    """
    查询 project_tasks：Query project_id（整数）
    返回行字段与表列一致，由前端按 phase_id / parent_task_id 组树。
    """
    try:
        project_id = request.args.get('project_id', type=int)
        if project_id is None:
            return jsonify({'code': 400, 'msg': '缺少必要参数 project_id'}), 400

        sql = f"""
            SELECT id
              ,  project_id
              ,  project_category_id
              ,  processes_id
              ,  parent_task_id
              ,  milestone_id
              ,  task_name
              ,  task_description
              ,  task_type
              ,  priority
              ,  is_critical
              ,  planned_start_date
              ,  planned_end_date
              ,  planned_duration
              ,  actual_start_date
              ,  actual_end_date
              ,  actual_duration
              ,  progress_percent
              ,  current_task_progress
              ,  task_status
              ,  assignee_id
              ,  assignee_name
              ,  reviewer_id
              ,  reviewer_name
              ,  participant_ids
              ,  depends_on
              ,  dependency_type
              ,  lag_days
              ,  estimated_hours
              ,  actual_hours
              ,  estimated_cost
              ,  sort_order
            FROM {PROJECT_TASKS_TABLE}
            WHERE project_id = :project_id
            {_project_task_active_id_subquery_sql()}
            ORDER BY 
                     (parent_task_id IS NULL) DESC,
                     parent_task_id ASC,
                     sort_order ASC,
                     id ASC
        """
        result = current_app.db_manager.execute_query(sql, {'project_id': project_id})
        resp = jsonify({'code': 200, 'msg': 'success', 'data': result or []})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('获取项目任务失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取项目任务失败: {str(e)}'}), 500


@project_app.route('/project/tasks/create', methods=['POST'])
def create_project_task():
    """
    在 project_tasks 表新增一行。JSON：project_id（必填）、task_name（必填）；
    可选 parent_task_id、project_category_id、project_processes_id（或 processes_id）、
    assignee_id、assignee_name、planned_start_date、planned_end_date（YYYY-MM-DD 或空）；
    actual_start_date、actual_end_date 可单独传；未传时若带了计划日期则默认与计划日期相同（新建任务对齐计划与实际）。
    """
    try:
        body = request.get_json(silent=True) or {}
        try:
            project_id = int(body.get('project_id'))
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 project_id'}), 400

        task_name = (body.get('task_name') or '').strip()
        if not task_name:
            return jsonify({'code': 400, 'msg': '任务名称不能为空'}), 400

        parent_raw = body.get('parent_task_id')
        if parent_raw is None or (isinstance(parent_raw, str) and parent_raw.strip() == ''):
            parent_task_id = None
        else:
            try:
                parent_task_id = int(parent_raw)
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': '非法 parent_task_id'}), 400

        if parent_task_id is not None:
            prow = current_app.db_manager.execute_query(
                f"""
                SELECT id FROM {PROJECT_TASKS_TABLE}
                WHERE id = :pid AND project_id = :project_id
                LIMIT 1
                """,
                {'pid': parent_task_id, 'project_id': project_id},
            )
            if not prow:
                return jsonify({'code': 400, 'msg': '父任务不存在或不属于该项目'}), 400

        pc_raw = body.get('project_category_id')
        if pc_raw is None or (isinstance(pc_raw, str) and pc_raw.strip() == ''):
            project_category_id = None
        else:
            try:
                project_category_id = int(pc_raw)
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': '非法 project_category_id'}), 400

        proc_raw = body.get('processes_id')
        if proc_raw is None and 'project_processes_id' in body:
            proc_raw = body.get('project_processes_id')
        if proc_raw is None or (isinstance(proc_raw, str) and str(proc_raw).strip() == ''):
            processes_id = None
        else:
            try:
                processes_id = int(proc_raw)
            except (TypeError, ValueError):
                processes_id = str(proc_raw).strip()

        def norm_date(key):
            v = body.get(key)
            if v is None or (isinstance(v, str) and v.strip() == ''):
                return None
            s = str(v).strip()[:10]
            return s if s else None

        planned_start_date = norm_date('planned_start_date')
        planned_end_date = norm_date('planned_end_date')
        actual_start_date = norm_date('actual_start_date')
        if actual_start_date is None and planned_start_date is not None:
            actual_start_date = planned_start_date
        actual_end_date = norm_date('actual_end_date')
        if actual_end_date is None and planned_end_date is not None:
            actual_end_date = planned_end_date

        assignee_id = None
        aid_raw = body.get('assignee_id')
        if aid_raw is not None and not (isinstance(aid_raw, str) and aid_raw.strip() == ''):
            assignee_id = str(aid_raw).strip()[:128]

        assignee_name = None
        an_raw = body.get('assignee_name')
        if an_raw is not None and not (isinstance(an_raw, str) and an_raw.strip() == ''):
            assignee_name = str(an_raw).strip()[:128]

        max_rows = current_app.db_manager.execute_query(
            f"""
            SELECT COALESCE(MAX(sort_order), 0) AS m
            FROM {PROJECT_TASKS_PHYSICAL_TABLE}
            WHERE project_id = :project_id
              AND (parent_task_id <=> :parent_task_id)
            """,
            {'project_id': project_id, 'parent_task_id': parent_task_id},
        )
        next_sort = 1
        if max_rows and max_rows[0].get('m') is not None:
            try:
                next_sort = int(max_rows[0]['m']) + 1
            except (TypeError, ValueError):
                next_sort = 1

        row = {
            'project_id': project_id,
            'task_name': task_name,
            'parent_task_id': parent_task_id,
            'project_category_id': project_category_id,
            'project_processes_id': processes_id,
            'planned_start_date': planned_start_date,
            'planned_end_date': planned_end_date,
            'actual_start_date': actual_start_date,
            'actual_end_date': actual_end_date,
            'sort_order': next_sort,
            'task_status': '待执行',
            'assignee_id': assignee_id,
            'assignee_name': assignee_name,
        }

        new_id = current_app.db_manager.insert_one(PROJECT_TASKS_PHYSICAL_TABLE, row)
        resp = jsonify({'code': 200, 'msg': 'success', 'data': {'id': new_id, **row}})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('创建项目任务失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'创建项目任务失败: {str(e)}'}), 500


@project_app.route('/project/tasks/update', methods=['POST'])
def update_project_task_hierarchy():
    """
    更新任务层级：JSON 需含 project_id、task_id；
    可选 parent_task_id（null 表示挂在流程/阶段下）、processes_id（与 GET /project/tasks 列名一致，也可用 project_processes_id）、
    sort_order、sibling_sort_orders（[{task_id, sort_order}]，平级拖放后批量更新同级序号）。
    """
    try:
        body = request.get_json(silent=True) or {}
        project_id = body.get('project_id')
        task_id = body.get('task_id') if body.get('task_id') is not None else body.get('id')
        try:
            project_id = int(project_id)
            task_id = int(task_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 project_id / task_id'}), 400

        rows = current_app.db_manager.execute_query(
            f"""
            SELECT id, project_id FROM {PROJECT_TASKS_TABLE}
            WHERE id = :task_id AND project_id = :project_id
            LIMIT 1
            """,
            {'task_id': task_id, 'project_id': project_id},
        )
        if not rows:
            return jsonify({'code': 404, 'msg': '任务不存在或不属于该项目'}), 404

        updates = {}
        if 'parent_task_id' in body:
            p = body.get('parent_task_id')
            if p is None or (isinstance(p, str) and p.strip() == ''):
                updates['parent_task_id'] = None
            else:
                try:
                    pid = int(p)
                except (TypeError, ValueError):
                    return jsonify({'code': 400, 'msg': '非法 parent_task_id'}), 400
                if pid == task_id:
                    return jsonify({'code': 400, 'msg': 'parent_task_id 不能为自身'}), 400
                prow = current_app.db_manager.execute_query(
                    f"""
                    SELECT id FROM {PROJECT_TASKS_TABLE}
                    WHERE id = :pid AND project_id = :project_id
                    LIMIT 1
                    """,
                    {'pid': pid, 'project_id': project_id},
                )
                if not prow:
                    return jsonify({'code': 400, 'msg': '父任务不存在或不属于该项目'}), 400
                updates['parent_task_id'] = pid

        proc_key = None
        if 'processes_id' in body:
            proc_key = body.get('processes_id')
        elif 'project_processes_id' in body:
            proc_key = body.get('project_processes_id')
        if proc_key is not None:
            proc_col = _project_task_processes_id_column()
            if proc_key is None or (isinstance(proc_key, str) and proc_key.strip() == ''):
                updates[proc_col] = None
            else:
                try:
                    updates[proc_col] = int(proc_key)
                except (TypeError, ValueError):
                    updates[proc_col] = str(proc_key).strip()

        if 'sort_order' in body and body.get('sort_order') is not None:
            try:
                updates['sort_order'] = int(body.get('sort_order'))
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': '非法 sort_order'}), 400

        sibling_sort_orders = body.get('sibling_sort_orders')
        if sibling_sort_orders is not None and not isinstance(sibling_sort_orders, list):
            return jsonify({'code': 400, 'msg': 'sibling_sort_orders 须为数组'}), 400

        if not updates and not sibling_sort_orders:
            return jsonify({'code': 400, 'msg': '没有可更新字段'}), 400

        applied = {}
        if updates:
            ok = current_app.db_manager.update_by_id(
                PROJECT_TASKS_PHYSICAL_TABLE, task_id, updates,
            )
            if not ok:
                check = current_app.db_manager.execute_query(
                    f"SELECT id FROM {PROJECT_TASKS_PHYSICAL_TABLE} WHERE id = :tid LIMIT 1",
                    {'tid': task_id},
                )
                if not check:
                    return jsonify({'code': 404, 'msg': '任务不存在'}), 404
                return jsonify({'code': 500, 'msg': '任务未更新'}), 500
            applied.update(updates)

        sorted_siblings = []
        if sibling_sort_orders:
            for item in sibling_sort_orders:
                if not isinstance(item, dict):
                    continue
                try:
                    sid = int(item.get('task_id') if item.get('task_id') is not None else item.get('id'))
                    sord = int(item.get('sort_order'))
                except (TypeError, ValueError):
                    continue
                if sid <= 0:
                    continue
                belong = current_app.db_manager.execute_query(
                    f"""
                    SELECT id FROM {PROJECT_TASKS_PHYSICAL_TABLE}
                    WHERE id = :sid AND project_id = :project_id
                    LIMIT 1
                    """,
                    {'sid': sid, 'project_id': project_id},
                )
                if not belong:
                    continue
                current_app.db_manager.update_by_id(
                    PROJECT_TASKS_PHYSICAL_TABLE, sid, {'sort_order': sord},
                )
                sorted_siblings.append({'task_id': sid, 'sort_order': sord})

        resp = jsonify({
            'code': 200,
            'msg': 'success',
            'data': {
                'task_id': task_id,
                **applied,
                'sibling_sort_orders': sorted_siblings,
            },
        })
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('更新任务层级失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'更新任务层级失败: {str(e)}'}), 500


def _norm_task_date_value(v):
    if v is None or v == '':
        return None
    s = str(v).strip()[:10]
    return s if s else None


def _norm_task_status_for_db(raw):
    """
    与新建任务默认 task_status='待执行'、库内 ENUM 习惯一致。
    前端三态「未开始」对应库内「待执行」，避免写入 ENUM 不存在的「未开始」触发 Data truncated。
    """
    s = str(raw if raw is not None else '').strip()
    if s in ('已完成', '已结束'):
        return '已完成'
    if s == '进行中':
        return '进行中'
    if s in ('未开始', '待执行', ''):
        return '待执行'
    return '待执行'






@project_app.route('/project/tasks/update-detail', methods=['POST'])
def update_project_task_detail():
    """
    更新任务内容（非评论）：JSON 需含 project_id、task_id；
    可选 task_name、task_description、current_task_progress、planned_start_date、planned_end_date、
    actual_start_date、actual_end_date、task_status、priority、assignee_id、assignee_name、
    participant_ids（JSON 字符串或数组，入库前转字符串）、collaborators_ids、collaborators_names。
    """
    try:
        body = request.get_json(silent=True) or {}
        try:
            project_id = int(body.get('project_id'))
            task_id = int(body.get('task_id') if body.get('task_id') is not None else body.get('id'))
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 project_id / task_id'}), 400

        rows = current_app.db_manager.execute_query(
            f"""
            SELECT id FROM {PROJECT_TASKS_TABLE}
            WHERE id = :task_id AND project_id = :project_id
            LIMIT 1
            """,
            {'task_id': task_id, 'project_id': project_id},
        )
        if not rows:
            return jsonify({'code': 404, 'msg': '任务不存在或不属于该项目'}), 404

        updates = {}

        if 'task_name' in body and body.get('task_name') is not None:
            tn = str(body.get('task_name') or '').strip()
            if not tn:
                return jsonify({'code': 400, 'msg': '任务名称不能为空'}), 400
            updates['task_name'] = tn[:512]

        if 'task_description' in body:
            td = body.get('task_description')
            if td is None:
                updates['task_description'] = None
            else:
                updates['task_description'] = str(td)[:8000]

        if 'planned_start_date' in body:
            updates['planned_start_date'] = _norm_task_date_value(body.get('planned_start_date'))
        if 'planned_end_date' in body:
            updates['planned_end_date'] = _norm_task_date_value(body.get('planned_end_date'))

        if 'actual_start_date' in body:
            updates['actual_start_date'] = _norm_task_date_value(body.get('actual_start_date'))
        if 'actual_end_date' in body:
            updates['actual_end_date'] = _norm_task_date_value(body.get('actual_end_date'))

        if 'task_status' in body and body.get('task_status') is not None:
            updates['task_status'] = _norm_task_status_for_db(body.get('task_status'))[:64] or None

        if 'priority' in body and body.get('priority') is not None:
            updates['priority'] = str(body.get('priority')).strip()[:32] or None

        if 'assignee_id' in body:
            aid = body.get('assignee_id')
            if aid is None or (isinstance(aid, str) and aid.strip() == ''):
                updates['assignee_id'] = None
            else:
                updates['assignee_id'] = str(aid).strip()[:128]

        if 'assignee_name' in body:
            an = body.get('assignee_name')
            if an is None or (isinstance(an, str) and an.strip() == ''):
                updates['assignee_name'] = None
            else:
                updates['assignee_name'] = str(an).strip()[:128]

        if 'participant_ids' in body:
            pid = body.get('participant_ids')
            if pid is None or pid == '':
                updates['participant_ids'] = None
            elif isinstance(pid, (list, tuple, dict)):
                updates['participant_ids'] = json.dumps(pid, ensure_ascii=False)
            else:
                updates['participant_ids'] = str(pid).strip()[:4000] or None

        if 'collaborators_ids' in body:
            cid = body.get('collaborators_ids')
            if cid is None or (isinstance(cid, str) and cid.strip() == ''):
                updates['collaborators_ids'] = None
            else:
                updates['collaborators_ids'] = str(cid).strip()[:4000] or None

        if 'collaborators_names' in body:
            cnm = body.get('collaborators_names')
            if cnm is None or (isinstance(cnm, str) and cnm.strip() == ''):
                updates['collaborators_names'] = None
            else:
                updates['collaborators_names'] = str(cnm).strip()[:4000] or None

        status_rows = current_app.db_manager.execute_query(
            f"""
            SELECT task_status FROM {PROJECT_TASKS_PHYSICAL_TABLE}
            WHERE id = :task_id AND project_id = :project_id
            LIMIT 1
            """,
            {'task_id': task_id, 'project_id': project_id},
        )
        cur_status_db = (
            status_rows[0].get('task_status') if status_rows else '待执行'
        )
        eff_status_db = updates.get('task_status', cur_status_db)
        status_tri = _task_status_tri_from_db(eff_status_db)
        has_children = _project_task_has_active_children(task_id, project_id)

        cpp_raw = None
        if 'current_task_progress' in body:
            cpp_raw = body.get('current_task_progress')
        elif 'current_project_progress' in body:
            cpp_raw = body.get('current_project_progress')

        if cpp_raw is not None:
            try:
                pct = float(cpp_raw)
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'current_task_progress 必须为数字'}), 400
            if pct < 0 or pct > 100:
                return jsonify({'code': 400, 'msg': 'current_task_progress 须在 0~100 之间'}), 400
            if has_children:
                updates['current_task_progress'] = round(pct, 2)
            elif status_tri == '进行中':
                updates['current_task_progress'] = round(pct, 2)
            else:
                # 未开始/已完成：随状态写入 0 或 100（可与 task_status 一并提交）
                updates['current_task_progress'] = _resolve_current_task_progress_value(
                    status_tri, False,
                )

        if 'task_status' in updates and not has_children and status_tri in ('未开始', '已完成'):
            if 'current_task_progress' not in updates:
                updates['current_task_progress'] = _resolve_current_task_progress_value(
                    status_tri, False,
                )

        if not updates:
            return jsonify({'code': 400, 'msg': '没有可更新字段'}), 400

        ok = current_app.db_manager.update_by_id(PROJECT_TASKS_PHYSICAL_TABLE, task_id, updates)
        if not ok:
            check = current_app.db_manager.execute_query(
                f"SELECT id FROM {PROJECT_TASKS_PHYSICAL_TABLE} WHERE id = :tid LIMIT 1",
                {'tid': task_id},
            )
            if not check:
                return jsonify({'code': 404, 'msg': '任务未更新'}), 404

        resp = jsonify({'code': 200, 'msg': 'success', 'data': {'task_id': task_id, **updates}})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('更新任务详情失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'更新任务详情失败: {str(e)}'}), 500


def _collect_project_task_descendant_ids(rows, root_task_id):
    """在 project_id 范围内，收集 root_task_id 及其全部子孙任务 id。"""
    by_parent = {}
    id_set = set()
    for r in rows or []:
        try:
            tid = int(r.get('id'))
        except (TypeError, ValueError):
            continue
        if tid <= 0:
            continue
        id_set.add(tid)
        pid_raw = r.get('parent_task_id')
        if pid_raw is None or pid_raw == '':
            continue
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        by_parent.setdefault(pid, []).append(tid)

    try:
        root_id = int(root_task_id)
    except (TypeError, ValueError):
        return set()
    if root_id not in id_set:
        return set()

    out = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(by_parent.get(cur, []))
    return out


def _project_task_processes_id_column():
    """物理表 project_tasks 上流程实例 id 列名（优先 project_processes_id）。"""
    try:
        rows = current_app.db_manager.execute_query(
            """
            SELECT COLUMN_NAME AS col
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table
              AND COLUMN_NAME IN ('project_processes_id', 'processes_id')
            ORDER BY FIELD(COLUMN_NAME, 'project_processes_id', 'processes_id')
            LIMIT 1
            """,
            {'table': PROJECT_TASKS_PHYSICAL_TABLE},
        )
        if rows:
            return rows[0].get('col')
    except Exception as e:
        logger.warning('读取 project_tasks 流程实例列失败: %s', e)
    return 'project_processes_id'


def _project_task_not_deleted_sql(table_alias=''):
    """返回用于 WHERE 的未软删条件（含前导 AND）；优先用物理表子查询（视图可能无 is_deleted）。"""
    col = _project_task_soft_delete_flag_column()
    if not col:
        return ''
    prefix = f'{table_alias}.' if table_alias else ''
    return f' AND ({prefix}{col} = 0 OR {prefix}{col} IS NULL)'


def _project_task_active_id_subquery_sql():
    """
    基于物理表 project_tasks 过滤未软删任务 id。
    v_project_tasks 可能不含 is_deleted，直接 WHERE is_deleted 会无效或报错。
    """
    col = _project_task_soft_delete_flag_column()
    if not col:
        return ''
    return f"""
      AND id IN (
        SELECT id FROM {PROJECT_TASKS_PHYSICAL_TABLE}
        WHERE project_id = :project_id
          AND ({col} = 0 OR {col} IS NULL)
      )
    """


def _project_task_active_by_id_sql():
    """单条任务详情：仅当物理表行未软删时返回。"""
    col = _project_task_soft_delete_flag_column()
    if not col:
        return ''
    return f"""
      AND EXISTS (
        SELECT 1 FROM {PROJECT_TASKS_PHYSICAL_TABLE} pt
        WHERE pt.id = :task_id
          AND (pt.{col} = 0 OR pt.{col} IS NULL)
      )
    """


def _task_status_tri_from_db(raw):
    s = str(raw if raw is not None else '').strip()
    if s == '进行中':
        return '进行中'
    if s in ('已完成', '已结束'):
        return '已完成'
    return '未开始'


def _project_task_has_active_children(task_id, project_id):
    col = _project_task_soft_delete_flag_column()
    del_clause = ''
    if col:
        del_clause = f' AND (pt.{col} = 0 OR pt.{col} IS NULL)'
    rows = current_app.db_manager.execute_query(
        f"""
        SELECT 1 AS ok
        FROM {PROJECT_TASKS_PHYSICAL_TABLE} pt
        WHERE pt.project_id = :project_id
          AND pt.parent_task_id = :task_id
          {del_clause}
        LIMIT 1
        """,
        {'project_id': int(project_id), 'task_id': int(task_id)},
    )
    return bool(rows)


def _resolve_current_task_progress_value(task_status_tri, has_children, requested=None):
    if task_status_tri == '未开始':
        return 0
    if task_status_tri == '已完成':
        return 100
    if task_status_tri == '进行中' and has_children:
        return None
    if requested is None or (isinstance(requested, str) and str(requested).strip() == ''):
        return None
    try:
        pct = float(requested)
    except (TypeError, ValueError):
        raise ValueError('current_task_progress 必须为数字')
    if pct < 0 or pct > 100:
        raise ValueError('current_task_progress 须在 0~100 之间')
    return round(pct, 2)


def _project_task_soft_delete_flag_column():
    """探测 project_tasks 软删除列名（优先 is_deleted，与库表常见命名一致）。"""
    try:
        rows = current_app.db_manager.execute_query(
            """
            SELECT COLUMN_NAME AS col
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table
              AND COLUMN_NAME IN ('is_deleted', 'is_delete')
            ORDER BY FIELD(COLUMN_NAME, 'is_deleted', 'is_delete')
            LIMIT 1
            """,
            {'table': PROJECT_TASKS_PHYSICAL_TABLE},
        )
        if rows:
            return rows[0].get('col')
    except Exception as e:
        logger.warning('读取 project_tasks 软删除列失败，回退探测: %s', e)

    for col in ('is_deleted', 'is_delete'):
        try:
            current_app.db_manager.execute_query(
                f"""
                SELECT {col} FROM {PROJECT_TASKS_PHYSICAL_TABLE}
                WHERE 1 = 0
                LIMIT 1
                """,
            )
            return col
        except Exception as e:
            err = str(e).lower()
            if 'unknown column' in err or '1054' in err:
                continue
            raise
    return None


def _soft_delete_project_tasks(project_id, task_id):
    flag_col = _project_task_soft_delete_flag_column()
    if not flag_col:
        return None, '数据表缺少 is_delete / is_deleted 字段，无法软删除任务'

    rows = current_app.db_manager.execute_query(
        f"""
        SELECT id, parent_task_id
        FROM {PROJECT_TASKS_PHYSICAL_TABLE}
        WHERE project_id = :project_id
          AND ({flag_col} = 0 OR {flag_col} IS NULL)
        """,
        {'project_id': project_id},
    )
    if not rows:
        return None, '任务不存在或不属于该项目'

    ids = _collect_project_task_descendant_ids(rows, task_id)
    if not ids:
        return None, '任务不存在或不属于该项目'

    placeholders = ', '.join(f':tid_{i}' for i in range(len(ids)))
    params = {'project_id': project_id}
    for i, tid in enumerate(ids):
        params[f'tid_{i}'] = tid

    affected_rows = current_app.db_manager.execute_update(
        f"""
        UPDATE {PROJECT_TASKS_PHYSICAL_TABLE}
        SET {flag_col} = 1
        WHERE project_id = :project_id
          AND id IN ({placeholders})
          AND ({flag_col} = 0 OR {flag_col} IS NULL)
        """,
        params,
    )
    if affected_rows <= 0:
        return None, '任务未删除（可能已删除）'
    return {
        'task_id': task_id,
        'deleted_ids': sorted(ids),
        'affected_rows': affected_rows,
        'flag_column': flag_col,
    }, None


@project_app.route('/project/tasks/delete', methods=['POST'])
@project_app.route('/project/tasks/soft-delete', methods=['POST'])
def delete_project_task():
    """
    软删除任务：将 project_tasks.is_delete（或 is_deleted）置为 1（含其全部子任务）。
    JSON：project_id、task_id（整数，必填）。
    """
    try:
        body = request.get_json(silent=True) or {}
        try:
            project_id = int(body.get('project_id'))
            task_id = int(body.get('task_id') if body.get('task_id') is not None else body.get('id'))
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 project_id / task_id'}), 400

        data, err = _soft_delete_project_tasks(project_id, task_id)
        if err:
            code = 404 if '不存在' in err or '已删除' in err else 500
            return jsonify({'code': code, 'msg': err}), code

        resp = jsonify({'code': 200, 'msg': '删除成功', 'data': data})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('删除项目任务失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'删除项目任务失败: {str(e)}'}), 500


def _dashboard_uid_str(user_id):
    return str(int(user_id)).strip()


def _dashboard_user_in_csv_ids(csv_val, user_id):
    if csv_val is None:
        return False
    uid = _dashboard_uid_str(user_id)
    raw = str(csv_val).strip()
    if not raw:
        return False
    return any(part.strip() == uid for part in raw.split(','))


def _dashboard_user_in_participant_ids(participant_ids, user_id):
    if participant_ids is None:
        return False
    uid = _dashboard_uid_str(user_id)
    raw = str(participant_ids).strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return any(str(x).strip() == uid for x in parsed)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return uid in raw


def _dashboard_user_is_assignee(row, user_id):
    aid = row.get('assignee_id')
    if aid is None or str(aid).strip() == '':
        return False
    return str(aid).strip() == _dashboard_uid_str(user_id)


def _dashboard_user_is_collaborator(row, user_id):
    if _dashboard_user_in_csv_ids(row.get('collaborators_ids'), user_id):
        return True
    return _dashboard_user_in_participant_ids(row.get('participant_ids'), user_id)


def _dashboard_resolve_my_role(row, user_id):
    is_assignee = _dashboard_user_is_assignee(row, user_id)
    is_collab = _dashboard_user_is_collaborator(row, user_id)
    if is_assignee and is_collab:
        return 'both'
    if is_assignee:
        return 'assignee'
    if is_collab:
        return 'collaborator'
    return None


def _dashboard_task_active_ids_subquery(alias='t'):
    col = _project_task_soft_delete_flag_column()
    if not col:
        return ''
    return f"""
      AND EXISTS (
        SELECT 1 FROM {PROJECT_TASKS_PHYSICAL_TABLE} pt
        WHERE pt.id = {alias}.id
          AND (pt.{col} = 0 OR pt.{col} IS NULL)
      )
    """


def _dashboard_leaf_task_only_sql(alias='t'):
    """无子任务的任务才展示；存在子任务的父任务不在首页看板展示。"""
    col = _project_task_soft_delete_flag_column()
    del_clause = ''
    if col:
        del_clause = f' AND (child.{col} = 0 OR child.{col} IS NULL)'
    return f"""
      AND NOT EXISTS (
        SELECT 1 FROM {PROJECT_TASKS_PHYSICAL_TABLE} child
        WHERE child.parent_task_id = {alias}.id
          AND child.project_id = {alias}.project_id
          {del_clause}
      )
    """


def _dashboard_planned_end_date_only(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    return s


def _dashboard_is_overdue(row):
    status = _task_status_tri_from_db(row.get('task_status'))
    if status == '已完成':
        return False
    end = _dashboard_planned_end_date_only(row.get('planned_end_date'))
    if not end:
        return False
    try:
        y, m, d = [int(x) for x in end.split('-')]
        return date(y, m, d) < date.today()
    except (TypeError, ValueError):
        return False


@project_app.route('/project/tasks/dashboard', methods=['GET'])
def get_project_tasks_dashboard():
    """
    首页任务看板：当前用户在空间内「我的任务」与「评论 @ 提及我的任务」。
    仅展示无子任务的叶子任务；存在子任务的父任务不展示。
    Query: namespace_id, user_id（整数）
    """
    try:
        namespace_id = request.args.get('namespace_id', type=int)
        user_id = request.args.get('user_id', type=int)
        if namespace_id is None or user_id is None:
            return jsonify({'code': 400, 'msg': '缺少必要参数 namespace_id 或 user_id'}), 400

        uid_str = _dashboard_uid_str(user_id)
        project_rows = current_app.db_manager.execute_query(
            """
            SELECT id AS project_id, project_name, project_code, status AS project_status
            FROM v_projects_info
            WHERE namespace_id = :namespace_id AND user_id = :user_id
            ORDER BY id DESC
            """,
            {'namespace_id': namespace_id, 'user_id': user_id},
        ) or []
        project_ids = [int(r['project_id']) for r in project_rows if r.get('project_id') is not None]
        project_name_by_id = {
            int(r['project_id']): str(r.get('project_name') or '').strip()
            for r in project_rows
            if r.get('project_id') is not None
        }

        my_tasks = []
        if project_ids:
            placeholders = ', '.join(f':pid{i}' for i in range(len(project_ids)))
            params = {f'pid{i}': pid for i, pid in enumerate(project_ids)}
            extra_cols = []
            if _table_has_column(PROJECT_TASKS_TABLE, 'collaborators_ids'):
                extra_cols.append('collaborators_ids')
            if _table_has_column(PROJECT_TASKS_TABLE, 'collaborators_names'):
                extra_cols.append('collaborators_names')
            extra_sql = (', ' + ', '.join(extra_cols)) if extra_cols else ''
            task_sql = f"""
                SELECT id
                     , project_id
                     , task_name
                     , task_status
                     , priority
                     , planned_start_date
                     , planned_end_date
                     , current_task_progress
                     , progress_percent
                     , assignee_id
                     , assignee_name
                     , participant_ids
                     {extra_sql}
                FROM {PROJECT_TASKS_TABLE} t
                WHERE t.project_id IN ({placeholders})
                {_dashboard_task_active_ids_subquery('t')}
                {_dashboard_leaf_task_only_sql('t')}
                ORDER BY t.planned_end_date IS NULL, t.planned_end_date ASC, t.id DESC
            """
            task_rows = current_app.db_manager.execute_query(task_sql, params) or []
            for row in task_rows:
                role = _dashboard_resolve_my_role(row, user_id)
                if not role:
                    continue
                tid = row.get('id')
                pid = row.get('project_id')
                status = _task_status_tri_from_db(row.get('task_status'))
                progress = row.get('current_task_progress')
                if progress is None:
                    progress = row.get('progress_percent')
                my_tasks.append({
                    'id': tid,
                    'project_id': pid,
                    'project_name': project_name_by_id.get(int(pid), '') if pid is not None else '',
                    'task_name': str(row.get('task_name') or '').strip() or '未命名任务',
                    'task_status': status,
                    'priority': str(row.get('priority') or 'medium').strip() or 'medium',
                    'planned_start_date': _dashboard_planned_end_date_only(row.get('planned_start_date')),
                    'planned_end_date': _dashboard_planned_end_date_only(row.get('planned_end_date')),
                    'current_task_progress': progress,
                    'assignee_id': row.get('assignee_id'),
                    'assignee_name': row.get('assignee_name'),
                    'my_role': role,
                    'is_overdue': _dashboard_is_overdue(row),
                })

        mention_sql = f"""
            SELECT c.id AS comment_id
                 , c.task_id
                 , c.project_id
                 , c.user_id AS comment_user_id
                 , c.content AS comment_content
                 , c.created_at AS comment_created_at
                 , c.mention_user_ids
                 , t.task_name
                 , t.task_status
                 , t.planned_end_date
                 , t.assignee_name
                 , p.project_name
            FROM {PROJECT_TASK_COMMENTS_PHYSICAL_TABLE} c
            INNER JOIN {PROJECT_TASKS_TABLE} t ON t.id = c.task_id
            INNER JOIN v_projects_info p
                ON p.id = c.project_id
               AND p.namespace_id = :namespace_id
               AND p.user_id = :user_id
            WHERE (c.is_deleted = 0 OR c.is_deleted IS NULL)
              AND c.mention_user_ids IS NOT NULL
              AND TRIM(c.mention_user_ids) != ''
              {_dashboard_task_active_ids_subquery('t')}
              {_dashboard_leaf_task_only_sql('t')}
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT 100
        """
        mention_rows = current_app.db_manager.execute_query(
            mention_sql,
            {'namespace_id': namespace_id, 'user_id': user_id},
        ) or []
        mentioned_tasks = []
        seen_mention_keys = set()
        for row in mention_rows:
            if not _dashboard_user_in_csv_ids(row.get('mention_user_ids'), user_id):
                continue
            key = (row.get('task_id'), row.get('comment_id'))
            if key in seen_mention_keys:
                continue
            seen_mention_keys.add(key)
            comment_uid = row.get('comment_user_id')
            mentioned_tasks.append({
                'comment_id': row.get('comment_id'),
                'task_id': row.get('task_id'),
                'project_id': row.get('project_id'),
                'project_name': str(row.get('project_name') or '').strip(),
                'task_name': str(row.get('task_name') or '').strip() or '未命名任务',
                'task_status': _task_status_tri_from_db(row.get('task_status')),
                'planned_end_date': _dashboard_planned_end_date_only(row.get('planned_end_date')),
                'assignee_name': row.get('assignee_name'),
                'comment_content': str(row.get('comment_content') or '').strip(),
                'comment_created_at': row.get('comment_created_at'),
                'comment_user_name': _resolve_user_display_name(comment_uid) if comment_uid is not None else '',
            })

        active_my = [t for t in my_tasks if t.get('task_status') != '已完成']
        stats = {
            'my_total': len(active_my),
            'my_in_progress': sum(1 for t in active_my if t.get('task_status') == '进行中'),
            'my_overdue': sum(1 for t in active_my if t.get('is_overdue')),
            'mentioned_total': len(mentioned_tasks),
        }

        resp = jsonify({
            'code': 200,
            'msg': 'success',
            'data': {
                'stats': stats,
                'my_tasks': my_tasks,
                'mentioned_tasks': mentioned_tasks,
            },
        })
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('获取任务看板失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取任务看板失败: {str(e)}'}), 500


@project_app.route('/project/dates', methods=['POST'])
def update_project_dates():
    """JSON: project_id（整数）, 以及若干日期列（值 YYYY-MM-DD 或 null/空字符串表示置空）"""
    try:
        body = request.get_json(silent=True) or {}
        project_id = body.get('project_id')

        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': '缺少或非法参数 project_id'}), 400

        updates = {}
        for k in PROJECT_DATE_FIELDS:
            if k not in body:
                continue
            v = body.get(k)
            if v is None or (isinstance(v, str) and v.strip() == ''):
                updates[k] = None
            else:
                updates[k] = str(v).strip()[:10]

        if not updates:
            return jsonify({'code': 400, 'msg': '没有可更新的日期字段'}), 400

        perm_err = _require_project_manage_permission(body)
        if perm_err:
            return jsonify({'code': 403, 'msg': perm_err}), 403

        ok = current_app.db_manager.update_by_id(PROJECT_TABLE, project_id, updates)
        if not ok:
            return jsonify({'code': 404, 'msg': '项目不存在或未更新'}), 404

        resp = jsonify({'code': 200, 'msg': 'success', 'data': updates})
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp
    except Exception as e:
        logger.error('更新项目日期失败: %s', str(e), exc_info=True)
        return jsonify({'code': 500, 'msg': f'更新项目日期失败: {str(e)}'}), 500


__all__ = [
    'project_app',
    'get_projectList',
    'get_projectDetail',
    'create_project',
    'get_project_tasks',
    'create_project_task',
    'update_project_task_hierarchy',
    'list_project_categories',
    'get_project_category',
    'update_project_info',
    'update_project_status',
    'update_project_dates',
    'save_project_manage',
    'get_project_manage_permission',
    'get_project_tasks_detail',
    'get_project_task_comments',
    'create_project_task_comment',
    'delete_project_task_comment',
    'update_project_task_detail',
    'delete_project_task',
    'get_project_user_authorize',
]

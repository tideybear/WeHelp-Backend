from flask import Blueprint, request, jsonify, current_app
import logging
import secrets
from werkzeug.security import check_password_hash

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建蓝图
user_app = Blueprint('user', __name__)


def _verify_password(stored: str, plain: str) -> bool:
    """校验密码：优先按 Werkzeug pbkdf2 哈希校验，否则按明文相等（兼容演示库）。"""
    if stored is None or plain is None:
        return False
    s = str(stored)
    if s.startswith('pbkdf2:') or s.startswith('scrypt:'):
        return check_password_hash(s, plain)
    sa, pa = s.encode('utf-8'), plain.encode('utf-8')
    if len(sa) != len(pa):
        return False
    return secrets.compare_digest(sa, pa)


def _login_payload(row: dict) -> dict:
    """构造前端 userStore 所需字段（须含 token）。"""
    uid = row.get('id')
    account = row.get('account') or row.get('username') or ''
    # 简易令牌：演示环境使用随机串；生产可换 JWT
    token = secrets.token_urlsafe(32)
    name = row.get('name')
    if name is not None:
        name = str(name).strip()
    out = {
        'token': token,
        'account': account,
        'id': uid,
        'name': name or account,
    }
    return out


# 获取用户table,conditions
@user_app.route('/user/list', methods=['GET'])
def get_userList():
    try:
        namespace_id = request.args.get('namespace_id')
        if not namespace_id:
            return jsonify({'code': 400, 'msg': '缺少必要参数namespace_id'}), 400
        sql = """
            SELECT u.id, u.name, u.account, na.data_authorize_lv,na.is_finance_professional
            FROM namespace_authorize na
            INNER JOIN `user` u ON u.id = na.user_id
            WHERE na.namespace_id = :namespace_id
              AND na.is_deleted = 0
              AND u.is_deleted = 0
              AND na.user_id != 1
        """
        result = current_app.db_manager.execute_query(sql, {'namespace_id': namespace_id})
        return jsonify({'code': 200, 'msg': 'success', 'data': result})
    except Exception as e:
        logger.error(f'获取用户列表失败: {str(e)}', exc_info=True)
        return jsonify({'code': 500, 'msg': f'获取用户列表失败: {str(e)}'}), 500


@user_app.route('/login', methods=['POST'])
def login():
    """
    用户登录校验：校验 account + password，成功返回 result（含 token）。
    请求体 JSON：{"account": "", "password": ""}
    依赖表：`user`，至少包含 account（或 username）、password 字段。
    """
    try:
        body = request.get_json(silent=True) or {}
        account = (body.get('account') or body.get('username') or '').strip()
        password = body.get('password')
        if password is not None and not isinstance(password, str):
            password = str(password)

        if not account:
            return jsonify({'message': '请输入账号'}), 400
        if not password:
            return jsonify({'message': '请输入密码'}), 400

        rows = []
        try:
            rows = current_app.db_manager.execute_query(
                'SELECT id, account, name, password FROM `user` WHERE account = :account LIMIT 1',
                {'account': account},
            )
        except Exception as ex:
            logger.debug('按 account 列查询用户失败，尝试 username: %s', ex)
            rows = []
        if not rows:
            try:
                rows = current_app.db_manager.execute_query(
                    'SELECT id, username AS account, name, password FROM `user` WHERE username = :account LIMIT 1',
                    {'account': account},
                )
            except Exception as ex:
                logger.debug('按 username 列查询不可用或失败（可能仅有 account 列）: %s', ex)
                rows = []

        if not rows:
            logger.warning('登录失败：账号不存在 account=%s', account)
            return jsonify({'message': '账号或密码错误'}), 401

        row = rows[0]
        stored_pwd = row.get('password')
        if not _verify_password(stored_pwd, password):
            logger.warning('登录失败：密码错误 account=%s', account)
            return jsonify({'message': '账号或密码错误'}), 401

        payload = _login_payload(row)
        logger.info('登录成功 account=%s id=%s', account, payload.get('id'))
        return jsonify({'result': payload})
    except Exception as e:
        logger.error(f'登录接口异常: {str(e)}', exc_info=True)
        return jsonify({'message': f'登录失败: {str(e)}'}), 500


# 导出所有路由函数
__all__ = ['user_app', 'get_userList', 'login']

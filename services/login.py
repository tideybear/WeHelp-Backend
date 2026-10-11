from flask import Blueprint, request, jsonify, current_app
import logging
import secrets
from werkzeug.security import check_password_hash

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建蓝图
login_app = Blueprint('login', __name__)


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


@login_app.route('/login', methods=['POST'])
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
__all__ = ['login_app', 'login']

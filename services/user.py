from flask import Blueprint, request, jsonify, current_app
import logging

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建蓝图
user_app = Blueprint('user', __name__)


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


# 导出所有路由函数
__all__ = ['user_app', 'get_userList']

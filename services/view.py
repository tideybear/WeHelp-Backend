# -*- coding: utf-8 -*-
from flask import Blueprint, request, jsonify, current_app
import logging

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建蓝图
view_app = Blueprint('view', __name__)

# 获取项目列表，根据userid筛选
@view_app.route('/view/list', methods=['GET'])
def get_viewList():
    try:
        # 获取 namespace_id 参数
        namespace_id = request.args.get('namespace_id', type=int)
        if not namespace_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id"}), 400
        # 构建SQL查询
        sql = """
                SELECT *
                FROM view_authorize
                WHERE  namespace_id = :namespace_id
        """
        # 构建参数
        params = {"namespace_id": namespace_id}

        result = current_app.db_manager.execute_query(sql, params)
        # 返回结果
        response = jsonify({
            "code": 200, 
            "msg": "success", 
            "data": result
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

# 导出蓝图
__all__ = ['view_app']
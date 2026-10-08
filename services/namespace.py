# -*- coding: utf-8 -*-
from flask import Blueprint, request, jsonify, current_app
import logging

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建蓝图
namespace_app = Blueprint('namespace', __name__)

# 获取项目列表，根据userid筛选
@namespace_app.route('/namespace/list', methods=['GET'])
def get_namespaceList():
    try:
        # 获取userid参数
        userid = request.args.get('userid')
        ##logger.info(f"接收到的userid参数: {userid}")
        ##if not userid:
        ##    return jsonify({"code": 400, "msg": "缺少必要参数userid"}), 400
        # 构建SQL查询
        sql = """
            SELECT t1.*
                ,  t2.data_authorize_lv
            FROM
            (
                SELECT *
                FROM namespace 
            )t1
            INNER JOIN 
            (
                SELECT namespace_id
                    ,data_authorize_lv
                FROM namespace_authorize 
                WHERE user_id = :userid
            )t2
            ON t1.id = t2.namespace_id
        """
        
        # 构建参数
        params = {"userid": userid}
        ##logger.info(f"SQL参数: {params}")
        
        # 执行查询
        result = current_app.db_manager.execute_query(sql, params)
        ##logger.info(f"查询结果数量: {len(result)}")
        ##logger.info(f"查询结果数量: {result}")
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

# 导出所有路由函数
__all__ = ['namespace_app', 'get_namespaceList']
# -*- coding: utf-8 -*-
from flask import Blueprint, request, jsonify, current_app
import logging

# 创建日志记录器
logger = logging.getLogger(__name__)

# 创建蓝图
order_app = Blueprint('order', __name__)

# 获取项目列表，根据userid筛选
@order_app.route('/order/list', methods=['GET'])
def get_orderList():
    try:
        # 获取namespace_id,data_authorize_lv参数
        namespace_id = request.args.get('namespace_id')
        data_authorize_lv = request.args.get('data_authorize_lv')
        page = request.args.get('page', default=1, type=int)
        page_size = request.args.get('page_size', default=100, type=int)
        # 可选的数据来源，支持逗号分隔的多个值
        data_sources_raw = request.args.get('data_sources')
        #logger.info(f"接收到的namespace_id参数: {namespace_id}")
        #logger.info(f"接收到的data_authorize_lv参数: {data_authorize_lv}")
        
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
        data_source_clause = ""
        if data_sources_raw:
            data_source_list = [src.strip() for src in data_sources_raw.split(',') if src.strip()]
            if data_source_list:
                placeholders = []
                for idx, src in enumerate(data_source_list):
                    key = f"data_source_{idx}"
                    placeholders.append(f":{key}")
                    params[key] = src
                data_source_clause = f" AND data_source IN ({', '.join(placeholders)})"
        #logger.info(f"SQL参数: {params}")

        # 先查总数
        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM orders
            WHERE namespace_id=:namespace_id
            AND data_authorize_lv>=:data_authorize_lv
            {data_source_clause}
        """
        count_res = current_app.db_manager.execute_query(count_sql, params)
        total = int(count_res[0].get('total', 0)) if count_res else 0

        # 分页查询
        sql = f"""
            SELECT *
            FROM orders
            WHERE namespace_id=:namespace_id
            AND data_authorize_lv>=:data_authorize_lv
            {data_source_clause}
            LIMIT :limit OFFSET :offset
        """
        page_params = {**params, "limit": page_size, "offset": offset}
        
        # 执行查询
        result = current_app.db_manager.execute_query(sql, page_params)
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

@order_app.route('/order/filter', methods=['GET'])
def get_orderFilter():
    pass


# 导出所有路由函数
__all__ = ['order_app', 'get_orderList','get_orderFilter']
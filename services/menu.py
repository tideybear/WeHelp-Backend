from flask import Blueprint, request, jsonify, current_app
import logging

# 创建蓝图
menu_app = Blueprint('menu', __name__)

# 日志
logger = logging.getLogger(__name__)

# menuList：保存最近一次生成的菜单树结构，便于其它地方复用
menuList = []

# 获取所有菜单栏目（从数据库按权限查询并组装为树形结构）
@menu_app.route('/menu/list', methods=['GET'])
def get_menuList():
    try:
        global menuList
        # TODO: 根据实际登录用户和项目传参，这里先使用查询参数，缺省时可按需要设置默认值
        namespace_id = request.args.get('namespace_id', type=int, default=1)
        user_id = request.args.get('user_id', type=int, default=1)

        if not namespace_id or not user_id:
            return jsonify({"code": 400, "msg": "缺少必要参数 namespace_id 或 user_id"}), 400

        # 从数据库查询菜单（按权限）
        sql = """
            SELECT
                t2.*,
                t1.menu_sort
            FROM
            (
                SELECT menu_authorize_id, menu_sort
                FROM menu_authorize
                WHERE namespace_id = :namespace_id
                AND user_id = :user_id
            ) t1
            INNER JOIN 
            (
                SELECT * FROM menu
            ) t2
            ON t1.menu_authorize_id = t2.id
            ORDER BY t1.menu_sort
        """

        params = {"namespace_id": namespace_id, "user_id": user_id}
        rows = current_app.db_manager.execute_query(sql, params)

        # 将扁平数据转换为前端需要的树形 menuList 结构
        # 目标结构：
        # {
        #   "name": menu_name 或 title 的英文标识,
        #   "path": path,
        #   "meta": { "title": title, "icon": icon },
        #   "children": [...]
        # }

        # 先构造成字典方便根据 parent_id 组装
        node_map = {}
        roots = []

        for row in rows:
            node = {
                "id": row.get("id"),
                "name": row.get("title") or row.get("menu_name") or f"menu_{row.get('id')}",
                "path": row.get("path") or "",
                "meta": {
                    "title": row.get("menu_name") or "",
                    "icon": row.get("icon") or ""
                },
                "children": []
            }
            node_map[row.get("id")] = {
                "node": node,
                "parent_id": row.get("parent_id")
            }

        # 第二遍：挂接子节点
        for item in node_map.values():
            parent_id = item["parent_id"]
            node = item["node"]

            if parent_id and parent_id in node_map:
                node_map[parent_id]["node"].setdefault("children", []).append(node)
            else:
                roots.append(node)

        # 如果没有查到任何记录，返回空数组
        data = roots

        # 写入全局 menuList（保存当前生成的树形结构）
        menuList = data

        return jsonify({"code": 200, "msg": "success", "data": data})

    except Exception as e:
        logger.error(f"获取菜单列表失败: {str(e)}", exc_info=True)
        return jsonify({"code": 500, "msg": f"获取菜单列表失败: {str(e)}"}), 500

# 导出所有路由函数
__all__ = ['menu_app', 'get_menuList']






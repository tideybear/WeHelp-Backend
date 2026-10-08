# -*- coding: utf-8 -*-
"""应收/应付按工作空间（namespace）的列表与写入范围。"""
from flask import current_app

_table_columns_cache = {}


def get_table_columns(table_name):
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


def stamp_namespace_on_row(row_data, namespace_id, table_name):
    """创建/更新时写入 namespace_id（表存在该列时）。"""
    if namespace_id is None:
        return
    if 'namespace_id' in get_table_columns(table_name):
        row_data['namespace_id'] = int(namespace_id)


def namespace_list_filter_clause(view_name, project_subquery_sql, alias='s'):
    """
    列表/删除前校验用的空间范围。
    有 namespace_id 列：本空间记录 或 项目属于本空间；
    否则：项目属于本空间 或 未挂项目（兼容旧表结构）。
    """
    view_cols = get_table_columns(view_name)
    if 'namespace_id' in view_cols:
        return (
            f'({alias}.namespace_id = :namespace_id '
            f'OR {alias}.project_id IN ({project_subquery_sql}))'
        )
    return (
        f'({alias}.project_id IN ({project_subquery_sql}) '
        f'OR {alias}.project_id IS NULL)'
    )


def namespace_delete_where_sql(table_name, project_subquery_sql):
    """软删除 UPDATE 的额外 WHERE（表级）。"""
    cols = get_table_columns(table_name)
    if 'namespace_id' in cols:
        return (
            f' AND (namespace_id = :namespace_id '
            f'OR project_id IN ({project_subquery_sql}))'
        )
    return (
        f' AND (project_id IN ({project_subquery_sql}) '
        f'OR project_id IS NULL)'
    )

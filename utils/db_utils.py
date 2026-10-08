import logging
from typing import Dict, List

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

logger = logging.getLogger(__name__)
db = SQLAlchemy()


class DatabaseManager:
    def __init__(self, app=None):
        self.db = db
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.db.init_app(app)
        with app.app_context():
            try:
                self.db.engine.connect().close()
                logger.info('数据库连接成功')
            except Exception as e:
                logger.error('数据库连接失败: %s', e)
                raise

    def execute_query(self, sql: str, params: Dict = None) -> List[Dict]:
        if not sql or not isinstance(sql, str):
            raise ValueError('SQL 语句不能为空且必须是字符串类型')
        if params is not None and not isinstance(params, dict):
            raise ValueError('参数必须是字典类型')

        try:
            logger.debug('SQL: %s | params: %s', sql, params)
            result = self.db.session.execute(text(sql), params or {})
            columns = result.keys()
            if not columns:
                return []

            rows = [dict(zip(columns, row)) for row in result]
            logger.debug('查询完成，共 %s 条', len(rows))
            return rows
        except Exception as e:
            logger.error('查询执行失败: %s', e, exc_info=True)
            self.db.session.rollback()
            raise

    def execute_update(self, sql: str, params: Dict = None) -> int:
        try:
            result = self.db.session.execute(text(sql), params or {})
            self.db.session.commit()
            return result.rowcount
        except Exception as e:
            logger.error('更新执行失败: %s', e)
            self.db.session.rollback()
            raise

    def insert_one(self, table: str, data: Dict) -> int:
        try:
            columns = ', '.join(data.keys())
            placeholders = ', '.join([f':{k}' for k in data.keys()])
            sql = f'INSERT INTO {table} ({columns}) VALUES ({placeholders})'
            result = self.db.session.execute(text(sql), data)
            self.db.session.commit()
            return result.lastrowid
        except Exception as e:
            logger.error('插入数据失败: %s', e)
            self.db.session.rollback()
            raise

    def update_by_id(self, table: str, id: int, data: Dict) -> bool:
        try:
            set_clause = ', '.join([f'{k} = :{k}' for k in data.keys()])
            sql = f'UPDATE {table} SET {set_clause} WHERE id = :id'
            data['id'] = id
            result = self.db.session.execute(text(sql), data)
            self.db.session.commit()
            return result.rowcount > 0
        except Exception as e:
            logger.error('更新数据失败: %s', e)
            self.db.session.rollback()
            raise

    def insert_many(self, table: str, data_list: List[Dict]) -> List[int]:
        try:
            if not data_list:
                return []

            columns = ', '.join(data_list[0].keys())
            placeholders = ', '.join([f':{k}' for k in data_list[0].keys()])
            sql = f'INSERT INTO {table} ({columns}) VALUES ({placeholders})'

            last_id = None
            for item in data_list:
                result = self.db.session.execute(text(sql), item)
                last_id = result.lastrowid
            self.db.session.commit()
            if last_id is None:
                return []
            return [last_id - (len(data_list) - 1 - i) for i in range(len(data_list))]
        except Exception as e:
            logger.error('批量插入数据失败: %s', e)
            self.db.session.rollback()
            raise

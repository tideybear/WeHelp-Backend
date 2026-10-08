import logging
import os

from flask import Flask
from flask_cors import CORS

from config import Config
from services.fin import fin_app
from services.finance_payables import finance_payables_app
from services.finance_receivables import finance_receivables_app
from services.menu import menu_app
from services.namespace import namespace_app
from services.order import order_app
from services.project import project_app
from services.purchase import purchase_app
from services.sales import sales_app
from services.user import user_app
from services.view import view_app
from utils.db_utils import DatabaseManager


def create_app() -> Flask:
    """应用工厂：创建并配置 Flask 实例。"""
    log_level = logging.DEBUG if os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes') else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    )
    logger = logging.getLogger(__name__)

    app = Flask(__name__)
    app.config.from_object(Config)
    CORS(app)

    db_manager = DatabaseManager(app)
    app.db_manager = db_manager

    app.register_blueprint(menu_app)
    app.register_blueprint(user_app)
    app.register_blueprint(order_app)
    app.register_blueprint(namespace_app)
    app.register_blueprint(project_app)
    app.register_blueprint(purchase_app)
    app.register_blueprint(sales_app)
    app.register_blueprint(view_app)
    app.register_blueprint(fin_app)
    app.register_blueprint(finance_payables_app)
    app.register_blueprint(finance_receivables_app)

    with app.app_context():
        from services.fin import ensure_private_account_balance_triggers_dropped
        ensure_private_account_balance_triggers_dropped()

    logger.info('Flask app 初始化完成')
    return app


__all__ = ['create_app']

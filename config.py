# -*- coding: utf-8 -*-
import os
from pathlib import Path

from sqlalchemy.pool import QueuePool

# 自动加载 backend/.env（若存在）
_env_path = Path(__file__).resolve().parent / '.env'
if _env_path.is_file():
    for line in _env_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(key: str, default: str = '') -> str:
    return os.environ.get(key, default).strip()


class Config:
    """应用配置：生产环境请通过环境变量注入敏感信息。"""

    SECRET_KEY = _env('SECRET_KEY', 'dev-change-me-in-production')

    MYSQL_HOST = _env('MYSQL_HOST', '127.0.0.1')
    MYSQL_PORT = int(_env('MYSQL_PORT', '3306') or '3306')
    MYSQL_USER = _env('MYSQL_USER', 'root')
    MYSQL_PASSWORD = _env('MYSQL_PASSWORD', '')
    MYSQL_DB = _env('MYSQL_DB', 'hhp')

    SQLALCHEMY_DATABASE_URI = (
        f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}'
        f'@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'poolclass': QueuePool,
        'pool_size': 10,
        'max_overflow': 20,
        'pool_timeout': 30,
        'pool_recycle': 1800,
        'echo': _env('SQLALCHEMY_ECHO', 'false').lower() in ('1', 'true', 'yes'),
        'connect_args': {
            'charset': 'utf8mb4',
            'use_unicode': True,
        },
    }

    # 银行流水 CSV 上传目录（相对 backend 目录）
    UPLOAD_FOLDER = _env('UPLOAD_FOLDER', 'uploads')

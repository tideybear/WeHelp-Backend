# HHP 后端 API

Flask 服务，为财务、项目、订单等前端模块提供 REST API。

## 目录结构

```
backend/
├── app_factory.py      # 应用工厂、蓝图注册
├── main.py             # 开发启动入口
├── config.py           # 配置（读取 .env / 环境变量）
├── requirements.txt    # Python 依赖
├── services/           # 业务蓝图
│   ├── fin.py          # 财务（银行流水、私账、报销）
│   ├── project.py      # 项目与任务
│   ├── user.py         # 登录与用户
│   ├── menu.py         # 菜单
│   ├── namespace.py    # 空间
│   ├── order.py        # 订单
│   └── view.py         # 视图权限
├── utils/
│   └── db_utils.py     # 数据库访问封装
└── sql/                # 运维用 SQL（如私账 balance 触发器清理）
```

## 本地运行

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 编辑 .env 填入数据库等信息
python main.py
```

默认 `http://0.0.0.0:5001`。

## 生产部署（服务器）

### 一、准备服务器环境

1. 安装 Python 3.10+、pip、（可选）nginx。
2. 确保服务器能访问 MySQL（安全组放行数据库端口）。
3. 创建部署目录，例如 `/var/www/hhp/backend`。

### 二、上传代码

任选一种方式：

**方式 A：Git**

```bash
# 服务器上
cd /var/www/hhp
git clone <你的仓库地址> .
cd backend
```

**方式 B：rsync（本机执行）**

```bash
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.env' \
  ./backend/ user@你的服务器IP:/var/www/hhp/backend/
```

**方式 C：打包上传**

```bash
# 本机 backend 目录下
tar czvf hhp-backend.tar.gz \
  --exclude='.venv' --exclude='__pycache__' --exclude='.env' \
  app_factory.py main.py config.py requirements.txt services utils sql README.md .env.example
scp hhp-backend.tar.gz user@服务器:/var/www/hhp/
# 服务器解压
ssh user@服务器 'cd /var/www/hhp && tar xzvf hhp-backend.tar.gz'
```

### 三、安装依赖与配置

```bash
cd /var/www/hhp/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # 填写 MYSQL_*、SECRET_KEY、FLASK_DEBUG=false
mkdir -p uploads/bank_statements
```

### 四、用 Gunicorn 启动（推荐）

```bash
cd /var/www/hhp/backend
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)   # 或 systemd 里配置 EnvironmentFile

gunicorn -w 4 -b 127.0.0.1:5001 main:app --timeout 120 --access-logfile - --error-logfile -
```

### 五、systemd 守护（可选）

创建 `/etc/systemd/system/hhp-api.service`：

```ini
[Unit]
Description=HHP Flask API
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/hhp/backend
EnvironmentFile=/var/www/hhp/backend/.env
ExecStart=/var/www/hhp/backend/.venv/bin/gunicorn -w 4 -b 127.0.0.1:5001 main:app --timeout 120
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable hhp-api
sudo systemctl start hhp-api
sudo systemctl status hhp-api
```

### 六、Nginx 反向代理（可选）

```nginx
server {
    listen 80;
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 20m;
    }
}
```

### 七、前端对接

构建前端时将 API 地址改为服务器域名，例如 `template/src/utils/http.js` 中：

```js
baseURL: 'https://api.yourdomain.com'
```

或生产环境变量 `VITE_API_BASE_URL`（若项目已配置）。

### 八、部署后检查

```bash
curl http://127.0.0.1:5001/namespace/list?namespace_id=1
```

确认返回 JSON 且无 500 错误；再在前端登录、打开财务模块验证。

## 环境变量说明

| 变量 | 说明 |
|------|------|
| `SECRET_KEY` | Flask 密钥，生产务必修改 |
| `MYSQL_HOST` | MySQL 主机 |
| `MYSQL_PORT` | 端口，默认 3306 |
| `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB` | 数据库账号 |
| `PORT` | 开发 `python main.py` 监听端口 |
| `FLASK_DEBUG` | 开发 true，生产 false |
| `SQLALCHEMY_ECHO` | 是否打印 SQL |

## 已清理内容

- 删除 `tests/`（含误放的 node_modules）、`test.py`、`mock/`、错误命名的 `READDME.md`
- 数据库工具类去掉未使用的 CRUD 方法与冗余日志
- 配置改为 `.env` / 环境变量，避免密码写入代码仓库





scp -r  /Users/pzj/Documents/hhp/backend/* pzj@39.105.51.60:/home/pzj/backend/

# 2. SSH 到服务器
ssh pzj@39.105.51.60

# 3. 激活虚拟环境并安装新依赖
cd /home/pzj/backend
source .venv/bin/activate
pip install -r requirements.txt  # 如果有新依赖

deactivate

# 4. 重启后端服务
sudo systemctl restart backend
sudo systemctl status backend   # 确认启动成功
方案2：使用 nohup（临时方案）







deactivate 2>/dev/null

# 2. 创建 systemd 服务
sudo tee /etc/systemd/system/backend.service > /dev/null << 'EOF'
[Unit]
Description=Python Backend Service
After=network.target

[Service]
Type=simple
User=pzj
Group=pzj
WorkingDirectory=/home/pzj/backend
Environment="PATH=/home/pzj/backend/.venv/bin"
ExecStart=/home/pzj/backend/.venv/bin/gunicorn -b 0.0.0.0:5001 -w 4 main:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 3. 重新加载并启动
sudo systemctl daemon-reload
sudo lsof -t -i:5001 | xargs kill -9 2>/dev/null
sudo systemctl start backend
sudo systemctl enable backend

# 4. 验证
sleep 2
sudo systemctl status backend --no-pager
curl http://127.0.0.1:5001/namespace/list?namespace_id=1
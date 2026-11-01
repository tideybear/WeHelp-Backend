# WeHelp Backend

Flask 后端服务。Python 3.13 + Gunicorn + systemd。

- 服务器代码目录：`/home/pzj/WeHelp-Backend`
- 服务器虚拟环境：`WeHelp`
- 服务名：`wehelp-backend`，监听端口 `5001`
- 本机代码目录：`/Users/pzj/Documents/WeHelp-Backend`

### 端口说明

代码里有两处端口，**用途不同，互不冲突**：

| 场景 | 启动命令 | 端口来源 | 实际端口 |
|------|----------|----------|----------|
| 本地开发 | `python main.py` | `main.py` 读 `PORT` 环境变量 | 默认 **5002** |
| 服务器部署 | `gunicorn -b 0.0.0.0:5001 main:app` | 命令行 `-b` 参数 | **5001** |

原因：`main.py` 里读 `PORT` 的那段代码在 `if __name__ == '__main__'` 块中：

```python
if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5002'))
    app.run(debug=debug, host='0.0.0.0', port=port)
```

gunicorn 加载 `main:app` 时是 **import** 这个模块，`__name__` 等于 `'main'` 而不是 `'__main__'`，
所以 `app.run()` 根本不执行。gunicorn 只取 `main.py` 顶层导出的 `app` 对象，
监听哪个端口**完全由 `-b` 参数决定**，与 `main.py` 里的 `PORT` 无关。

结论：
- **服务器上固定用 5001**，改端口要改 systemd 单元文件里的 `-b 0.0.0.0:5001`
- `PORT` 环境变量只影响本地 `python main.py`，且它只对**开发模式**有意义
- `.env` 里的 `PORT=5001` 不会影响 gunicorn（gunicorn 用命令行 `-b`），留着无妨但别当成生效配置

---

## 一、首次部署

### 1. Mac 打包并上传代码到服务器

在 **Mac 终端**执行（先打包，再上传）：

```bash
cd /Users/pzj/Documents/WeHelp-Backend

# 打包（排除 .env、虚拟环境、缓存）
tar czvf /tmp/wehelp-backend.tar.gz \
  --exclude='.git' --exclude='.env' --exclude='WeHelp' --exclude='venv313' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='uploads' --exclude='.DS_Store' \
  app_factory.py main.py config.py requirements.txt requirements-extra.txt services utils sql README.md

# 上传
scp /tmp/wehelp-backend.tar.gz pzj@39.105.51.60:/home/pzj/
```

到服务器上解压（Mac 上继续执行）：

```bash
ssh pzj@39.105.51.60 'cd /home/pzj/WeHelp-Backend && tar xzvf ../wehelp-backend.tar.gz && rm ../wehelp-backend.tar.gz'
```

### 2. 服务器建虚拟环境、装依赖

```bash
ssh pzj@39.105.51.60
```

登录后在服务器上执行：

```bash
cd /home/pzj/WeHelp-Backend

python3.13 -m venv WeHelp
source WeHelp/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

> **⚠️ 重要：本服务器的系统 Python 3.13 不能用**
>
> 服务器是 Alibaba Cloud Linux 3，`/usr/local/bin/python3.13` 是源码编译的，**编译时漏了 zlib/openssl**，
> 导致 `_ssl` / `_zlib` / `_lzma` / `_ctypes` 模块全部缺失。表现是：
>
> ```
> Error: Command '['.../python3.13', '-m', 'ensurepip', '--upgrade', '--default-pip']' returned non-zero exit status 1.
> ModuleNotFoundError: No module named 'zlib'
> ```
>
> 就算绕过 ensurepip 建出虚拟环境，pip 也走不了 HTTPS（`SSL module is not available`），
> **而且运行时也无法发起 HTTPS 请求**，DeepSeek 等接口会全部挂掉。
>
> 解决办法：改用 uv 提供的独立版 Python 3.13（自带 SSL，装在用户目录，无需 root）：
>
> ```bash
> # 装 uv
> curl -LsSf https://astral.sh/uv/install.sh | sh
> export PATH=$HOME/.local/bin:$PATH
>
> # 装自带 SSL 的 Python 3.13
> uv python install 3.13
>
> # 用它建虚拟环境
> cd /home/pzj/WeHelp-Backend
> uv venv --python 3.13 WeHelp
> WeHelp/bin/python -m ensurepip --upgrade --default-pip
>
> # 验证 SSL 正常（关键！不报错才算修好）
> WeHelp/bin/python -c "import ssl, zlib, lzma, ctypes; print('ok')"
>
> # 装依赖（走阿里云镜像，服务器访问 pypi.org 很慢）
> WeHelp/bin/python -m pip install -r requirements.txt \
>   -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
>
> # 需要 OCR / 离线翻译时再装这组（体积大，服务本身不依赖）
> WeHelp/bin/python -m pip install -r requirements-extra.txt \
>   -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
> ```
>
> 验证 HTTPS 可用：
>
> ```bash
> WeHelp/bin/python -c "import urllib.request; urllib.request.urlopen('https://api.deepseek.com', timeout=15)"
> # 报 401 Authorization Required = TLS 握手成功，正常
> ```

### 3. 服务器写配置

```bash
cd /home/pzj/WeHelp-Backend
nano .env
```

内容：

```ini
SECRET_KEY=改成随机字符串
MYSQL_HOST=数据库地址
MYSQL_PORT=3306
MYSQL_USER=数据库账号
MYSQL_PASSWORD=数据库密码
MYSQL_DB=数据库名

FLASK_DEBUG=false
```

> 不需要写 `PORT`。服务器端口固定 5001，由 systemd 单元文件里 gunicorn 的 `-b` 参数决定；
> `.env` 里的 `PORT` 只对本地 `python main.py` 开发模式有效（默认 5002）。

```bash
chmod 600 .env
mkdir -p uploads/bank_statements
```

### 4. 服务器建 systemd 服务（挂在后台，开机自启）

```bash
sudo tee /etc/systemd/system/wehelp-backend.service > /dev/null << 'EOF'
[Unit]
Description=WeHelp Backend Flask API
After=network.target

[Service]
Type=simple
User=pzj
Group=pzj
WorkingDirectory=/home/pzj/WeHelp-Backend
Environment="PATH=/home/pzj/WeHelp-Backend/WeHelp/bin"
EnvironmentFile=/home/pzj/WeHelp-Backend/.env
ExecStart=/home/pzj/WeHelp-Backend/WeHelp/bin/gunicorn -b 0.0.0.0:5001 -w 4 main:app --timeout 300
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable wehelp-backend     # 开机自启
sudo systemctl start wehelp-backend      # 后台常驻运行
sudo systemctl status wehelp-backend --no-pager
```

`systemctl enable` + `start` 就是挂在后台：SSH 断开不影响服务，且开机自动拉起。

> **端口 5001 写在 `ExecStart` 的 `-b 0.0.0.0:5001` 里**，改端口就改这一行，
> 改完执行 `sudo systemctl daemon-reload && sudo systemctl restart wehelp-backend`。
> `.env` 里的 `PORT` 对 gunicorn 不生效（详见开头「端口说明」）。

**如果服务器上已有旧的 `backend` 服务**，先停掉它避免抢 5001 端口：

```bash
sudo systemctl stop backend
sudo systemctl disable backend
```

### 5. 验证

```bash
curl -i http://127.0.0.1:5001/namespace/list?namespace_id=1
```

返回 JSON 即成功。

---

## 二、代码更新后怎么部署

**Mac 上传：**

```bash
cd /Users/pzj/Documents/WeHelp-Backend

tar czvf /tmp/wehelp-backend.tar.gz \
  --exclude='.git' --exclude='.env' --exclude='WeHelp' --exclude='venv313' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='uploads' --exclude='.DS_Store' \
  app_factory.py main.py config.py requirements.txt requirements-extra.txt services utils sql README.md

scp /tmp/wehelp-backend.tar.gz pzj@39.105.51.60:/home/pzj/

ssh pzj@39.105.51.60 'cd /home/pzj/WeHelp-Backend && tar xzvf ../wehelp-backend.tar.gz && rm ../wehelp-backend.tar.gz'
```

**服务器上操作：**

```bash
ssh pzj@39.105.51.60
```

```bash
cd /home/pzj/WeHelp-Backend

# 依赖没改可以跳过这步
# 注意：用阿里云镜像，服务器访问 pypi.org 很慢
WeHelp/bin/python -m pip install -r requirements.txt \
  -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

sudo systemctl restart wehelp-backend
sudo systemctl status wehelp-backend --no-pager
```

完事。

---

## 三、常用命令

```bash
sudo systemctl restart wehelp-backend     # 重启
sudo systemctl stop wehelp-backend        # 停止
sudo systemctl start wehelp-backend       # 启动
sudo systemctl enable wehelp-backend      # 开机自启
sudo systemctl status wehelp-backend --no-pager   # 查看状态
sudo journalctl -u wehelp-backend -f      # 看实时日志
sudo journalctl -u wehelp-backend -n 100 --no-pager   # 看最近 100 行日志
```

---

## 四、注意事项

- **`.env` 和 `WeHelp` 不会被上传**，服务器上的配置和虚拟环境是独立的，只在服务器上维护。
- **`uploads/` 不上传**，避免本机测试文件覆盖服务器上的真实数据。
- **不要删服务器的 `WeHelp` 虚拟环境**，更新只需 `WeHelp/bin/python -m pip install -r requirements.txt`。
- **本机虚拟环境叫 `venv313`**，服务器叫 `WeHelp`，别混。本机自测：`cd /Users/pzj/Documents/WeHelp-Backend && source venv313/bin/activate && python main.py`。
- **装了依赖记得加阿里云镜像**，服务器访问 pypi.org 只有十几 KB/s。
- **改了 gunicorn 参数/端口**要 `sudo systemctl daemon-reload` 再 restart。端口就在 `ExecStart` 的 `-b 0.0.0.0:5001`。
- **服务名是 `wehelp-backend`**（不是 `backend`），改单元文件后同样要 `daemon-reload` 再 restart。
- **服务是后台常驻的**：`systemctl enable` 保证开机自启，SSH 断开不受影响。
- **端口别看错**：服务器 5001（gunicorn `-b` 参数），本地开发 5002（`main.py` 的 `PORT`），两套机制互不影响。

---

## 五、常见问题

| 现象 | 原因 / 处理 |
|------|------|
| `ensurepip ... returned non-zero exit status 1` | 系统 Python 3.13 缺 zlib，按第 2 节改用 uv 装独立版 Python |
| `SSL module is not available` | 同上，必须换成自带 SSL 的 Python |
| `unknown url type: https` | 同上，该 Python 运行时不支持 HTTPS，DeepSeek 等接口全挂 |
| `pip install` 卡住不动 | 服务器访问 pypi.org 太慢，加 `-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com` |
| `ModuleNotFoundError: paddleocr` | OCR 依赖在 `requirements-extra.txt` 里，服务不依赖，需要时单独装 |
| torch 装成了 CUDA 版（几个 GB） | 装 CPU 版：`pip install --extra-index-url https://download.pytorch.org/whl/cpu torch` |
| `Address already in use` | 旧服务还占着 5001：`sudo lsof -t -i:5001 \| xargs kill -9` |
| 改 `.env` 的 `PORT` 没生效 | 正常现象，服务器端口由 gunicorn `-b` 决定，见开头「端口说明」 |
| `Can't connect to MySQL server` | 检查 `.env` 的 `MYSQL_HOST`，以及服务器到数据库的连通性 |
| `Permission denied` 写 `uploads` | `chown -R pzj:pzj /home/pzj/WeHelp-Backend/uploads` |

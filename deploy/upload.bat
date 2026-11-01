@echo off
REM ============================================
REM MathAgent 文件上传到服务器 (Windows)
REM 使用方法：双击运行，按提示操作
REM ============================================

echo ===== MathAgent 上传工具 =====
echo.
echo 请输入你的服务器信息：
echo （在阿里云控制台 → 实例详情里可以找到）
echo.

set /p SERVER_IP=服务器公网IP:
set /p SERVER_USER=用户名（默认root）:

if "%SERVER_USER%"=="" set SERVER_USER=root

echo.
echo 正在上传文件到 %SERVER_USER%@%SERVER_IP%:/opt/mathagent/
echo.

REM 上传整个项目（排除不需要的文件）
echo [1/5] 创建发布目录...
ssh %SERVER_USER%@%SERVER_IP% "mkdir -p /opt/mathagent/deploy /opt/mathagent/frontend /opt/mathagent/uploads /opt/mathagent/output /opt/mathagent/data /opt/mathagent/学生档案 /opt/mathagent/批阅失败"
if errorlevel 1 goto :error

echo [2/5] 上传项目文件...
REM .env is intentionally excluded: server secrets must never be overwritten from local development.
scp -r D:\MathAgent\*.py D:\MathAgent\*.txt %SERVER_USER%@%SERVER_IP%:/opt/mathagent/
if errorlevel 1 goto :error
scp -r D:\MathAgent\routers %SERVER_USER%@%SERVER_IP%:/opt/mathagent/
if errorlevel 1 goto :error
scp -r D:\MathAgent\services %SERVER_USER%@%SERVER_IP%:/opt/mathagent/
if errorlevel 1 goto :error
scp -r D:\MathAgent\references %SERVER_USER%@%SERVER_IP%:/opt/mathagent/
if errorlevel 1 goto :error
scp -r D:\MathAgent\templates %SERVER_USER%@%SERVER_IP%:/opt/mathagent/ 2>nul
if errorlevel 1 goto :error

REM 当前前端是 Vue/Vite，不再上传已废弃的 static/ 目录。
if not exist D:\MathAgent\frontend\dist\index.html (
    echo 未找到 frontend\dist。请先在 D:\MathAgent\frontend 执行 npm run build。
    goto :error
)
scp -r D:\MathAgent\frontend\dist %SERVER_USER%@%SERVER_IP%:/opt/mathagent/frontend/
if errorlevel 1 goto :error

REM 部署配置随代码上传，但运行时唯一权威副本会被下一步安装到 /etc/mathagent。
scp D:\MathAgent\deploy\gunicorn.conf.py D:\MathAgent\deploy\mathagent.service D:\MathAgent\deploy\setup.sh %SERVER_USER%@%SERVER_IP%:/opt/mathagent/deploy/
if errorlevel 1 goto :error

echo [3/5] 安装唯一 Gunicorn 配置和 systemd 服务...
ssh %SERVER_USER%@%SERVER_IP% "sudo install -d -m 0755 /etc/mathagent && sudo install -m 0644 /opt/mathagent/deploy/gunicorn.conf.py /etc/mathagent/gunicorn.conf.py && sudo install -m 0644 /opt/mathagent/deploy/mathagent.service /etc/systemd/system/mathagent.service && sudo rm -f /opt/mathagent/gunicorn.conf.py && sudo systemctl daemon-reload"
if errorlevel 1 goto :error

echo [4/5] 安装依赖...
ssh %SERVER_USER%@%SERVER_IP% "cd /opt/mathagent && /opt/mathagent/venv/bin/python -m pip install -r requirements.txt"
if errorlevel 1 goto :error

echo [5/5] Redis + Gunicorn 预检、重启与健康检查...
ssh %SERVER_USER%@%SERVER_IP% "redis-cli -h 127.0.0.1 ping | grep -qx PONG && sudo systemd-analyze verify /etc/systemd/system/mathagent.service && cd /opt/mathagent && sudo -u www-data /opt/mathagent/venv/bin/gunicorn --check-config -c /etc/mathagent/gunicorn.conf.py app:app && sudo systemctl restart mathagent && sleep 3 && curl -fsS http://127.0.0.1:8080/api/health"
if errorlevel 1 goto :error

echo.
echo ===== 上传完成！=====
echo 已完成 Redis、Gunicorn 配置和健康检查。
echo.
pause
exit /b 0

:error
echo.
echo 上传或部署预检失败；服务未被宣称为已更新。请检查上方错误输出。
pause
exit /b 1

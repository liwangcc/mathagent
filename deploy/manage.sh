#!/bin/bash
# ============================================
# MathAgent 管理工具
# SSH登录后执行: bash manage.sh
# ============================================

cd /opt/mathagent

echo "===== MathAgent 管理工具 ====="
echo ""
echo "1) 查看运行状态"
echo "2) 启动服务"
echo "3) 停止服务"
echo "4) 重启服务"
echo "5) 查看最近日志"
echo "6) 查看实时日志（按Ctrl+C退出）"
echo "7) 测试服务是否正常"
echo "0) 退出"
echo ""

read -p "请选择操作 [0-7]: " choice

case $choice in
    1)
        echo ""
        sudo systemctl status mathagent
        ;;
    2)
        echo "启动中..."
        sudo systemctl start mathagent
        sleep 2
        sudo systemctl status mathagent
        ;;
    3)
        echo "停止中..."
        sudo systemctl stop mathagent
        echo "已停止"
        ;;
    4)
        echo "重启中..."
        sudo systemctl restart mathagent
        sleep 2
        sudo systemctl status mathagent
        ;;
    5)
        echo "最近30行日志："
        echo "---"
        sudo journalctl -u mathagent --no-pager -n 30
        ;;
    6)
        echo "实时日志（按 Ctrl+C 退出）："
        echo "---"
        sudo journalctl -u mathagent -f
        ;;
    7)
        echo "测试服务..."
        curl -s http://127.0.0.1:8080/api/grade/results | head -c 100
        echo ""
        if [ $? -eq 0 ]; then
            echo "✅ 服务正常运行"
        else
            echo "❌ 服务未响应"
        fi
        ;;
    0)
        echo "退出"
        ;;
    *)
        echo "无效选择"
        ;;
esac

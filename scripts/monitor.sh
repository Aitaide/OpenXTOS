#!/bin/bash
# XTOS项目状态监控脚本
# 自动检查各子Agent的工作进度

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="/root/.hermes/cache/delegation/live"

echo "=========================================="
echo "XTOS 项目状态监控"
echo "=========================================="
echo ""

# 检查项目目录
echo "📁 项目目录: $PROJECT_ROOT"
echo ""

# 检查文档状态
echo "📄 已保存文档:"
find "$PROJECT_ROOT/docs" -name "*.md" -exec ls -lh {} \; | awk '{print "  - " $9 " (" $5 ")"}'
echo ""

find "$PROJECT_ROOT/plans" -name "*.md" -exec ls -lh {} \; | awk '{print "  - " $9 " (" $5 ")"}'
echo ""

# 检查子Agent状态
echo "🤖 子Agent状态:"
if [ -d "$LOG_DIR" ]; then
    log_found=0
    for deleg_dir in "$LOG_DIR"/*/ ; do
        if [ -d "$deleg_dir" ]; then
            for log in "$deleg_dir"task-*.log ; do
                if [ -f "$log" ]; then
                    log_found=1
                    echo "  - $(basename "$log")"
                    lines=$(wc -l < "$log")
                    size=$(du -h "$log" | cut -f1)
                    echo "    日志行数: $lines, 大小: $size"
                    echo "    最后10行:"
                    tail -n 10 "$log" | sed 's/^/      /'
                    echo ""
                fi
            done
        fi
    done
    if [ $log_found -eq 0 ]; then
        echo "  ⏳ 等待子Agent启动..."
    fi
else
    echo "  ⏳ 等待子Agent启动..."
fi
echo ""

# 检查内核目录
if [ -d "$PROJECT_ROOT/kernel" ]; then
    echo "🐧 内核状态:"
    if [ -d "$PROJECT_ROOT/kernel/linux" ]; then
        echo "  ✓ Linux内核源码已下载"
        if [ -f "$PROJECT_ROOT/kernel/linux/arch/x86/boot/bzImage" ]; then
            echo "  ✓ x86_64内核已编译"
        else
            echo "  ⏳ x86_64内核编译中..."
        fi
    else
        echo "  ⏳ 等待内核下载..."
    fi
    echo ""
else
    echo "⏳ 等待目录创建..."
    echo ""
fi

# 系统资源
echo "💻 系统资源:"
echo "  CPU: $(nproc) 核"
echo "  内存: $(free -h | awk 'NR==2{print $2 " (可用: " $7 ")"}')"
echo "  磁盘: $(df -h "$PROJECT_ROOT" | awk 'NR==2{print $2 " (可用: " $4 ")"}')"
echo ""

# 开发进度
echo "📊 开发进度:"
echo "  阶段1: 基础设施 (M1-M3)"
echo "    └─ M1 Week1: 环境搭建 🔄 进行中"
echo "    └─ M1 Week2: 内核编译 ⏳ 待开始"
echo "    └─ M1 Week3-4: 调度器原型 ⏳ 待开始"
echo ""

echo "=========================================="
echo "使用以下命令实时监控子Agent:"
echo "  tail -f $LOG_DIR/*/task-*.log"
echo "=========================================="

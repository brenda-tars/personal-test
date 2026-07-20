#!/usr/bin/env bash
set -e

ARCHIVE_NAME="tarsai_v2.6.0_thor_20260715_013550_personal.install.tar"
PAYLOAD_MARKER="__APOLLO_INSTALL_PAYLOAD_BELOW__"
PAYLOAD_LINE=$(awk "/^${PAYLOAD_MARKER}$/ {print NR + 1; exit 0;}" "$0")

if [ -z "$PAYLOAD_LINE" ]; then
    echo "错误: 安装包 payload 不存在"
    exit 1
fi

KILL_ALL_NODE_SCRIPT="/apollo/scripts/humanoid/kill_all_nodes.sh"
if [ -f "$KILL_ALL_NODE_SCRIPT" ]; then
    echo "检测到节点清理脚本，开始清理已有节点..."
    if ! bash "$KILL_ALL_NODE_SCRIPT" >/dev/null 2>&1; then
        echo "警告: 节点清理脚本执行失败，继续安装"
    fi
fi

TMP_PARENT="/mnt/gaea/package"
mkdir -p "$TMP_PARENT"
TMP_DIR=$(mktemp -d -p "$TMP_PARENT" ".${ARCHIVE_NAME}.payload.XXXXXX")
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "准备释放安装 payload 到: $TMP_DIR"
tail -n +"$PAYLOAD_LINE" "$0" | tar --warning=no-timestamp -xf - -C "$TMP_DIR"
echo "安装 payload 释放完成"

bash $TMP_DIR/check_firmware.sh 
EXIT_CODE=$?
echo "check_firmware.sh 退出码: $EXIT_CODE"

if [ $EXIT_CODE -ne 0 ]; then
    echo "检测失败，退出主脚本"
    exit $EXIT_CODE
fi

chmod +x "$TMP_DIR/decompression.sh"
echo "启动解压脚本..."
bash "$TMP_DIR/decompression.sh" "$TMP_DIR/$ARCHIVE_NAME"
exit $?


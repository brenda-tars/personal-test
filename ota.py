#!/usr/bin/env python3
"""Thor OTA 单文件编排器（单文件内联 tars_flash + PMU_OTA + rh850_udp_ota）。

三个工具内联进一个 .py：
  - tars_flash (bash)        SoC A/B 分区恢复，原样嵌入为 TARS_FLASH_SH，
                             已自带 --no-reboot（切换 slot 但跳过 reboot）。
  - PMU_OTA (python)         PMU CAN-over-UDP OTA（入口 pmu_main）。
  - rh850_udp_ota (python)   RH850 ZYT Link V1 OTA（入口 rh850_main）。

==================== 参数怎么传：两层 argparse + 透明 argv 转发 ====================

  你敲的命令 ──► ota.py 顶层 argparse（只认下表这些）
                     │  把 --*-args 字符串 shlex.split 成 argv 列表
                     ▼
               各工具自己的 argparse（pmu_main / rh850_main 内 parse_args(argv)）
               或 soc 的 bash（直接当命令行参数传给 tars_flash）

顶层 argparse 只声明这几样，其余参数一律走 passthrough 给工具自己：

  类别         参数                                          作用
  -----------  --------------------------------------------  ----------------------------
  选择器       --soc / --pmu / --mcu                         选谁；一个都没给→三个全跑
  全局糖       --dry-run                                      ota.py 自用：只打印不执行
               --verbose                                      ota.py 自用：soc 流式打印 bash 输出
               --no-reboot                                    注入 --no-reboot 到 soc+mcu
  前缀糖       --mcu-verbose                                  注入 --verbose 到 mcu
               --soc-verbose / --pmu-verbose                  目标工具无 --verbose，告警忽略
  passthrough  --soc-args / --pmu-args / --mcu-args          引号字符串，shlex 解析后原样给工具

旧指令（command.sh 三条）:
  sudo bash bin/tars_flash -r ./data_dir --no-reboot
  python3 bin/PMU_OTA.py -f data_dir/zephyr.signed.bin
  sudo python3 bin/rh850_udp_ota.py --iface lan0 --timeout 15 --data-timeout 10 --verbose --no-reboot

新指令（单工具一键；soc 默认会重启，加 --no-reboot 跳过）:
  sudo python3 ota.py --soc --soc-args "-r ./data_dir --no-reboot"
  python3 ota.py --pmu --pmu-args "-f PMU/data_dir/zephyr.signed.bin"
  python3 ota.py --pmu --pmu-args --apply
  python3 ota.py --mcu --mcu-args "--iface lan0 --timeout 15 --data-timeout 10 --verbose --no-reboot"

三种参数的三种归宿：
  - passthrough（-r / --apply / --iface …）：原样进工具 argv → 工具自己的 argparse 解析。
    ota.py 不碰语义，工具将来加任何新参数都自动透传（这就是“零漂移”）。
  - 全局糖（--no-reboot）：ota.py 识别后，往 soc 与 mcu 的 argv 追加字面 --no-reboot。
  - 前缀糖（--mcu-verbose）：同上，往 mcu 的 argv 追加字面 --verbose。
    --soc-verbose / --pmu-verbose 因目标工具无此旗标，告警后丢弃（避免给 PMU 塞非法 --verbose）。

透传值以 - 开头（如 --apply / -j / -h）时，空格形式 --pmu-args --apply 与等号形式
--pmu-args=--apply 均可：ota.py 在解析前会把 --*-args 后的裸值改成等号形式，绕开 argparse
“expected one argument” 的判定。含空格的多参数引号串（--pmu-args "-f fw.bin"）照常可用。

PMU 非交互触发（互斥）：-f <bin>（刷写固件，传完自动 apply 重启）、--apply（仅发 APPLY_OTA
等 0x200 ACK 退出）、-j（读固件版本）。

示例（soc 不重启；PMU 用 -f 刷写固件）:
  sudo python3 ota.py --no-reboot \\
      --soc-args "-r ./data_dir" \\
      --pmu-args "-f data_dir/zephyr.signed.bin" \\
      --mcu-args "--iface lan0 --timeout 15 --data-timeout 10" --mcu-verbose

  最终各工具收到：
    soc: bash <内联 tars_flash> -r ./data_dir --no-reboot
    pmu: pmu_main(['-f', 'data_dir/zephyr.signed.bin'])
    mcu: rh850_main(['--iface','lan0','--timeout','15','--data-timeout','10','--no-reboot','--verbose'])

生成说明：本文件由 build_ota.py 生成；改它后 `python3 build_ota.py` 重建
（三源文件删除后则直接改本文件）。PMU 经本工具支持 -f / --apply / -j 非交互，交互模式被拒绝。
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Optional, Tuple


class _Fore:
    RED = GREEN = YELLOW = CYAN = WHITE = RESET = ""


try:
    from colorama import Fore as _CF, Style as _CS, init as _cinit  # type: ignore
    _cinit(autoreset=True)
    _Fore.RED = _CF.RED
    _Fore.GREEN = _CF.GREEN
    _Fore.YELLOW = _CF.YELLOW
    _Fore.CYAN = _CF.CYAN
    _Fore.WHITE = _CF.WHITE
    _Fore.RESET = _CS.RESET_ALL
except Exception:
    pass

# ================================================================
# tars_flash (bash, 内联; 已注入 --no-reboot)

# ================================================================

TARS_FLASH_SH = r'''#!/bin/bash
# =============================================================================
# DriveOS A/B 分区备份/恢复工具
#
# 用法: flash.sh -b <path>   备份当前分区到指定路径
#       flash.sh -r <path>   从指定路径恢复到非活动分区
#
# 分区布局:
#   vblkdev80p9 = Slot A
#   vblkdev80p10 = Slot B
#
# 特性:
#   - 自动识别当前活动/非活动分区
#   - MD5 校验确保数据完整性
#   - pigz 并行压缩/解压加速
#   - 恢复后自动切换 slot 并重启（可用 --no-reboot 跳过重启）
# =============================================================================
set -euo pipefail

# --------------- 配置 ---------------
PART_A="/dev/vblkdev80p9"
PART_B="/dev/vblkdev80p10"
SLOT_A_MARKER="res_a"
SLOT_B_MARKER="res_b"

# 恢复后是否跳过重启（由 --no-reboot 置位）
NO_REBOOT=false

# 备份活动分区时被 fsfreeze 冻结的挂载点（供 trap 兜底解冻使用，空=未冻结）
FROZEN_MP=""

# bsp_param 分区（vblkdev73，16 MiB raw partition，无文件系统）
# 备份时与 res 一并导出，文件名形如 <res>.bsp_param.img.gz
# 恢复时默认 *不* 写回 —— 该分区由 BSP 流程维护，复写需显式 --with-bsp-param
BSP_PARAM_PART="/dev/vblkdev73"
BSP_PARAM_SUFFIX=".bsp_param.img.gz"

# --------------- 颜色输出 ---------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# --------------- 文件系统冻结（避免活动分区 dd 出撕裂快照）---------------
# 活动分区在 dd 期间仍被读写，逐块拷贝会得到数据/日志不一致的“撕裂快照”，
# 导致备份镜像 journal 校验失败、无法可写挂载。dd 前 fsfreeze -f 刷盘并挂起
# 写入，dd 后 -u 解冻，即可得到一致镜像。解冻由 trap 兜底，避免异常退出后
# 分区永久冻结导致系统卡死。
#
# 策略（严格模式）：源分区已挂载时，冻结是强制的——冻结不可用或被拒绝即返回
# 非 0，由调用方中止备份并回滚，绝不“降级继续”导致撕裂镜像。
# 返回码: 0=已冻结  1=参数为空  2=fsfreeze 不可用  3=冻结被拒绝
freeze_fs() {
    local mp="$1"
    [ -z "$mp" ] && return 1
    command -v fsfreeze &>/dev/null || return 2
    sync
    if fsfreeze -f "$mp" 2>/dev/null; then
        FROZEN_MP="$mp"
        log_info "  已冻结源文件系统: $mp"
        return 0
    fi
    return 3
}

unfreeze_fs() {
    [ -z "$FROZEN_MP" ] && return 0
    if fsfreeze -u "$FROZEN_MP" 2>/dev/null; then
        log_info "  已解冻源文件系统: $FROZEN_MP"
    else
        log_warn "  解冻 $FROZEN_MP 失败，请手动执行: fsfreeze -u $FROZEN_MP"
    fi
    FROZEN_MP=""
}

# 任何路径退出（含 set -e 中断、Ctrl-C、kill）都必须解冻，否则源分区永久挂起
trap 'unfreeze_fs' EXIT INT TERM

# --------------- 工具检测 ---------------
detect_tools() {
    log_info "检测可用工具..."
    
    # 优先使用 /mnt/ro 中的工具，否则使用系统工具
    if [ -x "/mnt/ro/usr/bin/pigz" ]; then
        PIGZ="/mnt/ro/usr/bin/pigz"
    elif command -v pigz &>/dev/null; then
        PIGZ="pigz"
    else
        PIGZ=""
        log_warn "pigz 不可用，将使用 gzip（较慢）"
    fi
    
    if [ -x "/mnt/ro/usr/bin/dd" ]; then
        DD="/mnt/ro/usr/bin/dd"
    else
        DD="dd"
    fi
    
    if [ -x "/mnt/ro/usr/bin/md5sum" ]; then
        MD5SUM="/mnt/ro/usr/bin/md5sum"
    else
        MD5SUM="md5sum"
    fi

    if [ -x "/mnt/ro/usr/sbin/zerofree" ]; then
        ZEROFREE="/mnt/ro/usr/sbin/zerofree"
    elif command -v zerofree &>/dev/null; then
        ZEROFREE="zerofree"
    else
        ZEROFREE=""
    fi
    
    # 检测 pv（进度显示，可选）
    if [ -x "/mnt/ro/usr/bin/pv" ]; then
        PV="/mnt/ro/usr/bin/pv"
    elif command -v pv &>/dev/null; then
        PV="pv"
    else
        PV=""
    fi
    
    log_info "  DD: $DD"
    log_info "  MD5SUM: $MD5SUM"
    log_info "  PIGZ: ${PIGZ:-gzip (fallback)}"
    log_info "  PV: ${PV:-不可用}"
    log_info "  ZEROFREE: ${ZEROFREE:-不可用}"
}

check_mount_with_marker() {
    local part="$1"
    local marker="$2"
    mount | grep -q "^${part} on .*${marker}"
}

# --------------- 分区识别 ---------------
detect_current_slot() {
    log_info "检测当前活动分区..."
    NO_ACTIVE_SLOT=false
    
    if [ ! -b "$PART_A" ]; then
        log_error "未找到 Slot A 分区: $PART_A"
        exit 1
    fi
    if [ ! -b "$PART_B" ]; then
        log_error "未找到 Slot B 分区: $PART_B"
        exit 1
    fi
    
    # 方法1: 检查 mount 中的 res_a/res_b
    if check_mount_with_marker "$PART_B" "$SLOT_B_MARKER"; then
        CURRENT_SLOT="B"
        CURRENT_PART="$PART_B"
        INACTIVE_SLOT="A"
        INACTIVE_PART="$PART_A"
    elif check_mount_with_marker "$PART_A" "$SLOT_A_MARKER"; then
        CURRENT_SLOT="A"
        CURRENT_PART="$PART_A"
        INACTIVE_SLOT="B"
        INACTIVE_PART="$PART_B"
    else
        # 方法2: 从 hostname/PS1 提取 SLOT 信息
        if [[ "$(hostname)" == *"SLOT_B"* ]] || [[ "${PS1:-}" == *"SLOT_B"* ]]; then
            CURRENT_SLOT="B"
            CURRENT_PART="$PART_B"
            INACTIVE_SLOT="A"
            INACTIVE_PART="$PART_A"
        elif [[ "$(hostname)" == *"SLOT_A"* ]] || [[ "${PS1:-}" == *"SLOT_A"* ]]; then
            CURRENT_SLOT="A"
            CURRENT_PART="$PART_A"
            INACTIVE_SLOT="B"
            INACTIVE_PART="$PART_B"
        else
            # 方法3: 检查哪个分区被挂载
            if mount | grep -q "$PART_B"; then
                CURRENT_SLOT="B"
                CURRENT_PART="$PART_B"
                INACTIVE_SLOT="A"
                INACTIVE_PART="$PART_A"
            elif mount | grep -q "$PART_A"; then
                CURRENT_SLOT="A"
                CURRENT_PART="$PART_A"
                INACTIVE_SLOT="B"
                INACTIVE_PART="$PART_B"
            else
                # 未检测到活动分区，说明 rw overlay 未生效
                # 直接以 Slot A 作为恢复目标
                NO_ACTIVE_SLOT=true
                CURRENT_SLOT=""
                CURRENT_PART=""
                INACTIVE_SLOT="A"
                INACTIVE_PART="$PART_A"
                log_warn "未检测到活动分区 (rw overlay 可能未生效)"
                log_warn "将直接对 Slot A ($PART_A) 执行恢复"
            fi
        fi
    fi
    
    # 获取分区大小（备份时使用 CURRENT_PART，恢复时使用 INACTIVE_PART）
    local size_part="${CURRENT_PART:-$INACTIVE_PART}"
    PART_SIZE=$(blockdev --getsize64 "$size_part")
    PART_SIZE_GB=$(awk "BEGIN {printf \"%.2f\", $PART_SIZE / 1024 / 1024 / 1024}")
    
    if [ "${NO_ACTIVE_SLOT:-false}" = true ]; then
        log_warn "活动分区: 未检测到"
        log_ok "恢复目标:   Slot $INACTIVE_SLOT ($INACTIVE_PART)"
    else
        log_ok "当前活动分区: Slot $CURRENT_SLOT ($CURRENT_PART)"
        log_ok "非活动分区:   Slot $INACTIVE_SLOT ($INACTIVE_PART)"
    fi
    log_info "分区大小: ${PART_SIZE_GB}GB ($PART_SIZE bytes)"
}

# --------------- 检查分区是否挂载 ---------------
check_partition_mounted() {
    local part="$1"
    if mount | grep -q "^$part "; then
        return 0  # mounted
    else
        return 1  # not mounted
    fi
}

# --------------- 备份文件名前缀 ---------------
# 取自 /etc/tars_fw_version 第一行（固件版本，如 thor_v5.2b）作为备份/恢复文件名前缀。
# 清洗：去掉首尾空白、并把不适合做文件名的字符（空格、/ 等）替换为 _。
# 文件缺失/为空时回退到 overlay 前缀并告警，避免备份因取不到版本号而中止。
FW_VERSION_FILE="/etc/tars_fw_version"
backup_prefix() {
    local prefix=""
    if [ -r "$FW_VERSION_FILE" ]; then
        prefix=$(head -n1 "$FW_VERSION_FILE" 2>/dev/null \
                 | tr -d '\r' \
                 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
                 | sed 's#[/[:space:]]\+#_#g')
    fi
    if [ -z "$prefix" ]; then
        log_warn "无法从 $FW_VERSION_FILE 读取版本号，回退使用前缀 overlay" >&2
        prefix="overlay"
    fi
    printf '%s' "$prefix"
}

# --------------- 备份操作 ---------------
do_backup() {
    if [ "${NO_ACTIVE_SLOT:-false}" = true ]; then
        log_error "未检测到活动分区，无法执行备份"
        exit 1
    fi

    local backup_path="$1"
    local backup_dir
    local backup_file
    local md5_file
    
    # 处理路径
    local fw_prefix
    fw_prefix="$(backup_prefix)"
    if [[ "$backup_path" == */ ]]; then
        backup_dir="$backup_path"
        backup_file="${backup_dir}${fw_prefix}_$(date +%Y%m%d_%H%M%S).img.gz"
    elif [[ "$backup_path" == *.img.gz ]]; then
        backup_file="$backup_path"
        backup_dir="$(dirname "$backup_path")"
    elif [[ "$backup_path" == *.img ]]; then
        backup_file="${backup_path}.gz"
        backup_dir="$(dirname "$backup_path")"
    else
        backup_dir="$backup_path"
        mkdir -p "$backup_dir"
        backup_file="${backup_dir}/${fw_prefix}_$(date +%Y%m%d_%H%M%S).img.gz"
    fi
    
    md5_file="${backup_file}.md5"
    
    # 确保目录存在
    mkdir -p "$backup_dir"
    
    local img_file="${backup_file%.gz}"
    
    log_info "=========================================="
    log_info "备份当前活动分区 -> $backup_file"
    log_info "=========================================="
    
    # 检查目标空间 (需要容纳原始镜像 + 压缩后文件)
    local avail_space
    avail_space=$(df -B1 "$backup_dir" | tail -1 | awk '{print $4}')
    log_info "目标可用空间: $(awk "BEGIN {printf \"%.2f\", $avail_space / 1024 / 1024 / 1024}")GB"
    
    local start_time
    start_time=$(date +%s)
    
    # Step 1: dd 导出分区为原始镜像
    log_info "[1/4] dd 导出分区 (分区: $CURRENT_PART, 大小: ${PART_SIZE_GB}GB)..."

    # 活动分区正被读写，dd 前必须冻结其文件系统以获得一致快照，避免撕裂。
    # 输出目录在另一分区(p1)，与被冻结分区不同源，故不会自我死锁。
    # 严格模式：源分区已挂载却无法冻结 -> 中止并回滚，绝不产出可能撕裂的镜像。
    local src_mp=""
    src_mp=$(findmnt -n -o TARGET --source "$CURRENT_PART" 2>/dev/null | head -1 || true)
    if [ -n "$src_mp" ]; then
        log_info "  冻结源文件系统以保证一致快照: $src_mp"
        freeze_fs "$src_mp"
        local frc=$?
        if [ "$frc" -ne 0 ]; then
            case "$frc" in
                2) log_error "fsfreeze 不可用，无法保证一致快照，中止备份" ;;
                3) log_error "冻结被拒绝（$src_mp 可能已冻结或不支持），中止备份" ;;
                *) log_error "冻结源文件系统失败 (code=$frc)，中止备份" ;;
            esac
            rm -f "$img_file"   # 回滚：清除可能的残留输出
            exit 1
        fi
        # 冻结已 sync 脏页落盘、磁盘成为权威且此后无新写入。此时丢弃干净缓存页
        # （含块设备 buffer cache 里残留的陈旧块），强制下面的 dd 从磁盘重新读，
        # 避免读到块设备缓存中某块的旧身份（如已被复用为 .so 的旧目录块）。
        # drop_caches 只丢“干净”页、不写盘，必须放在 freeze(已flush) 之后才有效。
        if [ -w /proc/sys/vm/drop_caches ]; then
            sync   # 冗余保险：确保无遗留脏页后再丢缓存
            echo 1 > /proc/sys/vm/drop_caches 2>/dev/null \
                && log_info "  已丢弃干净缓存页 (drop_caches=1)，dd 将从磁盘直读" \
                || log_warn "  drop_caches 写入失败，依赖 dd iflag=direct 兜底"
        else
            log_warn "  /proc/sys/vm/drop_caches 不可写，依赖 dd iflag=direct 兜底"
        fi
    else
        log_info "  源分区未挂载（无写入者），镜像天然一致，直接 dd"
    fi

    # iflag=direct: dd 以 O_DIRECT 绕过块设备 buffer cache 直读磁盘，从根上消除
    # “块设备缓存 vs 文件系统 page cache 对同一物理块不一致”导致的内容错位。
    if [ -n "$PV" ]; then
        $DD if="$CURRENT_PART" bs=4M iflag=direct status=none | $PV -s "$PART_SIZE" > "$img_file"
    else
        $DD if="$CURRENT_PART" bs=4M iflag=direct status=progress of="$img_file"
    fi

    unfreeze_fs
    
    # Step 2: 挂载并清理临时文件
    log_info "[2/4] 挂载并清理镜像中的临时文件..."
    # 严格模式：冻结后镜像本应一致，可写挂载应当成功。挂载失败说明镜像异常
    # （撕裂/损坏），视为备份失败并回滚——不再 e2fsck 修复后重试。
    if ! cleanup_raw_image "$img_file"; then
        log_error "镜像挂载/清理失败（冻结后镜像本应一致），备份中止"
        rm -f "$img_file"   # 回滚：删除不可信的原始镜像
        exit 1
    fi
    
    # Step 3: 压缩
    log_info "[3/4] 压缩镜像..."
    if [ -n "$PIGZ" ]; then
        $PIGZ "$img_file"
    else
        gzip "$img_file"
    fi
    
    # Step 4: 计算 MD5
    log_info "[4/4] 生成 MD5 校验值..."
    $MD5SUM "$backup_file" > "$md5_file"
    
    local end_time
    end_time=$(date +%s)
    local duration=$((end_time - start_time))
    
    # 显示结果
    local backup_size
    backup_size=$(ls -lh "$backup_file" | awk '{print $5}')
    local md5_value
    md5_value=$(cat "$md5_file" | awk '{print $1}')
    
    log_ok "=========================================="
    log_ok "备份完成!"
    log_ok "  文件: $backup_file"
    log_ok "  大小: $backup_size"
    log_ok "  MD5:  $md5_value"
    log_ok "  耗时: ${duration}秒"
    log_ok "=========================================="

    # bsp_param (vblkdev73) — 与 res 一并备份，作为伴生文件
    backup_bsp_param "$backup_file"
}

# --------------- 备份 bsp_param (vblkdev73) ---------------
# 与 res 备份伴生：文件命名为 <res>.bsp_param.img.gz
# 失败不致命 —— 主备份已完成，bsp_param 可后续单独处理
backup_bsp_param() {
    local res_file="$1"
    local bp_base="${res_file%.img.gz}"
    local bp_file="${bp_base}${BSP_PARAM_SUFFIX}"
    local bp_md5="${bp_file}.md5"
    local bp_raw="${bp_file%.gz}"

    log_info "------ 备份 bsp_param ($BSP_PARAM_PART) ------"
    if [ ! -b "$BSP_PARAM_PART" ]; then
        log_warn "未找到 $BSP_PARAM_PART，跳过 bsp_param 备份"
        return 0
    fi

    sync
    if ! $DD if="$BSP_PARAM_PART" bs=4M status=none of="$bp_raw" 2>/dev/null; then
        log_warn "bsp_param 导出失败，跳过"
        rm -f "$bp_raw"
        return 0
    fi

    if [ -n "$PIGZ" ]; then
        $PIGZ "$bp_raw"
    else
        gzip "$bp_raw"
    fi
    $MD5SUM "$bp_file" > "$bp_md5"

    local bp_size bp_hash
    bp_size=$(ls -lh "$bp_file" | awk '{print $5}')
    bp_hash=$(awk '{print $1}' "$bp_md5")
    log_ok "  bsp_param: $bp_file ($bp_size, md5 ${bp_hash:0:12}...)"
}

# --------------- 清理原始镜像 ---------------
cleanup_raw_image() {
    local img_file="$1"
    local mount_point
    mkdir -p /mnt/gaea/tmp
    mount_point=$(mktemp -d /mnt/gaea/tmp/flash_cleanup.XXXXXX)

    # 检测文件系统类型
    local fstype=""
    if command -v blkid &>/dev/null; then
        fstype=$(blkid -o value -s TYPE "$img_file" 2>/dev/null || true)
        log_info "  检测到文件系统类型: ${fstype:-未知}"
    fi
    if [ -z "$fstype" ] && command -v file &>/dev/null; then
        local file_info
        file_info=$(file -s "$img_file" 2>/dev/null || true)
        log_info "  文件类型: $file_info"
    fi

    # 冻结后 dd 出来的镜像本应一致（journal 干净）。此处只做只读 sanity check，
    # 绝不 e2fsck 自动修复：-fy 在不一致 image 上会 clear 看似悬挂的 inode，让备份
    # 里的文件凭空消失。若镜像真有问题，交由下面的挂载这一步把关并判为失败。
    if [ "$fstype" = "ext4" ] || [ "$fstype" = "ext3" ] || [ "$fstype" = "ext2" ]; then
        log_info "  检查文件系统 (e2fsck -fn，仅检查不修改)..."
        e2fsck -fn "$img_file" >/dev/null 2>&1 || true
    fi

    # 挂载
    local mounted=false
    local loop_dev=""

    if mount -o loop "$img_file" "$mount_point" 2>/dev/null; then
        mounted=true
    fi

    # 严格模式：冻结后镜像本应可正常可写挂载。挂载失败即说明镜像异常
    # （撕裂/损坏），判为失败上抛，由调用方回滚——不做 e2fsck 修复后重试。
    if [ "$mounted" = false ]; then
        log_error "无法挂载镜像（冻结后本应一致，疑似撕裂/损坏）"
        rmdir "$mount_point"
        return 1
    fi

    # 检测 overlay 结构: 实际文件在 rw_upper/ 中
    local clean_root="$mount_point"
    if [ -d "${mount_point}/rw_upper" ]; then
        clean_root="${mount_point}/rw_upper"
        log_info "  检测到 overlay 结构，清理目标: rw_upper/"
    fi

    local cleaned=0

    # 1. 清理 /media/ 下的内容
    if [ -d "${clean_root}/media" ]; then
        log_info "  清理 /media/ ..."
        find "${clean_root}/media" -mindepth 1 -exec rm -rf {} + 2>/dev/null || true
        cleaned=$((cleaned + 1))
    fi

    # 2. 清理 /tmp/
    if [ -d "${clean_root}/tmp" ]; then
        log_info "  清理 /tmp/ ..."
        find "${clean_root}/tmp" -mindepth 1 -exec rm -rf {} + 2>/dev/null || true
        cleaned=$((cleaned + 1))
    fi

    # 3. 清理其他临时文件
    for d in var/tmp var/cache; do
        if [ -d "${clean_root}/${d}" ]; then
            log_info "  清理 /${d}/ ..."
            find "${clean_root}/${d}" -mindepth 1 -exec rm -rf {} + 2>/dev/null || true
            cleaned=$((cleaned + 1))
        fi
    done

    # 4. 清理日志文件
    if [ -d "${clean_root}/var/log" ]; then
        log_info "  清理 /var/log/ ..."
        find "${clean_root}/var/log" -type f \( -name '*.log' -o -name '*.log.*' -o -name '*.gz' \) -delete 2>/dev/null || true
        cleaned=$((cleaned + 1))
    fi

    log_ok "清理了 $cleaned 个目录/类别"
    sync

    # 卸载
    umount "$mount_point"
    rmdir "$mount_point"

    # zerofree 已移除：在 LIVE dd 出来的不一致 image 上 zerofree 可能误判
    # bitmap，把仍属于文件的块写零，造成备份中 .so 内容变全零。代价是压缩后
    # 体积可能增加几百 MB（gzip/pigz 处理全零块本来很高效，已分配块的"残余"
    # 数据反而难压缩）。
    return 0
}

# --------------- 恢复操作 ---------------
do_restore() {
    local restore_path="$1"
    local img_file
    local md5_file
    
    # 查找镜像文件
    if [ -f "$restore_path" ]; then
        img_file="$restore_path"
    elif [ -d "$restore_path" ]; then
        # 查找目录中的 .img.gz 文件
        local -a img_list
        mapfile -t img_list < <(ls -t "$restore_path"/*.img.gz 2>/dev/null)
        
        if [ ${#img_list[@]} -eq 0 ]; then
            log_error "目录中未找到 .img.gz 文件: $restore_path"
            exit 1
        elif [ ${#img_list[@]} -eq 1 ]; then
            img_file="${img_list[0]}"
            log_info "找到镜像: $img_file"
        else
            log_info "找到 ${#img_list[@]} 个镜像文件（按时间排序）:"
            for i in "${!img_list[@]}"; do
                local fsize
                fsize=$(ls -lh "${img_list[$i]}" | awk '{print $5}')
                local ftime
                ftime=$(stat -c '%y' "${img_list[$i]}" | cut -d. -f1)
                echo "  [$((i+1))] ${img_list[$i]##*/}  ($fsize, $ftime)"
            done
            read -p "请选择 [1-${#img_list[@]}]: " choice
            if [[ ! "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt ${#img_list[@]} ]; then
                log_error "无效选择"
                exit 1
            fi
            img_file="${img_list[$((choice-1))]}"
        fi
    else
        log_error "路径不存在: $restore_path"
        exit 1
    fi
    
    md5_file="${img_file}.md5"
    
    log_info "=========================================="
    log_info "恢复 $img_file -> Slot $INACTIVE_SLOT"
    log_info "=========================================="
    
    # 检查非活动分区是否被挂载
    if check_partition_mounted "$INACTIVE_PART"; then
        log_warn "非活动分区 $INACTIVE_PART 已挂载，尝试卸载..."
        umount "$INACTIVE_PART" || {
            log_error "无法卸载 $INACTIVE_PART，请手动卸载后重试"
            exit 1
        }
        log_ok "卸载成功"
    fi
    
    # MD5 校验
    if [ -f "$md5_file" ]; then
        log_info "验证 MD5 校验值..."
        local expected_md5
        expected_md5=$(cat "$md5_file" | awk '{print $1}')
        local actual_md5
        actual_md5=$($MD5SUM "$img_file" | awk '{print $1}')
        
        if [ "$expected_md5" = "$actual_md5" ]; then
            log_ok "MD5 校验通过: $actual_md5"
        else
            log_error "MD5 校验失败!"
            log_error "  期望: $expected_md5"
            log_error "  实际: $actual_md5"
            exit 1
        fi
    else
        log_warn "未找到 MD5 文件 ($md5_file)，跳过校验"
        read -p "是否继续? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
    
    # 获取镜像大小
    local img_size
    img_size=$(ls -lh "$img_file" | awk '{print $5}')
    log_info "镜像大小: $img_size"
    
    # 确认操作
    log_warn "即将写入 $INACTIVE_PART (Slot $INACTIVE_SLOT)"
    log_warn "此操作将覆盖该分区所有数据!"
    read -p "确认继续? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "操作已取消"
        exit 0
    fi
    
    # 开始恢复
    log_info "开始恢复..."
    local start_time
    start_time=$(date +%s)
    
    if [ -n "$PIGZ" ]; then
        if [ -n "$PV" ]; then
            $PIGZ -dc "$img_file" | $PV | $DD of="$INACTIVE_PART" bs=4M status=none
        else
            $PIGZ -dc "$img_file" | $DD of="$INACTIVE_PART" bs=4M status=progress
        fi
    else
        gzip -dc "$img_file" | $DD of="$INACTIVE_PART" bs=4M status=progress
    fi
    
    # 同步
    log_info "同步磁盘..."
    sync
    
    local end_time
    end_time=$(date +%s)
    local duration=$((end_time - start_time))
    
    log_ok "=========================================="
    log_ok "恢复完成!"
    log_ok "  目标分区: $INACTIVE_PART (Slot $INACTIVE_SLOT)"
    log_ok "  耗时: ${duration}秒"
    log_ok "=========================================="

    # bsp_param 默认不恢复（保留 BSP 提供的当前状态）；--with-bsp-param 才尝试
    if [ "${WITH_BSP_PARAM:-false}" = true ]; then
        restore_bsp_param "$img_file"
    else
        local bp_sibling="${img_file%.img.gz}${BSP_PARAM_SUFFIX}"
        if [ -f "$bp_sibling" ]; then
            log_info "检测到伴生 bsp_param 备份 ($bp_sibling)，但未指定 --with-bsp-param，跳过"
        fi
    fi

    # 软链接目标目录的创建/owner 修复已交由开机服务
    # tars-init-user-partition.service（/usr/local/bin/tars_init_user_partition）
    # 统一负责，恢复后首次启动即生效，故此处不再重复处理。

    # 切换 slot 并重启
    switch_and_reboot
}

# --------------- 恢复 bsp_param (vblkdev73) ---------------
# 仅在显式 --with-bsp-param 时调用。从 res 备份旁边的伴生文件读取，
# 校验 MD5（如有），写入原 raw partition。
restore_bsp_param() {
    local res_file="$1"
    local bp_file="${res_file%.img.gz}${BSP_PARAM_SUFFIX}"
    local bp_md5="${bp_file}.md5"

    log_info "------ 恢复 bsp_param ($BSP_PARAM_PART) ------"
    if [ ! -f "$bp_file" ]; then
        log_warn "未找到伴生 bsp_param 备份: $bp_file，跳过"
        return 0
    fi
    if [ ! -b "$BSP_PARAM_PART" ]; then
        log_warn "未找到目标分区 $BSP_PARAM_PART，跳过"
        return 0
    fi

    if [ -f "$bp_md5" ]; then
        local expected actual
        expected=$(awk '{print $1}' "$bp_md5")
        actual=$($MD5SUM "$bp_file" | awk '{print $1}')
        if [ "$expected" != "$actual" ]; then
            log_error "bsp_param MD5 校验失败 ($expected vs $actual)，中止恢复 bsp_param"
            return 1
        fi
        log_ok "bsp_param MD5 校验通过"
    else
        log_warn "无 MD5 文件，跳过 bsp_param 校验"
    fi

    log_warn "即将写入 $BSP_PARAM_PART (bsp_param raw partition)"
    read -p "确认恢复 bsp_param? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "已跳过 bsp_param 恢复"
        return 0
    fi

    if [ -n "$PIGZ" ]; then
        $PIGZ -dc "$bp_file" | $DD of="$BSP_PARAM_PART" bs=4M status=none
    else
        gzip -dc "$bp_file" | $DD of="$BSP_PARAM_PART" bs=4M status=none
    fi
    sync
    log_ok "bsp_param 恢复完成"
}

# --------------- 切换 Slot 并重启 ---------------
switch_and_reboot() {
    log_info "准备切换到 Slot $INACTIVE_SLOT..."
    
    # 使用 switch_slot 切换分区，传入目标 slot 参数
    if ! command -v switch_slot &>/dev/null; then
        log_warn "未找到 switch_slot 命令"
        log_info "请手动执行 switch_slot $INACTIVE_SLOT 后重启"
        return 1
    fi
    
    log_info "执行: switch_slot $INACTIVE_SLOT"
    if switch_slot "$INACTIVE_SLOT"; then
        log_ok "Slot 切换成功 -> Slot $INACTIVE_SLOT"
    else
        log_error "Slot 切换失败"
        return 1
    fi

    if [ "${NO_REBOOT:-false}" = true ]; then
        log_info "已指定 --no-reboot，跳过重启。下次启动将进入 Slot $INACTIVE_SLOT"
        return 0
    fi
    
    log_warn "系统将在 5 秒后重启..."
    log_warn "按 Ctrl+C 取消"
    sleep 5
    
    log_info "正在重启..."
    /usr/bin/reboot
}

# --------------- 使用帮助 ---------------
usage() {
    cat << EOF
用法: $0 [选项] <路径>

选项:
    -b, --backup  <path>    备份当前分区到指定路径
                            （同时自动备份 bsp_param 到 <res>.bsp_param.img.gz）
    -r, --restore <path>    从指定路径恢复到非活动分区
                            默认仅恢复 res，不写 bsp_param
    --with-bsp-param        与 -r 配合：同时把伴生 bsp_param 备份写回
                            /dev/vblkdev73（需要确认）
    --no-reboot             与 -r 配合：切换 slot 后不重启
    -h, --help              显示此帮助信息

示例:
    $0 -b /media/sda_udisk/                    # 备份到 USB，自动生成文件名
    $0 -b /media/sda_udisk/my_backup.img.gz    # 备份到指定文件
    $0 -r /media/sda_udisk/my_backup.img.gz    # 仅恢复 res
    $0 -r /media/sda_udisk/                    # 从目录中最新镜像恢复
    $0 -r /media/sda_udisk/foo.img.gz --with-bsp-param   # 同时恢复 bsp_param
    $0 -r /media/sda_udisk/foo.img.gz --no-reboot        # 刷写后不重启

分区布局:
    vblkdev80p9  = Slot A (res_a)
    vblkdev80p10 = Slot B (res_b)
    vblkdev73    = bsp_param (16 MiB raw, 默认仅备份不恢复)

备份操作:
    - 读取当前活动分区的完整块数据
    - 使用 pigz 并行压缩 (如可用)
    - 生成 .md5 校验文件
    - 顺手导出 vblkdev73 (bsp_param) 为伴生文件

恢复操作:
    - 验证 MD5 校验值
    - 写入非活动分区
    - 不写 bsp_param（除非显式 --with-bsp-param）
    - 自动切换 slot 并重启（可用 --no-reboot 跳过重启）
EOF
}

# --------------- 主程序 ---------------
main() {
    # 检查 root 权限
    if [ "$(id -u)" -ne 0 ]; then
        log_error "需要 root 权限运行此脚本"
        exit 1
    fi
    
    # 解析参数
    local action=""
    local target_path=""
    
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -b|--backup)
                action="backup"
                shift
                if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
                    target_path="$1"
                    shift
                fi
                ;;
            -r|--restore)
                action="restore"
                shift
                if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
                    target_path="$1"
                    shift
                fi
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            --with-bsp-param)
                WITH_BSP_PARAM=true
                shift
                ;;
            --no-reboot)
                NO_REBOOT=true
                shift
                ;;
            *)
                if [ -z "$target_path" ]; then
                    target_path="$1"
                fi
                shift
                ;;
        esac
    done
    
    # 验证参数
    if [ -z "$action" ]; then
        log_error "请指定操作: -b (备份) 或 -r (恢复)"
        usage
        exit 1
    fi
    
    if [ -z "$target_path" ]; then
        log_error "请指定路径"
        usage
        exit 1
    fi
    
    # 初始化
    detect_tools
    detect_current_slot
    
    # 执行操作
    case "$action" in
        backup)
            do_backup "$target_path"
            ;;
        restore)
            do_restore "$target_path"
            ;;
    esac
}

main "$@"
'''

# ================================================================
# PMU_OTA (内联)

# ================================================================

#!/usr/bin/env python3
"""
Thor PMU 上位机 / OTA 工具

默认走 CAN-over-UDP 75B（与 power_udp_daemon、send_mcu_can_udp75.py 一致）：
  - 本机 bind :40189 收 MCU 回包（0x200 / 0x210 / 周期帧等）
  - sendto MCU :40188 发命令与 OTA 帧

依赖: 无强制第三方包（colorama 可选，仅彩色输出；无则纯文本）
台架直连 CAN（可选）: pip install python-can

使用方法:
  UDP（推荐，真机/与 daemon 同链路）:
    python3 PMU_OTA.py
    python3 PMU_OTA.py --mcu-ip 192.168.1.20 --listen-port 40189
    python3 PMU_OTA.py -j                    # 非交互读取固件版本，stdout 输出版本字符串
    python3 PMU_OTA.py -f fw.bin             # 非交互 OTA 刷写（自动 apply 重启）
    python3 PMU_OTA.py -f fw.bin --chunk 6   # 指定每包 payload 字节数（1..6，默认 6）
    python3 PMU_OTA.py --apply               # 非交互发送 APPLY_OTA，等待 0x200 ACK 后退出

  注意: 若 power_udp_daemon 已占用 40189，需先停 daemon 或换 --listen-port。

  台架 SocketCAN（可选）:
    python3 PMU_OTA.py -t can -i socketcan -c can0
    python3 PMU_OTA.py -t can -i pcan -c PCAN_USBBUS1
    python3 PMU_OTA.py -t can -i virtual -c test
"""


import argparse
import os
import socket
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

try:
    import can
except ImportError:
    can = None

try:
    from colorama import Fore, Style, init

    init(autoreset=True)
except ImportError:
    class _AnsiStub:
        def __getattr__(self, _name: str) -> str:
            return ""

    Fore = _AnsiStub()
    Style = _AnsiStub()

    def init(*_args, **_kwargs) -> None:
        return None

# ================================================================
# CAN 协议定义
# ================================================================

# 上位机 → 本机
CAN_ID_HOST_CMD             = 0x100
CAN_ID_HOST_NEGOTIATE_REPLY = 0x101
CAN_ID_HOST_HEARTBEAT       = 0x102
CAN_ID_HOST_TIME_SET        = 0x103
CAN_ID_HOST_OTA_START       = 0x110
CAN_ID_HOST_OTA_DATA        = 0x111
CAN_ID_HOST_OTA_END         = 0x112
CAN_ID_HOST_OTA_ABORT       = 0x113

# 本机 → 上位机
CAN_ID_DEV_ACK              = 0x200
CAN_ID_DEV_NEGOTIATE_REQ    = 0x201
CAN_ID_DEV_HEARTBEAT        = 0x202
CAN_ID_DEV_OTA_STATUS       = 0x210
CAN_ID_STATUS_CHANNEL       = 0x306
CAN_ID_STATUS_BATTERY       = 0x307
CAN_ID_STATUS_EFUSE         = 0x308
CAN_ID_STATUS_THERMAL       = 0x309
CAN_ID_ALERT_BATT_FAULT     = 0x206
CAN_ID_ALERT_EFUSE_FAULT    = 0x207
CAN_ID_ALERT_VOLTAGE_ABNORM = 0x208
# 命令码
HOST_CMD_POWER_ON     = 0x01
HOST_CMD_POWER_OFF    = 0x02
HOST_CMD_CLEAR_FAULT  = 0x03
HOST_CMD_ENTER_SLEEP  = 0x04
HOST_CMD_WAKE_UP      = 0x05
HOST_CMD_APPLY_OTA    = 0x06
HOST_CMD_GET_ID       = 0x0D

CMD_NAMES = {
    HOST_CMD_POWER_ON:    "POWER_ON",
    HOST_CMD_POWER_OFF:   "POWER_OFF",
    HOST_CMD_CLEAR_FAULT: "CLEAR_FAULT",
    HOST_CMD_ENTER_SLEEP: "ENTER_SLEEP",
    HOST_CMD_WAKE_UP:     "WAKE_UP",
    HOST_CMD_APPLY_OTA:   "APPLY_OTA",
    HOST_CMD_GET_ID:      "GET_ID",
}

# 协商类型
NEGOTIATE_TYPE_POWER_OFF = 0x01
NEGOTIATE_TYPE_SLEEP     = 0x02
NEGOTIATE_TYPE_GET_TIME  = 0x03

NEGOTIATE_TYPE_NAMES = {
    NEGOTIATE_TYPE_POWER_OFF: "POWER_OFF",
    NEGOTIATE_TYPE_SLEEP:     "SLEEP",
    NEGOTIATE_TYPE_GET_TIME:  "GET_TIME",
}

# 协商结果
NEGOTIATE_RESULT_APPROVED = 0x01
NEGOTIATE_RESULT_REJECTED = 0x02
NEGOTIATE_RESULT_DELAY    = 0x03

NEGOTIATE_RESULT_NAMES = {
    NEGOTIATE_RESULT_APPROVED: "APPROVED",
    NEGOTIATE_RESULT_REJECTED: "REJECTED",
    NEGOTIATE_RESULT_DELAY:    "DELAY",
}

# 告警级别
ALERT_LEVEL_WARNING  = 0x01
ALERT_LEVEL_CRITICAL = 0x02

ALERT_LEVEL_NAMES = {
    ALERT_LEVEL_WARNING:  "WARNING",
    ALERT_LEVEL_CRITICAL: "CRITICAL",
}

OTA_STATUS_NAMES = {
    0x10: "START_ACK",
    0x11: "DATA_ACK",
    0x12: "END_ACK",
    0x13: "ABORT_ACK",
    0x14: "READY_REBOOT",
    0xE0: "ERR_STATE",
    0xE1: "ERR_PARAM",
    0xE2: "ERR_SEQ",
    0xE3: "ERR_FLASH",
    0xE4: "ERR_SIZE",
    0xEF: "ERR_UNSUPPORTED",
}

OTA_STATUS_START_ACK = 0x10
OTA_STATUS_DATA_ACK = 0x11
OTA_STATUS_END_ACK = 0x12
OTA_STATUS_ABORT_ACK = 0x13
OTA_STATUS_READY_REBOOT = 0x14
OTA_STATUS_ERR_SEQ = 0xE2

# 状态机状态名称
STATE_NAMES = {
    0x00: "INIT",
    0x01: "IDLE",
    0x02: "RUN",
    0x03: "CHARGING",
    0x04: "FAULT",
    0x05: "SLEEP",
    0xFF: "UNKNOWN",
}

# ================================================================
# CAN-over-UDP 75B（common/can_over_udp_75b.h）
# ================================================================

UDP_FRAME_SIZE = 75
UDP_CRC_COVER_LEN = 74
UDP_CRC_OFFSET = 74
UDP_WIRE_MIN_DLC = 8
UDP_WIRE_MAX_DLC = 64
CAN_FD_FLAG = 0x40000000

DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 40189
DEFAULT_MCU_IP = "192.168.1.20"
DEFAULT_MCU_PORT = 40188
DEFAULT_BUS_ID = 0

_CRC8_TABLE = bytes([
    0x00, 0x5e, 0xbc, 0xe2, 0x61, 0x3f, 0xdd, 0x83, 0xc2, 0x9c, 0x7e, 0x20,
    0xa3, 0xfd, 0x1f, 0x41, 0x9d, 0xc3, 0x21, 0x7f, 0xfc, 0xa2, 0x40, 0x1e,
    0x5f, 0x01, 0xe3, 0xbd, 0x3e, 0x60, 0x82, 0xdc, 0x23, 0x7d, 0x9f, 0xc1,
    0x42, 0x1c, 0xfe, 0xa0, 0xe1, 0xbf, 0x5d, 0x03, 0x80, 0xde, 0x3c, 0x62,
    0xbe, 0xe0, 0x02, 0x5c, 0xdf, 0x81, 0x63, 0x3d, 0x7c, 0x22, 0xc0, 0x9e,
    0x1d, 0x43, 0xa1, 0xff, 0x46, 0x18, 0xfa, 0xa4, 0x27, 0x79, 0x9b, 0xc5,
    0x84, 0xda, 0x38, 0x66, 0xe5, 0xbb, 0x59, 0x07, 0xdb, 0x85, 0x67, 0x39,
    0xba, 0xe4, 0x06, 0x58, 0x19, 0x47, 0xa5, 0xfb, 0x78, 0x26, 0xc4, 0x9a,
    0x65, 0x3b, 0xd9, 0x87, 0x04, 0x5a, 0xb8, 0xe6, 0xa7, 0xf9, 0x1b, 0x45,
    0xc6, 0x98, 0x7a, 0x24, 0xf8, 0xa6, 0x44, 0x1a, 0x99, 0xc7, 0x25, 0x7b,
    0x3a, 0x64, 0x86, 0xd8, 0x5b, 0x05, 0xe7, 0xb9, 0x8c, 0xd2, 0x30, 0x6e,
    0xed, 0xb3, 0x51, 0x0f, 0x4e, 0x10, 0xf2, 0xac, 0x2f, 0x71, 0x93, 0xcd,
    0x11, 0x4f, 0xad, 0xf3, 0x70, 0x2e, 0xcc, 0x92, 0xd3, 0x8d, 0x6f, 0x31,
    0xb2, 0xec, 0x0e, 0x50, 0xaf, 0xf1, 0x13, 0x4d, 0xce, 0x90, 0x72, 0x2c,
    0x6d, 0x33, 0xd1, 0x8f, 0x0c, 0x52, 0xb0, 0xee, 0x32, 0x6c, 0x8e, 0xd0,
    0x53, 0x0d, 0xef, 0xb1, 0xf0, 0xae, 0x4c, 0x12, 0x91, 0xcf, 0x2d, 0x73,
    0xca, 0x94, 0x76, 0x28, 0xab, 0xf5, 0x17, 0x49, 0x08, 0x56, 0xb4, 0xea,
    0x69, 0x37, 0xd5, 0x8b, 0x57, 0x09, 0xeb, 0xb5, 0x36, 0x68, 0x8a, 0xd4,
    0x95, 0xcb, 0x29, 0x77, 0xf4, 0xaa, 0x48, 0x16, 0xe9, 0xb7, 0x55, 0x0b,
    0x88, 0xd6, 0x34, 0x6a, 0x2b, 0x75, 0x97, 0xc9, 0x4a, 0x14, 0xf6, 0xa8,
    0x74, 0x2a, 0xc8, 0x96, 0x15, 0x4b, 0xa9, 0xf7, 0xb6, 0xe8, 0x0a, 0x54,
    0xd7, 0x89, 0x6b, 0x35,
])


def _crc8(buf: bytes, init: int = 0) -> int:
    c = init & 0xFF
    for b in buf:
        c = _CRC8_TABLE[c ^ b]
    return c


def can_id_base(wire_id: int) -> int:
    wire_id &= ~CAN_FD_FLAG
    if wire_id & 0x80000000:
        return wire_id & 0x1FFFFFFF
    return wire_id & 0x7FF


def pad_can_payload(data) -> tuple[int, bytes]:
    payload = bytes(data)
    dlc = max(UDP_WIRE_MIN_DLC, len(payload))
    if dlc > UDP_WIRE_MAX_DLC:
        raise ValueError(f"CAN payload too long: {len(payload)}")
    if len(payload) < dlc:
        payload += b"\x00" * (dlc - len(payload))
    return dlc, payload


def build_can_over_udp75(
    bus: int,
    can_id: int,
    udp_counter: int,
    dlc: int,
    data: bytes,
) -> bytes:
    if dlc < UDP_WIRE_MIN_DLC or dlc > UDP_WIRE_MAX_DLC:
        raise ValueError(f"dlc must be in [{UDP_WIRE_MIN_DLC}, {UDP_WIRE_MAX_DLC}]")
    if len(data) < dlc:
        raise ValueError(f"data length {len(data)} < dlc {dlc}")

    frame = bytearray(UDP_FRAME_SIZE)
    frame[0] = bus & 0xFF
    struct.pack_into(">I", frame, 1, can_id & 0xFFFFFFFF)
    struct.pack_into("<I", frame, 5, udp_counter & 0xFFFFFFFF)
    frame[9] = dlc
    frame[10 : 10 + dlc] = data[:dlc]
    frame[UDP_CRC_OFFSET] = _crc8(bytes(frame[:UDP_CRC_COVER_LEN]), 0)
    return bytes(frame)


@dataclass
class ParsedUdpCanFrame:
    bus: int
    can_id: int
    dlc: int
    data: bytes
    udp_counter: int


def parse_can_over_udp75(packet: bytes) -> tuple[Optional[ParsedUdpCanFrame], Optional[str]]:
    if len(packet) < UDP_FRAME_SIZE:
        return None, f"length {len(packet)} < {UDP_FRAME_SIZE}"
    expect = packet[UDP_CRC_OFFSET]
    actual = _crc8(packet[:UDP_CRC_COVER_LEN], 0)
    if expect != actual:
        return None, f"crc mismatch (expect 0x{expect:02X}, got 0x{actual:02X})"

    bus = packet[0]
    can_id = struct.unpack_from(">I", packet, 1)[0]
    udp_counter = struct.unpack_from("<I", packet, 5)[0]
    dlc = packet[9]
    if dlc < 1 or dlc > UDP_WIRE_MAX_DLC:
        return None, f"invalid dlc={dlc}"

    data = bytes(packet[10 : 10 + dlc])
    return ParsedUdpCanFrame(bus, can_id, dlc, data, udp_counter), None

# ================================================================
# 帧解析器
# ================================================================

class FrameParser:
    """CAN 帧解析器"""
    
    @staticmethod
    def parse_dev_ack(data):
        """解析命令应答帧 (0x200)
        格式: [cmd, result] 或 [cmd, result, id0, id1, id2, id3, id4] (GET_ID)
        """
        if len(data) < 2:
            return None
        cmd = data[0]
        result = data[1]
        result_str = "OK" if result == 0 else f"ERR({result})"

        # GET_ID 命令返回版本信息
        if cmd == HOST_CMD_GET_ID and len(data) >= 7:
            id_bytes = data[2:7]
            # BCD 解码：V[0][1][2]R[3][4][5]B[6][7][8]SP[9]
            # 例 [12,00,04,00,11] -> V120R004B001SP1
            version_digits = []
            for b in id_bytes:
                version_digits.append((b >> 4) & 0x0F)
                version_digits.append(b & 0x0F)
            ver_str = (
                f"V{version_digits[0]}{version_digits[1]}{version_digits[2]}"
                f"R{version_digits[3]}{version_digits[4]}{version_digits[5]}"
                f"B{version_digits[6]}{version_digits[7]}{version_digits[8]}"
                f"SP{version_digits[9]}"
            )
            return {
                'cmd': CMD_NAMES.get(cmd, f"0x{cmd:02X}"),
                'result': result_str,
                'version': ver_str,
            }

        return {
            'cmd': CMD_NAMES.get(cmd, f"0x{cmd:02X}"),
            'result': result_str,
        }
    
    @staticmethod
    def parse_negotiate_req(data):
        """解析协商请求帧 (0x201)"""
        if len(data) < 1:
            return None
        neg_type = data[0]
        return {
            'type': NEGOTIATE_TYPE_NAMES.get(neg_type, f"0x{neg_type:02X}"),
        }
    
    @staticmethod
    def parse_dev_heartbeat(data):
        """解析本机心跳帧 (0x202)"""
        if len(data) < 1:
            return None
        state_id = data[0]
        return {
            'state': STATE_NAMES.get(state_id, f"0x{state_id:02X}"),
        }
    
    @staticmethod
    def parse_channel_status(data):
        """解析通道状态帧 (0x306)"""
        if len(data) < 8:
            return None
        ch_idx = data[0]
        is_on = data[1]
        voltage = (data[2] << 8) | data[3]
        current = (data[4] << 8) | data[5]
        pg_good = data[6]
        fault = data[7]
        return {
            'channel': ch_idx,
            'on': bool(is_on),
            'voltage_mV': voltage,
            'current_mA': current,
            'pg_good': bool(pg_good),
            'fault': fault,
        }
    
    @staticmethod
    def parse_battery_status(data):
        """解析电池状态帧 (0x307)"""
        if len(data) < 6:
            return None
        voltage = ((data[0] << 8) | data[1]) * 100  # mV
        current = struct.unpack('>h', bytes(data[2:4]))[0] * 100  # mA, signed
        soc = data[4]
        temp = struct.unpack('b', bytes([data[5]]))[0] * 10  # 0.1°C
        return {
            'voltage_mV': voltage,
            'current_mA': current,
            'soc_%': soc,
            'temp_0.1C': temp,
        }
    
    @staticmethod
    def parse_efuse_status(data):
        """解析 eFuse 状态帧 (0x308)"""
        if len(data) < 8:
            return None
        status = []
        for i in range(4):
            word = (data[i*2] << 8) | data[i*2 + 1]
            status.append(f"0x{word:04X}")
        return {
            'ch0': status[0],
            'ch1': status[1],
            'ch2': status[2],
            'ch3': status[3],
        }
    
    @staticmethod
    def parse_thermal_status(data):
        """解析温度状态帧 (0x309)
        帧格式: [channel(1B), temp_high(1B), temp_low(1B), is_over(1B), 0x00*4]
        temperature 存储 NTC 引脚 ADC 毫伏山其 (mV)
        """
        if len(data) < 4:
            return None
        ch_idx = data[0]
        temp_mv = struct.unpack('>H', bytes(data[1:3]))[0]  # uint16, mV
        is_over = data[3]
        return {
            'channel': ch_idx,
            'temp_mV': temp_mv,
            'is_over': bool(is_over),
        }
    
    @staticmethod
    def parse_alert_batt_fault(data):
        """解析电池故障告警帧 (0x206)"""
        if len(data) < 7:
            return None
        level = data[0]
        fault_high = (data[1] << 24) | (data[2] << 16) | (data[3] << 8) | data[4]
        fault_low = (data[5] << 8) | data[6]
        return {
            'level': ALERT_LEVEL_NAMES.get(level, f"0x{level:02X}"),
            'fault_high': f"0x{fault_high:08X}",
            'fault_low': f"0x{fault_low:04X}",
        }
    
    @staticmethod
    def parse_alert_efuse_fault(data):
        """解析 eFuse 故障告警帧 (0x207)"""
        if len(data) < 8:
            return None
        level = data[0]
        mask = data[1]
        faults = []
        for i in range(3):
            word = (data[2 + i*2] << 8) | data[3 + i*2]
            faults.append(f"0x{word:04X}")
        return {
            'level': ALERT_LEVEL_NAMES.get(level, f"0x{level:02X}"),
            'mask': f"0b{mask:04b}",
            'ch0_fault': faults[0],
            'ch1_fault': faults[1],
            'ch2_fault': faults[2],
        }
    
    @staticmethod
    def parse_alert_voltage_abnorm(data):
        """解析电压异常告警帧 (0x208)"""
        if len(data) < 4:
            return None
        level = data[0]
        mask = data[1]
        voltage = ((data[2] << 8) | data[3]) * 100  # mV
        
        abnorms = []
        if mask & 0x01: abnorms.append("VPOWER_UV")
        if mask & 0x02: abnorms.append("VPOWER_OV")
        if mask & 0x04: abnorms.append("VBAT_UV")
        if mask & 0x08: abnorms.append("VBAT_OV")
        if mask & 0x10: abnorms.append("24V_LOST")
        
        return {
            'level': ALERT_LEVEL_NAMES.get(level, f"0x{level:02X}"),
            'abnorms': abnorms if abnorms else ["NONE"],
            'voltage_mV': voltage,
        }

    @staticmethod
    def parse_ota_status(data):
        """解析 OTA 状态帧 (0x210)"""
        if len(data) < 5:
            return None
        status = data[0]
        detail = data[1]
        next_seq = data[2] | (data[3] << 8)
        progress = data[4]
        return {
            'status': OTA_STATUS_NAMES.get(status, f"0x{status:02X}"),
            'detail': f"0x{detail:02X}",
            'next_seq': next_seq,
            'progress_%': progress,
        }

# ================================================================
# CAN 上位机模拟器
# ================================================================

class ThorCanHost:
    """Thor PMU 上位机（UDP 75B 或直连 CAN）"""

    def __init__(
        self,
        transport: str = "udp",
        *,
        listen_host: str = DEFAULT_LISTEN_HOST,
        listen_port: int = DEFAULT_LISTEN_PORT,
        mcu_ip: str = DEFAULT_MCU_IP,
        mcu_port: int = DEFAULT_MCU_PORT,
        bus: int = DEFAULT_BUS_ID,
        interface: str = "socketcan",
        channel: str = "can0",
        bitrate: int = 1000000,
        quiet: bool = False,
    ):
        self.transport = transport
        self.quiet = quiet
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.mcu_ip = mcu_ip
        self.mcu_port = mcu_port
        self.bus = bus
        self.interface = interface
        self.channel = channel
        self.bitrate = bitrate

        self.bus_can = None
        self.udp_sock: Optional[socket.socket] = None
        self.mcu_addr = (mcu_ip, mcu_port)
        self.udp_tx_counter = 1

        self.running = False
        self.heartbeat_enabled = False
        self.heartbeat_interval = 1.0
        self.auto_negotiate_reply = True
        self.last_dev_heartbeat = 0
        self.parser = FrameParser()
        # 默认只打印 OTA 相关收发；view full 可开全量
        self.log_ota_only = True
        # 一键升级批量 DATA 期间压制 DATA_ACK 逐帧日志（进度行仍打印）
        self.ota_bulk_quiet = False

        self.rx_count = 0
        self.tx_count = 0
        self.alert_count = 0
        self.ota_status_count = 0
        self.ota_session_id = 1
        self.ota_status_cv = threading.Condition()
        self.ota_status_queue = deque(maxlen=512)
        self.get_id_cv = threading.Condition()
        self.last_firmware_version: Optional[str] = None
        self.apply_ack_cv = threading.Condition()
        self.last_apply_ack: Optional[dict] = None

    def _should_log_rx(self, can_id: int, data=None) -> bool:
        """是否打印 RX 日志。默认仅 OTA 相关帧。"""
        if not self.log_ota_only:
            return True
        if can_id == CAN_ID_DEV_OTA_STATUS:
            if not data:
                return True
            code = data[0]
            # 批量升级时 DATA_ACK 太多，进度行已覆盖；关键 ACK/错误仍打印
            if self.ota_bulk_quiet and code == OTA_STATUS_DATA_ACK:
                return False
            return True
        # APPLY_OTA 走 0x200 ACK
        if can_id == CAN_ID_DEV_ACK and data and data[0] == HOST_CMD_APPLY_OTA:
            return True
        return False

    def connect(self):
        """建立传输层连接"""
        if self.transport == "udp":
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((self.listen_host, self.listen_port))
                sock.settimeout(0.1)
                self.udp_sock = sock
                if not self.quiet:
                    print(
                        f"{Fore.GREEN}✓ UDP 已绑定 {self.listen_host}:{self.listen_port} "
                        f"(MCU -> {self.mcu_ip}:{self.mcu_port})"
                    )
                return True
            except OSError as exc:
                # 绑定失败属错误，即便 quiet（--get-version/--apply）也打到 stderr，便于诊断
                print(f"{Fore.RED}✗ UDP 绑定失败: {exc}", file=sys.stderr)
                if not self.quiet:
                    print(
                        f"{Fore.YELLOW}提示: 40189 若被 power_udp_daemon 占用，"
                        f"请先停 daemon 或换 --listen-port"
                    )
                return False

        if can is None:
            if not self.quiet:
                print(f"{Fore.RED}✗ 未安装 python-can，请 pip install python-can 或使用 -t udp")
            return False
        try:
            if self.interface == "virtual":
                self.bus_can = can.Bus(interface="virtual", channel=self.channel)
            else:
                self.bus_can = can.Bus(
                    interface=self.interface,
                    channel=self.channel,
                    bitrate=self.bitrate,
                )
            print(f"{Fore.GREEN}✓ CAN 连接成功: {self.interface}:{self.channel}")
            return True
        except Exception as exc:
            if not self.quiet:
                print(f"{Fore.RED}✗ CAN 连接失败: {exc}")
            return False

    def disconnect(self):
        """断开传输层"""
        if self.udp_sock is not None:
            self.udp_sock.close()
            self.udp_sock = None
            if not self.quiet:
                print(f"{Fore.YELLOW}UDP 已断开")
        if self.bus_can is not None:
            self.bus_can.shutdown()
            self.bus_can = None
            if not self.quiet:
                print(f"{Fore.YELLOW}CAN 已断开")

    def send_frame(self, can_id, data):
        """发送 CAN 帧（UDP 75B 或直连 CAN）"""
        if self.transport == "udp":
            if self.udp_sock is None:
                print(f"{Fore.RED}UDP 未连接")
                return False
            try:
                dlc, payload = pad_can_payload(data)
                frame = build_can_over_udp75(
                    self.bus, can_id, self.udp_tx_counter, dlc, payload
                )
                self.udp_sock.sendto(frame, self.mcu_addr)
                self.udp_tx_counter += 1
                self.tx_count += 1
                return True
            except (OSError, ValueError) as exc:
                print(f"{Fore.RED}UDP 发送失败: {exc}")
                return False

        if self.bus_can is None:
            print(f"{Fore.RED}CAN 未连接")
            return False

        msg = can.Message(
            arbitration_id=can_id,
            data=data,
            is_extended_id=False,
        )
        try:
            self.bus_can.send(msg)
            self.tx_count += 1
            return True
        except Exception as exc:
            print(f"{Fore.RED}发送失败: {exc}")
            return False
    
    # ---- 发送命令 ----
    
    def send_cmd(self, cmd_code, params=None):
        """发送主命令帧"""
        data = [cmd_code]
        if params:
            data.extend(params[:7])
        data.extend([0] * (8 - len(data)))
        
        if self.send_frame(CAN_ID_HOST_CMD, data):
            if not self.quiet:
                cmd_name = CMD_NAMES.get(cmd_code, f"0x{cmd_code:02X}")
                print(f"{Fore.CYAN}>>> TX CMD: {cmd_name}")
    
    def send_power_on(self):
        self.send_cmd(HOST_CMD_POWER_ON)
    
    def send_power_off(self):
        self.send_cmd(HOST_CMD_POWER_OFF)
    
    def send_clear_fault(self):
        self.send_cmd(HOST_CMD_CLEAR_FAULT)
    
    def send_enter_sleep(self):
        self.send_cmd(HOST_CMD_ENTER_SLEEP)
    
    def send_wake_up(self):
        self.send_cmd(HOST_CMD_WAKE_UP)

    def send_apply_ota(self):
        self.send_cmd(HOST_CMD_APPLY_OTA)

    def apply_ota_and_wait(self, timeout: float = 5.0) -> Optional[dict]:
        """发送 APPLY_OTA 并等待 0x200 ACK，超时返回 None。

        成功时返回 parse_dev_ack 结果，例如 {'cmd': 'APPLY_OTA', 'result': 'OK'}。
        """
        with self.apply_ack_cv:
            self.last_apply_ack = None
        self.send_apply_ota()
        deadline = time.monotonic() + timeout
        with self.apply_ack_cv:
            while self.last_apply_ack is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.apply_ack_cv.wait(timeout=remaining)
            return self.last_apply_ack

    def send_get_id(self):
        """发送获取版本ID命令"""
        self.send_cmd(HOST_CMD_GET_ID)

    def fetch_firmware_version(self, timeout: float = 5.0) -> Optional[str]:
        """发送 GET_ID 并等待固件版本字符串，超时或失败返回 None"""
        with self.get_id_cv:
            self.last_firmware_version = None
        self.send_get_id()
        deadline = time.monotonic() + timeout
        with self.get_id_cv:
            while self.last_firmware_version is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.get_id_cv.wait(timeout=remaining)
            return self.last_firmware_version
    
    def send_negotiate_reply(self, result, neg_type=0):
        """发送协商回复帧"""
        data = [result, neg_type, 0, 0, 0, 0, 0, 0]
        if self.send_frame(CAN_ID_HOST_NEGOTIATE_REPLY, data) and not self.log_ota_only:
            result_name = NEGOTIATE_RESULT_NAMES.get(result, f"0x{result:02X}")
            print(f"{Fore.CYAN}>>> TX NEGOTIATE_REPLY: {result_name}")

    def send_time_set(self, timestamp=None):
        """发送时间设置帧 (0x103)，携带 Unix 时间戳"""
        if timestamp is None:
            timestamp = int(time.time())
        # 5 字节小端时间戳
        data = [
            timestamp & 0xFF,
            (timestamp >> 8) & 0xFF,
            (timestamp >> 16) & 0xFF,
            (timestamp >> 24) & 0xFF,
            (timestamp >> 32) & 0xFF,
            0, 0, 0,
        ]
        dt_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        ok = self.send_frame(CAN_ID_HOST_TIME_SET, data)
        if self.log_ota_only:
            return
        if ok:
            print(f"{Fore.CYAN}>>> TX TIME_SET: {timestamp} ({dt_str})")
        else:
            print(f"{Fore.RED}>>> TX TIME_SET FAILED")

    def send_heartbeat(self):
        """发送上位机心跳帧"""
        data = [0, 0, 0, 0, 0, 0, 0, 0]
        self.send_frame(CAN_ID_HOST_HEARTBEAT, data)

    # ---- OTA 命令 ----

    def send_ota_start(self, image_size, total_packets, session_id=None):
        """发送 OTA_START 帧 (0x110)"""
        if session_id is None:
            session_id = self.ota_session_id

        data = [
            session_id & 0xFF,
            total_packets & 0xFF,
            (total_packets >> 8) & 0xFF,
            image_size & 0xFF,
            (image_size >> 8) & 0xFF,
            (image_size >> 16) & 0xFF,
            (image_size >> 24) & 0xFF,
            0,
        ]
        if self.send_frame(CAN_ID_HOST_OTA_START, data):
            print(f"{Fore.CYAN}>>> TX OTA_START: sid={session_id} size={image_size} packets={total_packets}")

    def send_ota_data(self, seq, payload, quiet=False):
        """发送 OTA_DATA 帧 (0x111)，payload 长度 1..6 字节"""
        if not payload or len(payload) > 6:
            raise ValueError("OTA payload length must be 1..6 bytes")

        data = [
            seq & 0xFF,
            (seq >> 8) & 0xFF,
            *payload,
        ]
        if self.send_frame(CAN_ID_HOST_OTA_DATA, data) and not quiet:
            print(f"{Fore.CYAN}>>> TX OTA_DATA: seq={seq} len={len(payload)}")

    def send_ota_end(self):
        """发送 OTA_END 帧 (0x112)"""
        if self.send_frame(CAN_ID_HOST_OTA_END, [0]):
            print(f"{Fore.CYAN}>>> TX OTA_END")

    def send_ota_abort(self, reason=0):
        """发送 OTA_ABORT 帧 (0x113)"""
        if self.send_frame(CAN_ID_HOST_OTA_ABORT, [reason & 0xFF]):
            print(f"{Fore.CYAN}>>> TX OTA_ABORT: reason=0x{reason:02X}")

    def _clear_ota_status_queue(self):
        with self.ota_status_cv:
            self.ota_status_queue.clear()

    def _wait_ota_status(self, timeout_s=1.0, expected_codes=None, allow_error=True):
        """等待 OTA 状态。可指定期望状态码，忽略其它无关键值。"""
        deadline = time.monotonic() + timeout_s
        expected = set(expected_codes or [])

        with self.ota_status_cv:
            while True:
                while self.ota_status_queue:
                    st = self.ota_status_queue.popleft()
                    code = st['status_code']

                    if expected and code in expected:
                        return st

                    if allow_error and code is not None and code >= 0xE0:
                        return st

                    if not expected:
                        return st

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None

                self.ota_status_cv.wait(timeout=remaining)

    def ota_upgrade_from_file(
        self,
        file_path,
        chunk_size=6,
        inter_frame_delay=0.0,
        auto_apply=False,
    ):
        """一键 OTA 升级: START -> DATA* -> END；auto_apply 时等待 READY_REBOOT 并发送 APPLY_OTA"""
        if chunk_size < 1 or chunk_size > 6:
            print(f"{Fore.RED}chunk_size 必须在 1..6")
            return False

        if not os.path.isfile(file_path):
            print(f"{Fore.RED}文件不存在: {file_path}")
            return False

        with open(file_path, 'rb') as f:
            image = f.read()

        image_size = len(image)
        if image_size == 0:
            print(f"{Fore.RED}固件文件为空")
            return False

        chunks = [image[i:i + chunk_size] for i in range(0, image_size, chunk_size)]
        total_packets = len(chunks)

        sid = self.ota_session_id
        self.ota_session_id = (self.ota_session_id + 1) & 0xFF
        if self.ota_session_id == 0:
            self.ota_session_id = 1

        print(f"{Fore.YELLOW}=== OTA 开始 ===")
        print(f"{Fore.YELLOW}文件: {file_path}")
        print(f"{Fore.YELLOW}大小: {image_size} bytes, 分包: {total_packets}, 每包: {chunk_size} bytes")

        self._clear_ota_status_queue()
        self.ota_bulk_quiet = True
        try:
            self.send_ota_start(image_size=image_size, total_packets=total_packets, session_id=sid)

            # START 之后设备会先擦除 slot1，可能持续较久
            st = self._wait_ota_status(
                timeout_s=30.0,
                expected_codes={OTA_STATUS_START_ACK},
                allow_error=True,
            )
            if not st:
                print(f"{Fore.RED}未收到 START_ACK（30s 超时），停止发送")
                return False
            if st['status_code'] != OTA_STATUS_START_ACK:
                print(
                    f"{Fore.RED}未收到 START_ACK，收到状态 "
                    f"code=0x{st['status_code']:02X} detail=0x{st['detail']:02X}，停止发送"
                )
                return False

            seq = 0
            retry = 0
            max_retry = 10
            while seq < total_packets:
                chunk = chunks[seq]
                self.send_ota_data(seq, list(chunk), quiet=True)
                if inter_frame_delay > 0:
                    time.sleep(inter_frame_delay)

                st = self._wait_ota_status(
                    timeout_s=1.0,
                    expected_codes={OTA_STATUS_DATA_ACK, OTA_STATUS_ERR_SEQ},
                    allow_error=True,
                )
                if st is None:
                    retry += 1
                    if retry > max_retry:
                        print(f"{Fore.RED}OTA 超时过多，停止发送 at seq={seq}")
                        return False
                    continue

                retry = 0
                status_code = st['status_code']
                next_seq = st['next_seq']

                if status_code == OTA_STATUS_DATA_ACK:
                    seq = next_seq
                elif status_code == OTA_STATUS_ERR_SEQ:
                    print(f"{Fore.YELLOW}设备请求重发，next_seq={next_seq}")
                    seq = next_seq
                else:
                    print(
                        f"{Fore.RED}OTA 收到错误状态: "
                        f"code=0x{status_code:02X} detail=0x{st['detail']:02X}"
                    )
                    return False

                if seq % 100 == 0 or seq == total_packets:
                    pct = seq * 100.0 / total_packets
                    print(f"{Fore.WHITE}OTA send progress: {pct:.1f}% ({seq}/{total_packets})")

            self.send_ota_end()
            print(f"{Fore.YELLOW}=== OTA 发送完成，等待设备回包 ===")

            st = self._wait_ota_status(
                timeout_s=60.0,
                expected_codes={OTA_STATUS_END_ACK, OTA_STATUS_READY_REBOOT},
                allow_error=True,
            )
            if not st:
                print(f"{Fore.RED}未收到 END_ACK（60s 超时）")
                return False
            if st['status_code'] >= 0xE0:
                print(
                    f"{Fore.RED}OTA END 阶段错误: "
                    f"code=0x{st['status_code']:02X} detail=0x{st['detail']:02X}"
                )
                return False

            if st['status_code'] == OTA_STATUS_END_ACK:
                st = self._wait_ota_status(
                    timeout_s=120.0,
                    expected_codes={OTA_STATUS_READY_REBOOT},
                    allow_error=True,
                )
            if not st or st['status_code'] != OTA_STATUS_READY_REBOOT:
                code = st['status_code'] if st else None
                detail = st['detail'] if st else 0
                print(
                    f"{Fore.RED}未收到 READY_REBOOT"
                    + (f"，收到 code=0x{code:02X} detail=0x{detail:02X}" if code is not None else "（120s 超时）")
                )
                return False

            print(f"{Fore.GREEN}设备已就绪，可重启切换固件 (READY_REBOOT)")
            if auto_apply:
                self.send_apply_ota()
                print(f"{Fore.GREEN}已发送 APPLY_OTA，设备将重启应用新固件")
            else:
                print(f"{Fore.YELLOW}请手动输入 apply 触发重启切换")
            return True
        finally:
            self.ota_bulk_quiet = False

    # ---- 接收处理 ----

    def process_frame(self, msg):
        """处理接收到的 CAN 帧"""
        self.rx_count += 1
        can_id = msg.arbitration_id
        data = list(msg.data)
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_rx = self._should_log_rx(can_id, data)

        # 根据帧 ID 解析（非 OTA 帧默认静默，逻辑仍执行）
        if can_id == CAN_ID_DEV_ACK:
            parsed = self.parser.parse_dev_ack(data)
            if parsed and data and data[0] == HOST_CMD_APPLY_OTA:
                with self.apply_ack_cv:
                    self.last_apply_ack = parsed
                    self.apply_ack_cv.notify_all()
            if parsed and parsed.get('result') == 'OK' and 'version' in parsed:
                with self.get_id_cv:
                    self.last_firmware_version = parsed['version']
                    self.get_id_cv.notify_all()
            if log_rx and 'version' not in parsed:
                print(f"{Fore.GREEN}[{ts}] <<< ACK: {parsed}")

        elif can_id == CAN_ID_DEV_NEGOTIATE_REQ:
            parsed = self.parser.parse_negotiate_req(data)
            if log_rx:
                print(f"{Fore.YELLOW}[{ts}] <<< NEGOTIATE_REQ: {parsed}")
            if self.auto_negotiate_reply:
                neg_type = data[0]
                time.sleep(0.1)
                self.send_negotiate_reply(NEGOTIATE_RESULT_APPROVED, neg_type)
                if neg_type == NEGOTIATE_TYPE_GET_TIME:
                    time.sleep(0.05)
                    self.send_time_set()
                    if log_rx:
                        print(f"{Fore.GREEN}  → Time sync completed")

        elif can_id == CAN_ID_DEV_HEARTBEAT:
            parsed = self.parser.parse_dev_heartbeat(data)
            self.last_dev_heartbeat = time.time()
            if log_rx:
                print(f"{Fore.BLUE}[{ts}] <<< HEARTBEAT: {parsed}")

        elif can_id == CAN_ID_DEV_OTA_STATUS:
            self.ota_status_count += 1
            parsed = self.parser.parse_ota_status(data)
            status_record = {
                'status_code': data[0] if len(data) > 0 else None,
                'detail': data[1] if len(data) > 1 else 0,
                'next_seq': (data[2] | (data[3] << 8)) if len(data) > 3 else 0,
                'progress': data[4] if len(data) > 4 else 0,
            }
            with self.ota_status_cv:
                self.ota_status_queue.append(status_record)
                self.ota_status_cv.notify_all()
            if log_rx:
                print(f"{Fore.GREEN}[{ts}] <<< OTA_STATUS: {parsed}")

        elif can_id == CAN_ID_STATUS_CHANNEL:
            if log_rx:
                parsed = self.parser.parse_channel_status(data)
                ch = parsed['channel']
                print(
                    f"{Fore.WHITE}[{ts}] <<< CH{ch}_STATUS: V={parsed['voltage_mV']}mV "
                    f"I={parsed['current_mA']}mA ON={parsed['on']} PG={parsed['pg_good']}"
                )

        elif can_id == CAN_ID_STATUS_BATTERY:
            if log_rx:
                parsed = self.parser.parse_battery_status(data)
                print(
                    f"{Fore.WHITE}[{ts}] <<< BATTERY: V={parsed['voltage_mV']}mV "
                    f"I={parsed['current_mA']}mA SOC={parsed['soc_%']}%"
                )

        elif can_id == CAN_ID_STATUS_EFUSE:
            if log_rx:
                parsed = self.parser.parse_efuse_status(data)
                print(f"{Fore.WHITE}[{ts}] <<< EFUSE: {parsed}")

        elif can_id == CAN_ID_STATUS_THERMAL:
            if log_rx:
                parsed = self.parser.parse_thermal_status(data)
                print(
                    f"{Fore.WHITE}[{ts}] <<< THERMAL ch{parsed['channel']}: "
                    f"NTC={parsed['temp_mV']} OVER={parsed['is_over']}"
                )

        elif can_id == CAN_ID_ALERT_BATT_FAULT:
            self.alert_count += 1
            if log_rx:
                parsed = self.parser.parse_alert_batt_fault(data)
                print(f"{Fore.RED}[{ts}] <<< ⚠ ALERT_BATT: {parsed}")

        elif can_id == CAN_ID_ALERT_EFUSE_FAULT:
            self.alert_count += 1
            if log_rx:
                parsed = self.parser.parse_alert_efuse_fault(data)
                print(f"{Fore.RED}[{ts}] <<< ⚠ ALERT_EFUSE: {parsed}")

        elif can_id == CAN_ID_ALERT_VOLTAGE_ABNORM:
            self.alert_count += 1
            if log_rx:
                parsed = self.parser.parse_alert_voltage_abnorm(data)
                print(f"{Fore.RED}[{ts}] <<< ⚠ ALERT_VOLTAGE: {parsed}")

        else:
            if log_rx:
                hex_data = ' '.join(f'{b:02X}' for b in data)
                print(f"{Fore.MAGENTA}[{ts}] <<< UNKNOWN 0x{can_id:03X}: [{hex_data}]")
    
    # ---- 线程 ----
    
    def rx_thread_func(self):
        """接收线程"""
        while self.running:
            try:
                if self.transport == "udp":
                    if self.udp_sock is None:
                        break
                    try:
                        packet, _peer = self.udp_sock.recvfrom(4096)
                    except (TimeoutError, socket.timeout):
                        continue
                    parsed, err = parse_can_over_udp75(packet)
                    if parsed is None:
                        continue
                    msg = SimpleNamespace(
                        arbitration_id=can_id_base(parsed.can_id),
                        data=list(parsed.data[: parsed.dlc]),
                    )
                    self.process_frame(msg)
                else:
                    if self.bus_can is None:
                        break
                    msg = self.bus_can.recv(timeout=0.1)
                    if msg:
                        self.process_frame(msg)
            except Exception as exc:
                if self.running:
                    print(f"{Fore.RED}RX 错误: {exc}")
    
    def heartbeat_thread_func(self):
        """心跳线程"""
        while self.running:
            if self.heartbeat_enabled:
                self.send_heartbeat()
            time.sleep(self.heartbeat_interval)
    
    def start(self):
        """启动服务"""
        if self.transport == "udp":
            if self.udp_sock is None and not self.connect():
                return False
        elif self.bus_can is None and not self.connect():
            return False

        self.running = True

        self.rx_thread = threading.Thread(target=self.rx_thread_func, daemon=True)
        self.rx_thread.start()

        self.hb_thread = threading.Thread(target=self.heartbeat_thread_func, daemon=False)
        self.hb_thread.start()

        mode = (
            f"UDP listen {self.listen_host}:{self.listen_port} "
            f"-> MCU {self.mcu_ip}:{self.mcu_port}"
            if self.transport == "udp"
            else f"CAN {self.interface}:{self.channel}"
        )
        if not self.quiet:
            print(f"{Fore.GREEN}✓ 上位机已启动 ({mode})")
        return True
    
    def stop(self):
        """停止服务"""
        self.running = False
        time.sleep(0.2)
        self.disconnect()
        if not self.quiet:
            print(f"{Fore.YELLOW}上位机模拟器已停止")
    
    def print_stats(self):
        """打印统计信息"""
        print(f"\n{Fore.CYAN}=== 统计信息 ===")
        print(f"  TX: {self.tx_count}")
        print(f"  RX: {self.rx_count}")
        print(f"  告警: {self.alert_count}")
        print(f"  OTA状态帧: {self.ota_status_count}")
        if self.last_dev_heartbeat > 0:
            elapsed = time.time() - self.last_dev_heartbeat
            print(f"  上次设备心跳: {elapsed:.1f}s 前")

# ================================================================
# 交互式命令行
# ================================================================

def print_help():
    """打印帮助信息"""
    print(f"""
{Fore.CYAN}=== Thor PMU 上位机 / OTA ==={Style.RESET_ALL}
默认传输: CAN-over-UDP 75B（bind :40189, sendto MCU :40188）

{Fore.YELLOW}命令列表:{Style.RESET_ALL}
  1, on        发送开机命令 (POWER_ON)
  2, off       发送关机命令 (POWER_OFF)
  3, clear     发送清除故障命令 (CLEAR_FAULT)
  4, sleep     发送进入睡眠命令 (ENTER_SLEEP)
  5, wake      发送唤醒命令 (WAKE_UP)
  6, apply     触发 OTA 应用重启 (APPLY_OTA)
  7, id        发送获取版本ID命令 (GET_ID)
  
  na           发送协商回复: APPROVED
  nr           发送协商回复: REJECTED
   nd           发送协商回复: DELAY
   
   ts           手动触发时间同步 (发送 TIME_SET 帧)
   time         查看上位机时间并同步到 MCU
   
   hb           手动发送一次心跳
  hb on/off    开启/关闭自动心跳
  auto on/off  开启/关闭自动协商回复
  view ota/full  仅OTA相关日志(默认) / 显示全部收发

    ota <file> [chunk]   一键OTA升级(默认chunk=6, 1..6)
    ota start <size> <pkts> [sid]
    ota data <seq> <hex...>
    ota end
    ota abort [reason]
  
  stat         显示统计信息
  raw ID D0 D1 ...  发送原始帧 (ID 和数据为十六进制)
  
  h, help      显示帮助
  q, quit      退出
""")

def interactive_loop(host):
    """交互式命令循环"""
    print_help()
    
    while True:
        try:
            raw_cmd = input(f"{Fore.GREEN}> {Style.RESET_ALL}").strip()
            cmd = raw_cmd.lower()
            
            if not cmd:
                continue
            
            # 退出
            if cmd in ('q', 'quit', 'exit'):
                break
            
            # 帮助
            elif cmd in ('h', 'help', '?'):
                print_help()
            
            # 开机
            elif cmd in ('1', 'on', 'power_on'):
                host.send_power_on()
            
            # 关机
            elif cmd in ('2', 'off', 'power_off'):
                host.send_power_off()
            
            # 清除故障
            elif cmd in ('3', 'clear', 'clear_fault'):
                host.send_clear_fault()
            
            # 睡眠
            elif cmd in ('4', 'sleep', 'enter_sleep'):
                host.send_enter_sleep()
            
            # 唤醒
            elif cmd in ('5', 'wake', 'wake_up'):
                host.send_wake_up()

            elif cmd in ('6', 'apply', 'apply_ota'):
                ack = host.apply_ota_and_wait()
                if ack is None:
                    print(f"{Fore.RED}APPLY_OTA 未收到 ACK（超时）")
                elif ack.get('result') == 'OK':
                    print(f"{Fore.GREEN}APPLY_OTA ACK: {ack}")
                else:
                    print(f"{Fore.RED}APPLY_OTA ACK: {ack}")

            # 获取版本ID
            elif cmd in ('7', 'id', 'get_id'):
                version = host.fetch_firmware_version()
                if version is None:
                    print(f"{Fore.RED}读取固件版本失败")
                else:
                    print(version)
            
            # 协商回复
            elif cmd == 'na':
                host.send_negotiate_reply(NEGOTIATE_RESULT_APPROVED)
            elif cmd == 'nr':
                host.send_negotiate_reply(NEGOTIATE_RESULT_REJECTED)
            elif cmd == 'nd':
                host.send_negotiate_reply(NEGOTIATE_RESULT_DELAY)
            
            # 时间同步 / 查看时间
            elif cmd == 'ts':
                host.send_time_set()
            elif cmd in ('time', 'now'):
                now = datetime.now()
                print(f"{Fore.CYAN}上位机时间: {now.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"(unix={int(time.time())})")
                host.send_time_set()
            
            # 心跳控制
            elif cmd == 'hb':
                host.send_heartbeat()
                print(f"{Fore.CYAN}>>> TX HEARTBEAT")
            elif cmd == 'hb on':
                host.heartbeat_enabled = True
                print(f"{Fore.GREEN}自动心跳已开启")
            elif cmd == 'hb off':
                host.heartbeat_enabled = False
                print(f"{Fore.YELLOW}自动心跳已关闭")
            
            # 自动协商回复控制
            elif cmd == 'auto on':
                host.auto_negotiate_reply = True
                print(f"{Fore.GREEN}自动协商回复已开启")
            elif cmd == 'auto off':
                host.auto_negotiate_reply = False
                print(f"{Fore.YELLOW}自动协商回复已关闭")

            # 打印视图控制
            elif cmd in ('view ota', 'view compact', 'vo', 'vc'):
                host.log_ota_only = True
                print(f"{Fore.YELLOW}已切换为 ota 视图: 仅打印 OTA 相关收发")
            elif cmd in ('view full', 'vf'):
                host.log_ota_only = False
                print(f"{Fore.GREEN}已切换为 full 视图: 显示所有帧")

            # OTA 模拟
            elif cmd.startswith('ota '):
                parts_raw = raw_cmd.split()
                parts = cmd.split()

                # ota <file> [chunk]
                if len(parts) >= 2 and parts[1] not in ('start', 'data', 'end', 'abort'):
                    file_path = parts_raw[1]
                    chunk_size = 6
                    if len(parts_raw) >= 3:
                        try:
                            chunk_size = int(parts_raw[2], 10)
                        except ValueError:
                            print(f"{Fore.RED}chunk 参数必须是整数，示例: ota fw.bin 6")
                            continue
                    host.ota_upgrade_from_file(file_path, chunk_size=chunk_size)

                # ota start <size> <pkts> [sid]
                elif len(parts) >= 4 and parts[1] == 'start':
                    size = int(parts[2], 10)
                    pkts = int(parts[3], 10)
                    sid = int(parts[4], 10) if len(parts) >= 5 else None
                    host.send_ota_start(image_size=size, total_packets=pkts, session_id=sid)

                # ota data <seq> <hex...>
                elif len(parts) >= 4 and parts[1] == 'data':
                    seq = int(parts[2], 10)
                    payload = [int(x, 16) for x in parts[3:]]
                    host.send_ota_data(seq, payload)

                # ota end
                elif len(parts) == 2 and parts[1] == 'end':
                    host.send_ota_end()

                # ota abort [reason]
                elif len(parts) >= 2 and parts[1] == 'abort':
                    reason = int(parts[2], 0) if len(parts) >= 3 else 0
                    host.send_ota_abort(reason)

                else:
                    print(f"{Fore.RED}用法: ota <file> [chunk] | ota start <size> <pkts> [sid] | ota data <seq> <hex...> | ota end | ota abort [reason]")
            
            # 统计
            elif cmd in ('stat', 'stats', 'status'):
                host.print_stats()
            
            # 原始帧发送
            elif cmd.startswith('raw '):
                parts = cmd.split()[1:]
                if len(parts) >= 1:
                    try:
                        can_id = int(parts[0], 16)
                        data = [int(x, 16) for x in parts[1:9]]
                        data.extend([0] * (8 - len(data)))
                        if host.send_frame(can_id, data):
                            hex_data = ' '.join(f'{b:02X}' for b in data)
                            print(f"{Fore.CYAN}>>> TX RAW 0x{can_id:03X}: [{hex_data}]")
                    except ValueError as e:
                        print(f"{Fore.RED}格式错误: {e}")
                else:
                    print(f"{Fore.RED}用法: raw <ID> [D0] [D1] ... (十六进制)")
            
            else:
                print(f"{Fore.RED}未知命令: {cmd} (输入 'h' 查看帮助)")
                
        except KeyboardInterrupt:
            print()
            break
        except EOFError:
            break

# ================================================================
# 主程序
# ================================================================

def run_get_version_probe(host: "ThorCanHost") -> int:
    """一次性读取固件版本：仅启动 RX 收包，读完立即断开并退出进程（不占用 40189）。"""
    if not host.connect():
        return 1

    host.running = True
    host.rx_thread = threading.Thread(target=host.rx_thread_func, daemon=True)
    host.rx_thread.start()
    try:
        version = host.fetch_firmware_version()
        if version is None:
            print(
                f"{Fore.RED}✗ 未收到 GET_ID 应答（5s 超时）："
                f"MCU({host.mcu_ip}:{host.mcu_port}) 的 PMU-CAN 桥可能未在线/未重启，"
                f"或 PMU 电源板无响应",
                file=sys.stderr,
            )
            return 1
        print(version)
        return 0
    finally:
        host.running = False
        if host.rx_thread.is_alive():
            host.rx_thread.join(timeout=0.5)
        host.disconnect()


def run_ota_flash(host: "ThorCanHost", firmware_path: str, chunk_size: int = 6) -> int:
    """一次性 OTA 刷写：传输固件、等待 READY_REBOOT 后自动 APPLY_OTA，完成后退出。"""
    if not host.connect():
        return 1

    host.running = True
    host.rx_thread = threading.Thread(target=host.rx_thread_func, daemon=True)
    host.rx_thread.start()
    try:
        ok = host.ota_upgrade_from_file(
            firmware_path,
            chunk_size=chunk_size,
            auto_apply=True,
        )
        return 0 if ok else 1
    finally:
        host.running = False
        if host.rx_thread.is_alive():
            host.rx_thread.join(timeout=0.5)
        host.disconnect()

def run_apply_ota_probe(host: "ThorCanHost") -> int:
    """一次性发送 APPLY_OTA：等待 0x200 ACK 后退出（成功 exit 0，失败/超时 exit 1）。"""
    if not host.connect():
        return 1

    host.running = True
    host.rx_thread = threading.Thread(target=host.rx_thread_func, daemon=True)
    host.rx_thread.start()
    try:
        ack = host.apply_ota_and_wait()
        if ack is None:
            print("APPLY_OTA ACK timeout", flush=True)
            return 1
        print(f"APPLY_OTA ACK: {ack.get('result', ack)}", flush=True)
        return 0 if ack.get('result') == 'OK' else 1
    finally:
        host.running = False
        if host.rx_thread.is_alive():
            host.rx_thread.join(timeout=0.5)
        host.disconnect()


def pmu_main(argv=None):
    parser = argparse.ArgumentParser(
        description="Thor PMU 上位机 / OTA（默认 UDP 75B）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "-t",
        "--transport",
        choices=("udp", "can"),
        default="udp",
        help="传输方式: udp=75B CAN-over-UDP（默认）, can=直连 CAN 总线",
    )
    parser.add_argument(
        "--listen-host",
        default=DEFAULT_LISTEN_HOST,
        help=f"UDP 本机监听地址（默认 {DEFAULT_LISTEN_HOST}）",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help=f"UDP 本机监听端口，收 MCU 回包（默认 {DEFAULT_LISTEN_PORT}）",
    )
    parser.add_argument(
        "--mcu-ip",
        default=DEFAULT_MCU_IP,
        help=f"MCU UDP 目标 IP（默认 {DEFAULT_MCU_IP}）",
    )
    parser.add_argument(
        "--mcu-port",
        type=int,
        default=DEFAULT_MCU_PORT,
        help=f"MCU UDP 目标端口（默认 {DEFAULT_MCU_PORT}）",
    )
    parser.add_argument(
        "--bus",
        type=int,
        default=DEFAULT_BUS_ID,
        help="75B 帧 bus_id 字段（默认 0）",
    )
    parser.add_argument(
        "-i",
        "--interface",
        default="socketcan",
        help="[-t can] CAN 接口类型 (socketcan/pcan/virtual/...)",
    )
    parser.add_argument(
        "-c",
        "--channel",
        default="can0",
        help="[-t can] CAN 通道",
    )
    parser.add_argument(
        "-b",
        "--bitrate",
        type=int,
        default=1000000,
        help="[-t can] CAN 波特率",
    )
    parser.add_argument(
        "-j",
        "--get-version",
        action="store_true",
        help="非交互读取固件版本，向 stdout 输出解析后的版本字符串",
    )
    parser.add_argument(
        "-f",
        "--firmware",
        metavar="BIN",
        help="非交互 OTA 刷写：指定固件 bin 文件路径，完成后自动 APPLY_OTA 重启",
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=6,
        help="[-f] OTA 每包 payload 字节数（1..6，默认 6）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="非交互发送 APPLY_OTA，等待 0x200 ACK 后退出（成功 0 / 失败或超时 1）",
    )
    args = parser.parse_args(argv)

    if args.get_version and (args.firmware or args.apply):
        parser.error("--get-version 不能与 --firmware/--apply 同时使用")
    if args.firmware and args.apply:
        parser.error("--firmware 与 --apply 不能同时使用")

    batch_mode = args.get_version or bool(args.firmware) or args.apply

    if not batch_mode:
        print(f"{Fore.CYAN}Thor PMU 上位机 / OTA{Style.RESET_ALL}")
        if args.transport == "udp":
            print(
                f"传输: UDP 75B | listen {args.listen_host}:{args.listen_port} "
                f"| MCU {args.mcu_ip}:{args.mcu_port} | bus={args.bus}"
            )
        else:
            print(
                f"传输: CAN | {args.interface}:{args.channel} @ {args.bitrate}"
            )
        print()

    host = ThorCanHost(
        transport=args.transport,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        mcu_ip=args.mcu_ip,
        mcu_port=args.mcu_port,
        bus=args.bus,
        interface=args.interface,
        channel=args.channel,
        bitrate=args.bitrate,
        quiet=batch_mode,
    )

    if args.get_version:
        return run_get_version_probe(host)

    if args.firmware:
        return run_ota_flash(host, args.firmware, chunk_size=args.chunk)

    if args.apply:
        return run_apply_ota_probe(host)

    if not host.start():
        return 1

    try:
        interactive_loop(host)
    finally:
        host.stop()

    return 0


# ================================================================
# rh850_udp_ota (内联)

# ================================================================

#!/usr/bin/env python3
"""RH850 Thor UDP OTA tool (ZYT Link V1).

Upgrades vip_fullmem_app_core0 and vip_fullmem_app_core1 in one run.
See README_rh850_udp_ota.md and ../RH850_UDP_OTA_Protocol.md.
"""


import argparse
import hashlib
import math
import os
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional, Tuple

# --- Network (fixed per protocol doc) ---
LOCAL_IP = "192.168.1.85"
LOCAL_PORT = 15000
MCU_IP = "192.168.1.20"
MCU_PORT = 15000
DEFAULT_IFACE = "lan0"

DEFAULT_FIRMWARE_DIR = os.path.dirname(os.path.abspath(__file__))
CORE0_BIN_NAME = "vip_fullmem_app_core0.bin"
CORE1_BIN_NAME = "vip_fullmem_app_core1.bin"

PACKAGE_SIZE = 512
COM_CRC16_INIT = 0x3692

# Command IDs
CMD_FIRM_INFO = 0xFF
CMD_DEVICE_STATUS = 0x0C
CMD_FW_ENTRY = 0x07
CMD_FW_START = 0x08
CMD_FW_DATA = 0x09
CMD_FW_FINISH = 0x0A
CMD_REBOOT = 0x0B
CMD_SLOT_OP = 0x0E

PARTITION_CORE0 = "vip_fullmem_app_core0"
PARTITION_CORE1 = "vip_fullmem_app_core1"

FW_ENTRY_PAYLOAD = bytes.fromhex("00adfa7620525d0000")
REBOOT_PAYLOAD = bytes.fromhex("000000000000e0ed7620525d0000")
# Wire send_index presets from successful capture (session_seq still increments each packet)
CAPTURE_FIRM_INFO_REQ = bytes.fromhex("550d0433000300004000ff91c1")

SEND_INDEX = {
    "firm_info": 0x33,
    "device_status": 0x66,
    "fw_entry": 0xFC,
    "fw_start": 0x2A,
    "fw_data": 0xD1,
    "fw_finish": 0x8A,
    "slot_op": 0xA2,
    "reboot": 0x75,
}
CRC16_TABLE = [
    0x0000, 0x1189, 0x2312, 0x329b, 0x4624, 0x57ad, 0x6536, 0x74bf,
    0x8c48, 0x9dc1, 0xaf5a, 0xbed3, 0xca6c, 0xdbe5, 0xe97e, 0xf8f7,
    0x1081, 0x0108, 0x3393, 0x221a, 0x56a5, 0x472c, 0x75b7, 0x643e,
    0x9cc9, 0x8d40, 0xbfdb, 0xae52, 0xdaed, 0xcb64, 0xf9ff, 0xe876,
    0x2102, 0x308b, 0x0210, 0x1399, 0x6726, 0x76af, 0x4434, 0x55bd,
    0xad4a, 0xbcc3, 0x8e58, 0x9fd1, 0xeb6e, 0xfae7, 0xc87c, 0xd9f5,
    0x3183, 0x200a, 0x1291, 0x0318, 0x77a7, 0x662e, 0x54b5, 0x453c,
    0xbdcb, 0xac42, 0x9ed9, 0x8f50, 0xfbef, 0xea66, 0xd8fd, 0xc974,
    0x4204, 0x538d, 0x6116, 0x709f, 0x0420, 0x15a9, 0x2732, 0x36bb,
    0xce4c, 0xdfc5, 0xed5e, 0xfcd7, 0x8868, 0x99e1, 0xab7a, 0xbaf3,
    0x5285, 0x430c, 0x7197, 0x601e, 0x14a1, 0x0528, 0x37b3, 0x263a,
    0xdecd, 0xcf44, 0xfddf, 0xec56, 0x98e9, 0x8960, 0xbbfb, 0xaa72,
    0x6306, 0x728f, 0x4014, 0x519d, 0x2522, 0x34ab, 0x0630, 0x17b9,
    0xef4e, 0xfec7, 0xcc5c, 0xddd5, 0xa96a, 0xb8e3, 0x8a78, 0x9bf1,
    0x7387, 0x620e, 0x5095, 0x411c, 0x35a3, 0x242a, 0x16b1, 0x0738,
    0xffcf, 0xee46, 0xdcdd, 0xcd54, 0xb9eb, 0xa862, 0x9af9, 0x8b70,
    0x8408, 0x9581, 0xa71a, 0xb693, 0xc22c, 0xd3a5, 0xe13e, 0xf0b7,
    0x0840, 0x19c9, 0x2b52, 0x3adb, 0x4e64, 0x5fed, 0x6d76, 0x7cff,
    0x9489, 0x8500, 0xb79b, 0xa612, 0xd2ad, 0xc324, 0xf1bf, 0xe036,
    0x18c1, 0x0948, 0x3bd3, 0x2a5a, 0x5ee5, 0x4f6c, 0x7df7, 0x6c7e,
    0xa50a, 0xb483, 0x8618, 0x9791, 0xe32e, 0xf2a7, 0xc03c, 0xd1b5,
    0x2942, 0x38cb, 0x0a50, 0x1bd9, 0x6f66, 0x7eef, 0x4c74, 0x5dfd,
    0xb58b, 0xa402, 0x9699, 0x8710, 0xf3af, 0xe226, 0xd0bd, 0xc134,
    0x39c3, 0x284a, 0x1ad1, 0x0b58, 0x7fe7, 0x6e6e, 0x5cf5, 0x4d7c,
    0xc60c, 0xd785, 0xe51e, 0xf497, 0x8028, 0x91a1, 0xa33a, 0xb2b3,
    0x4a44, 0x5bcd, 0x6956, 0x78df, 0x0c60, 0x1de9, 0x2f72, 0x3efb,
    0xd68d, 0xc704, 0xf59f, 0xe416, 0x90a9, 0x8120, 0xb3bb, 0xa232,
    0x5ac5, 0x4b4c, 0x79d7, 0x685e, 0x1ce1, 0x0d68, 0x3ff3, 0x2e7a,
    0xe70e, 0xf687, 0xc41c, 0xd595, 0xa12a, 0xb0a3, 0x8238, 0x93b1,
    0x6b46, 0x7acf, 0x4854, 0x59dd, 0x2d62, 0x3ceb, 0x0e70, 0x1ff9,
    0xf78f, 0xe606, 0xd49d, 0xc514, 0xb1ab, 0xa022, 0x92b9, 0x8330,
    0x7bc7, 0x6a4e, 0x58d5, 0x495c, 0x3de3, 0x2c6a, 0x1ef1, 0x0f78,
]


class OtaError(Exception):
    """OTA protocol or transport error."""


def setup_network(iface: str = DEFAULT_IFACE) -> None:
    """Configure local IP and host route to MCU before UDP traffic."""
    addr_cmd = ["sudo", "ip", "addr", "add", f"{LOCAL_IP}/24", "dev", iface]
    route_cmd = [
        "sudo",
        "ip",
        "route",
        "replace",
        f"{MCU_IP}/32",
        "dev",
        iface,
        "src",
        LOCAL_IP,
    ]
    result = subprocess.run(addr_cmd, capture_output=True, text=True)
    if result.returncode != 0 and "File exists" not in (result.stderr or ""):
        raise OtaError(
            f"ip addr add failed: {(result.stderr or result.stdout).strip()}"
        )
    result = subprocess.run(route_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise OtaError(
            f"ip route replace failed: {(result.stderr or result.stdout).strip()}"
        )


def teardown_network(iface: str = DEFAULT_IFACE) -> None:
    """还原 setup_network 的副作用：删除它加的 {MCU_IP}/32 主机路由。

    不删 LOCAL_IP 地址（可能是系统既有的；地址本身不劫持路由，对其它工具无害）。
    删这条主机路由是为了让后续走默认路由的工具（如 PMU get-version，它要经 MCU
    的 PMU-CAN 桥）不被 lan0 路径劫持而 5s 超时。失败静默（路由可能已不存在）。
    """
    for spec in (
        [f"{MCU_IP}/32", "dev", iface, "src", LOCAL_IP],
        [f"{MCU_IP}/32", "dev", iface],
    ):
        subprocess.run(
            ["sudo", "ip", "route", "del", *spec],
            capture_output=True,
            text=True,
        )


def crc16(data: bytes, init: int = COM_CRC16_INIT) -> int:
    crc = init
    for byte in data:
        crc = ((crc >> 8) & 0xFF) ^ CRC16_TABLE[(crc & 0xFF) ^ byte]
    return crc & 0xFFFF


def append_crc(body: bytes) -> bytes:
    c = crc16(body)
    return body + bytes([c & 0xFF, (c >> 8) & 0xFF])


def verify_crc(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    body = frame[:-2]
    got = frame[-2] | (frame[-1] << 8)
    return crc16(body) == got


def _frame_len_value(cmd_id: int, total_with_crc: int) -> int:
    """Wire frame_len: FW_DATA uses 0x14; all others equal total frame size."""
    if cmd_id == CMD_FW_DATA:
        return 0x14
    return total_with_crc


@dataclass
class ParsedFrame:
    send_id: int
    send_index: int
    recv_id: int
    recv_index: int
    cmd_set: int
    pack_type: int
    cmd_id: int
    payload: bytes


def parse_frame(frame: bytes) -> ParsedFrame:
    if len(frame) < 13 or frame[0] != 0x55:
        raise OtaError(f"invalid frame header (len={len(frame)})")
    if not verify_crc(frame):
        raise OtaError("CRC mismatch on received frame")
    return ParsedFrame(
        send_id=frame[2],
        send_index=frame[3],
        recv_id=frame[4],
        recv_index=frame[5],
        cmd_set=frame[6],
        pack_type=frame[8],
        cmd_id=frame[10],
        payload=frame[11:-2],
    )


class LinkV1Client:
    """ZYT Link V1 over UDP."""

    def __init__(
        self,
        timeout: float,
        verbose: bool = False,
        bind_iface: Optional[str] = None,
    ):
        self.timeout = timeout
        self.verbose = verbose
        self.bind_iface = bind_iface
        # Wire capture: recv_index stays 0x03; byte[6] session_seq increments per PC packet.
        self.send_index = 0x33
        self.recv_index = 0x03
        self.session_seq = 0
        self._sock: Optional[socket.socket] = None
        self._last_tx: bytes = b""
        self._last_rx: bytes = b""

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.bind_iface:
            if not hasattr(socket, "SO_BINDTODEVICE"):
                raise OtaError("SO_BINDTODEVICE not available on this platform")
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    self.bind_iface.encode() + b"\0",
                )
            except OSError as exc:
                raise OtaError(
                    f"cannot bind to interface {self.bind_iface!r} "
                    f"(try sudo, or check `ip link`)"
                ) from exc
        try:
            sock.bind((LOCAL_IP, LOCAL_PORT))
        except OSError as exc:
            raise OtaError(
                f"cannot bind {LOCAL_IP}:{LOCAL_PORT} — add {LOCAL_IP} on lan0; "
                f"check: ss -ulnp | grep {LOCAL_PORT}"
            ) from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        bound = sock.getsockname()
        if self.verbose or self.bind_iface:
            iface_msg = f", iface={self.bind_iface}" if self.bind_iface else ""
            print(
                f"UDP bound {bound[0]}:{bound[1]} -> {MCU_IP}:{MCU_PORT}{iface_msg}"
            )

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def _build_body(
        self,
        cmd_id: int,
        payload: bytes,
        *,
        send_id: int = 0x04,
        cmd_set: int = 0x00,
        pack_type: int = 0x40,
        attr7: int = 0x00,
        attr9: int = 0x00,
    ) -> bytes:
        total = 11 + len(payload) + 2
        flen = _frame_len_value(cmd_id, total)
        header = struct.pack(
            "<BBBBBBBBBBB",
            0x55,
            flen & 0xFF,
            send_id & 0xFF,
            self.send_index & 0xFF,
            0x00,
            self.recv_index & 0xFF,
            cmd_set & 0xFF,
            attr7 & 0xFF,
            pack_type & 0xFF,
            attr9 & 0xFF,
            cmd_id & 0xFF,
        )
        return header + payload

    def build_frame(
        self,
        cmd_id: int,
        payload: bytes,
        *,
        send_id: int = 0x04,
        cmd_set: int = 0x00,
        pack_type: int = 0x40,
        attr7: int = 0x00,
        attr9: int = 0x00,
    ) -> bytes:
        body = self._build_body(
            cmd_id,
            payload,
            send_id=send_id,
            cmd_set=cmd_set,
            pack_type=pack_type,
            attr7=attr7,
            attr9=attr9,
        )
        return append_crc(body)

    def _advance_session(self) -> None:
        """Increment session_seq (wire byte6); recv_index stays 0x03."""
        self.session_seq = (self.session_seq + 1) & 0xFF

    def _use_send_index(self, key: str) -> None:
        self.send_index = SEND_INDEX[key]

    def _recv_until_ack(
        self, expect_cmd_id: int, deadline: float
    ) -> Tuple[bytes, Tuple[str, int]]:
        assert self._sock is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout
            self._sock.settimeout(remaining)
            data, addr = self._sock.recvfrom(4096)
            if addr[0] != MCU_IP:
                if self.verbose:
                    print(f"  ignore UDP from {addr[0]}:{addr[1]}")
                continue
            if len(data) >= 11 and data[0] == 0x55 and data[10] == expect_cmd_id:
                return data, addr
            if self.verbose:
                print(
                    f"  ignore {len(data)}B from {addr[0]}:{addr[1]} "
                    f"cmd=0x{data[10]:02X}"
                )

    def transact_raw(
        self,
        tx: bytes,
        expect_cmd_id: int,
        *,
        timeout: Optional[float] = None,
    ) -> ParsedFrame:
        """Send prebuilt wire frame (e.g. capture replay)."""
        if not self._sock:
            raise OtaError("UDP socket not open")
        self._last_tx = tx
        if self.verbose:
            print(f"  TX raw ({len(tx)}B): {tx.hex()}")
        wait = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + wait
        try:
            self._sock.sendto(tx, (MCU_IP, MCU_PORT))
            data, addr = self._recv_until_ack(expect_cmd_id, deadline)
        except socket.timeout as exc:
            raise OtaError(
                f"timeout waiting ACK for cmd 0x{expect_cmd_id:02X} "
                f"(last TX: {tx.hex()})"
            ) from exc
        self._last_rx = data
        if self.verbose:
            print(f"  RX ({len(data)}B) from {addr[0]}:{addr[1]}: {data.hex()}")
        rx = parse_frame(data)
        if (rx.pack_type & 0x80) == 0:
            raise OtaError(f"not an ACK frame (pack_type=0x{rx.pack_type:02X})")
        self._advance_session()
        return rx

    def transact(
        self,
        cmd_id: int,
        payload: bytes,
        *,
        send_id: int = 0x04,
        pack_type: int = 0x40,
        attr7: int = 0x00,
        attr9: int = 0x00,
        timeout: Optional[float] = None,
    ) -> ParsedFrame:
        if not self._sock:
            raise OtaError("UDP socket not open")

        tx = self.build_frame(
            cmd_id,
            payload,
            send_id=send_id,
            cmd_set=self.session_seq,
            pack_type=pack_type,
            attr7=attr7,
            attr9=attr9,
        )
        self._last_tx = tx
        if self.verbose:
            print(f"  TX ({len(tx)}B): {tx.hex()}")

        wait = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + wait
        try:
            self._sock.sendto(tx, (MCU_IP, MCU_PORT))
            data, addr = self._recv_until_ack(cmd_id, deadline)
        except socket.timeout as exc:
            raise OtaError(
                f"timeout waiting ACK for cmd 0x{cmd_id:02X} (last TX: {tx.hex()})\n"
                f"Hints: sudo --iface lan0; ss -ulnp | grep {LOCAL_PORT}; "
                f"tcpdump -ni lan0 host {MCU_IP} and udp port {LOCAL_PORT}; "
                f"try --replay; compare ./mcu_update"
            ) from exc

        self._last_rx = data
        if self.verbose:
            print(f"  RX ({len(data)}B) from {addr[0]}:{addr[1]}: {data.hex()}")

        rx = parse_frame(data)
        if (rx.pack_type & 0x80) == 0:
            raise OtaError(f"not an ACK frame (pack_type=0x{rx.pack_type:02X})")
        self._advance_session()
        return rx

    def cmd_firm_info(self) -> str:
        self._use_send_index("firm_info")
        rx = self.transact(CMD_FIRM_INFO, b"")
        try:
            return rx.payload.decode("ascii")
        except UnicodeDecodeError:
            return rx.payload.hex()

    def cmd_device_status(self) -> int:
        self._use_send_index("device_status")
        rx = self.transact(
            CMD_DEVICE_STATUS,
            b"\x10",
            pack_type=0x20,
        )
        if len(rx.payload) < 6:
            raise OtaError(f"DEVICE_STATUS ACK too short: {rx.payload.hex()}")
        if rx.payload[0] != 0:
            raise OtaError(f"DEVICE_STATUS result={rx.payload[0]}")
        return struct.unpack_from("<I", rx.payload, 2)[0]

    def cmd_fw_entry(self) -> None:
        self._use_send_index("fw_entry")
        rx = self.transact(
            CMD_FW_ENTRY,
            FW_ENTRY_PAYLOAD,
            pack_type=0x20,
        )
        if not rx.payload or rx.payload[0] != 0:
            raise OtaError(f"FW_ENTRY failed result={rx.payload[:1].hex()}")

    def cmd_fw_start(self, partition_id: str, firmware: bytes) -> int:
        pid = partition_id.encode("ascii")
        if len(pid) > 64:
            raise OtaError(f"partition id too long: {partition_id}")
        id_field = pid + b"\x00" * (64 - len(pid))
        payload = struct.pack(
            "<BIB5sBB",
            0,
            len(firmware),
            0,
            b"\x00" * 5,
            1,
            0,
        ) + id_field
        self._use_send_index("fw_start")
        rx = self.transact(CMD_FW_START, payload)
        if len(rx.payload) < 3:
            raise OtaError(f"FW_START ACK too short: {rx.payload.hex()}")
        result = rx.payload[0]
        data_size = struct.unpack_from("<H", rx.payload, 1)[0]
        if result != 0:
            raise OtaError(f"FW_START failed result=0x{result:02X}")
        return data_size

    def cmd_fw_data(
        self, index: int, chunk: bytes, *, timeout: Optional[float] = None
    ) -> None:
        if len(chunk) != PACKAGE_SIZE:
            raise OtaError(f"chunk size must be {PACKAGE_SIZE}, got {len(chunk)}")
        payload = struct.pack("<BiH", 0, index, PACKAGE_SIZE) + chunk
        if index == 0:
            self._use_send_index("fw_data")
        rx = self.transact(
            CMD_FW_DATA,
            payload,
            send_id=0x06,
            pack_type=0x20,
            timeout=timeout,
        )
        if len(rx.payload) < 5:
            raise OtaError(f"FW_DATA ACK too short: {rx.payload.hex()}")
        result = rx.payload[0]
        ack_idx = struct.unpack_from("<I", rx.payload, 1)[0]
        if result != 0:
            raise OtaError(
                f"FW_DATA index={index} failed result=0x{result:02X}"
            )
        if ack_idx != index:
            raise OtaError(
                f"FW_DATA index mismatch: sent {index}, ack {ack_idx}"
            )

    def cmd_fw_finish(self, md5_digest: bytes) -> None:
        if len(md5_digest) != 16:
            raise OtaError("MD5 must be 16 bytes")
        payload = b"\x00" + md5_digest
        self._use_send_index("fw_finish")
        rx = self.transact(CMD_FW_FINISH, payload, pack_type=0x20)
        if not rx.payload or rx.payload[0] != 0:
            raise OtaError(f"FW_FINISH failed result={rx.payload[:1].hex()}")

    def cmd_slot_op(self, op_type: int, target_slot: int = 0) -> Tuple[int, int, int]:
        payload = struct.pack("<BB", op_type & 0xFF, target_slot & 0xFF)
        self._use_send_index("slot_op")
        rx = self.transact(CMD_SLOT_OP, payload, pack_type=0x20)
        if len(rx.payload) < 6:
            raise OtaError(f"SLOT_OP ACK too short: {rx.payload.hex()}")
        ret, cur, tgt = struct.unpack_from("<iBB", rx.payload, 0)
        return ret, cur, tgt

    def cmd_reboot(self) -> None:
        self._use_send_index("reboot")
        rx = self.transact(CMD_REBOOT, REBOOT_PAYLOAD, pack_type=0x20)
        if not rx.payload or rx.payload[0] != 0:
            raise OtaError(f"REBOOT failed result={rx.payload[:1].hex()}")


@dataclass
class FirmwareImage:
    path: str
    partition_id: str
    data: bytes
    md5: bytes
    num_packets: int


def resolve_firmware_paths(
    firmware_dir: str,
    core0: Optional[str] = None,
    core1: Optional[str] = None,
) -> Tuple[str, str]:
    """Resolve core0/core1 bins from a directory or explicit overrides."""
    if core0 and core1:
        return core0, core1

    abs_dir = os.path.abspath(firmware_dir)
    if not os.path.isdir(abs_dir):
        raise OtaError(f"firmware directory not found: {abs_dir}")

    c0 = core0 or os.path.join(abs_dir, CORE0_BIN_NAME)
    c1 = core1 or os.path.join(abs_dir, CORE1_BIN_NAME)

    missing = []
    if not os.path.isfile(c0):
        missing.append(CORE0_BIN_NAME if not core0 else c0)
    if not os.path.isfile(c1):
        missing.append(CORE1_BIN_NAME if not core1 else c1)
    if missing:
        raise OtaError(
            f"missing firmware file(s) in {abs_dir}: {', '.join(missing)}"
        )
    return c0, c1


def load_firmware(path: str, partition_id: str) -> FirmwareImage:
    if not os.path.isfile(path):
        raise OtaError(f"firmware not found: {path}")
    with open(path, "rb") as f:
        data = f.read()
    if len(data) == 0:
        raise OtaError(f"empty firmware: {path}")
    md5 = hashlib.md5(data).digest()
    n = math.ceil(len(data) / PACKAGE_SIZE)
    return FirmwareImage(
        path=path,
        partition_id=partition_id,
        data=data,
        md5=md5,
        num_packets=n,
    )


def pad_packet(data: bytes, index: int) -> bytes:
    offset = index * PACKAGE_SIZE
    chunk = data[offset : offset + PACKAGE_SIZE]
    if len(chunk) < PACKAGE_SIZE:
        chunk = chunk + b"\xFF" * (PACKAGE_SIZE - len(chunk))
    return chunk


def upgrade_partition(
    client: LinkV1Client,
    image: FirmwareImage,
    *,
    data_timeout: float,
    progress_label: str,
) -> None:
    print(
        f"\n[{progress_label}] {image.partition_id}: "
        f"{len(image.data)} bytes, {image.num_packets} packets, "
        f"MD5={image.md5.hex()}"
    )
    print(f"  FW_START ({os.path.basename(image.path)})...")
    pkg = client.cmd_fw_start(image.partition_id, image.data)
    if pkg != PACKAGE_SIZE:
        print(f"  warning: MCU data_size={pkg}, expected {PACKAGE_SIZE}")

    report_step = max(1, image.num_packets // 100)
    t0 = time.time()
    for i in range(image.num_packets):
        chunk = pad_packet(image.data, i)
        client.cmd_fw_data(i, chunk, timeout=data_timeout)
        if (i + 1) % report_step == 0 or i == image.num_packets - 1:
            pct = (i + 1) * 100 // image.num_packets
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(
                f"\r  {progress_label}: {i + 1}/{image.num_packets} "
                f"({pct}%) {rate:.1f} pkt/s",
                end="",
                flush=True,
            )
    print()
    print("  FW_FINISH...")
    client.cmd_fw_finish(image.md5)
    print(f"  [{progress_label}] done.")


def run_crc_self_test() -> None:
    samples = {
        "FIRM_INFO_req": "550d0433000300004000ff91c1",
        "START_ack": "5510045603000300c00008000002396e",
        "DATA_ack": "551204c703000400a0000900000000002e44",
    }
    for name, hx in samples.items():
        frame = bytes.fromhex(hx)
        if not verify_crc(frame):
            raise OtaError(f"CRC self-test failed: {name}")
    print("CRC16 self-test: OK")


def run_probe(
    *,
    timeout: float,
    verbose: bool,
    bind_iface: Optional[str],
    replay: bool,
) -> None:
    """Send only FIRM_INFO to verify UDP 15000 path."""
    print(f"local  {LOCAL_IP}:{LOCAL_PORT}")
    print(f"remote {MCU_IP}:{MCU_PORT}")
    print_route_hint()
    client = LinkV1Client(
        timeout=timeout, verbose=verbose, bind_iface=bind_iface
    )
    client.open()
    try:
        if replay:
            print("probe: replay capture FIRM_INFO request...")
            rx = client.transact_raw(
                CAPTURE_FIRM_INFO_REQ, CMD_FIRM_INFO, timeout=timeout
            )
            ver = rx.payload.decode("ascii", errors="replace")
        else:
            ver = client.cmd_firm_info()
        print(f"probe OK — firmware version: {ver}")
    finally:
        client.close()


def print_route_hint() -> None:
    """Best-effort hint when multiple NICs share 192.168.1.0/24 (common on Thor)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((MCU_IP, MCU_PORT))
            local = s.getsockname()[0]
        if local != LOCAL_IP:
            print(
                f"warning: kernel would route {MCU_IP} via local {local}, "
                f"not {LOCAL_IP}; use --iface if ACK times out"
            )
    except OSError:
        pass


def decode_slot_bank(slot_byte: int) -> int:
    """Extract bank id from SLOT_OP field (0x10|A, 0x11|B, or raw 0/1)."""
    return slot_byte & 0x0F


def inactive_boot_slot(current_slot: int) -> int:
    """OTA writes inactive bank; next boot should use the other bank."""
    bank = decode_slot_bank(current_slot)
    if bank not in (0, 1):
        raise OtaError(f"unknown current_slot=0x{current_slot:02X}")
    return 1 - bank


def run_slot_switch(client: LinkV1Client) -> None:
    print("\nSLOT_OP query...")
    ret, cur, tgt = client.cmd_slot_op(0x00, 0x00)
    print(f"  ret={ret} current_slot=0x{cur:02X} target_slot=0x{tgt:02X}")
    if ret != 0:
        raise OtaError(f"SLOT_OP query failed ret={ret}")

    next_bank = inactive_boot_slot(cur)
    print(
        f"SLOT_OP set boot bank -> {next_bank} "
        f"(0x00=A, 0x01=B; was running bank {decode_slot_bank(cur)})..."
    )
    ret, cur2, tgt2 = client.cmd_slot_op(0x01, next_bank)
    print(f"  ret={ret} current_slot=0x{cur2:02X} target_slot=0x{tgt2:02X}")
    if ret != 0:
        raise OtaError(f"SLOT_OP set failed ret={ret}")


def run_slot_query(
    *,
    timeout: float,
    verbose: bool,
    bind_iface: Optional[str],
) -> None:
    client = LinkV1Client(timeout=timeout, verbose=verbose, bind_iface=bind_iface)
    client.open()
    try:
        print("FIRM_INFO...")
        ver = client.cmd_firm_info()
        print(f"  current version: {ver}")
        print("\nSLOT_OP query...")
        ret, cur, tgt = client.cmd_slot_op(0x00, 0x00)
        bank_cur = decode_slot_bank(cur)
        bank_tgt = decode_slot_bank(tgt)
        print(f"  ret={ret} current_slot=0x{cur:02X} target_slot=0x{tgt:02X}")
        print(f"  running bank={bank_cur} (0=A 1=B), next boot bank={bank_tgt}")
    finally:
        client.close()


def run_finish_only(
    *,
    timeout: float,
    no_reboot: bool,
    verbose: bool,
    bind_iface: Optional[str],
) -> None:
    """SLOT switch + REBOOT only (after firmware already transferred)."""
    print_route_hint()
    client = LinkV1Client(
        timeout=timeout, verbose=verbose, bind_iface=bind_iface
    )
    client.open()
    try:
        print("FIRM_INFO...")
        ver = client.cmd_firm_info()
        print(f"  current version: {ver}")

        run_slot_switch(client)

        if no_reboot:
            print("\nSkipping REBOOT (--no-reboot).")
        else:
            print("REBOOT...")
            client.cmd_reboot()
            print("  reboot command sent.")

        print("\nFinish steps completed.")
    finally:
        client.close()


def run_ota(
    core0_path: str,
    core1_path: str,
    *,
    timeout: float,
    data_timeout: float,
    no_reboot: bool,
    verbose: bool,
    bind_iface: Optional[str],
    with_device_status: bool,
) -> None:
    core0 = load_firmware(core0_path, PARTITION_CORE0)
    core1 = load_firmware(core1_path, PARTITION_CORE1)

    print_route_hint()
    client = LinkV1Client(
        timeout=timeout, verbose=verbose, bind_iface=bind_iface
    )
    client.open()
    try:
        print("FIRM_INFO...")
        ver = client.cmd_firm_info()
        print(f"  current version: {ver}")

        if with_device_status:
            print("DEVICE_STATUS...")
            status = client.cmd_device_status()
            print(f"  status=0x{status:08X}")
        else:
            # Capture uses session_seq=2 for FW_ENTRY after FIRM_INFO + DEVICE_STATUS.
            print("Skipping DEVICE_STATUS (optional; use --with-device-status to enable)...")
            client.session_seq = 2

        print("FW_ENTRY (session start)...")
        client.cmd_fw_entry()

        upgrade_partition(
            client, core0, data_timeout=data_timeout, progress_label="core0"
        )

        print("\nFW_ENTRY (before core1)...")
        client.cmd_fw_entry()

        upgrade_partition(
            client, core1, data_timeout=data_timeout, progress_label="core1"
        )

        run_slot_switch(client)

        if no_reboot:
            print("\nSkipping REBOOT (--no-reboot).")
        else:
            print("REBOOT...")
            client.cmd_reboot()
            print("  reboot command sent.")

        print("\nOTA completed successfully.")
    finally:
        client.close()


def dry_run(core0_path: str, core1_path: str) -> None:
    run_crc_self_test()
    core0 = load_firmware(core0_path, PARTITION_CORE0)
    core1 = load_firmware(core1_path, PARTITION_CORE1)
    print(f"core0: {core0.path}")
    print(f"  size={len(core0.data)} packets={core0.num_packets} md5={core0.md5.hex()}")
    print(f"core1: {core1.path}")
    print(f"  size={len(core1.data)} packets={core1.num_packets} md5={core1.md5.hex()}")
    print("dry-run OK (no UDP traffic).")


def rh850_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="RH850 UDP OTA — upgrade core0 + core1 in one run.",
    )
    parser.add_argument(
        "firmware_dir",
        nargs="?",
        default=DEFAULT_FIRMWARE_DIR,
        help=(
            f"directory with {CORE0_BIN_NAME} and {CORE1_BIN_NAME} "
            f"(default: {DEFAULT_FIRMWARE_DIR})"
        ),
    )
    parser.add_argument(
        "--core0",
        default=None,
        help=f"override core0 bin (default: <firmware_dir>/{CORE0_BIN_NAME})",
    )
    parser.add_argument(
        "--core1",
        default=None,
        help=f"override core1 bin (default: <firmware_dir>/{CORE1_BIN_NAME})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="ACK timeout for control commands (seconds)",
    )
    parser.add_argument(
        "--data-timeout",
        type=float,
        default=5.0,
        help="ACK timeout per FW_DATA packet (seconds)",
    )
    parser.add_argument(
        "--no-reboot",
        action="store_true",
        help="finish SLOT switch but do not send REBOOT",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate bins and CRC only, no network",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print TX/RX hex",
    )
    parser.add_argument(
        "--iface",
        metavar="IFACE",
        default=None,
        help="bind UDP to NIC (Thor: use lan0 after moving .85); needs root",
    )
    parser.add_argument(
        "--query-slot",
        action="store_true",
        help="read FIRM_INFO + SLOT_OP query only",
    )
    parser.add_argument(
        "--finish-only",
        action="store_true",
        help="only SLOT_OP + REBOOT (firmware already on MCU)",
    )
    parser.add_argument(
        "--with-device-status",
        action="store_true",
        help="query DEVICE_STATUS before upgrade (optional; may timeout on some builds)",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="only send FIRM_INFO (test UDP 15000 link), then exit",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="with --probe: send exact FIRM_INFO frame from wire capture",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run CRC16 samples from protocol doc and exit",
    )
    args = parser.parse_args(argv)

    net_setup = False
    try:
        if not args.self_test and not args.dry_run:
            setup_network(args.iface or DEFAULT_IFACE)
            net_setup = True

        if args.self_test:
            run_crc_self_test()
            return 0
        if args.probe:
            run_probe(
                timeout=args.timeout,
                verbose=args.verbose,
                bind_iface=args.iface,
                replay=args.replay,
            )
            return 0
        if args.query_slot:
            run_slot_query(
                timeout=args.timeout,
                verbose=args.verbose,
                bind_iface=args.iface,
            )
            return 0
        if args.finish_only:
            run_finish_only(
                timeout=args.timeout,
                no_reboot=args.no_reboot,
                verbose=args.verbose,
                bind_iface=args.iface,
            )
            return 0
        core0_path, core1_path = resolve_firmware_paths(
            args.firmware_dir, args.core0, args.core1
        )
        if args.dry_run:
            dry_run(core0_path, core1_path)
            return 0
        run_ota(
            core0_path,
            core1_path,
            timeout=args.timeout,
            data_timeout=args.data_timeout,
            no_reboot=args.no_reboot,
            verbose=args.verbose,
            bind_iface=args.iface,
            with_device_status=args.with_device_status,
        )
        return 0
    except OtaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        if net_setup:
            teardown_network(args.iface or DEFAULT_IFACE)



# ================================================================
# 统一编排入口

# ================================================================

# ================================================================
# 统一编排入口（C 风格：选择器 + 全局糖 + 前缀糖 + passthrough）
# ================================================================

def _ok(msg: str) -> None:
    print(f"{_Fore.GREEN}{msg}{_Fore.RESET}")


def _err(msg: str) -> None:
    print(f"{_Fore.RED}{msg}{_Fore.RESET}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"{_Fore.CYAN}{msg}{_Fore.RESET}")


def _warn(msg: str) -> None:
    print(f"{_Fore.YELLOW}{msg}{_Fore.RESET}")


def _shlex(s: str) -> list:
    return shlex.split(s) if s else []


def _run_soc(argv, dry_run: bool, verbose: bool) -> int:
    _info(f"\n{'=' * 60}\n=== [soc] tars_flash  argv={argv}\n{'=' * 60}")
    if dry_run:
        _warn(f"[dry-run] 将执行: bash <内联 tars_flash> {' '.join(argv)}")
        return 0
    fd, path = tempfile.mkstemp(prefix="tars_flash_", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(TARS_FLASH_SH)
        cmd = ["bash", path] + argv
        # tars_flash 是交互式（read -p 确认/选镜像）且 dd 用 status=progress（\r 原地刷新）。
        # 若 pipe stdout 按行读：无换行的 read -p 提示会被行缓冲吞掉，进程又卡在等 stdin
        # 输入 → 表现为“停在 [WARN] 后没输出”；dd 进度也会被打乱。故直接继承终端 stdio，
        # 让 tars_flash 原生与 tty 交互（prompt/dd 进度/键盘输入都正常）。代价：soc 输出不带
        # [soc] 前缀，但 ota.py 仍能在它返回后继续后续步骤与汇总。
        return subprocess.run(cmd).returncode
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _run_pmu(argv, dry_run: bool, verbose: bool) -> int:
    _info(f"\n{'=' * 60}\n=== [pmu] PMU_OTA  argv={argv}\n{'=' * 60}")
    if dry_run:
        _warn(f"[dry-run] 将调用: pmu_main(argv={argv})")
        return 0
    if not any(a in ("-f", "--firmware", "-j", "--get-version", "--apply", "-h", "--help") for a in argv):
        _err("[pmu] 非交互运行需要 -f / --apply / -j（否则进入交互模式，不适合编排）")
        return 2
    try:
        rc = pmu_main(argv)
        return rc if isinstance(rc, int) else 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        _err(f"[pmu] 异常: {e!r}")
        return 1


def _run_mcu(argv, dry_run: bool, verbose: bool) -> int:
    _info(f"\n{'=' * 60}\n=== [mcu] rh850_udp_ota  argv={argv}\n{'=' * 60}")
    if dry_run:
        _warn(f"[dry-run] 将调用: rh850_main(argv={argv})")
        return 0
    try:
        rc = rh850_main(argv)
        return rc if isinstance(rc, int) else 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        _err(f"[mcu] 异常: {e!r}")
        return 1


def _rewrite_args(argv):
    """把 --soc-args/--pmu-args/--mcu-args 后跟的裸值改成等号形式，
    这样值以 - 开头（如 --apply/-j/-h）也不会被 argparse 误判为 ota.py 自己的选项
    （否则报 “expected one argument”）。已用等号形式的原样不动。"""
    passthrough = ("--soc-args", "--pmu-args", "--mcu-args")
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok in passthrough and i + 1 < len(argv):
            out.append(f"{tok}={argv[i + 1]}")
            i += 2
        else:
            out.append(tok)
            i += 1
    return out


def ota_main() -> int:
    p = argparse.ArgumentParser(
        description="Thor OTA 单文件编排器（内联 tars_flash + PMU_OTA + rh850_udp_ota）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sel = p.add_argument_group("子功能选择（默认全执行）")
    sel.add_argument("--soc", action="store_true", help="SoC A/B 分区恢复 (tars_flash)")
    sel.add_argument("--pmu", action="store_true", help="PMU OTA (PMU_OTA)")
    sel.add_argument("--mcu", action="store_true", help="RH850 MCU OTA (rh850_udp_ota)")

    g = p.add_argument_group("全局")
    g.add_argument("--dry-run", action="store_true",
                   help="只打印每步将执行的命令/argv，不执行")
    g.add_argument("--verbose", action="store_true",
                   help="soc 始终流式输出(无需此开关)；本开关仅令 pmu/mcu 额外打印 argv 横幅")
    g.add_argument("--no-reboot", action="store_true",
                   help="糖: 给 soc 与 mcu 注入 --no-reboot（pmu 无此概念）")
    g.add_argument("--soc-verbose", action="store_true",
                   help="(tars_flash 无 --verbose，此开关忽略并告警)")
    g.add_argument("--pmu-verbose", action="store_true",
                   help="(PMU 无 --verbose，此开关忽略并告警)")
    g.add_argument("--mcu-verbose", action="store_true",
                   help="给 mcu 注入 --verbose（RH850 TX/RX hex）")

    pt = p.add_argument_group("passthrough（原命令参数原样透传，引号包围）")
    pt.add_argument("--soc-args", default="", metavar="STR",
                    help='soc 透传，例: --soc-args "-r ./data_dir"')
    pt.add_argument("--pmu-args", default="", metavar="STR",
                    help='pmu 透传，例: --pmu-args "-f data_dir/zephyr.signed.bin"')
    pt.add_argument("--mcu-args", default="", metavar="STR",
                    help='mcu 透传，例: --mcu-args "--iface lan0 --timeout 15"')

    args = p.parse_args(_rewrite_args(sys.argv[1:]))

    if args.soc_verbose:
        _warn("--soc-verbose: tars_flash 无 --verbose 选项，忽略")
    if args.pmu_verbose:
        _warn("--pmu-verbose: PMU 无 --verbose 选项，忽略")

    selected = [t for t in ("soc", "pmu", "mcu") if getattr(args, t)] or ["soc", "pmu", "mcu"]

    soc_argv = _shlex(args.soc_args) + (["--no-reboot"] if args.no_reboot else [])
    pmu_argv = _shlex(args.pmu_args)
    mcu_argv = (_shlex(args.mcu_args)
                + (["--no-reboot"] if args.no_reboot else [])
                + (["--verbose"] if args.mcu_verbose else []))

    runners = {
        "soc": (_run_soc, soc_argv),
        "pmu": (_run_pmu, pmu_argv),
        "mcu": (_run_mcu, mcu_argv),
    }

    _info(f"将执行步骤: {' -> '.join(selected)}")
    results = []
    for tool in selected:
        fn, av = runners[tool]
        rc = fn(av, args.dry_run, args.verbose)
        results.append((tool, rc))
        if rc != 0:
            _err(f"[{tool}] 失败 (exit={rc})，停止后续步骤")
            break

    print()
    _info("=== 汇总 ===")
    for tool, rc in results:
        tag = f"{_Fore.GREEN}OK" if rc == 0 else f"{_Fore.RED}FAIL"
        print(f"  [{tool}] {tag}{_Fore.RESET} (exit={rc})")
    failed = [rc for _, rc in results if rc != 0]
    if failed:
        _err(f"存在失败步骤，首个失败退出码={failed[0]}")
        return failed[0]
    _ok("所有步骤完成")
    return 0


if __name__ == "__main__":
    sys.exit(ota_main())

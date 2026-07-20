#!/bin/bash
#
# 固件版本门禁脚本（从 scripts/humanoid/start_awr.sh 中提取）
#
# 仅做 thor-soc / thor-mcu / thor-pmu 三类固件的版本校验与按需升级，
# 校验/升级流程与 start_awr.sh 中的完全一致，可独立运行。
#
# 用法:
#   ./check_firmware.sh                 # 交互模式
#   ./check_firmware.sh --use-default   # 后续交互项自动使用各自默认值
#   ./check_firmware.sh -h              # 帮助
#
# 退出码:
#   0  全部固件校验通过（或用户选择不上下电后正常返回）
#   1  MCU / PMU 固件校验未通过，终止
#   2  SOC 镜像侧问题（版本不符 / OTA 工具缺失等）

# ==================== 颜色与模式变量 ====================
BOLD='\033[1m'
RED='\033[0;31m'
BLUE='\033[1;34;48m'
GREEN='\033[32m'
WHITE='\033[34m'
YELLOW='\033[33m'
NO_COLOR='\033[0m'
USE_DEFAULT_OPTIONS=1   # 1: 后续交互项自动使用各自默认值
PACKAGE_DIR="/mnt/gaea/package"
OTA_FILE="$(dirname "${BASH_SOURCE[0]}")/ota.py"
if [ ! -f "$OTA_FILE" ]; then
    echo "错误: 找不到OTA工具文件 $OTA_FILE" >&2
    exit 1
fi
# base_name=$(basename "${ARCHIVE%.install.tar}")
# BASE_OUTPUT_DIR="${PACKAGE_DIR}/${base_name}_output"
# OUTPUT_DIR="${PACKAGE_DIR}/${base_name}_output/output"


show_help() {
    echo "Usage: $0 [--use-default] [-h|--help]"
    echo ""
    echo "Options:"
    echo "  --use-default   后续交互项自动使用各自默认值"
    echo "  -h, --help      显示帮助信息"
}

# ==================== 参数解析 ====================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --use-default)
            USE_DEFAULT_OPTIONS=1
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "错误: 未知参数 '$1'"
            show_help
            exit 1
            ;;
    esac
done

VERSION_FILE="$(dirname "${BASH_SOURCE[0]}")/firmware_version.txt"

if [ ! -f "$VERSION_FILE" ]; then
    echo "错误: 找不到版本配置文件 $VERSION_FILE" >&2
    exit 1
fi

# 从配置文件中解析特定的变量值，避免直接 source 整个文本文件
get_var_from_file() {
    local var_name="$1"
    local file_path="$2"
    grep -E "^[[:space:]]*${var_name}=" "$file_path" | head -n 1 | sed -E "s/^[[:space:]]*${var_name}=[[:space:]]*['\"]?([^'\"]*)['\"]?/\1/"
}

EXPECTED_SOC_VERSION=$(get_var_from_file "EXPECTED_SOC_VERSION" "$VERSION_FILE")
EXPECTED_MCU_VERSION_KEYWORD=$(get_var_from_file "EXPECTED_MCU_VERSION_KEYWORD" "$VERSION_FILE")
EXPECTED_PMU_FIRMWARE_VERSION=$(get_var_from_file "EXPECTED_PMU_FIRMWARE_VERSION" "$VERSION_FILE")

echo EXPECTED_SOC_VERSION : $EXPECTED_SOC_VERSION
echo EXPECTED_MCU_VERSION_KEYWORD : $EXPECTED_MCU_VERSION_KEYWORD
echo EXPECTED_PMU_FIRMWARE_VERSION : $EXPECTED_PMU_FIRMWARE_VERSION


# ==================== MCU 固件版   本门禁 ====================
# thor-soc 镜像自带的 MCU OTA 工具，由其 --probe 子命令读取 thor-mcu 固件版本。
# 新版镜像已安装为无后缀可执行文件（带 shebang / 二进制），直接执行而非 python3 调用。
#RH850_OTA_TOOL="/usr/local/bin/rh850_udp_ota"
# /etc/tars_fw_version 第一行记录的 thor-soc 镜像版本（需严格一致）。
TARS_FW_VERSION_FILE="/etc/tars_fw_version"

THOR_IMAGES_DOWNLOAD_URL="http://10.100.100.51:8080/gitlab-ci/Thor_Images/"




# 任一 SOC/MCU/PMU .run 更新成功后置 1；全部门禁结束后统一询问是否整机上下电。
FIRMWARE_FLASHED=0
mcu_just_flashed=0
soc_just_flashed=0
pmu_just_flashed=0

# 下载并解压固件包：
#   - 文件不存在 → 下载
#   - 解压失败 → 判定文件损坏，删除后重新下载并重试一次
# 用法: download_and_extract "<标签>" "<目标包路径>" "<下载URL>" "<解压路径>" "<tar 额外参数...>"
# 返回 0 成功，非 0 失败。
download_and_extract() {
    local label="$1"
    local target_package="$2"
    local download_url="$3"
    local extract_path="$4"
    shift 4
    local -a tar_extra=("$@")

    local attempt
    for attempt in 1 2; do
        if [ ! -f "$target_package" ]; then
            echo -e "${YELLOW}⏳ 正在从 ${download_url} 下载 ${label} 固件到 ${target_package}${NO_COLOR}"
            if ! wget -q --show-progress -O "$target_package" "$download_url"; then
                echo -e "${RED}❌ ${label} 固件下载失败${NO_COLOR}"
                echo -e "${YELLOW}   手动下载地址: ${THOR_IMAGES_DOWNLOAD_URL}${NO_COLOR}"
                return 1
            fi
            echo -e "${GREEN}✅ ${label} 固件下载成功${NO_COLOR}"
        else
            echo -e "${YELLOW}⚠️ ${target_package} 已存在，跳过下载${NO_COLOR}"
        fi

        # 解压前先删除可能已存在的同名解压目标文件夹
        local folder_name=$(basename "$target_package" | sed -E 's/\.tar\.gz$|\.tgz$//')
        local target_folder="${extract_path}/${folder_name}"
        if [ -d "$target_folder" ]; then
            echo -e "${YELLOW}🗑️ 发现已存在的解压文件夹 ${target_folder}，正在删除...${NO_COLOR}"
            rm -rf "$target_folder"
        fi

        echo -e "${YELLOW}⏳ 正在解压 ${target_package} 到 ${extract_path}, 参数 ${tar_extra[@]} ${NO_COLOR}"
        if tar -zxvf "$target_package" -C "$extract_path" "${tar_extra[@]}"; then
            # 检查解压后是否包含 md5 校验文件，若有则进行核对
            local md5_file=$(find "$target_folder" -maxdepth 2 -type f \( -name "*.md5" -o -name "md5.txt" -o -name "MD5SUM" -o -name "md5sum.txt" \) 2>/dev/null | head -n 1)
            if [ -n "$md5_file" ]; then
                echo -e "${YELLOW}🔍 发现 MD5 校验文件: ${md5_file}，开始核对...${NO_COLOR}"
                local md5_dir=$(dirname "$md5_file")
                local md5_base=$(basename "$md5_file")
                if (cd "$md5_dir" && md5sum -c "$md5_base"); then
                    echo -e "${GREEN}✅ MD5 校验成功！${NO_COLOR}"
                else
                    echo -e "${RED}❌ MD5 校验失败(第 ${attempt}/2 次)！将清理损坏的数据并重新尝试下载${NO_COLOR}"
                    rm -rf "$target_folder"
                    rm -f "$target_package"
                    continue
                fi
            fi
            echo -e "${GREEN}✅ ${label} 固件解压成功${NO_COLOR}"
            return 0
        else
            echo -e "${RED}❌ ${label} 固件解压失败 (第 ${attempt}/2 次)，文件可能损坏，删除后重新下载${NO_COLOR}"
            rm -f "$target_package"
        fi
    done

    echo -e "${RED}❌ ${label} 固件重新下载并解压仍失败${NO_COLOR}"
    return 1
}

prompt_power_cycle_if_firmware_flashed() {
    [ "${FIRMWARE_FLASHED:-0}" -eq 1 ] || return 0
    echo -e "${YELLOW}⚠️ 本次有固件更新，新版本需整机重新上下电后生效${NO_COLOR}"
    if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
        echo -e "${YELLOW}是否对整机重新上下电使新固件生效? [默认模式: N]${NO_COLOR}"
        #echo -e "${GREEN}请对整机做一次完整上下电，然后再重新运行本脚本。${NO_COLOR}"
        return 0
    else
        local power_choice=""
        read -p "$(echo -e "${YELLOW}是否对整机重新上下电使新固件生效? [Y/n]: ${NO_COLOR}")" power_choice
        power_choice=${power_choice:-Y}
        case "$power_choice" in
            [Nn]* )
                echo -e "${YELLOW}⚠️ 用户选择暂不上下电${NO_COLOR}"
                return 0
                ;;
            * )
                echo -e "${GREEN}请对整机做一次完整上下电，然后再重新运行本脚本。${NO_COLOR}"
                return 1
                ;;
        esac
    fi
}

check_soc_firmware() { 
    echo "=========================================="
    echo "🔌 校验 thor-soc 镜像版本"
    echo "=========================================="

    local soc_version=$(head -n1 "$TARS_FW_VERSION_FILE" 2>/dev/null \
                  | tr -d '\r' \
                  | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    
    echo "📟 当前 SOC 版本: ${soc_version:-未知} （期望版本: $EXPECTED_SOC_VERSION）"

    if [ -z "$soc_version" ] || [ "$soc_version" != "$EXPECTED_SOC_VERSION" ]; then
        echo -e "${RED}❌ SOC 镜像版本不符 ${NO_COLOR}"
        if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
            echo -e "${YELLOW}是否自动从 ${THOR_IMAGES_DOWNLOAD_URL} 下载对应版本固件? [默认模式: y]${NO_COLOR}"
        else
            local download_choice=""
            while true; do
                read -p "$(echo -e "${YELLOW}是否自动下载并更新固件? [Y/n]: ${NO_COLOR}")" download_choice
                download_choice=${download_choice:-y}
                case "$download_choice" in
                    [Yy]* )
                        break
                        ;;
                    [Nn]* )
                        local force_choice=""
                        read -p "$(echo -e "${YELLOW}是否强制使用当前 SOC 镜像版本继续? [y/N]: ${NO_COLOR}")" force_choice
                        force_choice=${force_choice:-n}
                        case "$force_choice" in
                            [Yy]* )
                                echo -e "${YELLOW}⚠️ 已强制使用当前 SOC 镜像 (${soc_version:-未知})，继续 MCU 固件校验${NO_COLOR}"
                                return 0
                                ;;
                            * )
                                echo -e "${RED}❌ SOC 固件版本不符，用户选择不更新且不强制使用当前版本，终止流程${NO_COLOR}"
                                return 1
                                ;;
                        esac
                        ;;
                    * )
                        echo -e "${YELLOW}请输入 y 或 n${NO_COLOR}"
                        ;;
                esac
            done
        fi
    else
        echo -e "${GREEN}✅ SOC 镜像版本校验通过 ${NO_COLOR}"
        return 0
    fi

    local firmware_file="${EXPECTED_SOC_VERSION}.tar.gz"
    local target_package="${PACKAGE_DIR}/${firmware_file}"
    local download_url="${THOR_IMAGES_DOWNLOAD_URL}/${firmware_file}"
    download_and_extract "SOC" "$target_package" "$download_url" "${PACKAGE_DIR}" || return 1
    
    # 假设 ota.py 在当前目录或 PATH 中
    if sudo python3 $OTA_FILE --soc --soc-args "-r ${PACKAGE_DIR}/${EXPECTED_SOC_VERSION} --no-reboot"; then
        FIRMWARE_FLASHED=1
        soc_just_flashed=1
        echo -e "${GREEN}✅ SOC 镜像更新成功（待全部固件流程结束后询问上下电）${NO_COLOR}"
    else
        echo -e "${RED}❌ SOC 镜像更新失败${NO_COLOR}"
        return 1
    fi
    
    return 0
}

check_mcu_firmware() {
    echo "=========================================="
    echo "🔌 校验 thor-mcu 固件版本"
    echo "=========================================="

    echo "正在探测 MCU 版本: python3 $OTA_FILE --mcu --mcu-args \"--probe\""
    local probe_out=$(timeout 30 python3 $OTA_FILE --mcu --mcu-args "--probe" 2>&1)
    local probe_rc=$?
    echo "$probe_out"

    if [ "$probe_rc" -eq 124 ]; then
        echo -e "${RED}❌ MCU 探测超时（工具未在 30s 内返回）${NO_COLOR}"
        echo -e "${RED}   将自动为您下载并更新 thor-mcu 固件 ${NO_COLOR}"
    else
        # 解析新版探测输出：从输出中获取是否 probe OK 以及固件版本号
        local ok_val="0"
        if echo "$probe_out" | grep -q "probe OK"; then
            ok_val="1"
        fi
        local mcu_version
        mcu_version=$(echo "$probe_out" | grep 'firmware version:' | sed -E 's/.*firmware version:[[:space:]]*//; s/[[:space:]].*//')

        # ok!=1：通常为 ACK 超时（reason 已在上方原样打印）
        if [ "$ok_val" != "1" ]; then
            echo -e "${RED}❌ MCU 探测失败 (ok=${ok_val:-未知})，可能为通信/ACK 超时${NO_COLOR}"
            echo -e "${RED}   将自动为您下载并更新 thor-mcu 固件 ${NO_COLOR}"
        elif [ -z "$mcu_version" ]; then
            echo -e "${RED}❌ 探测返回 ok=1 但未解析到版本号${NO_COLOR}"
            echo -e "${RED}   将自动为您下载并更新 thor-mcu 固件 ${NO_COLOR}"
        else
            echo -e "${GREEN}✅ MCU 探测成功:${NO_COLOR}"
            echo "📟 当前 MCU 版本: $mcu_version （期望版本应含关键字: $EXPECTED_MCU_VERSION_KEYWORD）"
            if echo "$mcu_version" | grep -q "$EXPECTED_MCU_VERSION_KEYWORD"; then
                echo -e "${GREEN}✅ MCU 固件版本校验通过 ${NO_COLOR}"
                return 0
            else
                echo -e "${RED}❌ MCU 固件版本不符 ${NO_COLOR}"
                if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
                    echo -e "${YELLOW}是否自动从 ${THOR_IMAGES_DOWNLOAD_URL} 下载对应版本固件? [默认模式: y]${NO_COLOR}"
                else
                    local download_choice=""
                    while true; do
                        read -p "$(echo -e "${YELLOW}是否自动下载并更新 MCU 固件? [Y/n]: ${NO_COLOR}")" download_choice
                        download_choice=${download_choice:-y}
                        case "$download_choice" in
                            [Yy]*)
                                break
                                ;;
                            [Nn]*)
                                echo -e "${RED}❌ MCU 固件版本不符，用户选择不更新${NO_COLOR}"
                                local force_choice=""
                                read -p "$(echo -e "${YELLOW}是否强制使用当前 MCU 固件版本继续? [N/y]: ${NO_COLOR}")" force_choice
                                force_choice=${force_choice:-n}
                                case "$force_choice" in
                                    [Yy]*)
                                        echo -e "${YELLOW}⚠️ 已强制使用当前 MCU 固件 (${mcu_version:-未知})，继续下个固件校验${NO_COLOR}"
                                        return 0
                                        ;;
                                    *)
                                        echo -e "${RED}❌ MCU 固件版本不符，用户选择不更新且不强制使用当前版本，终止流程${NO_COLOR}"
                                        return 1
                                        ;;
                                esac
                                ;;
                            *)
                                echo -e "${YELLOW}请输入 y 或 n${NO_COLOR}"
                                ;;
                        esac
                    done
                fi
            fi
        fi
    fi

    local mcu_firmware_file="mcu_${EXPECTED_MCU_VERSION_KEYWORD}.tar.gz"
    local mcu_target_package="${PACKAGE_DIR}/${mcu_firmware_file}"
    local mcu_download_url="${THOR_IMAGES_DOWNLOAD_URL}/${mcu_firmware_file}"
    download_and_extract "MCU" "$mcu_target_package" "$mcu_download_url" "${PACKAGE_DIR}" || return 1

    if sudo python3 $OTA_FILE --mcu --mcu-args "${PACKAGE_DIR}/mcu_${EXPECTED_MCU_VERSION_KEYWORD} --iface lan0 --timeout 15 --data-timeout 10 --verbose --no-reboot"; then
        FIRMWARE_FLASHED=1
        mcu_just_flashed=1
        echo -e "${YELLOW}⚠️ MCU 已刷写，版本待整机上下电后再校验${NO_COLOR}"
        echo -e "${GREEN}✅ 待全部固件流程结束后询问上下电${NO_COLOR}"
    else
        echo -e "${RED}❌ MCU 固件更新失败${NO_COLOR}"
        return 1
    fi

    return 0
}

# 启动节点前校验 PMU 电源板固件版本（READ_ID / GET_ID）：
#   通过 PMU_OTA.py --get-version 读取版本，与 EXPECTED_PMU_FIRMWARE_VERSION 严格一致才继续。
check_pmu_firmware() {
    echo "=========================================="
    echo "🔌 校验 PMU 电源板固件版本"
    echo "=========================================="

    echo "正在探测 PMU 版本: python3 $OTA_FILE --pmu --pmu-args --get-version"
    local pmu_version=""
    local probe_rc=0
    local probe_err=$(mktemp)
    
    pmu_version=$(timeout 30 python3 $OTA_FILE --pmu --pmu-args "--get-version" 2>"$probe_err")
    probe_rc=$?
    
    if [ -s "$probe_err" ]; then
        cat "$probe_err"
    fi
    rm -f "$probe_err"

    if [ "$probe_rc" -eq 124 ]; then
        echo -e "${RED}❌ PMU 版本探测超时（工具未在 30s 内返回）${NO_COLOR}"
        echo -e "${RED}   将自动为您下载并更新 PMU 固件${NO_COLOR}"
    elif [ "$probe_rc" -ne 0 ]; then
        echo -e "${RED}❌ PMU 版本探测失败 (exit=${probe_rc})${NO_COLOR}"
        echo -e "${RED}   将自动为您下载并更新 PMU 固件${NO_COLOR}"
    else
        pmu_version=$(echo "$pmu_version" | grep -E '^[vV][0-9]' | tr -d '\r' \
            | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')

        if [ -z "$pmu_version" ]; then
            echo -e "${RED}❌ PMU 探测未返回版本号${NO_COLOR}"
            echo -e "${RED}   将自动为您下载并更新 PMU 固件${NO_COLOR}"
        else
            echo "📟 当前 PMU 版本: $pmu_version （要求: $EXPECTED_PMU_FIRMWARE_VERSION）"
            if [ "${pmu_version,,}" != "${EXPECTED_PMU_FIRMWARE_VERSION,,}" ]; then
                echo -e "${RED}❌ PMU 固件版本不符（当前 '$pmu_version'，要求 '$EXPECTED_PMU_FIRMWARE_VERSION'）${NO_COLOR}"

                if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
                    echo -e "${YELLOW}是否自动从 ${THOR_IMAGES_DOWNLOAD_URL} 下载对应版本固件? [默认模式: y]${NO_COLOR}"
                else
                    local download_choice=""
                    while true; do
                        read -p "$(echo -e "${YELLOW}是否自动下载并更新 PMU 固件? [Y/n]: ${NO_COLOR}")" download_choice
                        download_choice=${download_choice:-y}
                        case "$download_choice" in
                            [Yy]*)
                                break
                                ;;
                            [Nn]*)
                                echo -e "${RED}❌ PMU 固件版本不符，用户选择不更新${NO_COLOR}"
                                local force_choice=""
                                read -p "$(echo -e "${YELLOW}是否强制使用当前 PMU 固件版本继续? [N/y]: ${NO_COLOR}")" force_choice
                                force_choice=${force_choice:-n}
                                case "$force_choice" in
                                    [Yy]*)
                                        echo -e "${YELLOW}⚠️ 已强制使用当前 PMU 固件 (${pmu_version:-未知})${NO_COLOR}"
                                        return 0
                                        ;;
                                    *)
                                        echo -e "${RED}❌ PMU 固件版本不符，用户选择不更新且不强制使用当前版本，终止流程${NO_COLOR}"
                                        return 1
                                        ;;
                                esac
                                ;;
                            *)
                                echo -e "${YELLOW}请输入 y 或 n${NO_COLOR}"
                                ;;
                        esac
                    done
                fi
            else
                echo -e "${GREEN}✅ PMU 固件版本校验通过${NO_COLOR}"
                # 版本匹配后发送 APPLY_OTA，等待 0x200 ACK
                echo "正在执行 PMU APPLY_OTA: python3 $OTA_FILE --pmu --apply"
                local apply_rc=0
                timeout 30 python3 $OTA_FILE --pmu --pmu-args "--apply" || apply_rc=$?
                if [ "$apply_rc" -eq 124 ]; then
                    echo -e "${RED}❌ PMU APPLY_OTA 超时（工具未在 30s 内返回）${NO_COLOR}"
                    return 1
                fi
                if [ "$apply_rc" -ne 0 ]; then
                    echo -e "${RED}❌ PMU APPLY_OTA 失败 (exit=${apply_rc})${NO_COLOR}"
                    return 1
                fi
                echo -e "${GREEN}✅ PMU APPLY_OTA 已确认${NO_COLOR}"
                return 0
            fi
        fi
    fi

    # 下载并更新 PMU 固件
    local pmu_firmware_file="pmu_${EXPECTED_PMU_FIRMWARE_VERSION}.tar.gz"
    local download_url="${THOR_IMAGES_DOWNLOAD_URL}/${pmu_firmware_file}"
    local target_package="${PACKAGE_DIR}/${pmu_firmware_file}"
    download_and_extract "PMU" "$target_package" "$download_url" "${PACKAGE_DIR}" || return 1

    if sudo python3 $OTA_FILE --pmu --pmu-args "-f ${PACKAGE_DIR}/pmu_${EXPECTED_PMU_FIRMWARE_VERSION}/zephyr.signed.bin"; then
        FIRMWARE_FLASHED=1
        pmu_just_flashed=1
        echo -e "${YELLOW}⚠️ PMU 已刷写，版本待整机上下电后再校验${NO_COLOR}"
        echo -e "${GREEN}✅ 待全部固件流程结束后询问上下电${NO_COLOR}"
    else
        echo -e "${RED}❌ PMU 固件更新失败${NO_COLOR}"
        return 1
    fi

    return 0
}

# ==================== 主流程 ====================

echo "=========================================="
if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
    echo "⚙️ 默认值模式 (--use-default)"
else
    echo "👤 交互模式"
fi
echo "=========================================="
echo ""

# SOC 固件版本门禁
check_soc_firmware
_fw_check_rc=$?
if [ "$_fw_check_rc" -ne 0 ]; then
    echo -e "${RED}❌ thor-soc 固件校验未通过，终止启动流程${NO_COLOR}"
    prompt_power_cycle_if_firmware_flashed
    exit 2
fi

# MCU 固件版本门禁
check_mcu_firmware
_fw_check_rc=$?
if [ "$_fw_check_rc" -ne 0 ]; then
    echo -e "${RED}❌ thor-mcu 固件校验未通过，终止启动流程${NO_COLOR}"
    prompt_power_cycle_if_firmware_flashed
    exit 1
fi

# PMU 固件版本门禁
check_pmu_firmware
_pmu_fw_check_rc=$?
if [ "$_pmu_fw_check_rc" -ne 0 ]; then
    echo -e "${RED}❌ PMU 固件校验未通过，终止启动流程${NO_COLOR}"
    prompt_power_cycle_if_firmware_flashed
    exit 1
fi

echo -e "${GREEN}✅ 全部固件已经校验完成${NO_COLOR}"
prompt_power_cycle_if_firmware_flashed
exit $?  # 返回上一个命令的实际退出码

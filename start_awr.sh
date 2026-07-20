#!/bin/bash

source "/apollo/scripts/humanoid/robots/start_robot_config.sh"

# ==================== 全局变量 ====================
BOLD='\033[1m'
RED='\033[0;31m'
BLUE='\033[1;34;48m'
GREEN='\033[32m'
WHITE='\033[34m'
YELLOW='\033[33m'
NO_COLOR='\033[0m'
AUTO_MODE=0 # 非交互式模式，需要 --auto 参数启用
KILL_ALL_APOLLO=1
DOWNLOAD_RESOURCES=0
YES_TO_ALL=0
USE_DEFAULT_OPTIONS=0 # 1:后续交互项自动使用各自默认值
RESOURCE_OFFLINE_MODE=0 # 1:跳过云端 resources 查询/下载，继续使用本地资源
RESOURCE_USE_LOCAL_FALLBACK=0 # 1:专属 resources 不存在，改用仓库内 00
ENABLE_FORCE_CHECK=0 # 0:禁用 1:启用六维力检测
SKIP_COREDUMP_SETUP=1 # 0:设置 coredump 1:跳过 start_awr coredump 覆盖
DEPLOY_REGION="shanghai" # shanghai 或 suzhou
DATA_RECORDING_ENABLED=0 # 0:数据录制未就绪；1:就绪
EXTERNAL_DATA_DISK_AVAILABLE=0 # 1:本次检测到外接数据盘
ALLOW_QUICKDATA_WITHOUT_DATA_DISK=1 # 1:未检测到外接数据盘时仍允许 quickdata 降级启动
RECIPE_TYPE="" # THD30 / THHB / C134 / WAIC / AIO，写入参数服务器
RECIPE_TYPE_PARAM="/apollo/global/recipe_type"

LOG_DIR="/apollo/data/log/double_orin"
THIS_DEVICE_SN=$(getOrinSNLocal) # 获取Orin SN号
if [ $? -ne 0 ]; then
    echo "❌ 无法获取设备序列号，退出"
    exit 1
fi
THIS_DEVICE_SHORT=$(get_short_sn "$THIS_DEVICE_SN") # 短SN, 未注册时为空
if [ -n "$THIS_DEVICE_SHORT" ]; then
    THIS_DEVICE_ID="X1-${THIS_DEVICE_SHORT}"
    THIS_DEVICE_KNOWN=1
else
    # 未识别 SN: 使用本地 00 fallback, 跳过云端标定资源相关流程
    THIS_DEVICE_ID="00"
    THIS_DEVICE_KNOWN=0
    echo "⚠️ 未识别设备 SN: $THIS_DEVICE_SN, 使用本地默认配置 (THIS_DEVICE_ID=$THIS_DEVICE_ID), 将跳过云端标定资源流程"
fi
echo "当前设备 SN: $THIS_DEVICE_SN, 短 SN: $THIS_DEVICE_ID"

# ==================== 参数解析（支持长短参数） ====================

# 显示帮助信息的函数
show_help() {
    echo "Usage: $0 [--auto] [-f|--force] [-d|--download-resources] [--force-check] [--recipe-type TYPE] [--skip-coredump|--setup-coredump] [--region shanghai|suzhou] [-y|--yes-to-all] [-h|--help]"
    echo ""
    echo "Options:"
    echo "  --auto                      启用自动模式（非交互式），跳过用户确认提示"
    echo "  -f, --force                 强制清理所有 apollo mainboard 节点（默认启用）"
    echo "  -d, --download-resources    从数据平台下载最新标定资源"
    echo "      --force-check           启用六维力检测（无六维力传感器的机器请勿启用）"
    echo "      --recipe-type <type>    recipe 类型: THD30|THHB|C134|WAIC|AIO（写入参数服务器）"
    echo "      --skip-coredump         跳过 start_awr 的 coredump 覆盖（默认，不修改 core_pattern）"
    echo "      --setup-coredump        恢复旧行为：由 start_awr 覆盖 core_pattern"
    echo "      --region <region>       设置部署地区：shanghai（默认）或 suzhou"
    echo "  -y, --yes-to-all            所有确认全部选择自动选择 yes"
    echo "  -h, --help                  显示帮助信息"
    echo ""
    echo "Examples:"
    echo "  $0                          # 交互模式：根据用户选择执行相应操作"
    echo "  $0 -f -d                    # 交互模式：清理所有apollo mainboard节点，下载资源"
    echo "  $0 --auto --recipe-type THHB  # 自动模式并指定 recipe 类型"
}

# 解析参数（同时支持短参数和长参数）
while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto)
            AUTO_MODE=1
            shift
            ;;
        -f|--force)
            KILL_ALL_APOLLO=1
            shift
            ;;
        -d|--download-resources)
            DOWNLOAD_RESOURCES=1
            shift
            ;;
        --force-check)
            ENABLE_FORCE_CHECK=1
            shift
            ;;
        --recipe-type)
            if [ -z "$2" ] || [[ "$2" == -* ]]; then
                echo "错误: --recipe-type 需要指定类型（THD30|THHB|C134|WAIC|AIO）"
                exit 1
            fi
            RECIPE_TYPE=$(echo "$2" | tr '[:lower:]' '[:upper:]')
            case "$RECIPE_TYPE" in
                THD30|THHB|C134|WAIC|AIO) ;;
                *)
                    echo "错误: --recipe-type 非法: $2（仅支持 THD30|THHB|C134|WAIC|AIO）"
                    exit 1
                    ;;
            esac
            shift 2
            ;;
        --skip-coredump)
            SKIP_COREDUMP_SETUP=1
            shift
            ;;
        --setup-coredump)
            SKIP_COREDUMP_SETUP=0
            shift
            ;;
        --region)
            if [ -z "$2" ] || [[ "$2" == -* ]]; then
                echo "错误: --region 需要指定地区参数（shanghai 或 suzhou）"
                exit 1
            fi
            DEPLOY_REGION=$(echo "$2" | tr '[:upper:]' '[:lower:]')
            if [ "$DEPLOY_REGION" != "shanghai" ] && [ "$DEPLOY_REGION" != "suzhou" ]; then
                echo "错误: --region 只支持 shanghai 或 suzhou"
                exit 1
            fi
            shift 2
            ;;
        -y|--yes-to-all)
            YES_TO_ALL=1
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "错误: 未知参数 '$1'"
            echo ""
            show_help
            exit 1
            ;;
    esac
done

# ==================== 自动模式逻辑 ====================
# 如果是 AUTO_MODE，自动设置 YES_TO_ALL
if [ "$AUTO_MODE" -eq 1 ]; then
    YES_TO_ALL=1
fi

# ==================== RUN_CONFIG 加载（HMI 启动路径） ====================
# 由 robot_service_node 的 AwrLauncherHandler 通过环境变量 RUN_CONFIG_FILE
# 注入；文件存在时本段覆盖命令行已设的同名变量，无文件则完全跳过，保持
# 既有 SSH/--auto 入口行为不变。
#
# RUN_CONFIG_LOADED=1 表示已成功加载 JSON。后续凡是 `if YES_TO_ALL=1` 强制
# 开/强制关 jq 解析值的分支，都要先看这个标志：RUN_CONFIG 模式必须信任
# JSON 中的取值，绝不再覆盖。
RUN_CFG_FAIL_POLICY=""
RUN_CONFIG_LOADED=0
if [ -n "${RUN_CONFIG_FILE:-}" ] && [ -f "$RUN_CONFIG_FILE" ]; then
    if ! command -v jq >/dev/null 2>&1; then
        echo "❌ RUN_CONFIG 模式需要 jq，但未安装：$RUN_CONFIG_FILE"
        exit 1
    fi
    AUTO_MODE=1
    YES_TO_ALL=1
    RUN_CONFIG_LOADED=1
    _region=$(jq -r '.region // "shanghai"' "$RUN_CONFIG_FILE")
    case "$_region" in
        shanghai|suzhou) DEPLOY_REGION="$_region" ;;
        *) echo "⚠️ RUN_CONFIG.region 非法($_region)，回退 shanghai"; DEPLOY_REGION="shanghai" ;;
    esac
    KILL_ALL_APOLLO=$(jq         -r 'if has("killAllApollo") then (if .killAllApollo then 1 else 0 end) else 1 end' "$RUN_CONFIG_FILE")
    RM_ROS_DOCKER=$(jq           -r 'if .rmRosDocker       then 1 else 0 end' "$RUN_CONFIG_FILE")
    RESTART_ROS_DOCKER=$(jq      -r 'if .restartRosDocker  then 1 else 0 end' "$RUN_CONFIG_FILE")
    DOWNLOAD_RESOURCES=$(jq      -r 'if .downloadResources then 1 else 0 end' "$RUN_CONFIG_FILE")
    ENABLE_FORCE_CHECK=$(jq      -r 'if .forceCheck        then 1 else 0 end' "$RUN_CONFIG_FILE")
    SKIP_COREDUMP_SETUP=$(jq     -r 'if has("skipCoredumpSetup") then (if .skipCoredumpSetup then 1 else 0 end) else 1 end' "$RUN_CONFIG_FILE")
    _rc_recipe=$(jq -r '.recipeType // empty' "$RUN_CONFIG_FILE")
    if [ -n "$_rc_recipe" ]; then
        _rc_recipe=$(echo "$_rc_recipe" | tr '[:lower:]' '[:upper:]')
        case "$_rc_recipe" in
            THD30|THHB|C134|WAIC|AIO) RECIPE_TYPE="$_rc_recipe" ;;
            *) echo "⚠️ RUN_CONFIG.recipeType 非法($_rc_recipe)，将回退默认 THD30" ;;
        esac
    fi
    RUN_CFG_FAIL_POLICY=$(jq     -r '.onFailurePolicy.default // "continue"' "$RUN_CONFIG_FILE")
    case "$RUN_CFG_FAIL_POLICY" in
        continue|abort) ;;
        *) echo "⚠️ RUN_CONFIG.onFailurePolicy.default 非法($RUN_CFG_FAIL_POLICY)，回退 continue"
           RUN_CFG_FAIL_POLICY="continue" ;;
    esac
    # ArUco：可选对象。省略整个对象 = 沿用当前 yaml 值；提供则 sed 改 yaml。
    if jq -e '.aruco' "$RUN_CONFIG_FILE" >/dev/null 2>&1; then
        RUN_CFG_ARUCO_DICT_ID=$(jq -r '.aruco.dictionaryId   // empty' "$RUN_CONFIG_FILE")
        RUN_CFG_ARUCO_LEN_M=$(jq   -r '.aruco.markerLengthM  // empty' "$RUN_CONFIG_FILE")
        RUN_CFG_ARUCO_TARGET=$(jq  -r '.aruco.targetMarkerId // empty' "$RUN_CONFIG_FILE")
    fi
    echo "✅ 已加载 RUN_CONFIG: $RUN_CONFIG_FILE"
    echo "   region=$DEPLOY_REGION killAll=$KILL_ALL_APOLLO rmDocker=$RM_ROS_DOCKER \
restartDocker=$RESTART_ROS_DOCKER download=$DOWNLOAD_RESOURCES \
forceCheck=$ENABLE_FORCE_CHECK failPolicy=$RUN_CFG_FAIL_POLICY \
skipCoredumpSetup=$SKIP_COREDUMP_SETUP recipeType=${RECIPE_TYPE:-unset} \
aruco=(${RUN_CFG_ARUCO_DICT_ID:-keep},${RUN_CFG_ARUCO_LEN_M:-keep},${RUN_CFG_ARUCO_TARGET:-keep})"
fi

# ==================== 辅助函数 ====================

# 检查并配置 Nginx
# 在 /etc/nginx/conf.d/awr_hmi.conf 写入 awr_hmi 前端站点配置（监听 8080，托管 hmi/dist）
check_nginx_conf() {
    local nginx_conf_file="/etc/nginx/conf.d/awr_hmi.conf"
    echo "正在检查 Nginx 配置: $nginx_conf_file ..."

    # 确认 nginx 已安装
    if ! command -v nginx > /dev/null 2>&1; then
        echo "❌ 错误: 未检测到 nginx，请先安装 (apt-get install -y nginx)"
        return 1
    fi
    # 确认目标目录存在（nginx-common 装好后必有）
    if [ ! -d "/etc/nginx/conf.d" ]; then
        echo "❌ 错误: /etc/nginx/conf.d 不存在，nginx 安装可能不完整"
        return 1
    fi

    # 写入 awr_hmi 站点配置（覆盖式更新）
    sudo tee "$nginx_conf_file" > /dev/null << 'EOF'
# AWR HMI 前端静态站点 nginx 配置
# 仅托管 /apollo/modules/awr_hmi/hmi/dist 下的 Vite 构建产物
# 后端（aw_backend, 默认 0.0.0.0:8892）未启动时，页面可打开但业务接口会失败

server {
    listen       1995;
    server_name  _;

    root   /apollo/modules/awr_hmi/hmi/dist;
    index  index.html;

    charset utf-8;

    # gzip
    gzip               on;
    gzip_min_length    1k;
    gzip_comp_level    5;
    gzip_types         text/plain text/css application/javascript application/json image/svg+xml application/wasm;
    gzip_vary          on;

    # 带 hash 的静态资源长期缓存
    location ^~ /assets/ {
        access_log off;
        expires    30d;
        add_header Cache-Control "public, immutable";
        try_files  $uri =404;
    }

    # 运行时配置文件不缓存（每次部署可能变更后端地址）
    location = /config.yaml {
        add_header Cache-Control "no-cache, no-store, must-revalidate";
        try_files $uri =404;
    }

    # SPA history 路由 fallback：所有未命中文件的路径都返回 index.html
    location / {
        add_header Cache-Control "no-cache";
        try_files $uri $uri/ /index.html;
    }

    # ---- 以下为后端反向代理示例，启用后端时取消注释 ----
    # location /api/ {
    #     proxy_pass         http://127.0.0.1:8892;
    #     proxy_http_version 1.1;
    #     proxy_set_header   Host              $host;
    #     proxy_set_header   X-Real-IP         $remote_addr;
    #     proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    #     proxy_set_header   X-Forwarded-Proto $scheme;
    # }
    #
    # location /ws/ {
    #     proxy_pass         http://127.0.0.1:8892;
    #     proxy_http_version 1.1;
    #     proxy_set_header   Upgrade    $http_upgrade;
    #     proxy_set_header   Connection "upgrade";
    #     proxy_set_header   Host       $host;
    #     proxy_read_timeout 3600s;
    # }
}
EOF

    if [ $? -ne 0 ]; then
        echo "❌ 写入 Nginx 配置失败: $nginx_conf_file"
        return 1
    fi
    echo "✅ Nginx 配置文件已更新: $nginx_conf_file"

    # 测试 Nginx 配置
    if ! sudo nginx -t; then
        echo "❌ Nginx 配置测试失败，请检查配置文件语法"
        return 1
    fi
    echo "✅ Nginx 配置测试通过"

    # 重启 Nginx（优先 systemctl，失败时尝试 reload，最后兜底 nginx -s reload）
    if sudo systemctl restart nginx 2>/dev/null; then
        echo "✅ Nginx 服务已重新启动 (systemctl restart)"
        return 0
    elif sudo nginx -s reload 2>/dev/null; then
        echo "✅ Nginx 配置已 reload (nginx -s reload)"
        return 0
    else
        echo "⚠️  Nginx 重启/reload 失败，请手动检查 nginx 服务状态"
        return 1
    fi
}

# 用户交互函数（YES_TO_ALL 时直接返回 true，USE_DEFAULT_OPTIONS 时返回默认值）
ask_user() {
    local prompt="$1"
    local default="$2"  # y 或 n
    local ignore_default_options="${3:-0}"

    # RUN_CONFIG 模式：按 onFailurePolicy.default 决策；abort 时回退到默认值，
    # continue 时强制 yes（与原 YES_TO_ALL 行为一致）。
    if [ "$YES_TO_ALL" -eq 1 ] && [ -n "$RUN_CFG_FAIL_POLICY" ]; then
        if [ "$RUN_CFG_FAIL_POLICY" = "abort" ]; then
            echo -e "${YELLOW}$prompt [策略: abort, 自动选 $default]${NO_COLOR}"
            [ "$default" = "y" ] && return 0 || return 1
        fi
        echo -e "${YELLOW}$prompt [策略: continue, 自动选 y]${NO_COLOR}"
        return 0
    fi

    # 如果是 YES_TO_ALL，直接返回 true（不询问用户）
    if [ "$YES_TO_ALL" -eq 1 ]; then
        echo -e "${YELLOW}$prompt [自动选择: y]${NO_COLOR}"
        return 0
    fi

    if [ "$USE_DEFAULT_OPTIONS" -eq 1 ] && [ "$ignore_default_options" -ne 1 ]; then
        echo -e "${YELLOW}$prompt [自动选择默认值: $default]${NO_COLOR}"
        [ "$default" = "y" ] && return 0 || return 1
    fi

    # 交互式询问
    while true; do
        if [ "$default" = "y" ]; then
            read -p "$(echo -e "${YELLOW}$prompt [Y/n]: ${NO_COLOR}")" answer
            answer=${answer:-y}
        else
            read -p "$(echo -e "${YELLOW}$prompt [y/N]: ${NO_COLOR}")" answer
            answer=${answer:-n}
        fi

        case "$answer" in
            [Yy]* ) return 0;;
            [Nn]* ) return 1;;
            * ) echo "请输入 y 或 n";;
        esac
    done
}

# 网络不可用或 resources 下载失败时，选择终止或使用本地 fallback 继续。
choose_resource_failure_action() {
    local reason="$1"

    echo -e "${YELLOW}⚠️ $reason${NO_COLOR}"
    if [ "$YES_TO_ALL" -eq 1 ]; then
        if [ "${RUN_CFG_FAIL_POLICY:-continue}" = "abort" ]; then
            echo "❌ 非交互策略为 abort，终止启动"
            return 1
        fi
        echo "ℹ️ 非交互模式：跳过 resources 下载，继续使用本地资源"
        RESOURCE_OFFLINE_MODE=1
        DOWNLOAD_RESOURCES=0
        if [ ! -d "/mnt/gaea/resources/${THIS_DEVICE_ID}" ]; then
            RESOURCE_USE_LOCAL_FALLBACK=1
        fi
        return 0
    fi

    echo "  1) 终止脚本"
    echo "  2) 跳过 resources 下载，使用本地资源继续"
    while true; do
        local choice
        read -p "$(echo -e "${YELLOW}请选择 [1/2，默认 2]: ${NO_COLOR}")" choice
        choice=${choice:-2}
        case "$choice" in
            1) return 1 ;;
            2)
                RESOURCE_OFFLINE_MODE=1
                DOWNLOAD_RESOURCES=0
                if [ ! -d "/mnt/gaea/resources/${THIS_DEVICE_ID}" ]; then
                    RESOURCE_USE_LOCAL_FALLBACK=1
                fi
                return 0
                ;;
            *) echo "无效输入: $choice，请输入 1 或 2" ;;
        esac
    done
}

show_current_selection() {
    # 仅展示当前已经选择的选项情况
    echo "=========================================="
    echo "当前已通过命令行参数指定的选项："
    [ "$KILL_ALL_APOLLO" -eq 1 ] && echo "  ✓ 强制清理所有 Apollo 进程 (-f)"
    [ "$DOWNLOAD_RESOURCES" -eq 1 ] && echo "  ✓ 下载标定资源 (-d)"
    [ "$ENABLE_FORCE_CHECK" -eq 1 ] && echo "  ✓ 启用六维力检测 (--force-check)"
    [ -n "$RECIPE_TYPE" ] && echo "  ✓ recipe 类型: $RECIPE_TYPE (--recipe-type)"
    [ "$SKIP_COREDUMP_SETUP" -eq 1 ] && echo "  ✓ 跳过 start_awr coredump 覆盖 (--skip-coredump)"
    [ "$DEPLOY_REGION" ] && echo "  ✓ 部署地区: $DEPLOY_REGION (--region)"
    [ "$YES_TO_ALL" -eq 1 ] && echo "  ✓ 所有确认选择 yes (-y)"
    [ "$USE_DEFAULT_OPTIONS" -eq 1 ] && echo "  ✓ 后续选项使用默认值"
    echo "=========================================="
    echo ""
    echo "=========================================="
    echo "✅ 命令行指定选项完成，开始执行..."
    echo "=========================================="
    echo ""
}

# 选择 recipe 类型并写入参数服务器（供其它组件读取）
# 取值: 1=THD30 2=THHB 3=C134 4=WAIC 5=AIO
select_and_set_recipe_type() {
    local choice=""
    local selected=""

    if [ -n "$RECIPE_TYPE" ]; then
        selected="$RECIPE_TYPE"
        echo "✅ 使用已指定 recipe 类型: $selected"
    elif [ "$YES_TO_ALL" -eq 1 ]; then
        selected="THD30"
        echo "✅ 非交互模式：recipe 类型默认 THD30"
    else
        echo ""
        echo "=========================================="
        echo " 请选择 recipe 类型（写入 ${RECIPE_TYPE_PARAM}）"
        echo "=========================================="
        echo "  1) THD30"
        echo "  2) THHB"
        echo "  3) C134"
        echo "  4) WAIC"
        echo "  5) AIO"
        echo "=========================================="
        while true; do
            read -p "$(echo -e "${YELLOW}请输入序号 [1-5]: ${NO_COLOR}")" choice
            case "$choice" in
                1) selected="THD30"; break ;;
                2) selected="THHB"; break ;;
                3) selected="C134"; break ;;
                4) selected="WAIC"; break ;;
                5) selected="AIO"; break ;;
                *) echo "无效输入: $choice，请输入 1-5" ;;
            esac
        done
    fi

    RECIPE_TYPE="$selected"
    if cyber_params_client set "$RECIPE_TYPE_PARAM" string "$RECIPE_TYPE" > /dev/null 2>&1; then
        echo "✅ 已写入参数服务器: $RECIPE_TYPE_PARAM = $RECIPE_TYPE"
        return 0
    fi
    echo "⚠️ 写入参数服务器失败: $RECIPE_TYPE_PARAM = $RECIPE_TYPE"
    return 1
}

# ==================== MCU 固件版本门禁 ====================
# thor-soc 镜像自带的 MCU OTA 工具，由其 --probe 子命令读取 thor-mcu 固件版本。
# 新版镜像已安装为无后缀可执行文件（带 shebang / 二进制），直接执行而非 python3 调用。
RH850_OTA_TOOL="/usr/local/bin/rh850_udp_ota"
# /etc/tars_fw_version 第一行记录的 thor-soc 镜像版本（需严格一致）。
TARS_FW_VERSION_FILE="/etc/tars_fw_version"
EXPECTED_SOC_VERSION="thor_v5.3a"
THOR_IMAGES_DOWNLOAD_URL="http://10.100.100.51:8080/gitlab-ci/Thor_Images/"
# 期望的 thor-mcu 固件版本关键字（升级 MCU 固件后同步在此处更新）。
EXPECTED_MCU_VERSION_KEYWORD="v20260710.103100"
#EXPECTED_MCU_VERSION_KEYWORD="v20260611.173257"
# 期望的 PMU 电源板固件版本（升级 PMU 固件后同步在此处更新）。
EXPECTED_PMU_FIRMWARE_VERSION="V200R004B002SP1"
EXPECTED_PMU_FIRMWARE_vERSION="v200R004B002SP1"
PMU_OTA_SCRIPT="/apollo/scripts/tools/PMU_OTA.py"

# 任一 SOC/MCU/PMU .run 更新成功后置 1；全部门禁结束后统一询问是否整机上下电。
FIRMWARE_FLASHED=0

prompt_power_cycle_if_firmware_flashed() {
    [ "${FIRMWARE_FLASHED:-0}" -eq 1 ] || return 0
    echo -e "${YELLOW}⚠️ 本次有固件更新，新版本需整机重新上下电后生效${NO_COLOR}"
    if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
        echo -e "${YELLOW}是否对整机重新上下电使新固件生效? [自动选择默认值: Y]${NO_COLOR}"
        echo -e "${GREEN}请对整机做一次完整上下电，然后再重新运行本脚本。${NO_COLOR}"
        exit 0
    elif [ "$YES_TO_ALL" -eq 0 ]; then
        local power_choice=""
        read -p "$(echo -e "${YELLOW}是否对整机重新上下电使新固件生效? [Y/n]: ${NO_COLOR}")" power_choice
        power_choice=${power_choice:-Y}
        case "$power_choice" in
            [Nn]* )
                echo -e "${YELLOW}⚠️ 用户选择暂不上下电${NO_COLOR}"
                return 0
                ;;
        esac
        echo -e "${GREEN}请对整机做一次完整上下电，然后再重新运行本脚本。${NO_COLOR}"
        exit 0
    else
        echo -e "${YELLOW}是否对整机重新上下电使新固件生效? [YES_TO_ALL → Y]${NO_COLOR}"
        echo -e "${GREEN}请对整机做一次完整上下电，然后再重新运行本脚本。${NO_COLOR}"
        exit 0
    fi
}

# 启动节点前校验 thor-soc 镜像与 thor-mcu 固件版本，规则：
#   2.0 /etc/tars_fw_version 不等于期望 SOC 版本 → 提示升级；用户确认可强制使用当前镜像继续
#   2.1 OTA 工具不存在 → thor-soc 镜像过旧，提示更新 thor-soc 镜像（见 releasenote）
#   2.2 --probe 超时 / 返回 ok!=1 / 版本不含期望关键字 → 提示更新 thor-mcu 固件（见 releasenote）
# 任一不满足返回非 0（2=SOC/镜像侧，1=MCU 固件），由调用方负责退出整个启动流程。
check_mcu_firmware() {
    local soc_forced=0
    local mcu_just_flashed=0
    echo "=========================================="
    echo "🔌 校验 thor-soc 镜像与 thor-mcu 固件版本"
    echo "=========================================="

    local soc_version
    soc_version=$(head -n1 "$TARS_FW_VERSION_FILE" 2>/dev/null \
                  | tr -d '\r' \
                  | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')  
    if [ -z "$soc_version" ] || [ "$soc_version" != "$EXPECTED_SOC_VERSION" ]; then
        echo -e "${RED}❌ SOC 镜像版本不符（当前: ${soc_version:-未知}，要求: $EXPECTED_SOC_VERSION) ${NO_COLOR}"
        
        if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
            echo -e "${YELLOW}是否自动从 ${THOR_IMAGES_DOWNLOAD_URL} 下载对应版本固件? [自动选择默认值: n]${NO_COLOR}"
            return 2
        elif [ "$YES_TO_ALL" -eq 0 ]; then
            local download_choice=""
            read -p "$(echo -e "${YELLOW}是否自动下载并更新固件? [y/N]: ${NO_COLOR}")" download_choice
            download_choice=${download_choice:-n}
            
            case "$download_choice" in
                [Yy]* )
                    local firmware_file="${EXPECTED_SOC_VERSION}.run"
                    local APOLLO_REAL="$(readlink -f "/apollo")"
                    local PACKAGE_DIR="$(realpath "${APOLLO_REAL}/../..")"
                    if [ -f "$PACKAGE_DIR/${firmware_file}" ]; then
                        echo -e "${YELLOW}⚠️ ${PACKAGE_DIR}/${firmware_file} 文件已存在，跳过下载${NO_COLOR}"
                    else
                        echo -e "${YELLOW}⏳ 正在从 ${THOR_IMAGES_DOWNLOAD_URL}/${firmware_file} 下载固件...${NO_COLOR}"
                        if wget -O "$PACKAGE_DIR/${firmware_file}" "${THOR_IMAGES_DOWNLOAD_URL}/${firmware_file}" 2>/dev/null; then
                            echo -e "${GREEN}✅ SOC下载成功，开始更新SOC...${NO_COLOR}"
                        else
                            echo -e "${RED}❌ SOC下载失败，请手动下载${NO_COLOR}"
                            echo -e "${YELLOW}   下载地址: ${THOR_IMAGES_DOWNLOAD_URL}${NO_COLOR}"
                            return 2
                        fi
                    fi
                    bash "$PACKAGE_DIR/${firmware_file}" || return 2
                    FIRMWARE_FLASHED=1
                    echo -e "${GREEN}✅ SOC 镜像更新成功（待全部固件流程结束后询问上下电）${NO_COLOR}"
                    ;;
                [Nn]* )
                    # 询问是否强制继续
                    local force_choice=""
                    read -p "$(echo -e "${YELLOW}是否强制使用当前 SOC 镜像版本继续? [y/N]: ${NO_COLOR}")" force_choice
                    force_choice=${force_choice:-n}
                    case "$force_choice" in
                        [Yy]* )
                            soc_forced=1
                            echo -e "${YELLOW}⚠️ 已强制使用当前 SOC 镜像 (${soc_version:-未知})，继续 MCU 固件校验${NO_COLOR}"
                            ;;
                        [Nn]* )
                            return 2
                            ;;
                    esac
                    ;;
                * )
                    return 2
                    ;;
            esac
        else
            return 2
        fi
    else
        echo -e "${GREEN}✅ SOC 镜像: $soc_version${NO_COLOR}"
    fi
    
        # 2.1 thor-soc 镜像是否自带 MCU OTA 工具
    if [ ! -f "$RH850_OTA_TOOL" ]; then
        echo -e "${RED}❌ 未找到 MCU OTA 工具: $RH850_OTA_TOOL${NO_COLOR}"
        if [ "$soc_forced" -eq 0 ]; then
            echo -e "${RED}   thor-soc 镜像版本过旧，请先更新 thor-soc 镜像后重试。${NO_COLOR}"
            echo -e "${YELLOW}   请前往 ${THOR_IMAGES_DOWNLOAD_URL} 下载对应版本固件。${NO_COLOR}"
            echo -e "${YELLOW}   更新方法见 releasenote。${NO_COLOR}"
        fi
        return 2
    fi

    # 2.2 探测 MCU 版本；外层 timeout 仅作兜底，防止工具本身卡死不返回。
    echo "正在探测 MCU 版本: $RH850_OTA_TOOL --probe"
    local probe_out probe_rc
    probe_out=$(timeout 30 "$RH850_OTA_TOOL" --probe 2>&1)
    probe_rc=$?
    echo "$probe_out"

    if [ "$probe_rc" -eq 124 ]; then
        echo -e "${RED}❌ MCU 探测超时（工具未在 30s 内返回）${NO_COLOR}"
        echo -e "${RED}   请更新 thor-mcu 固件后重试，更新方法见 releasenote。${NO_COLOR}"
        return 1
    fi

    # 解析 python repr 风格输出：ok=0/1，version='...'
    local ok_val mcu_version
    ok_val=$(echo "$probe_out" | grep -m1 '^ok=' | sed 's/^ok=//; s/[^0-9].*$//')
    mcu_version=$(echo "$probe_out" | grep -m1 '^version=' | sed "s/^version=//; s/^['\"]//; s/['\"].*$//")

    # ok!=1：通常为 ACK 超时（reason 已在上方原样打印）
    if [ "$ok_val" != "1" ]; then
        echo -e "${RED}❌ MCU 探测失败 (ok=${ok_val:-未知})，可能为通信/ACK 超时${NO_COLOR}"
        echo -e "${RED}   请更新 thor-mcu 固件后重试，更新方法见 releasenote。${NO_COLOR}"
        return 1
    fi

    if [ -z "$mcu_version" ]; then
        echo -e "${RED}❌ 探测返回 ok=1 但未解析到版本号${NO_COLOR}"
        echo -e "${RED}   请更新 thor-mcu 固件后重试，更新方法见 releasenote。${NO_COLOR}"
        return 1
    fi

echo "📟 当前 MCU 版本: $mcu_version （期望包含关键字: $EXPECTED_MCU_VERSION_KEYWORD）"
if ! echo "$mcu_version" | grep -q "$EXPECTED_MCU_VERSION_KEYWORD"; then
    echo -e "${RED}❌ MCU 固件版本不符（当前 '$mcu_version' 不含 '$EXPECTED_MCU_VERSION_KEYWORD'）${NO_COLOR}"
    
    if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
        echo -e "${YELLOW}是否自动从 ${THOR_IMAGES_DOWNLOAD_URL} 下载对应版本固件? [自动选择默认值: n]${NO_COLOR}"
        return 1
    elif [ "$YES_TO_ALL" -eq 0 ]; then
        local download_choice=""
        read -p "$(echo -e "${YELLOW}是否自动下载并更新 MCU 固件? [y/N]: ${NO_COLOR}")" download_choice
        download_choice=${download_choice:-n}
        
        case "$download_choice" in
            [Yy]* )
                local mcu_firmware_file="mcu_${EXPECTED_MCU_VERSION_KEYWORD}.run"
                local download_url="${THOR_IMAGES_DOWNLOAD_URL}/${mcu_firmware_file}"
                local APOLLO_REAL="$(readlink -f "/apollo")"
                local PACKAGE_DIR="$(realpath "${APOLLO_REAL}/../..")"
                if [ -f "$PACKAGE_DIR/${mcu_firmware_file}" ]; then
                    echo -e "${YELLOW}⚠️ ${PACKAGE_DIR}/${mcu_firmware_file} 固件文件已存在，跳过下载${NO_COLOR}"
                else
                    echo -e "${YELLOW}⏳ 正在从 ${THOR_IMAGES_DOWNLOAD_URL}/${mcu_firmware_file} 下载 MCU 固件到${PACKAGE_DIR}{NO_COLOR}"
    
                    if wget -O "$PACKAGE_DIR/$mcu_firmware_file" "$download_url" 2>/dev/null; then
                        echo -e "${GREEN}✅ MCU 固件下载成功，正在更新...${NO_COLOR}"
                    else
                        echo -e "${RED}❌ MCU 固件下载失败${NO_COLOR}"
                        echo -e "${YELLOW}   下载地址: ${THOR_IMAGES_DOWNLOAD_URL}${NO_COLOR}"
                        return 1
                    fi
                fi

                if bash "$PACKAGE_DIR/$mcu_firmware_file"; then
                    FIRMWARE_FLASHED=1
                    mcu_just_flashed=1
                    echo -e "${GREEN}✅ MCU 固件更新成功（待全部固件流程结束后询问上下电）${NO_COLOR}"
                else
                    echo -e "${RED}❌ MCU 固件更新失败${NO_COLOR}"
                    return 1
                fi

                ;;
            [Nn]* )
                echo -e "${RED}❌ MCU 固件版本不符，用户选择不更新${NO_COLOR}"
                return 1
                ;;
            * )
                return 1
                ;;
        esac
    else
        return 1
    fi
fi

    # 刚刷写时读到的仍是旧分区版本，勿误报「校验通过」
    if [ "$mcu_just_flashed" -eq 1 ]; then
        echo -e "${YELLOW}⚠️ MCU 已刷写，版本待整机上下电后再校验（当前探测仍为: $mcu_version）${NO_COLOR}"
    else
        echo -e "${GREEN}✅ MCU 固件版本校验通过: $mcu_version${NO_COLOR}"
    fi
    return 0
}

# 启动节点前校验 PMU 电源板固件版本（READ_ID / GET_ID）：
#   通过 PMU_OTA.py --get-version 读取版本，与 EXPECTED_PMU_FIRMWARE_VERSION 严格一致才继续。
# 放在 kill_all_nodes 之后（释放 UDP 40189，避免与旧 power_udp_daemon 冲突）。
check_pmu_firmware() {
    local pmu_just_flashed=0
    echo "=========================================="
    echo "🔌 校验 PMU 电源板固件版本"
    echo "=========================================="

    local pmu_ota_script="$PMU_OTA_SCRIPT"
    if [ ! -f "$pmu_ota_script" ]; then
        echo -e "${RED}❌ 未找到 PMU OTA 工具: $pmu_ota_script${NO_COLOR}"
        return 1
    fi

    echo "正在探测 PMU 版本: python3 $pmu_ota_script --get-version"
    local pmu_version probe_rc probe_err
    probe_err=$(mktemp)
    pmu_version=$(timeout 30 python3 "$pmu_ota_script" --get-version 2>"$probe_err")
    probe_rc=$?
    if [ -s "$probe_err" ]; then
        cat "$probe_err"
    fi
    rm -f "$probe_err"

    if [ "$probe_rc" -eq 124 ]; then
        echo -e "${RED}❌ PMU 版本探测超时（工具未在 30s 内返回）${NO_COLOR}"
        return 1
    fi

    if [ "$probe_rc" -ne 0 ]; then
        echo -e "${RED}❌ PMU 版本探测失败 (exit=${probe_rc})${NO_COLOR}"
        return 1
    fi

    pmu_version=$(echo "$pmu_version" | tail -n1 | tr -d '\r' \
                  | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')

    if [ -z "$pmu_version" ]; then
        echo -e "${RED}❌ PMU 探测未返回版本号${NO_COLOR}"
        return 1
    fi

    echo "📟 当前 PMU 版本: $pmu_version （要求: $EXPECTED_PMU_FIRMWARE_VERSION）"
    if [ "$pmu_version" != "$EXPECTED_PMU_FIRMWARE_VERSION" ]; then
        echo -e "${RED}❌ PMU 固件版本不符（当前 '$pmu_version'，要求 '$EXPECTED_PMU_FIRMWARE_VERSION'）${NO_COLOR}"
        
        if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
            echo -e "${YELLOW}是否自动从 ${THOR_IMAGES_DOWNLOAD_URL} 下载对应版本固件? [自动选择默认值: n]${NO_COLOR}"
            return 1
        elif [ "$YES_TO_ALL" -eq 0 ]; then
            local download_choice=""
            read -p "$(echo -e "${YELLOW}是否自动下载并更新 PMU 固件? [y/N]: ${NO_COLOR}")" download_choice
            download_choice=${download_choice:-n}
            
            case "$download_choice" in
                [Yy]* )
                                    
                    local pmu_firmware_file="pmu_${EXPECTED_PMU_FIRMWARE_vERSION}.run"
                    local download_url="${THOR_IMAGES_DOWNLOAD_URL}/${pmu_firmware_file}"
                    local APOLLO_REAL="$(readlink -f "/apollo")"
                    local PACKAGE_DIR="$(realpath "${APOLLO_REAL}/../..")"
                    if [ -f "$PACKAGE_DIR/${pmu_firmware_file}" ]; then
                        echo -e "${YELLOW}⚠️ $PACKAGE_DIR/${pmu_firmware_file} PMU 固件文件已存在，跳过下载${NO_COLOR}"
                    else
                        echo -e "${YELLOW}⏳ 正在从 ${download_url} 下载 PMU 固件到${PACKAGE_DIR}/${pmu_firmware_file}{NO_COLOR}"
                        if wget -O "${PACKAGE_DIR}/${pmu_firmware_file}" "$download_url" 2>/dev/null; then
                            echo -e "${GREEN}✅ PMU 固件下载成功，正在更新， OTA completed 后 10 会自动重启 ${NO_COLOR}"
                        else
                            echo -e "${RED}❌ PMU 固件下载失败${NO_COLOR}"
                            echo -e "${YELLOW}   下载地址: ${THOR_IMAGES_DOWNLOAD_URL}${NO_COLOR}"
                            return 1
                        fi
                    fi   
                    # 执行 PMU 固件更新（根据实际情况调整）
                    if bash "$PACKAGE_DIR/${pmu_firmware_file}"; then
                        FIRMWARE_FLASHED=1
                        pmu_just_flashed=1
                        echo -e "${GREEN}✅ PMU 固件更新成功（待全部固件流程结束后询问上下电）${NO_COLOR}"
                    else
                        echo -e "${RED}❌ PMU 固件更新失败${NO_COLOR}"
                        return 1
                    fi

                    ;;
                [Nn]* )
                    echo -e "${RED}❌ MCU 固件版本不符，用户选择不更新${NO_COLOR}"
                    return 1
                    ;;
                * )
                    return 1
                    ;;
            esac
        else
            return 1
        fi
fi

    # 刚刷写时读到的仍是旧分区版本，勿误报「校验通过」
    if [ "$pmu_just_flashed" -eq 1 ]; then
        echo -e "${YELLOW}⚠️ PMU 已刷写，版本待整机上下电后再校验（当前探测仍为: $pmu_version）${NO_COLOR}"
    else
        echo -e "${GREEN}✅ PMU 固件版本校验通过: $pmu_version${NO_COLOR}"
        # 版本匹配后发送 APPLY_OTA，等待 0x200 ACK
        echo "正在执行 PMU APPLY_OTA: python3 $pmu_ota_script --apply"
        local apply_rc=0
        timeout 30 python3 "$pmu_ota_script" --apply || apply_rc=$?
        if [ "$apply_rc" -eq 124 ]; then
            echo -e "${RED}❌ PMU APPLY_OTA 超时（工具未在 30s 内返回）${NO_COLOR}"
            return 1
        fi
        if [ "$apply_rc" -ne 0 ]; then
            echo -e "${RED}❌ PMU APPLY_OTA 失败 (exit=${apply_rc})${NO_COLOR}"
            return 1
        fi
        echo -e "${GREEN}✅ PMU APPLY_OTA 已确认${NO_COLOR}"
    fi
    return 0
}

# 设置工作目录
cd /apollo || {
    echo "Error: Cannot change directory to /apollo"
    exit 1
}

# 创建日志目录
LOG_DIR="/apollo/data/log/double_orin"
mkdir -p "$LOG_DIR" || {
    echo "Error: Cannot create log directory $LOG_DIR"
    exit 1
}

# 检查脚本是否已 source gaea.bashrc
if [ -z "$GAEA_SOURCED" ]; then
    # 检查文件是否存在
    if [ ! -f "gaea.bashrc" ]; then
        echo "Error: gaea.bashrc not found in /apollo directory"
        exit 1
    fi

    # source gaea.bashrc
    echo "Sourcing gaea.bashrc..."
    source gaea.bashrc

    # 设置标记，表示已 source
    export GAEA_SOURCED=1
fi

# 设置core dump路径
if [ "$SKIP_COREDUMP_SETUP" -eq 1 ]; then
    echo -e "${YELLOW}⚠️ 已跳过 start_awr coredump 覆盖：不修改 core_pattern${NO_COLOR}"
else
    echo "/apollo/data/core/core_%e.%p.%s" | sudo tee /proc/sys/kernel/core_pattern > /dev/null
    if [ $? -ne 0 ]; then
        echo -e "⚠️ ${RED}无法设置 core dump 路径，继续执行但可能无法生成 core dump 文件${NO_COLOR}"
        sleep 2
    else
        echo -e "✅ ${BLUE}已设置 core dump 路径: /apollo/data/core/core_%e.%p.%s${NO_COLOR}"
    fi
fi

# ==================== 主流程开始 ====================

# 步骤1: 显示当前模式
if [ "$AUTO_MODE" -eq 1 ]; then
    echo "=========================================="
    echo "🤖 自动模式 (--auto)"
    echo "=========================================="
else
    echo "=========================================="
    echo "👤 交互模式"
    echo "=========================================="
fi
echo ""

if [ "$AUTO_MODE" -eq 0 ] && [ "$RUN_CONFIG_LOADED" -eq 0 ] && [ "$YES_TO_ALL" -eq 0 ]; then
    if ask_user "是否后续全选默认选项？" "n"; then
        USE_DEFAULT_OPTIONS=1
        echo "✅ 后续选项将自动使用默认值（最终继续启动确认除外）"
    fi
fi

# 步骤1.1: 检查并配置 Nginx
if check_nginx_conf; then
    echo "✅ Nginx 配置检查和更新完成"
else
    if ask_user "Nginx 配置检查或更新失败，是否继续执行后续步骤？" "n"; then
        echo "✅ 用户选择继续执行后续步骤"
    else
        echo "❌ 用户选择停止脚本执行，退出"
        exit 1
    fi
fi

# 步骤1.2: 选择所在地区(shanghai or suzhou)
AWR_HMI_CONFIG="/apollo/modules/awr_hmi/hmi/dist/config.yaml"
if [ "$YES_TO_ALL" -eq 0 ] && [ "$USE_DEFAULT_OPTIONS" -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo "请选择当前部署地区："
    echo "  1) shanghai(默认)"
    echo "  2) suzhou"
    echo "=========================================="
    read -p "请输入选项 [1/2](默认 1): " region_choice
    region_choice=${region_choice:-1}
    case "$region_choice" in
        1) DEPLOY_REGION="shanghai" ;;
        2) DEPLOY_REGION="suzhou" ;;
        *) echo "⚠️ 无效选择，使用默认: shanghai"; DEPLOY_REGION="shanghai" ;;
    esac
elif [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
    echo "✅ 已自动选择默认部署地区: $DEPLOY_REGION"
fi

echo "✅ 部署地区: $DEPLOY_REGION"
# config.yaml 由前端 build 时从 hmi/public/config.yaml 拷贝到 dist/,除了 VITE_MOCK_API_BASE
# 和 VITE_FILE_BASE 两个跟部署地区相关的 URL 之外,其它字段(VITE_ROS_URL、VITE_TRACK_ENDPOINT、
# 各类 *_ENABLED 开关等)都不分地区,本脚本只 sed 替换地区相关的两行,其余字段保留 yaml 原值,
# 避免将来 public/config.yaml 新增字段时这里整体覆写导致丢失。
if [ ! -f "$AWR_HMI_CONFIG" ]; then
    echo "❌ HMI 配置文件不存在: $AWR_HMI_CONFIG"
    exit 1
fi
if [ "$DEPLOY_REGION" = "suzhou" ]; then
    REGION_API_BASE="https://awr-backend-test.tars-ai.com/api"
    REGION_FILE_BASE="https://awr-backend-test.tars-ai.com"
else
    REGION_API_BASE="https://awr-backend-test.tars-ai.com/api"
    REGION_FILE_BASE="https://awr-backend-test.tars-ai.com"
fi
sed -i "s#^VITE_MOCK_API_BASE:.*#VITE_MOCK_API_BASE: '${REGION_API_BASE}'#" "$AWR_HMI_CONFIG"
sed -i "s#^VITE_FILE_BASE:.*#VITE_FILE_BASE: '${REGION_FILE_BASE}'#" "$AWR_HMI_CONFIG"
echo "✅ HMI 配置已更新: $AWR_HMI_CONFIG (region=$DEPLOY_REGION)"
echo "   VITE_MOCK_API_BASE: $(grep -E '^VITE_MOCK_API_BASE:' "$AWR_HMI_CONFIG")"
echo "   VITE_FILE_BASE:     $(grep -E '^VITE_FILE_BASE:' "$AWR_HMI_CONFIG")"

# 步骤2： 显示当前选择的选项
show_current_selection

#步骤3： 大包版本信息确认
APOLLO_LINK=$(readlink /apollo)
PACKAGE_NAME=$(echo "$APOLLO_LINK" | awk -F'/' '{print $(NF-1)}')
echo "📦 当前运行包名：$PACKAGE_NAME"
# 向用户确认是否需要是新大包：
if [ "$RUN_CONFIG_LOADED" -eq 1 ]; then
    # RUN_CONFIG 模式：完全信任 JSON 中的 killAllApollo / restartRosDocker
    echo "✅ RUN_CONFIG 模式：跳过新/旧大包询问，使用 JSON 取值 KILL_ALL_APOLLO=$KILL_ALL_APOLLO RESTART_ROS_DOCKER=$RESTART_ROS_DOCKER"
elif [ "$YES_TO_ALL" -eq 0 ] && [ "$KILL_ALL_APOLLO" -eq 0 ]; then
    if ask_user "当前 Apollo 运行包为 $PACKAGE_NAME，是否为刚部署的全新大包版本？" "n"; then
        echo "✅ 当前 Apollo 软件为新版本大包，需要清理所有节点..."
        KILL_ALL_APOLLO=1
    else
        echo "✅ 当前 Apollo 软件包为旧版本，继续使用当前包..."
    fi
else
    echo "✅ 已自动选择当前 Apollo 软件包为新版本，清理所有节点..."
    KILL_ALL_APOLLO=1
fi
# 清理节点
echo "正在清理 Apollo mainboard 进程..."

if [ "$KILL_ALL_APOLLO" -eq 1 ]; then
    bash /apollo/scripts/humanoid/kill_all_nodes.sh -f
else
    bash /apollo/scripts/humanoid/kill_all_nodes.sh
fi

# MCU 固件版本门禁：放在 kill_all_nodes 之后（UDP 15000 端口此时已被释放，避免
# 旧节点占用导致探测假性超时），且在云端标定下载等耗时步骤之前，尽早失败退出。
check_mcu_firmware
_fw_check_rc=$?
if [ "$_fw_check_rc" -eq 2 ]; then
    prompt_power_cycle_if_firmware_flashed
    exit 1
elif [ "$_fw_check_rc" -ne 0 ]; then
    echo -e "${RED}❌ thor-mcu 固件校验未通过，终止启动流程${NO_COLOR}"
    prompt_power_cycle_if_firmware_flashed
    exit 1
fi

check_pmu_firmware
_pmu_fw_check_rc=$?
if [ "$_pmu_fw_check_rc" -ne 0 ]; then
    echo -e "${RED}❌ PMU 固件校验未通过，终止启动流程${NO_COLOR}"
    prompt_power_cycle_if_firmware_flashed
    exit 1
fi

# thor-soc / thor-mcu / thor-pmu 全部门禁完成后：若本次有 .run 更新，统一询问整机上下电
prompt_power_cycle_if_firmware_flashed

sudo ntpdate pool.ntp.org
if ! sudo timeout 10 ntpdate pool.ntp.org; then
    echo "⚠️ 网络对时失败或超时，继续使用当前系统时间"
fi

# 获取sn号,通过参数服务器设置配置路径参数，必须在 kill_all_nodes之后，否则参数服务器会挂掉
setRobotType "$THIS_DEVICE_SN"

# parse_robot_global_params 在资源缺失时会尝试下载。先快速探测网络，
# 断网时允许用户终止，或直接使用仓库内 00 fallback 继续。
RESOURCE_API_URL="https://open.tars-ai.com/api/v1/device/resource/page"
if [ "$THIS_DEVICE_KNOWN" -eq 1 ] &&
    ! curl -sS --connect-timeout 3 --max-time 5 \
        -o /dev/null "$RESOURCE_API_URL"; then
    if ! choose_resource_failure_action "无法连接 resources 服务，当前可能没有网络"; then
        echo "❌ 用户选择终止脚本"
        exit 1
    fi
fi

parse_robot_global_params "$THIS_DEVICE_SN" "$RESOURCE_USE_LOCAL_FALLBACK"
_parse_params_rc=$?
if [ "$_parse_params_rc" -eq 2 ]; then
    if ! choose_resource_failure_action "resources 下载失败"; then
        echo "❌ 用户选择终止脚本"
        exit 1
    fi
    RESOURCE_USE_LOCAL_FALLBACK=1
    parse_robot_global_params "$THIS_DEVICE_SN" "$RESOURCE_USE_LOCAL_FALLBACK"
    _parse_params_rc=$?
fi
if [ "$_parse_params_rc" -ne 0 ]; then
    echo "❌ 解析机器人全局参数失败，脚本退出"
    exit 1
fi
echo "✅ 机器人全局参数配置成功"

# 将设备 SN 写入参数服务器，供下游模块按需读取（同 /apollo/global/robot_type 约定）
cyber_params_client set /apollo/global/device_sn string "$THIS_DEVICE_SN" > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "⚠️ 写入 /apollo/global/device_sn 失败，下游依赖该参数的模块可能异常"
else
    echo "✅ 已写入参数服务器: /apollo/global/device_sn = $THIS_DEVICE_SN"
fi

# 选择并写入 recipe 类型（参数服务器，供下游模块读取）
select_and_set_recipe_type

# 步骤4：显示本地/云端的标定资源版本信息
echo "=========================================="
echo "📋 检查标定资源版本信息"
echo "=========================================="

# 本地标定资源路径
CURRENT_LOCAL_CALIB_EXISTS=0
if [ "$THIS_DEVICE_KNOWN" -eq 1 ] &&
    [ "$RESOURCE_USE_LOCAL_FALLBACK" -eq 0 ]; then
    LOCAL_CALIB_DIR="/mnt/gaea/resources/${THIS_DEVICE_ID}"
else
    # 未识别 SN 或离线跳过下载：复用 parse_robot_global_params 的 00 fallback
    LOCAL_CALIB_DIR="/apollo/modules/resources/robot_configs/00"
    echo "ℹ️ 本地标定资源使用 fallback: $LOCAL_CALIB_DIR"
fi
LOCAL_CALIB_TIMESTAMP=""
LOCAL_CALIB_TIME_READABLE=""

# 检查本地标定文件
if [ -d "$LOCAL_CALIB_DIR" ]; then
    CURRENT_LOCAL_CALIB_EXISTS=1
    # 获取目录最后修改时间（秒级时间戳）
    LOCAL_CALIB_TIMESTAMP=$(stat -c %Y "$LOCAL_CALIB_DIR" 2>/dev/null)
    if [ -n "$LOCAL_CALIB_TIMESTAMP" ]; then
        LOCAL_CALIB_TIME_READABLE=$(date -d "@$LOCAL_CALIB_TIMESTAMP" "+%Y-%m-%d %H:%M:%S")
    else
        echo "⚠️  本地标定资源目录存在，但无法获取修改时间"
    fi
else
    CURRENT_LOCAL_CALIB_EXISTS=0
    echo "⚠️ 本地标定资源 $LOCAL_CALIB_DIR 不存在，必须从云端下载标定资源"
    DOWNLOAD_RESOURCES=1
fi

# 未识别 SN 或离线模式跳过云端查询/下载, 直接走本地 fallback
if [ "$THIS_DEVICE_KNOWN" -eq 0 ] || [ "$RESOURCE_OFFLINE_MODE" -eq 1 ]; then
    echo "ℹ️ 跳过云端标定资源版本查询与下载，使用本地资源继续启动"
    DOWNLOAD_RESOURCES=0
    SKIP_CLOUD_CALIB=1
else
    SKIP_CLOUD_CALIB=0
fi

# 获取云端标定资源信息
if [ "$SKIP_CLOUD_CALIB" -eq 0 ]; then
echo ""
echo "🌐 正在查询云端标定资源信息..."

CLOUD_CALIB_TIMESTAMP=""
CLOUD_CALIB_TIME_READABLE=""
API_URL="$RESOURCE_API_URL"
INTERNAL_TOKEN="c8f7b130-43a6-4eb2-904e-84a70ea9e5d2-4fc1d8f6e0ac0c5c6e1e7aa3f2bc3d9d"
sudo resolvectl dns dji0 8.8.8.8 1.1.1.1
echo "已临时设置dji0的DNS为8.8.8.8 1.1.1.1"
# 调用API获取云端资源信息CLOUD_CALIB_TIMESTAMP_MS
api_response=$(curl -s --connect-timeout 3 --max-time 10 --location "$API_URL" \
    --header 'accept: application/json, text/plain, */*' \
    --header 'content-type: application/json' \
    --header "X-Internal-Token: $INTERNAL_TOKEN" \
    --data "{\"sn\":\"$THIS_DEVICE_SN\"}")

# 解析JSON响应，提取resource类型的createTimestamp（毫秒级）
if [ -n "$api_response" ]; then
    # 使用jq解析JSON
    CLOUD_CALIB_TIMESTAMP_MS=$(echo "$api_response" | jq -r '.data.list[0].previewFiles[] | select(.resourceType == "resource") | .createTimestamp' 2>/dev/null)
    if [ -n "$CLOUD_CALIB_TIMESTAMP_MS" ] && [ "$CLOUD_CALIB_TIMESTAMP_MS" != "null" ]; then
        # 转换毫秒时间戳为秒
        CLOUD_CALIB_TIMESTAMP=$((CLOUD_CALIB_TIMESTAMP_MS / 1000))
        CLOUD_CALIB_TIME_READABLE=$(date -d "@$CLOUD_CALIB_TIMESTAMP" "+%Y-%m-%d %H:%M:%S")
    else
        echo "⚠️  云端未找到该设备的标定资源（SN: $THIS_DEVICE_SN）"
    fi
else
    if ! choose_resource_failure_action "无法连接到云端 API"; then
        echo "❌ 用户选择终止脚本"
        exit 1
    fi
    SKIP_CLOUD_CALIB=1
fi
fi  # end SKIP_CLOUD_CALIB guard

# 比较本地和云端版本
echo ""
echo "=========================================="
echo -e "${BLUE}本地标定文件文件夹最后更新时间: $LOCAL_CALIB_TIME_READABLE， 云端标定文件最后更新时间: $CLOUD_CALIB_TIME_READABLE${NO_COLOR}"

if [ "$CURRENT_LOCAL_CALIB_EXISTS" -eq 1 ]; then
    PERCEPTION_CALIB_PATH="$LOCAL_CALIB_DIR/config/sys/global/perception_calib.yaml"
    ROBOT_URDF_PATH="$LOCAL_CALIB_DIR/x_robot_description/share/x_robot_description/urdf/robot.urdf"

    if [ -f "$PERCEPTION_CALIB_PATH" ]; then
        PERCEPTION_CALIB_TS=$(stat -c %Y "$PERCEPTION_CALIB_PATH" 2>/dev/null)
        echo -e "本地标定文件最后更新时间 ${BOLD}perception_calib.yaml${NO_COLOR}  : ${YELLOW}$(date -d "@$PERCEPTION_CALIB_TS" "+%Y-%m-%d %H:%M:%S")${NO_COLOR}"
    else
        echo "本地标定文件 perception_calib.yaml  : 文件不存在"
    fi

    if [ -f "$ROBOT_URDF_PATH" ]; then
        ROBOT_URDF_TS=$(stat -c %Y "$ROBOT_URDF_PATH" 2>/dev/null)
        echo -e "本地urdf文件最后更新时间 ${BOLD}robot.urdf${NO_COLOR}             : ${YELLOW}$(date -d "@$ROBOT_URDF_TS" "+%Y-%m-%d %H:%M:%S")${NO_COLOR}"
    else
        echo "本地urdf文件 robot.urdf             : 文件不存在"
    fi
fi


UPDATES=""
if [ -n "$CLOUD_CALIB_TIMESTAMP" ]; then
    # 1. 检查标定文件夹
    if [ -n "$LOCAL_CALIB_TIMESTAMP" ] && [ "$CLOUD_CALIB_TIMESTAMP" -gt "$LOCAL_CALIB_TIMESTAMP" ]; then
        UPDATES="标定文件夹 "
    fi
    # 2. 检查 perception 标定文件
    if [ -n "$PERCEPTION_CALIB_TS" ] && [ "$CLOUD_CALIB_TIMESTAMP" -gt "$PERCEPTION_CALIB_TS" ]; then
        [ -n "$UPDATES" ] && UPDATES="${UPDATES}、"
        UPDATES="${UPDATES}perception标定文件 "
    fi
    # 3. 检查 urdf 文件
    if [ -n "$ROBOT_URDF_TS" ] && [ "$CLOUD_CALIB_TIMESTAMP" -gt "$ROBOT_URDF_TS" ]; then
        [ -n "$UPDATES" ] && UPDATES="${UPDATES}、"
        UPDATES="${UPDATES}urdf文件 "
    fi
fi

# 汇总输出
if [ -n "$UPDATES" ]; then
    echo -e "${YELLOW}提示：云端${UPDATES}有更新，建议下载云端标定资源${NO_COLOR}"
fi

echo "=========================================="

if [ "$SKIP_CLOUD_CALIB" -eq 1 ]; then
    echo "ℹ️ 已跳过云端标定资源下载"
else
    if [ "$YES_TO_ALL" -eq 0 ] && [ "$DOWNLOAD_RESOURCES" -eq 0 ]; then
        if ask_user "是否从数据平台下载最新标定文件？" "n"; then
            DOWNLOAD_RESOURCES=1
        fi
    else
        echo "✅ 已自动选择从云端下载标定资源"
        DOWNLOAD_RESOURCES=1
    fi

    if [ "$DOWNLOAD_RESOURCES" -eq 1 ]; then
        echo "✅ 已选择从云端下载标定资源"
        if ! bash /apollo/scripts/humanoid/download_resource_file.sh \
            "$THIS_DEVICE_SN" "resource" "open"; then
            if ! choose_resource_failure_action "标定资源下载失败"; then
                echo "❌ 用户选择终止脚本"
                exit 1
            fi
            if [ "$RESOURCE_USE_LOCAL_FALLBACK" -eq 1 ]; then
                if ! parse_robot_global_params "$THIS_DEVICE_SN" 1; then
                    echo "❌ 本地 00 fallback 参数配置失败，无法继续启动"
                    exit 1
                fi
                LOCAL_CALIB_DIR="/apollo/modules/resources/robot_configs/00"
            fi
        else
            echo "✅ 标定资源下载成功"
        fi
    fi
fi
echo ""

# 软件版本满足要求（上方 MCU 固件门禁已通过，否则脚本已退出）后，修正本机标定
# 资源中 can_config.yaml 的 CAN 总线编号：can8 -> can4。
#
# 放置点保证「标定文件已固定」：脚本内所有下载分支（-d / --auto / RUN_CONFIG /
# 本地缺失强制下载 / 交互选 yes）都汇聚到上方唯一的 download_resource_file.sh，
# 且该下载会先 rm -rf /mnt/gaea/resources 再解压；本段在其之后执行，之后直到
# 启动各模块不再触碰 resources，因此无论哪条下载路径，改的都是最终生效的文件。
#
# 仅对「已识别 SN」的云端标定目录生效：此时 LOCAL_CALIB_DIR=/mnt/gaea/resources/<ID>
# 在 git 仓库之外，sed 安全。未识别 SN 时 LOCAL_CALIB_DIR 回退到仓库内纳管的
# robot_configs/00（受 git 跟踪），不应被 sed 污染，故跳过。LOCAL_CALIB_DIR 按
# THIS_DEVICE_ID（例如 X1-70）动态取值，并非固定路径。
if [ "$THIS_DEVICE_KNOWN" -eq 1 ] &&
    [ "$RESOURCE_USE_LOCAL_FALLBACK" -eq 0 ]; then
    CAN_CONFIG_PATH="$LOCAL_CALIB_DIR/x_robot_description/share/x_robot_description/config/can_config.yaml"
    if [ -f "$CAN_CONFIG_PATH" ]; then
        if grep -q '^can8:' "$CAN_CONFIG_PATH"; then
            sed -i 's/^can8:/can4:/' "$CAN_CONFIG_PATH"
            echo "✅ 已修正 CAN 配置: can8 -> can4 ($CAN_CONFIG_PATH)"
        elif grep -q '^can4:' "$CAN_CONFIG_PATH"; then
            echo "ℹ️ CAN 配置已是 can4，无需修改 ($CAN_CONFIG_PATH)"
        else
            echo "⚠️ CAN 配置中未找到 can8/can4 总线定义，跳过 ($CAN_CONFIG_PATH)"
        fi
    else
        echo "⚠️ CAN 配置文件不存在，跳过 can8->can4 修正: $CAN_CONFIG_PATH"
    fi
else
    echo "ℹ️ 使用仓库内 fallback 标定（受 git 跟踪），跳过 can8->can4 修正"
fi
echo ""


# 步骤6: 六维力检测配置（控制 awr_robot_config.yaml 中的 enable_force_check）
AWR_ROBOT_CONFIG="/apollo/modules/awr_workflow/robot/conf/awr_robot_config.yaml"
if [ "$ENABLE_FORCE_CHECK" -eq 0 ]; then
    if [ "$RUN_CONFIG_LOADED" -eq 1 ]; then
        # RUN_CONFIG 模式：尊重 JSON 中的 forceCheck=false，不再强制开。
        echo "✅ RUN_CONFIG 模式：保持六维力检测禁用"
    elif [ "$YES_TO_ALL" -eq 1 ]; then
        echo "✅ 已自动选择禁用六维力检测"
    else
        if ask_user "是否启用六维力检测？" "y"; then
            ENABLE_FORCE_CHECK=1
        else
            ENABLE_FORCE_CHECK=0
        fi
    fi
fi
if [ "$ENABLE_FORCE_CHECK" -eq 1 ]; then
    sed -i 's/^\(\s*enable_force_check:\s*\).*/\1true # 是否启用六维力检测/' "$AWR_ROBOT_CONFIG"
else
    sed -i 's/^\(\s*enable_force_check:\s*\).*/\1false # 是否启用六维力检测/' "$AWR_ROBOT_CONFIG"
fi
echo "✅ 六维力检测配置已更新: $([ "$ENABLE_FORCE_CHECK" -eq 1 ] && echo '启用' || echo '禁用')"

# 步骤7: 确认 aruco marker 检测参数
ARUCO_DETECT_CONFIG="/apollo/modules/awr_workflow/perception/common/conf/aruco_detect.yaml"
if [ -f "$ARUCO_DETECT_CONFIG" ]; then
    # ---- 标定码类型选择：老码(aruco 单码) / 新码(charuco 整板，板中心 id24) ----
    CUR_DETECTOR_TYPE=$(awk -F'"' '/^detector_type:/{print $2; exit}' "$ARUCO_DETECT_CONFIG")
    [ -z "$CUR_DETECTOR_TYPE" ] && CUR_DETECTOR_TYPE="aruco"
    DETECTOR_TYPE="$CUR_DETECTOR_TYPE"

    if [ "$RUN_CONFIG_LOADED" -eq 1 ]; then
        _rc_dt=$(jq -r '.detectorType // empty' "$RUN_CONFIG_FILE")
        case "$_rc_dt" in
            aruco|charuco) DETECTOR_TYPE="$_rc_dt"; echo "✅ RUN_CONFIG 模式：检测码类型=${DETECTOR_TYPE}" ;;
            "") echo "✅ RUN_CONFIG 模式：未提供 detectorType，沿用当前(${CUR_DETECTOR_TYPE})" ;;
            *) echo "⚠️ RUN_CONFIG.detectorType 非法($_rc_dt)，沿用当前(${CUR_DETECTOR_TYPE})" ;;
        esac
    elif [ "$YES_TO_ALL" -eq 1 ] || [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
        echo "✅ 已自动沿用当前检测码类型: ${CUR_DETECTOR_TYPE}"
    else
        echo ""
        echo "请选择底盘精定位使用的标定码类型（当前: ${CUR_DETECTOR_TYPE}）："
        echo "  1) 老码 aruco  （单 ArUco 码）"
        echo "  2) 新码 charuco（整块 ChArUco 板，板中心 id24 定位）"
        _def_choice=1; [ "$CUR_DETECTOR_TYPE" = "charuco" ] && _def_choice=2
        read -p "请输入选项 [1/2](默认 ${_def_choice}): " _dt_choice
        _dt_choice=${_dt_choice:-$_def_choice}
        case "$_dt_choice" in
            2) DETECTOR_TYPE="charuco" ;;
            *) DETECTOR_TYPE="aruco" ;;
        esac
    fi

    sed -i "0,/^detector_type:.*/s//detector_type: \"${DETECTOR_TYPE}\"/" "$ARUCO_DETECT_CONFIG"
    echo "✅ 检测码类型已设置为: ${DETECTOR_TYPE}"

    if [ "$DETECTOR_TYPE" = "charuco" ]; then
        # 新码：板规格采用 aruco_detect.yaml 的 charuco: 段默认值，无需逐项确认 aruco 参数。
        echo "ℹ️ 使用新码 charuco，板规格采用默认值（11x9 / square 8mm / marker 6mm / DICT_4X4_50 / 板心 id24）。"
        echo "   如需修改请编辑 ${ARUCO_DETECT_CONFIG} 的 charuco: 段。"
    else
    # 读取当前配置值（只匹配顶层的 key，跳过 wire_grasp_aruco 子节点下的同名 key）
    CUR_DICT_ID=$(awk '/^dictionary_id:/{print $2; exit}' "$ARUCO_DETECT_CONFIG")
    CUR_MARKER_LEN=$(awk '/^marker_length_m:/{print $2; exit}' "$ARUCO_DETECT_CONFIG")
    CUR_TARGET_ID=$(awk '/^target_marker_id:/{print $2; exit}' "$ARUCO_DETECT_CONFIG")

    echo ""
    echo "=========================================="
    echo " 当前 ArUco Marker 检测参数："
    echo "  dictionary_id:    ${CUR_DICT_ID}"
    echo "  marker_length_m:  ${CUR_MARKER_LEN}"
    echo "  target_marker_id: ${CUR_TARGET_ID}"
    echo "=========================================="

    NEED_UPDATE_ARUCO=0
    if [ "$RUN_CONFIG_LOADED" -eq 1 ]; then
        # RUN_CONFIG 模式：JSON 给了 aruco 三元组就 sed 写 yaml，没给则保留当前值
        if [ -n "${RUN_CFG_ARUCO_DICT_ID:-}" ] || [ -n "${RUN_CFG_ARUCO_LEN_M:-}" ] || [ -n "${RUN_CFG_ARUCO_TARGET:-}" ]; then
            NEED_UPDATE_ARUCO=1
            NEW_DICT_ID=${RUN_CFG_ARUCO_DICT_ID:-$CUR_DICT_ID}
            NEW_MARKER_LEN=${RUN_CFG_ARUCO_LEN_M:-$CUR_MARKER_LEN}
            NEW_TARGET_ID=${RUN_CFG_ARUCO_TARGET:-$CUR_TARGET_ID}
            echo "✅ RUN_CONFIG 模式：将更新 ArUco 参数为 (${NEW_DICT_ID}, ${NEW_MARKER_LEN}, ${NEW_TARGET_ID})"
        else
            echo "✅ RUN_CONFIG 模式：未提供 aruco，沿用 yaml 当前值"
        fi
    elif [ "$YES_TO_ALL" -eq 1 ]; then
        echo "✅ 已自动确认当前 ArUco 参数"
    else
        if ! ask_user "以上 ArUco 参数是否正确？" "y"; then
            NEED_UPDATE_ARUCO=1
        fi
    fi

    if [ "$NEED_UPDATE_ARUCO" -eq 1 ]; then
        if [ "$RUN_CONFIG_LOADED" -eq 0 ]; then
            echo ""
            echo "请依次输入新的 ArUco 参数（直接回车保留当前值）："
            read -p "  dictionary_id [${CUR_DICT_ID}]: " NEW_DICT_ID
            NEW_DICT_ID=${NEW_DICT_ID:-$CUR_DICT_ID}

            read -p "  marker_length_m [${CUR_MARKER_LEN}]: " NEW_MARKER_LEN
            NEW_MARKER_LEN=${NEW_MARKER_LEN:-$CUR_MARKER_LEN}

            read -p "  target_marker_id [${CUR_TARGET_ID}]: " NEW_TARGET_ID
            NEW_TARGET_ID=${NEW_TARGET_ID:-$CUR_TARGET_ID}
        fi

        # 更新配置文件（仅替换顶层的 key，不影响 wire_grasp_aruco 下的同名 key）
        sed -i "0,/^dictionary_id:.*/s//dictionary_id: ${NEW_DICT_ID}/" "$ARUCO_DETECT_CONFIG"
        sed -i "0,/^marker_length_m:.*/s//marker_length_m: ${NEW_MARKER_LEN}/" "$ARUCO_DETECT_CONFIG"
        sed -i "0,/^target_marker_id:.*/s//target_marker_id: ${NEW_TARGET_ID}/" "$ARUCO_DETECT_CONFIG"

        echo ""
        echo "✅ ArUco 参数已更新:"
        echo "  dictionary_id:    ${NEW_DICT_ID}"
        echo "  marker_length_m:  ${NEW_MARKER_LEN}"
        echo "  target_marker_id: ${NEW_TARGET_ID}"
    else
        echo "✅ ArUco 参数确认无需修改"
    fi
    fi  # end 老码 aruco / 新码 charuco 分支
else
    echo "⚠️ ArUco 配置文件不存在: $ARUCO_DETECT_CONFIG, 无法检查或更新 ArUco 参数"
fi


# 步骤8: 选择 pose 模型对应的项目（TH / LZY）
# 不同项目使用不同的端子型号(kit_type)和对应的 pose 模型权重，必须二选一
PERCEPTION_THRESHOLDS_CONFIG="/apollo/modules/awr_workflow/perception/conf/perception_thresholds.yaml"
TH_POSE_MODEL="modules/awr_workflow/perception/model/pose/TH/best_pose_1536_1536.engine"
TH_POSE_CLASSES="modules/awr_workflow/perception/model/pose/TH/classes.txt"
LZY_POSE_MODEL="modules/awr_workflow/perception/model/pose/LZY/best_pose_832_960.engine"
LZY_POSE_CLASSES="modules/awr_workflow/perception/model/pose/LZY/classes.txt"

if [ -f "$PERCEPTION_THRESHOLDS_CONFIG" ]; then
    # 检测当前配置的项目（根据 pose_model 路径中是否含 /TH/ 或 /LZY/ 判定）
    CUR_POSE_MODEL_LINE=$(grep -E '^[[:space:]]*pose_model:[[:space:]]*"' "$PERCEPTION_THRESHOLDS_CONFIG" | head -n 1)
    if echo "$CUR_POSE_MODEL_LINE" | grep -q "/TH/"; then
        CUR_POSE_PROJECT="TH"
    elif echo "$CUR_POSE_MODEL_LINE" | grep -q "/LZY/"; then
        CUR_POSE_PROJECT="LZY"
    else
        CUR_POSE_PROJECT="UNKNOWN"
    fi

    case "$CUR_POSE_PROJECT" in
        TH) CUR_POSE_PROJECT_CN="T" ;;
        LZY) CUR_POSE_PROJECT_CN="LZY" ;;
        *) CUR_POSE_PROJECT_CN="UNKNOWN" ;;
    esac

    echo ""
    echo "=========================================="
    echo " 选择 pose 模型对应的项目"
    echo " 当前生效的项目: ${CUR_POSE_PROJECT_CN}"
    echo "=========================================="

    SELECTED_POSE_PROJECT=""
    if [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
        SELECTED_POSE_PROJECT="TH"
        echo "✅ 已自动选择默认项目: T"
    else
        while true; do
            read -p "$(echo -e "${YELLOW}请选择项目 [1=T(默认) / 2=LZY]: ${NO_COLOR}")" pose_choice
            pose_choice=${pose_choice:-1}
            case "$pose_choice" in
                1) SELECTED_POSE_PROJECT="TH"; break ;;
                2) SELECTED_POSE_PROJECT="LZY"; break ;;
                *) echo "请输入 1 或 2" ;;
            esac
        done
    fi

    if [ "$SELECTED_POSE_PROJECT" = "TH" ]; then
        NEW_POSE_MODEL="$TH_POSE_MODEL"
        NEW_POSE_CLASSES="$TH_POSE_CLASSES"
    else
        NEW_POSE_MODEL="$LZY_POSE_MODEL"
        NEW_POSE_CLASSES="$LZY_POSE_CLASSES"
    fi

    # 仅替换 model_paths 段的 pose_model / pose_classes（首个未注释行即可，其余段无同名 key）
    sed -i "0,/^[[:space:]]*pose_model:[[:space:]]*\"[^\"]*\\.engine\"/s#^\([[:space:]]*\)pose_model:[[:space:]]*\"[^\"]*\"#\1pose_model: \"${NEW_POSE_MODEL}\"#" "$PERCEPTION_THRESHOLDS_CONFIG"
    sed -i "0,/^[[:space:]]*pose_classes:[[:space:]]*\"/s#^\([[:space:]]*\)pose_classes:[[:space:]]*\"[^\"]*\"#\1pose_classes: \"${NEW_POSE_CLASSES}\"#" "$PERCEPTION_THRESHOLDS_CONFIG"

    case "$SELECTED_POSE_PROJECT" in
        TH) SELECTED_POSE_PROJECT_CN="T" ;;
        LZY) SELECTED_POSE_PROJECT_CN="LZY" ;;
    esac
    echo "✅ pose 模型项目已设置为: ${SELECTED_POSE_PROJECT_CN} (${SELECTED_POSE_PROJECT})"
    echo "   pose_model:   ${NEW_POSE_MODEL}"
    echo "   pose_classes: ${NEW_POSE_CLASSES}"
else
    echo "⚠️ perception_thresholds 配置文件不存在: $PERCEPTION_THRESHOLDS_CONFIG, 无法切换 pose 模型项目"
fi


# 定义函数：检查命令执行状态
check_status() {
    if [ $? -eq 0 ]; then
        echo "✓ $1 启动成功"
    else
        echo "✗ $1 启动失败"
        return 1
    fi
}

# 定义函数：启动模块并重定向日志
START_MODULE_LAST_PID=0

start_module() {
    local module_name=$1
    local command=$2
    local timestamp=$(date +"%Y%m%d.%H%M%S")
    local log_file="$LOG_DIR/${module_name}.log.${timestamp}"

    echo "启动 $module_name..."
    echo "日志文件: $log_file"

    # 执行命令并重定向输出
    $command > "$log_file" 2>&1 &
    local pid=$!
    ln -sfn "$(basename "$log_file")" "$LOG_DIR/${module_name}.log"

    sleep 2
    if kill -0 $pid 2>/dev/null; then
        echo "✓ $module_name 启动成功 (PID: $pid)"
        START_MODULE_LAST_PID=$pid
        return 0
    else
        echo "✗ $module_name 启动失败"
        echo "查看日志: tail -f $log_file"
        START_MODULE_LAST_PID=0
        return 1
    fi
}

# 记录启动时间
date +%s > /apollo/data/log/.pipeline_start_time

# 数据录制节点函数
function check_and_mount_hard_disk() {
    echo "准备复制cyber_player使用的配置文件..."
    local cyber_player_conf_path="/apollo/DEFAULT_CONFIG/META/runtime_calibration_awr.yaml"
    local target_conf_path="/apollo/DEFAULT_CONFIG/META/runtime_calibration.yaml"
    if [ -f "$cyber_player_conf_path" ]; then
        sudo cp "$cyber_player_conf_path" "$target_conf_path"
        sudo chmod 777 "$target_conf_path"
        echo "✅ 已复制配置文件到 $target_conf_path"
    else
        echo "⚠️ 默认配置文件 $cyber_player_conf_path 不存在，无法复制"
        return 1
    fi
    echo "准备复制awr专用配置文件..."
    local awr_calib_file_path=${PERCEPTION_CALIB_PATH}
    local awr_calib_target_path="/apollo/DEFAULT_CONFIG/META/perception_calib.yaml"
    if [ -f "$awr_calib_file_path" ]; then
        sudo cp "$awr_calib_file_path" "$awr_calib_target_path"
        sudo chmod 777 "$awr_calib_target_path"
        echo "✅ 已复制 AWR 专用配置文件到 $awr_calib_target_path"
    else
        echo "⚠️ AWR 专用配置文件 $awr_calib_file_path 不存在，无法复制"
        return 1
    fi

    echo "检查数据录制条件..."
    RDCS_PATH="/mnt/gaea/RDCS_ROOT"
    DEV_PATH=""

    # 识别根分区所在物理盘，排除误挂载系统盘
    local root_src root_disk
    root_src=$(findmnt -no SOURCE /)
    root_disk=$(lsblk -no PKNAME "$root_src" 2>/dev/null)
    [ -z "$root_disk" ] && root_disk=$(basename "$root_src")

    # 1) 优先认 tars_data_disk 标签盘
    if [ -e /dev/disk/by-label/tars_data_disk ]; then
        DEV_PATH=$(readlink -f /dev/disk/by-label/tars_data_disk)
    fi

    # 2) 无标签盘且 RDCS_PATH 已挂载，沿用当前设备
    if [ -z "$DEV_PATH" ] && mountpoint -q "$RDCS_PATH"; then
        DEV_PATH=$(findmnt -no SOURCE "$RDCS_PATH")
    fi

    # 3) 仍未确定 → 扫描候选 (RM=1 或 TRAN=usb，排除根盘)
    if [ -z "$DEV_PATH" ]; then
        echo "未发现 tars_data_disk 标签盘，扫描可用外接分区..."
        local -a candidates=()
        while IFS= read -r line; do
            local name pk
            name=$(awk '{print $1}' <<<"$line")
            pk=$(lsblk -no PKNAME "/dev/$name" 2>/dev/null)
            [ "$name" = "$root_disk" ] && continue
            [ "$pk" = "$root_disk" ] && continue
            candidates+=("$line")
        done < <(lsblk -rno NAME,SIZE,FSTYPE,LABEL,RM,TRAN | awk '$5=="1" || $6=="usb"')

        if [ ${#candidates[@]} -eq 0 ]; then
            echo ""
            if [ "$ALLOW_QUICKDATA_WITHOUT_DATA_DISK" -eq 1 ]; then
                if [ -L "$RDCS_PATH" ]; then
                    rm -f "$RDCS_PATH"
                fi
                sudo mkdir -p "$RDCS_PATH"
                if id nvidia >/dev/null 2>&1; then
                    sudo chown -R nvidia:nvidia "$RDCS_PATH"
                else
                    echo "⚠️  未找到 nvidia 用户，跳过 $RDCS_PATH 属主设置"
                fi
                echo -e "${YELLOW}=========================================="
                echo -e "⚠️  Warning：未检测到任何外接数据盘！"
                echo -e "=========================================="
                echo -e "  • 数据录制 (collect) 将无法工作"
                echo -e "  • quickdata 节点将继续以降级模式启动"
                echo -e "  • 已创建本地目录 $RDCS_PATH 并尝试设置 nvidia 用户权限"
                echo -e "  • 不创建 ts_collect 子目录，避免占用系统盘空间"
                echo -e "  • 如需完整数据录制，请插入数据盘并重新启动本脚本"
                echo -e "==========================================${NO_COLOR}"
            else
                echo -e "${RED}=========================================="
                echo -e "🚨 严重告警：未检测到任何外接数据盘！"
                echo -e "=========================================="
                echo -e "  • 数据录制 (collect) 将无法工作"
                echo -e "  • quickdata 节点将被禁止启动"
                echo -e "  • 请在启动后立即插入数据盘并重新启动本脚本"
                echo -e "==========================================${NO_COLOR}"
            fi
            echo ""
            return 1
        fi

        EXTERNAL_DATA_DISK_AVAILABLE=1
        echo "发现以下候选外接分区 (序号  NAME SIZE FSTYPE LABEL RM TRAN):"
        local i=0
        for c in "${candidates[@]}"; do
            echo "  [$i] $c"
            i=$((i + 1))
        done

        if [ "$YES_TO_ALL" -eq 1 ] || [ "$USE_DEFAULT_OPTIONS" -eq 1 ]; then
            echo "⚠️  自动/默认模式下不自动选择外接盘，跳过数据录制"
            return 1
        fi

        local choice
        read -p "请输入要挂载的分区编号 (留空跳过录制并降级运行): " choice
        if [ -z "$choice" ] || ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -ge "${#candidates[@]}" ]; then
            echo -e "${YELLOW}⚠️  未选择有效编号 → 跳过数据录制，进入降级模式（quickdata 将被禁用）${NO_COLOR}"
            return 1
        fi
        DEV_PATH="/dev/$(awk '{print $1}' <<<"${candidates[$choice]}")"
    fi
    if [ -n "$DEV_PATH" ]; then
        EXTERNAL_DATA_DISK_AVAILABLE=1
    fi

    # 已识别到外接盘：在交互模式下提供"不挂载"选项以降级运行
    if [ "$YES_TO_ALL" -eq 0 ]; then
        if ! ask_user "已识别到外接数据盘 $DEV_PATH，是否挂载并启用数据录制？(选 N 将降级运行，不挂载，禁用 quickdata)" "y"; then
            echo -e "${YELLOW}⚠️  用户选择不挂载外接盘 → 跳过数据录制，进入降级模式（quickdata 将被禁用）${NO_COLOR}"
            return 1
        fi
    fi

    # 4) 校验文件系统类型：必须 ext4
    local fstype
    fstype=$(lsblk -no FSTYPE "$DEV_PATH" 2>/dev/null)
    if [ "$fstype" != "ext4" ]; then
        echo "❌ $DEV_PATH 文件系统为 '${fstype:-未知}', 期望 ext4 → 拒绝挂载, 跳过录制"
        return 1
    fi

    # 5) 挂载状态与 async 模式校验
    if mountpoint -q "$RDCS_PATH"; then
        local cur_dev cur_opts
        cur_dev=$(findmnt -no SOURCE "$RDCS_PATH")
        cur_opts=$(findmnt -no OPTIONS "$RDCS_PATH")
        if [ "$cur_dev" != "$DEV_PATH" ]; then
            echo "⚠️  $RDCS_PATH 当前挂载到 $cur_dev (期望 $DEV_PATH), 卸载后重挂..."
            sudo umount "$RDCS_PATH" || { echo "❌ 卸载失败 → 跳过录制"; return 1; }
        elif grep -qw sync <<<"$cur_opts"; then
            echo "⚠️  当前挂载选项含 sync ($cur_opts), 重挂为 async..."
            sudo mount -o remount,async "$RDCS_PATH" || echo "⚠️  remount async 失败，继续使用现挂载"
        else
            echo "✅ $RDCS_PATH 已正确挂载 ($cur_dev, ext4, async)"
        fi
    fi

    # 6) 未挂载 → 准备挂载点并挂载
    if ! mountpoint -q "$RDCS_PATH"; then
        if [ -L "$RDCS_PATH" ]; then
            rm -f "$RDCS_PATH"
        elif [ -d "$RDCS_PATH" ] && [ -n "$(ls -A "$RDCS_PATH" 2>/dev/null)" ]; then
            mv "$RDCS_PATH" "${RDCS_PATH}-$(date +%Y%m%d%H%M%S)"
        fi
        sudo mkdir -p "$RDCS_PATH"
        echo "挂载 $DEV_PATH → $RDCS_PATH (ext4, async)..."
        if ! sudo mount -t ext4 -o async "$DEV_PATH" "$RDCS_PATH"; then
            echo "❌ 挂载失败 → 跳过数据录制"
            return 1
        fi
    fi
    sudo chmod 777 -R "$RDCS_PATH"
    COLLECT_RDCS_PATH="${RDCS_PATH}/ts_collect"
    if [ "$EXTERNAL_DATA_DISK_AVAILABLE" -eq 0 ]; then
        echo "⚠️  无外接数据盘，跳过创建 ts_collect 目录: $COLLECT_RDCS_PATH"
    else
        sudo mkdir -p "$COLLECT_RDCS_PATH"
        sudo chmod 777 "$COLLECT_RDCS_PATH"
        echo "✅ AWR 数据录制目录已准备: $COLLECT_RDCS_PATH"
    fi
    # 生成ts_collect所需的meta文件
    COLLECT_FILE="/apollo/DEFAULT_CONFIG/META/data_collect_files/data_collect_awr.yaml"
    echo "📦 当前运行包名：$PACKAGE_NAME"/apollo/DEFAULT_CONFIG/META/data_collect_files
    echo "📁 使用数据录制配置: $COLLECT_FILE，sn：$THIS_DEVICE_SN，id：$THIS_DEVICE_ID"
    final_collect_file="/apollo/DEFAULT_CONFIG/META/data_collect_files/data_collect_conf.yaml"
    sudo rm -f "$final_collect_file"
    # 将 device_ID 和 device_SN 写入配置文件开头，然后追加原配置内容
    {
        echo "device_ID: ${THIS_DEVICE_ID}  # 设备ID，所有设备ID需要统一存储管理"
        echo "device_SN: ${THIS_DEVICE_SN}  # 设备序列号，和设备ID一一映射"
        echo "software_version: ${PACKAGE_NAME}  # 设备运行的包版本"
        cat "$COLLECT_FILE"
    } > "$final_collect_file"
    echo "✅ 已将设备信息写入ts_collect META文件: ${final_collect_file}, device_ID=${THIS_DEVICE_ID}, device_SN=${THIS_DEVICE_SN}, software_version=${PACKAGE_NAME}"
    # 检查目录是否存在
    if [ ! -d "$RDCS_PATH" ]; then
        echo "⚠️  目录 $RDCS_PATH 不存在，跳过数据录制"
        return 1
    else
        # 获取可用空间（单位：GB）
        AVAILABLE_SPACE=$(df -BG "$RDCS_PATH" | awk 'NR==2 {print $4}' | sed 's/G//')
        echo "📊 $RDCS_PATH 可用空间: ${AVAILABLE_SPACE}G"
        sudo chmod 777 -R $RDCS_PATH
        if [ "$AVAILABLE_SPACE" -ge 100 ]; then
            echo "✅ 空间充足，允许启动数据录制节点..."
            return 0
        else
            echo "⚠️  可用空间不足（${AVAILABLE_SPACE}G < 100G），跳过数据录制"
            return 1
        fi
    fi
}

date +%s > /apollo/data/log/.pipeline_start_time

if check_and_mount_hard_disk; then
    DATA_RECORDING_ENABLED=1
    echo "已经做好数据录制准备...，请检查启动日志，确认硬盘挂载情况"
    if [ "$YES_TO_ALL" -eq 0 ]; then
        if ask_user "是否已经检查过硬盘挂载情况，并且要继续启动其他模块？" "n" 1; then
            echo "✅ 继续启动..."
        else
            echo "❌ 退出启动流程"
            #exit 1
        fi
    else
        echo "✅ 已自动选择继续启动其他模块"
    fi
else
    DATA_RECORDING_ENABLED=0
    echo ""
    if [ "$EXTERNAL_DATA_DISK_AVAILABLE" -eq 0 ] && [ "$ALLOW_QUICKDATA_WITHOUT_DATA_DISK" -eq 1 ]; then
        echo -e "${YELLOW}=========================================="
        echo -e "⚠️  Warning：未检测到外接数据盘 → quickdata 降级启动"
        echo -e "=========================================="
        echo -e "  • collect 不允许使用（无外接盘）"
        echo -e "  • quickdata 节点将继续启动"
        echo -e "  • 其他模块将继续启动以保证基础功能可用"
        echo -e "==========================================${NO_COLOR}"
    else
        echo -e "${RED}=========================================="
        echo -e "🚨 严重告警：数据录制未就绪 → 进入降级模式"
        echo -e "=========================================="
        echo -e "  • collect 不允许使用（用户未挂载 / 空间不足 / 文件系统不符）"
        echo -e "  • quickdata 节点将被禁止启动"
        echo -e "  • 其他模块将继续启动以保证基础功能可用"
        echo -e "==========================================${NO_COLOR}"
    fi
fi

# AWR 资源监控先于所有业务节点启动，尽量捕获启动阶段资源背景状态。
start_module "resource_monitor" "mainboard -d modules/ts_monitor/dag/resource_monitor_awr.dag"
RESOURCE_MONITOR_PID=$?

# 电源 ts_bridge：Cyber <-> power_udp_daemon UDP（须先于 daemon，监听 19527/9527）
POWER_BRIDGE_DAG="/apollo/modules/ts_Systempowermanager/bridgeConfig/bridge_system_power_loop.dag"
POWER_BRIDGE_MAINBOARD="/apollo/bazel-bin/cyber/mainboard/mainboard"
POWER_BRIDGE_SO="/apollo/bazel-bin/modules/ts_bridge/libts_bridge_component.so"
POWER_TS_BRIDGE_PID=0
if [[ -x "${POWER_BRIDGE_MAINBOARD}" && -f "${POWER_BRIDGE_DAG}" && -f "${POWER_BRIDGE_SO}" ]]; then
    if pgrep -f "bridge_system_power_loop" > /dev/null; then
        POWER_TS_BRIDGE_PID=$(pgrep -f "bridge_system_power_loop" | head -n 1)
        echo "⚠ power_ts_bridge 进程已存在，跳过启动 (PID: $POWER_TS_BRIDGE_PID)"
    else
        start_module "power_ts_bridge" "mainboard -d ${POWER_BRIDGE_DAG}"
        POWER_TS_BRIDGE_PID=$START_MODULE_LAST_PID
    fi
else
    echo "⚠️  跳过 power_ts_bridge：需 ${POWER_BRIDGE_MAINBOARD}、/apollo/${POWER_BRIDGE_DAG}、${POWER_BRIDGE_SO}"
    echo "    bazel build //cyber/mainboard:mainboard //modules/ts_bridge:libts_bridge_component.so"
fi

# 电源监控 daemon：MCU UDP -> CAN 解析 -> WebSocket / ts_bridge（直接启动二进制）
POWER_UDP_DAEMON="/apollo/bazel-bin/modules/ts_Systempowermanager/power_udp_daemon"
POWER_UDP_WS_JSON="${WS_MESSAGES_JSON:-/apollo/modules/ts_Systempowermanager/WebSocketCore/json/power_ws_messages.json}"
POWER_UDP_DAEMON_PID=0
if [[ -x "${POWER_UDP_DAEMON}" ]]; then
    if pgrep -f "${POWER_UDP_DAEMON}" > /dev/null; then
        POWER_UDP_DAEMON_PID=$(pgrep -f "${POWER_UDP_DAEMON}" | head -n 1)
        echo "⚠ power_udp_daemon 进程已存在，跳过启动 (PID: $POWER_UDP_DAEMON_PID)"
    elif [[ ! -f "${POWER_UDP_WS_JSON}" ]]; then
        echo "⚠️  未找到 ${POWER_UDP_WS_JSON}，跳过 power_udp_daemon 启动"
    else
        start_module "power_udp_daemon" "${POWER_UDP_DAEMON} --ws-messages-json ${POWER_UDP_WS_JSON}"
        POWER_UDP_DAEMON_PID=$START_MODULE_LAST_PID
    fi
else
    echo "⚠️  未找到 ${POWER_UDP_DAEMON}，跳过 power_udp_daemon 启动（需先 bazel build //modules/ts_Systempowermanager:power_udp_daemon）"
fi

echo "======================================="
echo "开始启动各个模块..."
echo "======================================="

# 启动各个模块

# xmotion / wbc / arm_planner 优先启动（运动控制层先就绪）
start_module "xmotion" "taskset -c 11 mainboard -d modules/awr_control/x_motion/dag/x1d.dag"
XMOTION_PID=$?

start_module "arm_planner" "/apollo/bazel-bin/modules/awr_workflow/wbp/app/wbp_server_node"
ARM_PLANNER_PID=$?

start_module "offline_simulator" "/apollo/bazel-bin/modules/awr_workflow/offline_simulator/app/offline_sim_node"
OFFLINE_SIMULATOR_PID=$?

start_module "trajectory_player" "/apollo/bazel-bin/modules/awr_workflow/offline_simulator/app/trajectory_player/start_node"
TRAJECTORY_PLAYER_PID=$?

start_module "okr_node" "/apollo/bazel-bin/modules/awr_workflow/online_kit_refiner/app/okr_node"
OKR_NODE_PID=$?

start_module "wbc" "mainboard -d modules/awr_workflow/x_wbc/src/dag/x_wbc.dag -p x_wbc"
WBC_PID=$?

# 启动相机
start_module "camera" "mainboard -d modules/drivers/tars_camera/dag/awr_full_10ch.dag"
CAMERA_PID=$?

# 内窥镜 YUV->JPEG 桥(awr_bridge -> web 前端只识别 JPEG，tars_camera 这边
# 已经把内窥镜 encode 关了，由本组件读 /yuv 转 JPEG 发 /compressed)
start_module "dndoscopic_jpeg" "mainboard -d modules/awr_bridge/dndoscopic_jpeg/dag/dndoscopic_jpeg.dag"

# 检查 bridge_web_backend 进程是否已存在
BRIDGE_WEB_PID=0
if pgrep -f "bridge_web_backend" > /dev/null; then
    BRIDGE_WEB_PID=$(pgrep -f "bridge_web_backend" | head -n 1)
    echo "⚠ bridge_web_backend 进程已存在，跳过启动 (PID: $BRIDGE_WEB_PID)"
else
    start_module "bridge_web" "mainboard -d modules/awr_bridge/dag/bridge_web_backend.dag"
    BRIDGE_WEB_PID=$?
fi

# 启动六维力传感器驱动，让驱动先于 robot 等消费方就绪
start_module "force_sensor" "mainboard -d modules/drivers/tars_force_sensor_drivers/dag/force_sensor_modbus.dag"
FORCE_SENSOR_PID=$?

echo "🔍 不区分架构 ($(uname -m))，不需要nsys了，直接启动 robot 模块..."
start_module "robot" "mainboard -d modules/awr_workflow/robot/dag/ts_aw_robot.dag"
ROBOT_PID=$?

start_module "slam" "mainboard -d modules/awr_workflow/slam/dag/x1.dag"
SLAM_PID=$?

start_module "mapping_verifier" "mainboard -d modules/awr_workflow/slam/dag/mapping_verifier.dag"
MAPPING_VERIFIER_PID=$?

start_module "task_manager" "mainboard -d modules/awr_workflow/task_manager/dag/task_manager.dag"
TASK_MGR_PID=$?

start_module "perception" "mainboard -d modules/awr_workflow/perception/perception/app/conf/perception_component.dag"
PERCEPTION_PID=$?

start_module "ts_e2e" "mainboard -d modules/ts_e2e/ts_e2e_fsd/launch/e2e.dag"
TS_E2E_PID=$?

start_module "qualitycheck_live" "mainboard -d modules/awr_qualitycheck/dag/qualitycheck_live.dag"
QUALITYCHECK_PID=$?

ROBOT_SERVICE_PID=0
if pgrep -f "robot_service_node" > /dev/null; then
    ROBOT_SERVICE_PID=$(pgrep -f "robot_service_node" | head -n 1)
    echo "⚠ robot_service_node 进程已存在，跳过启动 (PID: $ROBOT_SERVICE_PID)"
else
    start_module "robot_service" "mainboard -d modules/awr_bridge/robot_service/dag/robot_service_node.dag"
    ROBOT_SERVICE_PID=$?
fi

start_module "calibration" "mainboard -d modules/awr_bridge/calib/dag/calibration_common.dag"
CALIBRATION_PID=$?

if [ "$DATA_RECORDING_ENABLED" -eq 1 ] || \
    { [ "$EXTERNAL_DATA_DISK_AVAILABLE" -eq 0 ] && [ "$ALLOW_QUICKDATA_WITHOUT_DATA_DISK" -eq 1 ]; }; then
    start_module "quickdata" "mainboard -d modules/ts_quickdata/dag/quickdata_awr.dag"
    QUICKDATA_PID=$?
else
    echo -e "${RED}🚫 数据录制未就绪（外接盘未挂载或空间不足），禁止启动 quickdata 节点${NO_COLOR}"
    QUICKDATA_PID=0
fi
start_module "system_monitor" "mainboard -d modules/awr_bridge/robot_system_monitor/dag/robot_system_monitor_node.dag"

# ts_logcore: 单独起一个进程加载 LogCore(SIGUSR1 触发 + DumpWriter 增强 dump)。
# SHM 共享,所有进程日志已在同一个 ring(cyber::Init 自动挂 ShmSink,无需 dag);
# LogCore 只需一个进程:oncall kill -USR1 <logcore_pid> 即抓全设备日志。
start_module "ts_logcore" "mainboard -d modules/ts_logcore/dag/ts_logcore.dag"

# 保存所有进程ID到文件，便于管理
echo "保存进程信息..."
cat > /tmp/apollo_processes.pid << EOF
RESOURCE_MONITOR_PID=$RESOURCE_MONITOR_PID
POWER_TS_BRIDGE_PID=$POWER_TS_BRIDGE_PID
POWER_UDP_DAEMON_PID=$POWER_UDP_DAEMON_PID
POWER_TS_BRIDGE_LOG=$LOG_DIR/power_ts_bridge.log
POWER_UDP_DAEMON_LOG=$LOG_DIR/power_udp_daemon.log
BRIDGE_WEB_PID=$BRIDGE_WEB_PID
ROBOT_PID=$ROBOT_PID
SLAM_PID=$SLAM_PID
MAPPING_VERIFIER_PID=$MAPPING_VERIFIER_PID
TASK_MGR_PID=$TASK_MGR_PID
XMOTION_PID=$XMOTION_PID
WBC_PID=$WBC_PID
PERCEPTION_PID=$PERCEPTION_PID
TS_E2E_PID=$TS_E2E_PID
ROBOT_SERVICE_PID=$ROBOT_SERVICE_PID
QUICKDATA_PID=$QUICKDATA_PID
LOG_DIR=$LOG_DIR
QUALITYCHECK_PID=$QUALITYCHECK_PID
FORCE_SENSOR_PID=$FORCE_SENSOR_PID
CALIBRATION_PID=$CALIBRATION_PID
EOF

# 启动手柄控制
echo "启动手柄控制..."
JOY_LOG="$LOG_DIR/joy.log"

py_tars_joy_linux > "$JOY_LOG" 2>&1 &
JOY_PID=$!
sleep 2

if kill -0 $JOY_PID 2>/dev/null; then
    echo "✓ 手柄控制启动成功 (PID: $JOY_PID)"
    echo "日志文件: $JOY_LOG"
else
    echo "✗ 手柄控制启动失败"
    echo "查看日志: tail -f $JOY_LOG"
    JOY_PID=""
fi


start_module "awr_envcheck" "mainboard -d modules/awr_envcheck/dag/envcheck.dag"
start_module "awr_map_provision" "mainboard -d modules/awr_envcheck/modules/map_provision/dag/map_provision.dag"
start_module "awr_envcheck_base_fault_check" "mainboard -d modules/awr_envcheck/base_fault_check/dag/base_fault_check.dag"

ensure_oss_python_dependencies() {
    if ! command -v pip >/dev/null 2>&1 && \
        ! command -v pip3 >/dev/null 2>&1; then
        if ! sudo apt-get update >/dev/null 2>&1; then
            return 1
        fi
        if ! sudo apt-get install -y python3-pip >/dev/null 2>&1; then
            return 1
        fi
    fi

    if python3 -c 'import alibabacloud_oss_v2' >/dev/null 2>&1; then
        return 0
    fi

    if command -v pip >/dev/null 2>&1; then
        sudo pip install alibabacloud-oss-v2 \
            -i https://mirrors.aliyun.com/pypi/simple/ \
            --trusted-host mirrors.aliyun.com \
            --break-system-packages >/dev/null 2>&1
    else
        sudo pip3 install alibabacloud-oss-v2 \
            -i https://mirrors.aliyun.com/pypi/simple/ \
            --trusted-host mirrors.aliyun.com \
            --break-system-packages >/dev/null 2>&1
    fi

    if python3 -c 'import alibabacloud_oss_v2' >/dev/null 2>&1; then
        return 0
    fi

    return 1
}

if ensure_oss_python_dependencies; then
    echo -e "${GREEN}=======================================${NO_COLOR}"
    echo -e "${GREEN}✅ OSS依赖项检查成功！${NO_COLOR}"
    echo -e "${GREEN}=======================================${NO_COLOR}"
else
    echo "OSS依赖项检查失败"
fi

ensure_graphviz_installed() {
    if command -v dot >/dev/null 2>&1 && dpkg -s libgraphviz-dev >/dev/null 2>&1; then
        echo "✅ graphviz / libgraphviz-dev 已安装，跳过"
        return 0
    fi
    echo "正在安装 graphviz / libgraphviz-dev ..."
    sudo apt-get update >/dev/null 2>&1
    sudo apt-get install -y graphviz libgraphviz-dev
}

ensure_graphviz_installed



echo "======================================="
echo "所有模块启动完成"
echo "日志目录: $LOG_DIR"
echo "进程信息保存在: /tmp/apollo_processes.pid"
echo ""
echo ""
echo "查看所有日志: ls -la $LOG_DIR/"
echo "======================================="


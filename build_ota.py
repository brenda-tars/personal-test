#!/usr/bin/env python3
"""构建器：把 tars_flash + PMU_OTA.py + rh850_udp_ota.py 组装成单文件 ota.py。

- tars_flash(bash)：原样嵌入（已自带 --no-reboot），用 repr() 保真、零转义风险。
- PMU_OTA.py / rh850_udp_ota.py：粘贴为活源码，仅做最小去冲突重命名
  (main->pmu_main/rh850_main、parse_args(argv)、剥离 __main__ 守卫与重复 __future__)。
"""
import re


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


# ----------------------------------------------------------------
# tars_flash：原样嵌入（已自带 --no-reboot）
# ----------------------------------------------------------------
tars = read("tars_flash")
assert 'NO_REBOOT=false' in tars, "tars_flash 缺少 NO_REBOOT 初始化"
assert tars.count("--no-reboot)") >= 1, "tars_flash 缺少 --no-reboot 分支"
assert 'NO_REBOOT:-false' in tars, "tars_flash 缺少 switch_and_reboot 守卫"

# 用可读的三引号原文嵌入（repr 会挤成一长行 \n 转义，不可读）。
# 已确认 tars_flash 不含 '''，且不以反斜杠结尾，故 r'''...''' 安全。
_body = tars if tars.endswith("\n") else tars + "\n"
assert "'''" not in _body, "tars_flash 含 '''，需改用 repr 嵌入"
TARS_BLOCK = "TARS_FLASH_SH = r'''" + _body + "'''\n"

# ----------------------------------------------------------------
# PMU_OTA.py：粘贴 + 最小重命名
# ----------------------------------------------------------------
pmu = read("PMU_OTA.py")
assert "from __future__ import annotations\n" in pmu
pmu = pmu.replace("from __future__ import annotations\n", "", 1)
assert "def main():\n" in pmu
pmu = pmu.replace("def main():\n", "def pmu_main(argv=None):\n", 1)
assert pmu.count("args = parser.parse_args()") == 1
pmu = pmu.replace("args = parser.parse_args()", "args = parser.parse_args(argv)", 1)
# 剥离 __main__ 守卫（兼容单/双引号）
pmu, n = re.subn(r"\nif __name__ == [\"']__main__[\"']:\s*\n\s*exit\(main\(\)\)\s*\n?", "\n", pmu, count=1)
assert n == 1, "pmu: __main__ 守卫未命中"
assert "from __future__" not in pmu
assert "def main(" not in pmu and "if __name__" not in pmu
assert "def pmu_main(argv=None):" in pmu
assert pmu.count("args = parser.parse_args(argv)") == 1

# ----------------------------------------------------------------
# rh850_udp_ota.py：粘贴 + 最小重命名
# ----------------------------------------------------------------
rh = read("rh850_udp_ota.py")
assert "from __future__ import annotations\n" in rh
rh = rh.replace("from __future__ import annotations\n", "", 1)
assert "def main() -> int:\n" in rh
rh = rh.replace("def main() -> int:\n", "def rh850_main(argv=None) -> int:\n", 1)
assert rh.count("args = parser.parse_args()") == 1
rh = rh.replace("args = parser.parse_args()", "args = parser.parse_args(argv)", 1)
# 剥离 __main__ 守卫（兼容单/双引号）
rh, n = re.subn(r"\nif __name__ == [\"']__main__[\"']:\s*\n\s*sys\.exit\(main\(\)\)\s*\n?", "\n", rh, count=1)
assert n == 1, "rh: __main__ 守卫未命中"
assert "from __future__" not in rh
assert "def main(" not in rh and "if __name__" not in rh
assert "def rh850_main(argv=None) -> int:" in rh
assert rh.count("args = parser.parse_args(argv)") == 1

# ----------------------------------------------------------------
# 头部（shebang / 模块文档 / __future__ / 全量 import / 颜色）
# ----------------------------------------------------------------
HEADER = r'''#!/usr/bin/env python3
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
'''

# ----------------------------------------------------------------
# 统一编排入口
# ----------------------------------------------------------------
DISPATCHER = r'''# ================================================================
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
'''

# ----------------------------------------------------------------
# 写出 ota.py
# ----------------------------------------------------------------
SEP = "\n# ================================================================\n"
with open("ota.py", "w", encoding="utf-8") as f:
    f.write(HEADER)
    f.write(SEP + "# tars_flash (bash, 内联; 已注入 --no-reboot)\n" + SEP + "\n")
    f.write(TARS_BLOCK)
    f.write(SEP + "# PMU_OTA (内联)\n" + SEP + "\n")
    f.write(pmu)
    f.write(SEP + "# rh850_udp_ota (内联)\n" + SEP + "\n")
    f.write(rh)
    f.write(SEP + "# 统一编排入口\n" + SEP + "\n")
    f.write(DISPATCHER)

print("ota.py built:", sum(1 for _ in open("ota.py")), "lines")

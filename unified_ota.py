#!/usr/bin/env python3
"""
Thor 统一 OTA 刷写工具

按顺序执行三个步骤：
  1. tars_flash - 刷写固件
  2. PMU OTA - CAN-over-UDP OTA 升级（zephyr.signed.bin）
  3. RH850 OTA - ZYT Link V1 UDP OTA 升级（core0 + core1）

使用方法:
  sudo python3 unified_ota.py -r ./data_dir --iface lan0 --no-reboot
  sudo python3 unified_ota.py --skip-tars -f data_dir/zephyr.signed.bin --iface lan0 --no-reboot
  sudo python3 unified_ota.py --skip-pmu --skip-rh850 -r ./data_dir --no-reboot
"""

from __future__ import annotations

import argparse
import math
import os
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional, Tuple


# ================================================================
# 颜色输出
# ================================================================

try:
    from colorama import Fore, Style, init
    init(autoreset=True)
except ImportError:
    class Fore:
        RED = GREEN = YELLOW = CYAN = WHITE = MAGENTA = ""
    class Style:
        RESET_ALL = ""
    def init(*_a, **_kw): pass


# ================================================================
# 共享工具
# ================================================================

def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()

def find_bin_files(directory: str, core0_name: str, core1_name: str) -> Tuple[str, str]:
    core0 = os.path.join(directory, core0_name)
    core1 = os.path.join(directory, core1_name)
    return core0, core1


# ================================================================
# Step 1: tars_flash
# ================================================================

def run_tars_flash(data_dir: str, no_reboot: bool, verbose: bool) -> bool:
    tars_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tars_flash")
    if not os.path.isfile(tars_bin):
        print(f"{Fore.RED}错误: tars_flash 二进制不存在: {tars_bin}")
        return False
    cmd = ["sudo", tars_bin, "-r", data_dir]
    if no_reboot:
        cmd.append("--no-reboot")
    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"{Fore.CYAN}=== Step 1/3: tars_flash 刷写 ===")
    print(f"{Fore.CYAN}{'='*60}")
    print(f"{Fore.WHITE}命令: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=not verbose, text=True)
        if result.returncode != 0:
            print(f"{Fore.RED}tars_flash 失败 (exit={result.returncode})")
            if result.stderr:
                print(f"{Fore.RED}{result.stderr}")
            return False
        print(f"{Fore.GREEN}tars_flash 完成")
        return True
    except FileNotFoundError:
        print(f"{Fore.RED}错误: 找不到 sudo 或 tars_flash 命令")
        return False
    except Exception as exc:
        print(f"{Fore.RED}tars_flash 异常: {exc}")
        return False


# ================================================================
# Step 2: PMU OTA (CAN-over-UDP 75B)
# ================================================================

CAN_ID_HOST_OTA_START = 0x110
CAN_ID_HOST_OTA_DATA = 0x111
CAN_ID_HOST_OTA_END = 0x112
CAN_ID_HOST_CMD = 0x100
CAN_ID_DEV_OTA_STATUS = 0x210

HOST_CMD_APPLY_OTA = 0x06
OTA_STATUS_START_ACK = 0x10
OTA_STATUS_DATA_ACK = 0x11
OTA_STATUS_END_ACK = 0x12
OTA_STATUS_READY_REBOOT = 0x14
OTA_STATUS_ERR_SEQ = 0xE2

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
    0x00, 0x5E, 0xBC, 0xE2, 0x61, 0x3F, 0xDD, 0x83, 0xC2, 0x9C, 0x7E, 0x20,
    0xA3, 0xFD, 0x1F, 0x41, 0x9D, 0xC3, 0x21, 0x7F, 0xFC, 0xA2, 0x40, 0x1E,
    0x5F, 0x01, 0xE3, 0xBD, 0x3E, 0x60, 0x82, 0xDC, 0x23, 0x7D, 0x9F, 0xC1,
    0x42, 0x1C, 0xFE, 0xA0, 0xE1, 0xBF, 0x5D, 0x03, 0x80, 0xDE, 0x3C, 0x62,
    0xBE, 0xE0, 0x02, 0x5C, 0xDF, 0x81, 0x63, 0x3D, 0x7C, 0x22, 0xC0, 0x9E,
    0x1D, 0x43, 0xA1, 0xFF, 0x46, 0x18, 0xFA, 0xA4, 0x27, 0x79, 0x9B, 0xC5,
    0x84, 0xDA, 0x38, 0x66, 0xE5, 0xBB, 0x59, 0x07, 0xDB, 0x85, 0x67, 0x39,
    0xBA, 0xE4, 0x06, 0x58, 0x19, 0x47, 0xA5, 0xFB, 0x78, 0x26, 0xC4, 0x9A,
    0x65, 0x3B, 0xD9, 0x87, 0x04, 0x5A, 0xB8, 0xE6, 0xA7, 0xF9, 0x1B, 0x45,
    0xC6, 0x98, 0x7A, 0x24, 0xF8, 0xA6, 0x44, 0x1A, 0x99, 0xC7, 0x25, 0x7B,
    0x3A, 0x64, 0x86, 0xD8, 0x5B, 0x05, 0xE7, 0xB9, 0x8C, 0xD2, 0x30, 0x6E,
    0xED, 0xB3, 0x51, 0x0F, 0x4E, 0x10, 0xF2, 0xAC, 0x2F, 0x71, 0x93, 0xCD,
    0x11, 0x4F, 0xAD, 0xF3, 0x70, 0x2E, 0xCC, 0x92, 0xD3, 0x8D, 0x6F, 0x31,
    0xB2, 0xEC, 0x0E, 0x50, 0xAF, 0xF1, 0x13, 0x4D, 0xCE, 0x90, 0x72, 0x2C,
    0x6D, 0x33, 0xD1, 0x8F, 0x0C, 0x52, 0xB0, 0xEE, 0x32, 0x6C, 0x8E, 0xD0,
    0x53, 0x0D, 0xEF, 0xB1, 0xF0, 0xAE, 0x4C, 0x12, 0x91, 0xCF, 0x2D, 0x73,
    0xCA, 0x94, 0x76, 0x28, 0xAB, 0xF5, 0x17, 0x49, 0x08, 0x56, 0xB4, 0xEA,
    0x69, 0x37, 0xD5, 0x8B, 0x57, 0x09, 0xEB, 0xB5, 0x36, 0x68, 0x8A, 0xD4,
    0x95, 0xCB, 0x29, 0x77, 0xF4, 0xAA, 0x48, 0x16, 0xE9, 0xB7, 0x55, 0x0B,
    0x88, 0xD6, 0x34, 0x6A, 0x2B, 0x75, 0x97, 0xC9, 0x4A, 0x14, 0xF6, 0xA8,
    0x74, 0x2A, 0xC8, 0x96, 0x15, 0x4B, 0xA9, 0xF7, 0xB6, 0xE8, 0x0A, 0x54,
    0xD7, 0x89, 0x6B, 0x35,
])

def _crc8(buf: bytes, init_val: int = 0) -> int:
    c = init_val & 0xFF
    for b in buf:
        c = _CRC8_TABLE[c ^ b]
    return c

def can_id_base(wire_id: int) -> int:
    wire_id &= ~CAN_FD_FLAG
    if wire_id & 0x80000000:
        return wire_id & 0x1FFFFFFF
    return wire_id & 0x7FF

def pad_can_payload(data) -> Tuple[int, bytes]:
    payload = bytes(data)
    dlc = max(UDP_WIRE_MIN_DLC, len(payload))
    if dlc > UDP_WIRE_MAX_DLC:
        raise ValueError(f"CAN payload too long: {len(payload)}")
    if len(payload) < dlc:
        payload += b"\x00" * (dlc - len(payload))
    return dlc, payload

def build_can_over_udp75(bus: int, can_id: int, udp_counter: int, dlc: int, data: bytes) -> bytes:
    frame = bytearray(UDP_FRAME_SIZE)
    frame[0] = bus & 0xFF
    struct.pack_into(">I", frame, 1, can_id & 0xFFFFFFFF)
    struct.pack_into("<I", frame, 5, udp_counter & 0xFFFFFFFF)
    frame[9] = dlc
    frame[10: 10 + dlc] = data[:dlc]
    frame[UDP_CRC_OFFSET] = _crc8(bytes(frame[:UDP_CRC_COVER_LEN]), 0)
    return bytes(frame)

@dataclass
class ParsedUdpCanFrame:
    bus: int
    can_id: int
    dlc: int
    data: bytes
    udp_counter: int

def parse_can_over_udp75(packet: bytes) -> Tuple[Optional[ParsedUdpCanFrame], Optional[str]]:
    if len(packet) < UDP_FRAME_SIZE:
        return None, f"length {len(packet)} < {UDP_FRAME_SIZE}"
    expect = packet[UDP_CRC_OFFSET]
    actual = _crc8(packet[:UDP_CRC_COVER_LEN], 0)
    if expect != actual:
        return None, f"crc mismatch"
    bus = packet[0]
    can_id = struct.unpack_from(">I", packet, 1)[0]
    udp_counter = struct.unpack_from("<I", packet, 5)[0]
    dlc = packet[9]
    if dlc < 1 or dlc > UDP_WIRE_MAX_DLC:
        return None, f"invalid dlc={dlc}"
    data = bytes(packet[10: 10 + dlc])
    return ParsedUdpCanFrame(bus, can_id, dlc, data, udp_counter), None

class ThorCanHost:
    """Thor PMU 上位机（UDP 75B 非交互模式）"""

    def __init__(self, listen_host=DEFAULT_LISTEN_HOST, listen_port=DEFAULT_LISTEN_PORT,
                 mcu_ip=DEFAULT_MCU_IP, mcu_port=DEFAULT_MCU_PORT, bus=DEFAULT_BUS_ID, quiet=True):
        self.quiet = quiet
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.mcu_ip = mcu_ip
        self.mcu_port = mcu_port
        self.bus = bus
        self.udp_sock: Optional[socket.socket] = None
        self.mcu_addr = (mcu_ip, mcu_port)
        self.udp_tx_counter = 1
        self.running = False
        self.tx_count = 0
        self.ota_status_cv = threading.Condition()
        self.ota_status_queue = deque(maxlen=512)

    def connect(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.listen_host, self.listen_port))
            sock.settimeout(0.1)
            self.udp_sock = sock
            if not self.quiet:
                print(f"{Fore.GREEN}✓ PMU UDP 已绑定 {self.listen_host}:{self.listen_port} "
                      f"(MCU -> {self.mcu_ip}:{self.mcu_port})")
            return True
        except OSError as exc:
            print(f"{Fore.RED}✗ PMU UDP 绑定失败: {exc}")
            return False

    def disconnect(self) -> None:
        if self.udp_sock is not None:
            self.udp_sock.close()
            self.udp_sock = None

    def send_frame(self, can_id: int, data) -> bool:
        if self.udp_sock is None:
            return False
        try:
            dlc, payload = pad_can_payload(data)
            frame = build_can_over_udp75(self.bus, can_id, self.udp_tx_counter, dlc, payload)
            self.udp_sock.sendto(frame, self.mcu_addr)
            self.udp_tx_counter += 1
            self.tx_count += 1
            return True
        except (OSError, ValueError) as exc:
            print(f"{Fore.RED}PMU UDP 发送失败: {exc}")
            return False

    def send_ota_start(self, image_size: int, total_packets: int, session_id: int = 1) -> None:
        data = [
            session_id & 0xFF,
            total_packets & 0xFF, (total_packets >> 8) & 0xFF,
            image_size & 0xFF, (image_size >> 8) & 0xFF,
            (image_size >> 16) & 0xFF, (image_size >> 24) & 0xFF,
            0,
        ]
        if self.send_frame(CAN_ID_HOST_OTA_START, data):
            print(f"{Fore.CYAN}>>> TX OTA_START: size={image_size} packets={total_packets}")

    def send_ota_data(self, seq: int, payload) -> None:
        data = [seq & 0xFF, (seq >> 8) & 0xFF, *payload]
        self.send_frame(CAN_ID_HOST_OTA_DATA, data)

    def send_ota_end(self) -> None:
        self.send_frame(CAN_ID_HOST_OTA_END, [0])

    def send_apply_ota(self) -> None:
        self.send_frame(CAN_ID_HOST_CMD, [HOST_CMD_APPLY_OTA] + [0]*7)

    def _clear_ota_status_queue(self) -> None:
        with self.ota_status_cv:
            self.ota_status_queue.clear()

    def _wait_ota_status(self, timeout_s=1.0, expected_codes=None, allow_error=True):
        deadline = time.monotonic() + timeout_s
        expected = set(expected_codes or [])
        with self.ota_status_cv:
            while True:
                while self.ota_status_queue:
                    st = self.ota_status_queue.popleft()
                    code = st["status_code"]
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

    def ota_upgrade_from_file(self, file_path: str, chunk_size: int = 6) -> bool:
        if chunk_size < 1 or chunk_size > 6:
            print(f"{Fore.RED}chunk_size 必须在 1..6")
            return False
        if not os.path.isfile(file_path):
            print(f"{Fore.RED}文件不存在: {file_path}")
            return False
        with open(file_path, "rb") as f:
            image = f.read()
        image_size = len(image)
        if image_size == 0:
            print(f"{Fore.RED}固件文件为空")
            return False
        chunks = [image[i: i + chunk_size] for i in range(0, image_size, chunk_size)]
        total_packets = len(chunks)
        print(f"{Fore.YELLOW}=== PMU OTA 开始 ===")
        print(f"{Fore.YELLOW}文件: {file_path}")
        print(f"{Fore.YELLOW}大小: {image_size} bytes, 分包: {total_packets}, 每包: {chunk_size} bytes")

        self._clear_ota_status_queue()
        try:
            self.send_ota_start(image_size=image_size, total_packets=total_packets)
            st = self._wait_ota_status(30.0, {OTA_STATUS_START_ACK})
            if not st or st["status_code"] != OTA_STATUS_START_ACK:
                print(f"{Fore.RED}未收到 START_ACK")
                return False

            seq, retry, max_retry = 0, 0, 10
            while seq < total_packets:
                self.send_ota_data(seq, list(chunks[seq]))
                st = self._wait_ota_status(1.0, {OTA_STATUS_DATA_ACK, OTA_STATUS_ERR_SEQ})
                if st is None:
                    retry += 1
                    if retry > max_retry:
                        print(f"{Fore.RED}PMU OTA 超时过多 at seq={seq}")
                        return False
                    continue
                retry = 0
                status_code = st["status_code"]
                next_seq = st["next_seq"]
                if status_code in (OTA_STATUS_DATA_ACK, OTA_STATUS_ERR_SEQ):
                    seq = next_seq
                else:
                    print(f"{Fore.RED}PMU OTA 错误: code=0x{status_code:02X}")
                    return False
                if seq % 100 == 0 or seq == total_packets:
                    pct = seq * 100.0 / total_packets
                    print(f"{Fore.WHITE}PMU OTA 进度: {pct:.1f}% ({seq}/{total_packets})")

            self.send_ota_end()
            st = self._wait_ota_status(60.0, {OTA_STATUS_END_ACK, OTA_STATUS_READY_REBOOT})
            if not st:
                print(f"{Fore.RED}未收到 END_ACK（60s 超时）")
                return False
            if st["status_code"] >= 0xE0:
                print(f"{Fore.RED}PMU OTA END 错误: code=0x{st['status_code']:02X}")
                return False
            if st["status_code"] == OTA_STATUS_END_ACK:
                st = self._wait_ota_status(120.0, {OTA_STATUS_READY_REBOOT})
            if not st or st["status_code"] != OTA_STATUS_READY_REBOOT:
                print(f"{Fore.RED}未收到 READY_REBOOT")
                return False
            print(f"{Fore.GREEN}PMU 设备已就绪 (READY_REBOOT)")
            self.send_apply_ota()
            print(f"{Fore.GREEN}已发送 APPLY_OTA")
            return True
        finally:
            pass

    def process_frame(self, msg) -> None:
        can_id = msg.arbitration_id
        data = list(msg.data)
        if can_id == CAN_ID_DEV_OTA_STATUS:
            status_record = {
                "status_code": data[0] if len(data) > 0 else None,
                "detail": data[1] if len(data) > 1 else 0,
                "next_seq": (data[2] | (data[3] << 8)) if len(data) > 3 else 0,
                "progress": data[4] if len(data) > 4 else 0,
            }
            with self.ota_status_cv:
                self.ota_status_queue.append(status_record)
                self.ota_status_cv.notify_all()

    def rx_thread_func(self) -> None:
        while self.running:
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

    def start(self) -> bool:
        if not self.connect():
            return False
        self.running = True
        self.rx_thread = threading.Thread(target=self.rx_thread_func, daemon=True)
        self.rx_thread.start()
        return True

    def stop(self) -> None:
        self.running = False
        time.sleep(0.2)
        self.disconnect()


def run_pmu_ota(firmware_path: str, chunk_size=6, mcu_ip=DEFAULT_MCU_IP, listen_port=DEFAULT_LISTEN_PORT) -> bool:
    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"{Fore.CYAN}=== Step 2/3: PMU OTA 升级 ===")
    print(f"{Fore.CYAN}{'='*60}")
    host = ThorCanHost(mcu_ip=mcu_ip, listen_port=listen_port, quiet=False)
    if not host.start():
        return False
    try:
        return host.ota_upgrade_from_file(firmware_path, chunk_size=chunk_size)
    finally:
        host.stop()


# ================================================================
# Step 3: RH850 OTA (ZYT Link V1)
# ================================================================

LOCAL_IP = "192.168.1.85"
LOCAL_PORT = 15000
MCU_IP_RH850 = "192.168.1.20"
MCU_PORT_RH850 = 15000
PACKAGE_SIZE = 512
COM_CRC16_INIT = 0x3692

CMD_FIRM_INFO = 0xFF
CMD_DEVICE_STATUS = 0x0C
CMD_FW_ENTRY = 0x07
CMD_FW_START = 0x08
CMD_FW_DATA = 0x09
CMD_FW_FINISH = 0x0A
CMD_REBOOT = 0x0B

PARTITION_CORE0 = "vip_fullmem_app_core0"
PARTITION_CORE1 = "vip_fullmem_app_core1"

FW_ENTRY_PAYLOAD = bytes.fromhex("00adfa7620525d0000")
REBOOT_PAYLOAD = bytes.fromhex("000000000000e0ed7620525d0000")

SEND_INDEX = {
    "firm_info": 0x33, "device_status": 0x66, "fw_entry": 0xFC,
    "fw_start": 0x2A, "fw_data": 0xD1, "fw_finish": 0x8A,
    "slot_op": 0xA2, "reboot": 0x75,
}

# CRC16 表（256 项）
CRC16_TABLE = [
    0x0000, 0x1189, 0x2312, 0x329B, 0x4624, 0x57AD, 0x6536, 0x74BF,
    0x8C48, 0x9DC1, 0xAF5A, 0xBED3, 0xCA6C, 0xDBE5, 0xE97E, 0xF8F7,
    0x1081, 0x0108, 0x3393, 0x221A, 0x56A5, 0x472C, 0x75B7, 0x643E,
    0x9CC9, 0x8D40, 0xBFDB, 0xAE52, 0xDAED, 0xCB64, 0xF9FF, 0xE876,
    0x2102, 0x308B, 0x0210, 0x1399, 0x6726, 0x76AF, 0x4434, 0x55BD,
    0xAD4A, 0xBCC3, 0x8E58, 0x9FD1, 0xEB6E, 0xFAE7, 0xC87C, 0xD9F5,
    0x3183, 0x200A, 0x1291, 0x0318, 0x77A7, 0x662E, 0x54B5, 0x453C,
    0xBDCB, 0xAC42, 0x9ED9, 0x8F50, 0xFBEF, 0xEA66, 0xD8FD, 0xC974,
    0x4204, 0x538D, 0x6116, 0x709F, 0x0420, 0x15A9, 0x2732, 0x36BB,
    0xCE4C, 0xDFC5, 0xED5E, 0xFCD7, 0x8868, 0x99E1, 0xAB7A, 0xBAF3,
    0x5285, 0x430C, 0x7197, 0x601E, 0x14A1, 0x0528, 0x37B3, 0x263A,
    0xDECD, 0xCF44, 0xFDDF, 0xEC56, 0x98E9, 0x8960, 0xBBFB, 0xAA72,
    0x6306, 0x728F, 0x4014, 0x519D, 0x2522, 0x34AB, 0x0630, 0x17B9,
    0xEF4E, 0xFEC7, 0xCC5C, 0xDDD5, 0xA96A, 0xB8E3, 0x8A78, 0x9BF1,
    0x7387, 0x620E, 0x5095, 0x411C, 0x35A3, 0x242A, 0x16B1, 0x0738,
    0xFFCF, 0xEE46, 0xDCDD, 0xCD54, 0xB9EB, 0xA862, 0x9AF9, 0x8B70,
    0x8408, 0x9581, 0xA71A, 0xB693, 0xC22C, 0xD3A5, 0xE13E, 0xF0B7,
    0x0840, 0x19C9, 0x2B52, 0x3ADB, 0x4E64, 0x5FED, 0x6D76, 0x7CFF,
    0x9489, 0x8500, 0xB79B, 0xA612, 0xD2AD, 0xC324, 0xF1BF, 0xE036,
    0x18C1, 0x0948, 0x3BD3, 0x2A5A, 0x5EE5, 0x4F6C, 0x7DF7, 0x6C7E,
    0xA50A, 0xB483, 0x8618, 0x9791, 0xE32E, 0xF2A7, 0xC03C, 0xD1B5,
    0x2942, 0x38CB, 0x0A50, 0x1BD9, 0x6F66, 0x7EEF, 0x4C74, 0x5DFD,
    0xB58B, 0xA402, 0x9699, 0x8710, 0xF3AF, 0xE226, 0xD0BD, 0xC134,
    0x39C3, 0x284A, 0x1AD1, 0x0B58, 0x7FE7, 0x6E6E, 0x5CF5, 0x4D7C,
    0xC60C, 0xD785, 0xE51E, 0xF497, 0x8028, 0x91A1, 0xA33A, 0xB2B3,
    0x4A44, 0x5BCD, 0x6956, 0x78DF, 0x0C60, 0x1DE9, 0x2F72, 0x3EFB,
    0xD68D, 0xC704, 0xF597, 0xE41E, 0x90A1, 0x8128, 0xB3B3, 0xA23A,
    0x5ACD, 0x4B44, 0x79DF, 0x6856, 0x1CE9, 0x0D60, 0x3FFB, 0x2E72,
    0xE70E, 0xF687, 0xC41C, 0xD595, 0xA12A, 0xB0A3, 0x8238, 0x93B1,
    0x6B46, 0x7ACF, 0x4854, 0x59DD, 0x2D62, 0x3CEB, 0x0E70, 0x1FF9,
    0xF78F, 0xE606, 0xD49D, 0xC514, 0xB1AB, 0xA022, 0x92B9, 0x8330,
    0x7BC7, 0x6A4E, 0x58D5, 0x495C, 0x3DE3, 0x2C6A, 0x1EF1, 0x0F78,
]


def crc16(data: bytes, init_val: int = COM_CRC16_INIT) -> int:
    crc = init_val & 0xFFFF
    for b in data:
        crc = ((crc << 8) | ((crc >> 8) & 0xFF)) ^ CRC16_TABLE[(crc ^ b) & 0xFF]
        crc &= 0xFFFF
    return crc


class ZytLink:
    """ZYT Link V1 UDP 通信协议"""

    def __init__(self, iface: str, verbose: bool = False):
        self.iface = iface
        self.verbose = verbose
        self.sock: Optional[socket.socket] = None
        self.tx_count = 0
        self.rx_count = 0
        self.udp_counter = 0

    def open(self, local_ip: str = LOCAL_IP, local_port: int = LOCAL_PORT) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.iface.encode())
            sock.bind((local_ip, local_port))
            sock.settimeout(0.5)
            self.sock = sock
            print(f"{Fore.GREEN}✓ RH850 UDP 已绑定 {local_ip}:{local_port} (iface={self.iface})")
            return True
        except OSError as exc:
            print(f"{Fore.RED}✗ RH850 UDP 绑定失败: {exc}")
            return False

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def build_packet(self, send_index: int, cmd: int, payload: bytes) -> bytes:
        length = 8 + len(payload)
        hdr = struct.pack("<BBH H B", 0x55, 0xCC, length, send_index, cmd) + b"\x00\x00"
        pkt = hdr + payload
        crc = crc16(pkt)
        return pkt + struct.pack("<H", crc)

    def send_to(self, payload: bytes, dst_ip: str = MCU_IP_RH850, dst_port: int = MCU_PORT_RH850) -> bool:
        if self.sock is None:
            return False
        try:
            self.sock.sendto(payload, (dst_ip, dst_port))
            self.tx_count += 1
            if self.verbose:
                print(f"{Fore.CYAN}>>> TX ({len(payload)}B): {payload.hex()}")
            return True
        except OSError as exc:
            print(f"{Fore.RED}RH850 UDP 发送失败: {exc}")
            return False

    def recv(self, timeout_s: float = 2.0) -> Optional[bytes]:
        if self.sock is None:
            return None
        try:
            self.sock.settimeout(timeout_s)
            data, _addr = self.sock.recvfrom(65536)
            self.rx_count += 1
            if self.verbose:
                print(f"{Fore.GREEN}<<< RX ({len(data)}B): {data.hex()}")
            return data
        except socket.timeout:
            return None
        except OSError:
            return None

    def send_cmd_and_wait_ack(self, send_index: int, cmd: int, payload: bytes = b"",
                              timeout_s: float = 2.0, retries: int = 5) -> Optional[bytes]:
        pkt = self.build_packet(send_index, cmd, payload)
        for attempt in range(retries):
            if not self.send_to(pkt):
                continue
            resp = self.recv(timeout_s)
            if resp is not None and len(resp) >= 8:
                return resp
            if self.verbose:
                print(f"{Fore.YELLOW}超时重试 {attempt + 1}/{retries}")
            time.sleep(0.1)
        return None


def run_rh850_ota(
    iface: str,
    timeout: int = 15,
    data_timeout: int = 10,
    verbose: bool = False,
    no_reboot: bool = False,
    with_device_status: bool = False,
    firmware_dir: Optional[str] = None,
    core0_path: Optional[str] = None,
    core1_path: Optional[str] = None,
) -> bool:
    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"{Fore.CYAN}=== Step 3/3: RH850 OTA 升级 ===")
    print(f"{Fore.CYAN}{'='*60}")

    if firmware_dir is None:
        firmware_dir = os.path.dirname(os.path.abspath(__file__))

    if core0_path is None:
        core0_path = os.path.join(firmware_dir, "vip_fullmem_app_core0.bin")
    if core1_path is None:
        core1_path = os.path.join(firmware_dir, "vip_fullmem_app_core1.bin")

    if not os.path.isfile(core0_path):
        print(f"{Fore.RED}错误: core0 固件不存在: {core0_path}")
        return False
    if not os.path.isfile(core1_path):
        print(f"{Fore.RED}错误: core1 固件不存在: {core1_path}")
        return False

    link = ZytLink(iface=iface, verbose=verbose)
    if not link.open():
        return False

    try:
        if with_device_status:
            print(f"{Fore.YELLOW}查询 DEVICE_STATUS...")
            resp = link.send_cmd_and_wait_ack(
                SEND_INDEX["device_status"], CMD_DEVICE_STATUS, b"",
                timeout_s=timeout
            )
            if resp is None:
                print(f"{Fore.RED}DEVICE_STATUS 无响应")
                return False
            print(f"{Fore.GREEN}DEVICE_STATUS OK")

        print(f"{Fore.YELLOW}获取固件信息...")
        resp = link.send_cmd_and_wait_ack(
            SEND_INDEX["firm_info"], CMD_FIRM_INFO, b"", timeout_s=timeout
        )
        if resp is None:
            print(f"{Fore.RED}FIRM_INFO 无响应")
            return False
        print(f"{Fore.GREEN}FIRM_INFO OK")

        print(f"{Fore.YELLOW}进入升级模式...")
        resp = link.send_cmd_and_wait_ack(
            SEND_INDEX["fw_entry"], CMD_FW_ENTRY, FW_ENTRY_PAYLOAD, timeout_s=timeout
        )
        if resp is None:
            print(f"{Fore.RED}FW_ENTRY 无响应")
            return False
        print(f"{Fore.GREEN}FW_ENTRY OK")

        if not _flash_rh850_partition(link, "core0", PARTITION_CORE0, core0_path,
                                       timeout, data_timeout):
            return False
        if not _flash_rh850_partition(link, "core1", PARTITION_CORE1, core1_path,
                                       timeout, data_timeout):
            return False

        if not no_reboot:
            print(f"{Fore.YELLOW}发送 REBOOT...")
            resp = link.send_cmd_and_wait_ack(
                SEND_INDEX["reboot"], CMD_REBOOT, REBOOT_PAYLOAD, timeout_s=timeout
            )
            if resp is None:
                print(f"{Fore.YELLOW}REBOOT 无响应（可能已重启）")
            else:
                print(f"{Fore.GREEN}REBOOT 完成")
        else:
            print(f"{Fore.YELLOW}--no-reboot 已设置，跳过重启")

        return True
    finally:
        link.close()


def _flash_rh850_partition(
    link: ZytLink,
    label: str,
    partition_name: str,
    bin_path: str,
    timeout: int,
    data_timeout: int,
) -> bool:
    print(f"\n{Fore.YELLOW}=== 刷写 {label}: {partition_name} ===")
    with open(bin_path, "rb") as f:
        image = f.read()
    image_size = len(image)
    image_hash = _sha256_hex(image)
    print(f"{Fore.WHITE}文件: {bin_path}")
    print(f"{Fore.WHITE}大小: {image_size} bytes, SHA256: {image_hash}")
    total_pkts = math.ceil(image_size / PACKAGE_SIZE)
    start_payload = struct.pack(
        "<II32s", image_size, total_pkts, partition_name.encode().ljust(32, b"\x00")[:32]
    )
    print(f"{Fore.YELLOW}FW_START: size={image_size} pkts={total_pkts}")
    resp = link.send_cmd_and_wait_ack(
        SEND_INDEX["fw_start"], CMD_FW_START, start_payload, timeout_s=timeout
    )
    if resp is None:
        print(f"{Fore.RED}{label} FW_START 无响应")
        return False
    print(f"{Fore.GREEN}{label} FW_START OK")

    seq = 0
    while seq < total_pkts:
        offset = seq * PACKAGE_SIZE
        chunk = image[offset: offset + PACKAGE_SIZE]
        data_payload = struct.pack("<I", seq) + chunk
        resp = link.send_cmd_and_wait_ack(
            SEND_INDEX["fw_data"], CMD_FW_DATA, data_payload,
            timeout_s=data_timeout
        )
        if resp is None:
            print(f"{Fore.RED}{label} FW_DATA seq={seq} 无响应")
            return False
        seq += 1
        if seq % 50 == 0 or seq == total_pkts:
            pct = seq * 100.0 / total_pkts
            print(f"{Fore.WHITE}{label} 进度: {pct:.1f}% ({seq}/{total_pkts})")

    finish_payload = image_hash.encode()[:32].ljust(32, b"\x00")
    print(f"{Fore.YELLOW}{label} FW_FINISH")
    resp = link.send_cmd_and_wait_ack(
        SEND_INDEX["fw_finish"], CMD_FW_FINISH, finish_payload, timeout_s=timeout * 2
    )
    if resp is None:
        print(f"{Fore.RED}{label} FW_FINISH 无响应")
        return False
    print(f"{Fore.GREEN}{label} FW_FINISH OK")
    return True


# ================================================================
# 主程序
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Thor 统一 OTA 刷写工具 — 依次执行 tars_flash / PMU OTA / RH850 OTA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-r", "--data-dir", help="tars_flash data 目录路径")
    parser.add_argument("-f", "--firmware", metavar="BIN",
                        help="PMU OTA 固件路径 (zephyr.signed.bin)")
    parser.add_argument("--chunk", type=int, default=6,
                        help="PMU OTA 每包字节数 (1..6, 默认 6)")
    parser.add_argument("--mcu-ip", default=DEFAULT_MCU_IP,
                        help=f"PMU MCU IP (默认 {DEFAULT_MCU_IP})")
    parser.add_argument("--pmu-port", type=int, default=DEFAULT_LISTEN_PORT,
                        help=f"PMU 监听端口 (默认 {DEFAULT_LISTEN_PORT})")
    parser.add_argument("--iface", help="RH850 绑定的网络接口 (如 lan0)")
    parser.add_argument("--timeout", type=int, default=15,
                        help="RH850 控制命令超时秒数 (默认 15)")
    parser.add_argument("--data-timeout", type=int, default=10,
                        help="RH850 数据包超时秒数 (默认 10)")
    parser.add_argument("--verbose", action="store_true",
                        help="RH850 打印 TX/RX 十六进制")
    parser.add_argument("--no-reboot", action="store_true",
                        help="不发送 REBOOT / APPLY_OTA")
    parser.add_argument("--with-device-status", action="store_true",
                        help="RH850 升级前查询 DEVICE_STATUS")
    parser.add_argument("--core0", help="RH850 core0 bin 路径")
    parser.add_argument("--core1", help="RH850 core1 bin 路径")
    parser.add_argument("--skip-tars", action="store_true", help="跳过 tars_flash")
    parser.add_argument("--skip-pmu", action="store_true", help="跳过 PMU OTA")
    parser.add_argument("--skip-rh850", action="store_true", help="跳过 RH850 OTA")

    args = parser.parse_args()

    if args.skip_tars and args.skip_pmu and args.skip_rh850:
        parser.error("所有步骤都被跳过了，请至少启用一个步骤")

    all_ok = True

    if not args.skip_tars:
        if not args.data_dir:
            print(f"{Fore.RED}错误: tars_flash 需要 -r/--data-dir 参数")
            return 1
        if not run_tars_flash(args.data_dir, args.no_reboot, args.verbose):
            all_ok = False

    if not args.skip_pmu:
        firmware = args.firmware
        if firmware is None and args.data_dir:
            firmware = os.path.join(args.data_dir, "zephyr.signed.bin")
        if not firmware or not os.path.isfile(firmware):
            print(f"{Fore.RED}错误: PMU OTA 需要 -f/--firmware 或有效的固件路径")
            return 1
        if not run_pmu_ota(firmware, chunk_size=args.chunk,
                           mcu_ip=args.mcu_ip, listen_port=args.pmu_port):
            all_ok = False

    if not args.skip_rh850:
        if not args.iface:
            print(f"{Fore.RED}错误: RH850 OTA 需要 --iface 参数")
            return 1
        fw_dir = args.data_dir if args.data_dir else None
        if not run_rh850_ota(
            iface=args.iface,
            timeout=args.timeout,
            data_timeout=args.data_timeout,
            verbose=args.verbose,
            no_reboot=args.no_reboot,
            with_device_status=args.with_device_status,
            firmware_dir=fw_dir,
            core0_path=args.core0,
            core1_path=args.core1,
        ):
            all_ok = False

    if all_ok:
        print(f"\n{Fore.GREEN}{'='*60}")
        print(f"{Fore.GREEN}=== 全部 OTA 刷写完成 ===")
        print(f"{Fore.GREEN}{'='*60}")
        return 0
    else:
        print(f"\n{Fore.RED}{'='*60}")
        print(f"{Fore.RED}=== OTA 刷写失败 ===")
        print(f"{Fore.RED}{'='*60}")
        return 1


if __name__ == "__main__":
    exit(main())

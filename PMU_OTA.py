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
    python3 PMU_OTA.py --apply               # 非交互发送 APPLY_OTA，等待 0x200 ACK 后退出

  注意: 若 power_udp_daemon 已占用 40189，需先停 daemon 或换 --listen-port。

  台架 SocketCAN（可选）:
    python3 PMU_OTA.py -t can -i socketcan -c can0
    python3 PMU_OTA.py -t can -i pcan -c PCAN_USBBUS1
    python3 PMU_OTA.py -t can -i virtual -c test
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
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
                if not self.quiet:
                    print(f"{Fore.RED}✗ UDP 绑定失败: {exc}")
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

    def ota_upgrade_from_file(self, file_path, chunk_size=6, inter_frame_delay=0.0):
        """一键模拟 OTA 升级流程: START -> DATA* -> END"""
        if chunk_size < 1 or chunk_size > 6:
            print(f"{Fore.RED}chunk_size 必须在 1..6")
            return

        if not os.path.isfile(file_path):
            print(f"{Fore.RED}文件不存在: {file_path}")
            return

        with open(file_path, 'rb') as f:
            image = f.read()

        image_size = len(image)
        if image_size == 0:
            print(f"{Fore.RED}固件文件为空")
            return

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
                return
            if st['status_code'] != OTA_STATUS_START_ACK:
                print(
                    f"{Fore.RED}未收到 START_ACK，收到状态 "
                    f"code=0x{st['status_code']:02X} detail=0x{st['detail']:02X}，停止发送"
                )
                return

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
                        return
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
                    return

                if seq % 100 == 0 or seq == total_packets:
                    pct = seq * 100.0 / total_packets
                    print(f"{Fore.WHITE}OTA send progress: {pct:.1f}% ({seq}/{total_packets})")

            self.send_ota_end()
            print(f"{Fore.YELLOW}=== OTA 发送完成，等待设备回包 ===")
            print(f"{Fore.YELLOW}收到 READY_REBOOT 后，请手动输入 apply 触发重启切换")
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
            return 1
        print(version)
        return 0
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


def main():
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
        "--apply",
        action="store_true",
        help="非交互发送 APPLY_OTA，等待 0x200 ACK 后退出（成功 0 / 失败或超时 1）",
    )
    args = parser.parse_args()

    if args.get_version and args.apply:
        parser.error("--get-version 与 --apply 不能同时使用")

    quiet = args.get_version or args.apply
    if not quiet:
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
        quiet=quiet,
    )

    if args.get_version:
        return run_get_version_probe(host)

    if args.apply:
        return run_apply_ota_probe(host)

    if not host.start():
        return 1

    try:
        interactive_loop(host)
    finally:
        host.stop()

    return 0

if __name__ == '__main__':
    exit(main())
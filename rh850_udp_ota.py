#!/usr/bin/env python3
"""RH850 Thor UDP OTA tool (ZYT Link V1).

Upgrades vip_fullmem_app_core0 and vip_fullmem_app_core1 in one run.
See README_rh850_udp_ota.md and ../RH850_UDP_OTA_Protocol.md.
"""

from __future__ import annotations

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


def main() -> int:
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
    args = parser.parse_args()

    try:
        if not args.self_test and not args.dry_run:
            setup_network(args.iface or DEFAULT_IFACE)

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


if __name__ == "__main__":
    sys.exit(main())

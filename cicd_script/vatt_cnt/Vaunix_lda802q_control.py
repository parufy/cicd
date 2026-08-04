# -*- coding: utf-8 -*-
"""
vaunix_lda802q_control.py

Vaunix LDA-802Q (4チャンネル デジタルアッテネータ) を
USB接続 + Vaunix純正 Windows USB API (VNX_atten64.dll) 経由で制御するプログラム。

本プログラムは Vaunix "Lab Brick Programmable Attenuator Windows USB API
User Manual" (Library Versions 2.14+) に記載の公式関数名・エンコーディングに
準拠して実装しています。

事前準備
--------
1. Vaunix社SDKに含まれる VNX_atten64.dll (64bit Windows用。32bit環境では
   VNX_atten.dll) を任意のフォルダ (既定: C:\\Vaunix) に配置する。
2. LDA-802Q をUSBでPCに接続し、認識されていることを確認する。
3. Python 3.x / Windows専用。ctypes標準ライブラリのみで動作 (追加pip不要)。

API仕様のポイント (マニュアルより)
----------------------------------
- HR (High Resolution) 系API (fnLDA_SetAttenuationHR 等) は 0.1dB刻みモデル用で、
  整数値1カウント = 0.05dB。LDA-802Qはこの系統を使用する
  (attenuation_raw = round(dB / 0.05))。
- fnLDA_SetAttenuationMCHR(deviceID, attenuation, chmask)
    → chmaskで指定した全chに同じ減衰値を1回のAPI呼び出しで設定 (真の同時設定)。
      chmaskはch1=bit0, ch2=bit1, ... のビットマスク。
- fnLDA_SetAttenuationHRQ(deviceID, ScaledAttenuation, Channel)
    → 指定chに対して1回で減衰値を設定 (呼び出し後、内部カレントchはChannelになる)。
- ランプ(掃引)はLDA-802Q内蔵のハードウェアランプエンジンを使用する。
  対象chをfnLDA_SetChannelで選択した状態で、ランプパラメータ
  (開始値/終了値/ステップ/dwell時間など) を設定し、fnLDA_StartRamp(deviceID, go)
  でそのchのランプを開始/停止する。
- 全てのSet系関数はLVSTATUS (0=成功、非0=エラー) を返す。
  Get系関数はエラー時に負の値(内部的には最上位ビットが立った値)を返す。

必須関数
--------
1. set_all_channels(atten_db, channels=None)   : 複数ch(既定は全4ch)に同じ
                                                  アッテネーション値を同時に設定
2. set_channel(channel, atten_db)              : 指定した1chのみアッテネーション値を設定
3. ramp_channel(channel, params, go=True)      : 指定した1chのみハードウェアランプを実行
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Optional


class VaunixLDAError(Exception):
    """Vaunix LDA API呼び出し関連のエラー"""
    pass


@dataclass
class RampParams:
    """1チャンネル分のハードウェアランプ設定パラメータ (マニュアル 3.5節に準拠)"""
    start_db: float                 # ランプ開始アッテネーション値 [dB] (SetRampStartHR)
    stop_db: float                  # ランプ終了アッテネーション値 [dB] (SetRampEndHR)
    step_db: float = 0.5            # 第1フェーズのステップ幅 [dB] 最小0.1dB (SetAttenuationStepHR)
    dwell_ms: int = 50              # 第1フェーズの各ステップ保持時間 [ms] 最小1ms (SetDwellTime)
    step_db2: Optional[float] = None  # 双方向ランプ第2フェーズのステップ幅 [dB] (SetAttenuationStepTwoHR)
    dwell_ms2: Optional[int] = None   # 双方向ランプ第2フェーズのdwell時間 [ms] (SetDwellTimeTwo)
    idle_ms: int = 0                # リピート時、ランプ終了後の待機時間 [ms] (SetIdleTime)
    hold_ms: int = 0                # 双方向ランプの第1/第2フェーズ間の待機時間 [ms] (SetHoldTime)
    bidirectional: bool = False     # True: start->stop->start を1フェーズセットとする (SetRampBidirectional)
    repeat: bool = False            # True: ランプを繰り返す (SetRampMode)


class VaunixLDA802Q:
    """
    Vaunix LDA-802Q (4ch) デジタルアッテネータ制御クラス。
    USB接続 + VNX_atten64.dll (Vaunix純正Windows USB API) をctypes経由で呼び出す。
    """

    ATTEN_SCALE = 0.05  # HR系APIの内部分解能: 1カウント = 0.05dB (マニュアル3.5節)
    NUM_CHANNELS_EXPECTED = 4

    def __init__(
        self,
        serial_number: Optional[int] = None,
        dll_dir: str = r"C:\Vaunix",
        test_mode: bool = False,
    ):
        """
        Args:
            serial_number: 接続したいLDA-802Qのシリアル番号。
                Noneの場合、最初に見つかった対応デバイスに接続する
                (複数台接続時は必ずシリアル番号を指定すること)。
            dll_dir: VNX_atten64.dll / VNX_atten.dll が置いてあるフォルダ。
            test_mode: TrueにするとDLLのテストモードで動作する。実機とは通信しない。
                マニュアルの注記の通り、テストモードでは「ランプの値設定用関数の
                挙動」はシミュレートされるが、「実機によるランプの実際の動作」は
                シミュレートされない点に注意。
        """
        if sys.platform != "win32":
            raise OSError("このプログラムはWindows専用です (Vaunix USB API DLLを使用します)")

        self.serial_number = serial_number
        self.device_id: Optional[int] = None
        self.num_channels: int = 0
        self.min_atten_db: float = 0.0
        self.max_atten_db: float = 0.0

        self.dll = self._load_dll(dll_dir)
        self.dll.fnLDA_SetTestMode(ctypes.c_bool(test_mode))
        self._connect()

    # ------------------------------------------------------------------
    # 初期化 / 接続まわり
    # ------------------------------------------------------------------
    def _load_dll(self, dll_dir: str) -> ctypes.WinDLL:
        is_64bit = sys.maxsize > 2**32
        dll_name = "VNX_atten64.dll" if is_64bit else "VNX_atten.dll"
        dll_path = os.path.join(dll_dir, dll_name)

        if not os.path.isfile(dll_path):
            raise OSError(
                f"DLLが見つかりません: {dll_path}\n"
                f"Vaunix社SDKを取得し、{dll_name} を配置してください。"
            )

        try:
            dll = ctypes.WinDLL(dll_path)
        except OSError as e:
            raise OSError(f"DLLの読み込みに失敗しました ({dll_path}): {e}")

        U, I, B = ctypes.c_uint, ctypes.c_int, ctypes.c_bool

        # --- 3.3 環境設定 ---
        dll.fnLDA_SetTestMode.argtypes = [B]
        dll.fnLDA_SetTestMode.restype = None
        dll.fnLDA_GetDLLVersion.argtypes = []
        dll.fnLDA_GetDLLVersion.restype = I

        # --- 3.4 デバイス選択 ---
        dll.fnLDA_GetNumDevices.argtypes = []
        dll.fnLDA_GetNumDevices.restype = I
        dll.fnLDA_GetDevInfo.argtypes = [ctypes.POINTER(U)]
        dll.fnLDA_GetDevInfo.restype = I
        dll.fnLDA_GetModelNameA.argtypes = [U, ctypes.c_char_p]
        dll.fnLDA_GetModelNameA.restype = I
        dll.fnLDA_GetSerialNumber.argtypes = [U]
        dll.fnLDA_GetSerialNumber.restype = I
        dll.fnLDA_GetDeviceStatus.argtypes = [U]
        dll.fnLDA_GetDeviceStatus.restype = I
        dll.fnLDA_InitDevice.argtypes = [U]
        dll.fnLDA_InitDevice.restype = I
        dll.fnLDA_CloseDevice.argtypes = [U]
        dll.fnLDA_CloseDevice.restype = I

        # --- 3.5 パラメータ設定 ---
        dll.fnLDA_SetChannel.argtypes = [U, I]
        dll.fnLDA_SetChannel.restype = I
        dll.fnLDA_SetAttenuationHR.argtypes = [U, I]
        dll.fnLDA_SetAttenuationHR.restype = I
        dll.fnLDA_SetAttenuationHRQ.argtypes = [U, I, I]
        dll.fnLDA_SetAttenuationHRQ.restype = I
        dll.fnLDA_SetAttenuationMCHR.argtypes = [U, I, ctypes.c_ulonglong]
        dll.fnLDA_SetAttenuationMCHR.restype = I
        dll.fnLDA_SetRampStartHR.argtypes = [U, I]
        dll.fnLDA_SetRampStartHR.restype = I
        dll.fnLDA_SetRampEndHR.argtypes = [U, I]
        dll.fnLDA_SetRampEndHR.restype = I
        dll.fnLDA_SetAttenuationStepHR.argtypes = [U, I]
        dll.fnLDA_SetAttenuationStepHR.restype = I
        dll.fnLDA_SetAttenuationStepTwoHR.argtypes = [U, I]
        dll.fnLDA_SetAttenuationStepTwoHR.restype = I
        dll.fnLDA_SetDwellTime.argtypes = [U, I]
        dll.fnLDA_SetDwellTime.restype = I
        dll.fnLDA_SetDwellTimeTwo.argtypes = [U, I]
        dll.fnLDA_SetDwellTimeTwo.restype = I
        dll.fnLDA_SetIdleTime.argtypes = [U, I]
        dll.fnLDA_SetIdleTime.restype = I
        dll.fnLDA_SetHoldTime.argtypes = [U, I]
        dll.fnLDA_SetHoldTime.restype = I
        dll.fnLDA_SetRFOn.argtypes = [U, B]
        dll.fnLDA_SetRFOn.restype = I
        dll.fnLDA_SetRampDirection.argtypes = [U, B]
        dll.fnLDA_SetRampDirection.restype = I
        dll.fnLDA_SetRampMode.argtypes = [U, B]
        dll.fnLDA_SetRampMode.restype = I
        dll.fnLDA_SetRampBidirectional.argtypes = [U, B]
        dll.fnLDA_SetRampBidirectional.restype = I
        dll.fnLDA_StartRamp.argtypes = [U, B]
        dll.fnLDA_StartRamp.restype = I
        dll.fnLDA_SaveSettings.argtypes = [U]
        dll.fnLDA_SaveSettings.restype = I

        # --- 3.6 パラメータ読み取り ---
        dll.fnLDA_GetNumChannels.argtypes = [U]
        dll.fnLDA_GetNumChannels.restype = I
        dll.fnLDA_GetAttenuationHR.argtypes = [U]
        dll.fnLDA_GetAttenuationHR.restype = I
        dll.fnLDA_GetMinAttenuationHR.argtypes = [U]
        dll.fnLDA_GetMinAttenuationHR.restype = I
        dll.fnLDA_GetMaxAttenuationHR.argtypes = [U]
        dll.fnLDA_GetMaxAttenuationHR.restype = I

        return dll

    def _connect(self) -> None:
        num_devices = self.dll.fnLDA_GetNumDevices()
        if num_devices <= 0:
            raise VaunixLDAError("Vaunixデバイスが1台も見つかりませんでした。USB接続を確認してください。")

        arr_type = ctypes.c_uint * num_devices
        dev_ids = arr_type()
        self.dll.fnLDA_GetDevInfo(dev_ids)

        target_id = None
        found_serials = []
        for dev_id in dev_ids:
            sn = self.dll.fnLDA_GetSerialNumber(dev_id)
            found_serials.append(sn)
            if self.serial_number is None or sn == self.serial_number:
                target_id = dev_id
                if self.serial_number is not None:
                    break

        if target_id is None:
            raise VaunixLDAError(
                f"指定したシリアル番号 {self.serial_number} のデバイスが見つかりません。"
                f"検出されたシリアル番号一覧: {found_serials}"
            )

        self.device_id = target_id
        ret = self.dll.fnLDA_InitDevice(self.device_id)
        if ret != 0:
            raise VaunixLDAError(f"fnLDA_InitDevice に失敗しました (code={ret})")

        self.num_channels = self.dll.fnLDA_GetNumChannels(self.device_id)
        if self.num_channels < self.NUM_CHANNELS_EXPECTED:
            raise VaunixLDAError(
                f"想定するチャンネル数({self.NUM_CHANNELS_EXPECTED})に満たないデバイスです"
                f" (検出チャンネル数={self.num_channels})。型番を確認してください。"
            )

        # ch1のレンジを全chの範囲として採用 (LDA-802Qは全ch同一レンジ)
        self._select_channel(1)
        self.min_atten_db = self.dll.fnLDA_GetMinAttenuationHR(self.device_id) * self.ATTEN_SCALE
        self.max_atten_db = self.dll.fnLDA_GetMaxAttenuationHR(self.device_id) * self.ATTEN_SCALE

        model_buf = ctypes.create_string_buffer(64)
        self.dll.fnLDA_GetModelNameA(self.device_id, model_buf)
        model_name = model_buf.value.decode(errors="ignore")
        actual_serial = self.dll.fnLDA_GetSerialNumber(self.device_id)

        print(
            f"[VaunixLDA802Q] 接続完了: model={model_name}, serial={actual_serial}, "
            f"channels={self.num_channels}, "
            f"attenuation range={self.min_atten_db:.2f}〜{self.max_atten_db:.2f} dB"
        )

    def close(self) -> None:
        """デバイスをクローズする"""
        if self.device_id is not None:
            self.dll.fnLDA_CloseDevice(self.device_id)
            self.device_id = None

    def __enter__(self) -> "VaunixLDA802Q":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------
    def _check_channel(self, channel: int) -> None:
        if not (1 <= channel <= self.num_channels):
            raise ValueError(f"channel は 1〜{self.num_channels} の範囲で指定してください (指定値={channel})")

    def _check_atten_range(self, atten_db: float) -> None:
        if not (self.min_atten_db <= atten_db <= self.max_atten_db):
            raise ValueError(
                f"attenuation {atten_db} dB は設定可能範囲外です "
                f"({self.min_atten_db:.2f}〜{self.max_atten_db:.2f} dB)"
            )

    def _to_raw(self, atten_db: float) -> int:
        """dB値をHR系APIの整数カウント値 (1カウント=0.05dB) に変換"""
        return round(atten_db / self.ATTEN_SCALE)

    def _select_channel(self, channel: int) -> None:
        self._check_channel(channel)
        ret = self.dll.fnLDA_SetChannel(self.device_id, channel)
        if ret != 0:
            raise VaunixLDAError(f"fnLDA_SetChannel(ch={channel}) に失敗しました (code={ret})")

    def _chmask(self, channels: Iterable[int]) -> int:
        """channelリストから chmask (ch1=bit0, ch2=bit1, ...) を作成"""
        mask = 0
        for ch in channels:
            self._check_channel(ch)
            mask |= (1 << (ch - 1))
        return mask

    def _check(self, ret: int, func_name: str) -> None:
        if ret != 0:
            raise VaunixLDAError(f"{func_name} に失敗しました (code={ret})")

    # ------------------------------------------------------------------
    # 必須関数 1: 複数ch(既定は全4ch)に同じアッテネーション値を同時に設定
    # fnLDA_SetAttenuationMCHR を1回呼び出すことで真の同時設定を実現する
    # ------------------------------------------------------------------
    def set_all_channels(self, atten_db: float, channels: Optional[Iterable[int]] = None) -> None:
        """
        指定したチャンネル群 (省略時は全4ch) を同じアッテネーション値に
        「同時に」設定する。fnLDA_SetAttenuationMCHR (マルチチャンネル一括設定API)
        を1回呼び出すことで実現しており、chごとにループしないため
        真に同時性の高い設定となる。

        Args:
            atten_db: 設定するアッテネーション値 [dB]
            channels: 対象チャンネルのリスト。省略時は1〜num_channelsの全チャンネル。
        """
        self._check_atten_range(atten_db)
        if channels is None:
            channels = range(1, self.num_channels + 1)
        chmask = self._chmask(channels)
        raw = self._to_raw(atten_db)
        ret = self.dll.fnLDA_SetAttenuationMCHR(self.device_id, raw, ctypes.c_ulonglong(chmask))
        self._check(ret, f"fnLDA_SetAttenuationMCHR(chmask=0x{chmask:X}, {atten_db}dB)")
        print(f"[set_all_channels] ch(mask=0x{chmask:X}) を {atten_db:.2f} dB に同時設定しました")

    # ------------------------------------------------------------------
    # 必須関数 2: 指定した1チャンネルのみアッテネーション値を設定
    # 対象chは引数 channel で指定する。fnLDA_SetAttenuationHRQ を使用し、
    # 1回のAPI呼び出しで完結する (内部でのSetChannel呼び出しは不要)。
    # ------------------------------------------------------------------
    def set_channel(self, channel: int, atten_db: float) -> None:
        """
        指定した1チャンネルのみアッテネーション値を設定する。

        Args:
            channel: 対象チャンネル番号 (1〜num_channels)
            atten_db: 設定するアッテネーション値 [dB]

        Example:
            lda.set_channel(2, 15.5)   # ch2のみ 15.5dB に設定
        """
        self._check_channel(channel)
        self._check_atten_range(atten_db)
        raw = self._to_raw(atten_db)
        ret = self.dll.fnLDA_SetAttenuationHRQ(self.device_id, raw, channel)
        self._check(ret, f"fnLDA_SetAttenuationHRQ(ch={channel}, {atten_db}dB)")
        print(f"[set_channel] ch{channel} を {atten_db:.2f} dB に設定しました")

    def get_channel_attenuation(self, channel: int) -> float:
        """指定した1チャンネルの現在のアッテネーション値[dB]を取得する"""
        self._select_channel(channel)
        raw = self.dll.fnLDA_GetAttenuationHR(self.device_id)
        if raw < 0:
            raise VaunixLDAError(f"fnLDA_GetAttenuationHR(ch={channel}) に失敗しました (code={raw})")
        return raw * self.ATTEN_SCALE

    def get_all_channel_attenuations(self, channels: Optional[Iterable[int]] = None) -> Dict[int, float]:
        """
        複数チャンネル(省略時は全4ch)の現在のアッテネーション値[dB]をまとめて取得する。

        内部的には get_channel_attenuation(channel) をチャンネルごとに呼び出す
        (fnLDA_SetChannelで対象chを切り替えてからfnLDA_GetAttenuationHRで読み出す
        方式のため、1回のAPI呼び出しで全chを同時に読み出す専用関数はマニュアル上
        存在しない)。

        Args:
            channels: 読み出し対象チャンネルのリスト。省略時は1〜num_channelsの全チャンネル。

        Returns:
            {チャンネル番号: アッテネーション値[dB]} の辞書

        Example:
            print(lda.get_all_channel_attenuations())
            # {1: 5.0, 2: 10.0, 3: 15.0, 4: 20.0}
        """
        if channels is None:
            channels = range(1, self.num_channels + 1)
        return {ch: self.get_channel_attenuation(ch) for ch in channels}

    # ------------------------------------------------------------------
    # 必須関数 3: 指定した1チャンネルのみアッテネーション値をランプ(掃引)させる
    # 対象chは引数 channel で指定する。LDA-802Q内蔵のハードウェアランプ
    # エンジンを使用する (fnLDA_SetRampStartHR等でパラメータ設定 →
    # fnLDA_StartRampでそのchのみランプ開始)。
    # ------------------------------------------------------------------
    def ramp_channel(self, channel: int, params: RampParams, go: bool = True) -> None:
        """
        指定した1チャンネルのみ、ハードウェアランプ(掃引)パラメータを設定し、
        go=True の場合はそのままランプを開始する。

        Args:
            channel: 対象チャンネル番号 (1〜num_channels)
            params: RampParams (開始値/終了値/ステップ/dwell時間/繰り返し等)
            go: True の場合、パラメータ設定後に即座にランプを開始する。
                False の場合、パラメータ設定のみ行い、開始は行わない
                (別途 start_ramp(channel) で開始できる)。

        Example:
            lda.ramp_channel(
                3,
                RampParams(start_db=0.0, stop_db=30.0, step_db=1.0, dwell_ms=50),
            )
        """
        self._check_channel(channel)
        self._check_atten_range(params.start_db)
        self._check_atten_range(params.stop_db)
        if params.step_db <= 0:
            raise ValueError("step_db は正の値で指定してください")
        if params.dwell_ms < 1:
            raise ValueError("dwell_ms は1ms以上で指定してください")

        self._select_channel(channel)  # 以降の設定・開始操作は全てこのchに対して適用される

        self._check(
            self.dll.fnLDA_SetRampStartHR(self.device_id, self._to_raw(params.start_db)),
            f"SetRampStartHR(ch={channel})",
        )
        self._check(
            self.dll.fnLDA_SetRampEndHR(self.device_id, self._to_raw(params.stop_db)),
            f"SetRampEndHR(ch={channel})",
        )
        # 最小ステップは2カウント(=0.1dB)。マニュアル3.5節の規定に合わせて丸める。
        raw_step = max(2, self._to_raw(params.step_db))
        self._check(
            self.dll.fnLDA_SetAttenuationStepHR(self.device_id, raw_step),
            f"SetAttenuationStepHR(ch={channel})",
        )
        self._check(
            self.dll.fnLDA_SetDwellTime(self.device_id, int(params.dwell_ms)),
            f"SetDwellTime(ch={channel})",
        )

        if params.step_db2 is not None:
            raw_step2 = max(2, self._to_raw(params.step_db2))
            self._check(
                self.dll.fnLDA_SetAttenuationStepTwoHR(self.device_id, raw_step2),
                f"SetAttenuationStepTwoHR(ch={channel})",
            )
        if params.dwell_ms2 is not None:
            self._check(
                self.dll.fnLDA_SetDwellTimeTwo(self.device_id, int(params.dwell_ms2)),
                f"SetDwellTimeTwo(ch={channel})",
            )

        self._check(
            self.dll.fnLDA_SetIdleTime(self.device_id, int(params.idle_ms)),
            f"SetIdleTime(ch={channel})",
        )
        self._check(
            self.dll.fnLDA_SetHoldTime(self.device_id, int(params.hold_ms)),
            f"SetHoldTime(ch={channel})",
        )
        # ランプ方向: start < stop なら上昇(up=True)、start > stop なら下降
        self._check(
            self.dll.fnLDA_SetRampDirection(self.device_id, ctypes.c_bool(params.stop_db >= params.start_db)),
            f"SetRampDirection(ch={channel})",
        )
        self._check(
            self.dll.fnLDA_SetRampMode(self.device_id, ctypes.c_bool(params.repeat)),
            f"SetRampMode(ch={channel})",
        )
        self._check(
            self.dll.fnLDA_SetRampBidirectional(self.device_id, ctypes.c_bool(params.bidirectional)),
            f"SetRampBidirectional(ch={channel})",
        )

        if go:
            self.start_ramp(channel, go=True)
            print(f"[ramp_channel] ch{channel} のランプを開始しました "
                  f"(start={params.start_db}dB, stop={params.stop_db}dB, "
                  f"step={params.step_db}dB, dwell={params.dwell_ms}ms)")
        else:
            print(f"[ramp_channel] ch{channel} のランプパラメータを設定しました (未開始)")

    def start_ramp(self, channel: int, go: bool = True) -> None:
        """
        指定した1チャンネルのハードウェアランプを開始/停止する。
        (ramp_channel() で事前にパラメータ設定済みであること)

        Args:
            channel: 対象チャンネル番号 (1〜num_channels)
            go: True=開始, False=停止
        """
        self._select_channel(channel)
        self._check(
            self.dll.fnLDA_StartRamp(self.device_id, ctypes.c_bool(go)),
            f"StartRamp(ch={channel}, go={go})",
        )

    def stop_ramp(self, channel: int) -> None:
        """指定した1チャンネルのランプを停止する"""
        self.start_ramp(channel, go=False)


# ==========================================================================
# コマンドライン引数
# ==========================================================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vaunix LDA-802Q control")
    parser.add_argument("--mode", required=True,
                        choices=["status", "set", "set_all", "ramp", "stop_ramp"])
    parser.add_argument("--serial", type=int, default=None)
    parser.add_argument("--dll-dir", type=str, default=r"C:\Vaunix")
    parser.add_argument("--test-mode", action="store_true")
    parser.add_argument("--channel", type=int, default=None)
    parser.add_argument("--channels", type=int, nargs="*", default=None)
    parser.add_argument("--attenuation-db", type=float, default=None)
    parser.add_argument("--start-db", type=float, default=None)
    parser.add_argument("--stop-db", type=float, default=None)
    parser.add_argument("--step-db", type=float, default=0.5)
    parser.add_argument("--dwell-ms", type=int, default=50)
    parser.add_argument("--step-db2", type=float, default=None)
    parser.add_argument("--dwell-ms2", type=int, default=None)
    parser.add_argument("--idle-ms", type=int, default=0)
    parser.add_argument("--hold-ms", type=int, default=0)
    parser.add_argument("--bidirectional", action="store_true")
    parser.add_argument("--repeat", action="store_true")
    parser.add_argument("--no-go", action="store_true")
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def _require(value, name: str):
    if value is None:
        raise ValueError(f"{name} is required")
    return value


def run_cli(args: argparse.Namespace) -> dict:
    report = {
        "timestamp": datetime.now().isoformat(),
        "mode": args.mode,
        "serial": args.serial,
        "success": False,
        "message": "",
        "values": None,
    }

    with VaunixLDA802Q(
        serial_number=args.serial,
        dll_dir=args.dll_dir,
        test_mode=args.test_mode,
    ) as lda:
        if args.mode == "status":
            report["values"] = lda.get_all_channel_attenuations(args.channels)
            report["message"] = "status acquired"
        elif args.mode == "set":
            lda.set_channel(
                _require(args.channel, "--channel"),
                _require(args.attenuation_db, "--attenuation-db"),
            )
            report["message"] = "channel attenuation set"
        elif args.mode == "set_all":
            lda.set_all_channels(
                _require(args.attenuation_db, "--attenuation-db"),
                args.channels,
            )
            report["message"] = "attenuation set"
        elif args.mode == "ramp":
            params = RampParams(
                start_db=_require(args.start_db, "--start-db"),
                stop_db=_require(args.stop_db, "--stop-db"),
                step_db=args.step_db,
                dwell_ms=args.dwell_ms,
                step_db2=args.step_db2,
                dwell_ms2=args.dwell_ms2,
                idle_ms=args.idle_ms,
                hold_ms=args.hold_ms,
                bidirectional=args.bidirectional,
                repeat=args.repeat,
            )
            lda.ramp_channel(_require(args.channel, "--channel"), params, go=not args.no_go)
            report["message"] = "ramp configured"
        elif args.mode == "stop_ramp":
            lda.stop_ramp(_require(args.channel, "--channel"))
            report["message"] = "ramp stopped"

    report["success"] = True
    return report


def main() -> None:
    args = parse_args()
    try:
        report = run_cli(args)
        rc = 0
    except Exception as exc:
        report = {
            "timestamp": datetime.now().isoformat(),
            "mode": args.mode,
            "serial": args.serial,
            "success": False,
            "message": str(exc),
            "values": None,
        }
        rc = 1

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    sys.exit(rc)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
ssh_client.py
パスワード認証SSH共通モジュール (paramiko使用)
ping_test.py / iperf_test.py / logcollect.py から import して使用する
"""

import logging
import os
import stat
from pathlib import Path

import paramiko

logger = logging.getLogger("ssh_client")


class SSHClient:
    """パスワード認証SSHクライアント（コンテキストマネージャ対応）"""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 22,
        connect_timeout: int = 10,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.connect_timeout = connect_timeout
        self._client: paramiko.SSHClient | None = None

    # ── 接続 / 切断 ────────────────────────────────────────────
    def connect(self) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            timeout=self.connect_timeout,
            allow_agent=False,
            look_for_keys=False,  # 鍵認証を無効化（パスワード認証のみ）
        )
        logger.debug(f"SSH接続成功: {self.user}@{self.host}:{self.port}")

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ── コマンド実行 ────────────────────────────────────────────
    def run(self, command: str, timeout: int = 600) -> tuple[int, str, str]:
        """
        リモートコマンド実行。
        Returns: (returncode, stdout, stderr)

        デバッグモード（logging.DEBUG 有効時）の場合、
        stdout / stderr を行単位でリアルタイムにログ出力する。
        """
        if self._client is None:
            raise RuntimeError("SSH未接続。connect()を先に呼んでください")

        is_debug = logger.isEnabledFor(logging.DEBUG)
        logger.debug(f"[{self.host}] 実行コマンド: {command}")

        _, stdout_ch, stderr_ch = self._client.exec_command(command, timeout=timeout)

        if is_debug:
            # ── デバッグモード: 行単位でリアルタイム表示 ─────────
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            for raw_line in stdout_ch:
                line = raw_line.rstrip("\n")
                stdout_lines.append(line)
                logger.debug(f"[{self.host}][stdout] {line}")
            for raw_line in stderr_ch:
                line = raw_line.rstrip("\n")
                stderr_lines.append(line)
                logger.debug(f"[{self.host}][stderr] {line}")
            exit_code = stdout_ch.channel.recv_exit_status()
            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)
        else:
            # ── 通常モード: まとめて受信 ─────────────────────────
            exit_code = stdout_ch.channel.recv_exit_status()
            stdout = stdout_ch.read().decode("utf-8", errors="replace")
            stderr = stderr_ch.read().decode("utf-8", errors="replace")

        logger.debug(f"[{self.host}] 終了コード: {exit_code}")
        return exit_code, stdout, stderr

    # ── ファイル転送 (SFTPダウンロード) ─────────────────────────
    def get_file(self, remote_path: str, local_path: Path) -> None:
        """
        リモートファイルをローカルに転送 (SFTP)
        """
        if self._client is None:
            raise RuntimeError("SSH未接続。connect()を先に呼んでください")

        local_path.parent.mkdir(parents=True, exist_ok=True)
        with self._client.open_sftp() as sftp:
            sftp.get(remote_path, str(local_path))
        logger.debug(f"  SFTP取得: {remote_path} → {local_path}")


def make_client(host: str, user: str, password: str, port: int = 22) -> SSHClient:
    """SSHClientインスタンスを生成するファクトリ関数"""
    return SSHClient(host=host, user=user, password=password, port=port)

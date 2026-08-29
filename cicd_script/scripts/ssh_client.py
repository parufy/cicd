#!/usr/bin/env python3
"""
ssh_client.py
パスワード認証SSH共通モジュール (paramiko使用)
ping_test.py / iperf_test.py / logcollect.py から import して使用する
"""

import base64
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import paramiko

logger = logging.getLogger("ssh_client")


def decode_jump_hosts(encoded: str | None) -> list[dict[str, Any]]:
    """Decode and validate the URL-safe Base64 SSH jump-host payload."""
    if not encoded:
        return []
    try:
        value = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except Exception as exc:
        raise ValueError("SSH踏み台情報をデコードできません") from exc
    if not isinstance(value, list):
        raise ValueError("SSH踏み台情報はリストで指定してください")

    jumps: list[dict[str, Any]] = []
    for index, host in enumerate(value, 1):
        if not isinstance(host, dict) or not host.get("host"):
            raise ValueError(f"SSH踏み台{index}のhostが未指定です")
        jumps.append({
            "host": str(host["host"]),
            "user": str(host.get("user", "root")),
            "password": str(host.get("password", "")),
            "port": int(host.get("port", 22)),
        })
    return jumps


class SSHClient:
    """パスワード認証SSHクライアント（コンテキストマネージャ対応）"""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 22,
        connect_timeout: int = 10,
        jump_hosts: list[dict[str, Any]] | None = None,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.connect_timeout = connect_timeout
        self.jump_hosts = jump_hosts or []
        self._client: paramiko.SSHClient | None = None
        self._clients: list[paramiko.SSHClient] = []
        self._channels: list[Any] = []

    # ── 接続 / 切断 ────────────────────────────────────────────
    def connect(self) -> None:
        route = [
            {
                "host": str(h["host"]),
                "user": str(h.get("user", "root")),
                "password": str(h.get("password", "")),
                "port": int(h.get("port", 22)),
            }
            for h in self.jump_hosts
        ]
        route.append({
            "host": self.host,
            "user": self.user,
            "password": self.password,
            "port": self.port,
        })

        sock = None
        try:
            for index, endpoint in enumerate(route):
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname=endpoint["host"],
                    port=endpoint["port"],
                    username=endpoint["user"],
                    password=endpoint["password"],
                    timeout=self.connect_timeout,
                    allow_agent=False,
                    look_for_keys=False,  # 鍵認証を無効化（パスワード認証のみ）
                    sock=sock,
                )
                self._clients.append(client)
                logger.debug(
                    "SSH接続成功 (%d/%d): %s@%s:%s",
                    index + 1,
                    len(route),
                    endpoint["user"],
                    endpoint["host"],
                    endpoint["port"],
                )

                if index < len(route) - 1:
                    next_endpoint = route[index + 1]
                    transport = client.get_transport()
                    if transport is None:
                        raise RuntimeError("SSH transportを取得できません")
                    sock = transport.open_channel(
                        "direct-tcpip",
                        (next_endpoint["host"], next_endpoint["port"]),
                        ("127.0.0.1", 0),
                    )
                    self._channels.append(sock)

            self._client = self._clients[-1]
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for client in reversed(self._clients):
            client.close()
        for channel in reversed(self._channels):
            channel.close()
        self._client = None
        self._clients = []
        self._channels = []

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

    def put_file(self, local_path: Path, remote_path: str) -> None:
        """Upload one local file to the remote host with SFTP."""
        if self._client is None:
            raise RuntimeError("SSH未接続です。connect()を先に呼んでください")

        with self._client.open_sftp() as sftp:
            self._sftp_mkdirs(sftp, self._remote_parent(remote_path))
            sftp.put(str(local_path), remote_path)
        logger.debug(f"  SFTP put: {local_path} -> {remote_path}")

    def put_directory(self, local_dir: Path, remote_dir: str) -> None:
        """Upload a local directory tree to the remote host with SFTP."""
        if self._client is None:
            raise RuntimeError("SSH未接続です。connect()を先に呼んでください")

        local_dir = local_dir.resolve()
        with self._client.open_sftp() as sftp:
            self._sftp_mkdirs(sftp, remote_dir)
            for path in local_dir.rglob("*"):
                rel = path.relative_to(local_dir).as_posix()
                remote_path = self._remote_join(remote_dir, rel)
                if path.is_dir():
                    self._sftp_mkdirs(sftp, remote_path)
                else:
                    self._sftp_mkdirs(sftp, self._remote_parent(remote_path))
                    sftp.put(str(path), remote_path)
                    logger.debug(f"  SFTP put: {path} -> {remote_path}")

    @staticmethod
    def _remote_join(base: str, child: str) -> str:
        return base.rstrip("/\\") + "/" + child.replace("\\", "/")

    @staticmethod
    def _remote_parent(path: str) -> str:
        normalized = path.replace("\\", "/")
        parent = normalized.rsplit("/", 1)[0]
        return parent if parent else "."

    @staticmethod
    def _sftp_mkdirs(sftp, remote_dir: str) -> None:
        remote_dir = remote_dir.replace("\\", "/").rstrip("/")
        if not remote_dir or remote_dir == ".":
            return

        parts = remote_dir.split("/")
        current = parts[0]
        start_index = 1
        if current == "":
            current = "/"
        elif current.endswith(":"):
            current += "/"

        for part in parts[start_index:]:
            if not part:
                continue
            current = current.rstrip("/") + "/" + part
            try:
                sftp.stat(current)
            except IOError:
                sftp.mkdir(current)


def make_client(
    host: str,
    user: str,
    password: str,
    port: int = 22,
    jump_hosts: list[dict[str, Any]] | None = None,
) -> SSHClient:
    """SSHClientインスタンスを生成するファクトリ関数"""
    return SSHClient(
        host=host,
        user=user,
        password=password,
        port=port,
        jump_hosts=jump_hosts,
    )

#!/usr/bin/env python3
"""Start and stop tcpdump on a host reached through zero or more SSH jumps."""

import argparse
import json
import logging
import posixpath
import re
import shlex
import sys
from pathlib import Path

from ssh_client import decode_jump_hosts, make_client


logger = logging.getLogger("tcpdump_control")
CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _quote(value: object) -> str:
    return shlex.quote(str(value))


def build_start_command(args: argparse.Namespace) -> str:
    tcpdump = [
        args.tcpdump_path,
        "-U",
        "-n",
        "-i",
        args.interface,
        "-s",
        str(args.snaplen),
        "-w",
        args.remote_file,
    ]
    if args.packet_count:
        tcpdump += ["-c", str(args.packet_count)]
    if args.capture_filter:
        tcpdump += shlex.split(args.capture_filter)

    tcpdump_command = " ".join(_quote(token) for token in tcpdump)
    if args.sudo:
        tcpdump_command = f"sudo -n {tcpdump_command}"
    inner = (
        f"umask 022; nohup {tcpdump_command} >{_quote(args.log_file)} 2>&1 & "
        f"echo $! >{_quote(args.pid_file)}"
    )
    launcher = f"sh -c {_quote(inner)}"

    check_command = "kill -0 \"$pid\""
    if args.sudo:
        check_command = "sudo -n kill -0 \"$pid\""

    return (
        "set -eu; "
        f"if [ -f {_quote(args.pid_file)} ]; then "
        f"pid=$(cat {_quote(args.pid_file)}); "
        f"if [ -n \"$pid\" ] && {check_command} 2>/dev/null; then "
        "echo 'tcpdump is already running' >&2; exit 3; fi; "
        f"rm -f {_quote(args.pid_file)}; fi; "
        f"mkdir -p {_quote(posixpath.dirname(args.remote_file) or '.')}; "
        f"{launcher}; cat {_quote(args.pid_file)}"
    )


def build_stop_command(args: argparse.Namespace) -> str:
    signal_command = "kill -INT \"$pid\""
    check_command = "kill -0 \"$pid\""
    term_command = "kill -TERM \"$pid\""
    chmod_command = f"chmod a+r {_quote(args.remote_file)}"
    if args.sudo:
        signal_command = "sudo -n kill -INT \"$pid\""
        check_command = "sudo -n kill -0 \"$pid\""
        term_command = "sudo -n kill -TERM \"$pid\""
        chmod_command = f"sudo -n chmod a+r {_quote(args.remote_file)}"

    expected_file = _quote(args.remote_file)

    return (
        "set -eu; "
        f"test -f {_quote(args.pid_file)} || "
        "{ echo 'tcpdump pid file not found' >&2; exit 4; }; "
        f"pid=$(cat {_quote(args.pid_file)}); "
        "case \"$pid\" in ''|*[!0-9]*) echo 'invalid tcpdump pid' >&2; exit 5;; esac; "
        "process_args=$(ps -p \"$pid\" -o args= 2>/dev/null || true); "
        f"expected_file={expected_file}; "
        "if [ -z \"$process_args\" ]; then "
        f"test -f {_quote(args.remote_file)} || "
        "{ echo 'tcpdump process and capture file not found' >&2; exit 7; }; "
        f"{chmod_command}; rm -f {_quote(args.pid_file)}; echo \"$pid\"; exit 0; fi; "
        "case \"$process_args\" in *tcpdump*\"$expected_file\"*) ;; "
        "*) echo 'pid does not match the requested tcpdump capture' >&2; exit 6;; esac; "
        f"{signal_command}; "
        "i=0; while " + check_command + " 2>/dev/null && [ \"$i\" -lt "
        f"{int(args.stop_timeout)} ]; do sleep 1; i=$((i+1)); done; "
        "if " + check_command + " 2>/dev/null; then " + term_command + "; fi; "
        f"{chmod_command}; rm -f {_quote(args.pid_file)}; echo \"$pid\""
    )


def run(args: argparse.Namespace) -> dict:
    jump_hosts = decode_jump_hosts(args.ssh_jumps_b64)
    command = build_start_command(args) if args.mode == "start" else build_stop_command(args)
    route_label = [h["host"] for h in jump_hosts] + [args.ssh_host]

    with make_client(
        args.ssh_host,
        args.ssh_user,
        args.ssh_password,
        args.ssh_port,
        jump_hosts=jump_hosts,
    ) as ssh:
        rc, stdout, stderr = ssh.run(command, timeout=args.timeout)
        if rc != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or f"remote command failed: {rc}")

        local_file = None
        if args.mode == "stop" and args.local_file:
            local_path = Path(args.local_file)
            ssh.get_file(args.remote_file, local_path)
            local_file = str(local_path)

    return {
        "success": True,
        "mode": args.mode,
        "capture_id": args.capture_id,
        "ssh_route": route_label,
        "remote_file": args.remote_file,
        "local_file": local_file,
        "pid": stdout.strip(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多段SSH先のtcpdumpを開始・停止します")
    parser.add_argument("--mode", required=True, choices=("start", "stop"))
    parser.add_argument("--capture-id", default="capture")
    parser.add_argument("--interface", default="any")
    parser.add_argument("--capture-filter", default="")
    parser.add_argument("--snaplen", type=int, default=0)
    parser.add_argument("--packet-count", type=int, default=0)
    parser.add_argument("--tcpdump-path", default="tcpdump")
    parser.add_argument("--remote-file")
    parser.add_argument("--pid-file")
    parser.add_argument("--log-file")
    parser.add_argument("--local-file")
    parser.add_argument("--stop-timeout", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--no-sudo", dest="sudo", action="store_false")
    parser.set_defaults(sudo=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-password", default="")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-jumps-b64", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not CAPTURE_ID_RE.fullmatch(args.capture_id):
        parser.error("--capture-idには英数字、'.'、'_'、'-'のみ使用できます")
    args.remote_file = args.remote_file or f"/tmp/tcpdump_{args.capture_id}.pcap"
    args.pid_file = args.pid_file or f"/tmp/tcpdump_{args.capture_id}.pid"
    args.log_file = args.log_file or f"/tmp/tcpdump_{args.capture_id}.log"
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    try:
        result = run(args)
    except Exception as exc:
        result = {"success": False, "mode": args.mode, "error": str(exc)}
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(exc), file=sys.stderr)
        return 1

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

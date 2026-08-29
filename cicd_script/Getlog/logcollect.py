import logging
import shlex
import sys
from pathlib import Path

import config


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_ROOT / "scripts"))

from ssh_client import make_client  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] logcollect - %(message)s",
)
logger = logging.getLogger("logcollect")


def _quote(value: object) -> str:
    return shlex.quote(str(value))


def _run_checked(ssh, command: str, label: str) -> str:
    rc, stdout, stderr = ssh.run(command)
    if rc != 0:
        detail = stderr.strip() or stdout.strip() or f"return code {rc}"
        raise RuntimeError(f"{label} failed: {detail}")
    return stdout


def _connect(params: dict):
    return make_client(
        params["host"],
        params["user"],
        params["password"],
        int(params.get("port", 22)),
        jump_hosts=params.get("jumps", getattr(config, "SSH_JUMPS", [])),
    )


def _last_nonempty_line(output: str, label: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"{label} returned no output")
    return lines[-1]


def get_4gdu_log() -> Path:
    params = config.ENB_DU_PARAM
    local_script_dir = Path(__file__).parent / params["script_dir"]
    remote_script_dir = (
        params["script_install_path"].rstrip("/") + "/" + params["script_dir"]
    )
    output_dir = Path.home() / "LOG"
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Copy 4GDU log script to %s", params["host"])
    with _connect(params) as ssh:
        ssh.put_directory(local_script_dir, remote_script_dir)
        pod_output = _run_checked(
            ssh,
            f"{params['export_kubeconf']}; cd {_quote(remote_script_dir)}; "
            f"/bin/bash ./get_dupod_name.sh {_quote(params['namespace'])}",
            "Get DU pod name",
        )
        pod_name = _last_nonempty_line(pod_output, "Get DU pod name")
        _run_checked(
            ssh,
            f"{params['export_kubeconf']}; cd {_quote(remote_script_dir)}; "
            f"python3 4gdu_getlog.py {_quote(pod_name)}",
            "Collect DU log",
        )
        remote_log = params["log_path"].rstrip("/") + f"/{pod_name}.tar.gz"
        local_log = output_dir / f"{pod_name}.tar.gz"
        ssh.get_file(remote_log, local_log)
        _run_checked(ssh, f"rm -f -- {_quote(remote_log)}", "Delete remote DU log")

    logger.info("4GDU log: %s", local_log)
    return local_log


def get_4gcu_log() -> Path:
    params = config.ENB_CU_PARAM
    local_script_dir = Path(__file__).parent / params["script_dir"]
    remote_script_dir = (
        params["script_install_path"].rstrip("/") + "/" + params["script_dir"]
    )
    output_dir = Path.home() / "LOG"
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Copy 4GCU log script to %s", params["host"])
    with _connect(params) as ssh:
        ssh.put_directory(local_script_dir, remote_script_dir)
        log_output = _run_checked(
            ssh,
            f"cd {_quote(remote_script_dir)}; /bin/bash ./4gcu_getlog.sh "
            f"{_quote(params['user'])} {_quote(params['password'])}",
            "Collect CU log",
        )
        log_name = _last_nonempty_line(log_output, "Collect CU log")
        remote_log = remote_script_dir + f"/{log_name}.tar.gz"
        local_log = output_dir / f"{log_name}.tar.gz"
        ssh.get_file(remote_log, local_log)
        _run_checked(ssh, f"rm -f -- {_quote(remote_log)}", "Delete remote CU log")

    logger.info("4GCU log: %s", local_log)
    return local_log


# Keep the original public names for callers outside this repository.
Get_4GDU_LOG = get_4gdu_log
Get_4GCU_LOG = get_4gcu_log


def main() -> None:
    get_4gdu_log()
    get_4gcu_log()


if __name__ == "__main__":
    main()

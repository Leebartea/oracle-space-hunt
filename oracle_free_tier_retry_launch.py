#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_INTERVAL_SECONDS = 1800
DEFAULT_JITTER_SECONDS = 300
DEFAULT_UI_PORT = 5065
CRON_MARKER = "ORACLE_RETRY_JOB=1"
LAUNCH_AGENT_LABEL = "com.greystone.oracle-retry"
SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parent.parent
DEFAULT_RUNTIME_ROOT = Path.home() / ".oracle-retry"
DEFAULT_LOG_PATH = PROJECT_ROOT / "oracle_retry.log"
DEFAULT_STATE_PATH = PROJECT_ROOT / "oracle_retry_state.json"
DEFAULT_LOCK_PATH = PROJECT_ROOT / "oracle_retry.lock"
SAFE_RETRY_CATEGORIES = {"capacity", "throttled", "network"}
RETRYABLE_EXIT_CODES = {3, 4, 5, 6, 8, 9}


def _now(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def log(message: str) -> None:
    print(f"[oracle-retry] {_now()} {message}", flush=True)


def _read_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return data


def _write_json_file(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
        temp_name = fh.name
    os.replace(temp_name, path)


def _expand_path(path_str: Optional[str]) -> Optional[Path]:
    if not path_str:
        return None
    return Path(os.path.expanduser(path_str)).resolve()


def _get_first(mapping: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _bool_flag(value: bool) -> str:
    return "true" if bool(value) else "false"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def classify_oci_error(message: str) -> str:
    text = str(message or "").lower()
    if any(token in text for token in ("out of host capacity", "out_of_host_capacity", "outofhostcapacity")):
        return "capacity"
    if any(token in text for token in ("too many requests", "toomanyrequests", "throttl", "rate limit", "429")):
        return "throttled"
    if any(
        token in text
        for token in (
            "timed out",
            "timeout",
            "network is unreachable",
            "temporary failure",
            "connection reset",
            "could not connect",
            "failed to establish a new connection",
            "name or service not known",
            "service unavailable",
            "requestexception",
        )
    ):
        return "network"
    if any(token in text for token in ("quotaexceeded", "limitexceeded", "service limit", "limit exceeded")):
        return "limits"
    if any(token in text for token in ("notauthenticated", "notauthorized", "permission denied", "config file", "no such file")):
        return "auth"
    if any(token in text for token in ("invalidparameter", "missingparameter", "bad request", "validation", "subnet", "image", "shape")):
        return "config"
    return "other"


def _build_oci_base_command(profile: Optional[str], region: Optional[str]) -> List[str]:
    cmd = ["oci"]
    if profile:
        cmd.extend(["--profile", profile])
    if region:
        cmd.extend(["--region", region])
    return cmd


def _run_oci_command(cmd: List[str], *, dry_run: bool = False) -> Tuple[bool, Dict[str, Any], str]:
    rendered = " ".join(cmd)
    if dry_run:
        log(f"dry-run: {rendered}")
        return True, {}, ""

    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return False, {}, "oci CLI not found. Install the official OCI CLI before using this script."
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    if proc.returncode == 0:
        payload: Dict[str, Any] = {}
        if stdout:
            try:
                decoded = json.loads(stdout)
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                pass
        return True, payload, stderr
    return False, {}, "\n".join(part for part in (stdout, stderr) if part).strip()


def _write_temp_json(payload: Any) -> str:
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8")
    json.dump(payload, handle)
    handle.flush()
    handle.close()
    return handle.name


def _extract_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else {}


def _pick_existing_instance(items: List[Dict[str, Any]], display_name: str) -> Optional[Dict[str, Any]]:
    target = str(display_name or "").strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        if _get_first(item, "display-name", "displayName", default="") != target:
            continue
        lifecycle = str(_get_first(item, "lifecycle-state", "lifecycleState", default="")).upper()
        if lifecycle in {"TERMINATED", "TERMINATING"}:
            continue
        return item
    return None


def find_existing_instance(config: Dict[str, Any], *, dry_run: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    tenancy = config["tenancy"]
    launch = config["launch"]
    cmd = _build_oci_base_command(tenancy.get("profile"), tenancy.get("region"))
    cmd.extend(
        [
            "compute",
            "instance",
            "list",
            "--compartment-id",
            launch["target_compartment_id"],
            "--all",
            "--output",
            "json",
        ]
    )
    ok, payload, err = _run_oci_command(cmd, dry_run=dry_run)
    if not ok:
        return None, err
    data = payload.get("data", [])
    items = data if isinstance(data, list) else []
    return _pick_existing_instance(items, str(launch["display_name"])), None


def _launch_profile_from_config(config: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    launch = dict(config["launch"])
    launch["ocpus"] = profile["ocpus"]
    launch["memory_in_gbs"] = profile["memory_in_gbs"]
    return launch


def _build_profiles(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    launch = config["launch"]
    retry_cfg = config.get("retry") or {}
    fallback_cfg = retry_cfg.get("fallback") or {}
    profiles = [
        {
            "label": "primary",
            "ocpus": _safe_float(launch.get("ocpus"), 0),
            "memory_in_gbs": _safe_float(launch.get("memory_in_gbs"), 0),
            "fallback": False,
        }
    ]
    fallback_enabled = bool(fallback_cfg.get("enabled"))
    fallback_ocpus = _safe_float(fallback_cfg.get("ocpus", fallback_cfg.get("fallback_ocpus", 0)), 0)
    fallback_memory = _safe_float(fallback_cfg.get("memory_in_gbs", fallback_cfg.get("fallback_memory_in_gbs", 0)), 0)
    if fallback_enabled and fallback_ocpus > 0 and fallback_memory > 0:
        profiles.append(
            {
                "label": "fallback",
                "ocpus": fallback_ocpus,
                "memory_in_gbs": fallback_memory,
                "fallback": True,
            }
        )
    return profiles


def create_capacity_report(
    config: Dict[str, Any],
    launch_cfg: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    tenancy = config["tenancy"]
    shape_payload: Dict[str, Any] = {
        "instanceShape": launch_cfg["shape"],
        "instanceShapeConfig": {
            "ocpus": _safe_float(launch_cfg["ocpus"]),
            "memoryInGBs": _safe_float(launch_cfg["memory_in_gbs"]),
        },
    }
    if launch_cfg.get("fault_domain"):
        shape_payload["faultDomain"] = launch_cfg["fault_domain"]

    shape_file = _write_temp_json([shape_payload])
    try:
        cmd = _build_oci_base_command(tenancy.get("profile"), tenancy.get("region"))
        cmd.extend(
            [
                "compute",
                "compute-capacity-report",
                "create",
                "--availability-domain",
                tenancy["availability_domain"],
                "--compartment-id",
                tenancy["root_compartment_id"],
                "--shape-availabilities",
                f"file://{shape_file}",
                "--output",
                "json",
            ]
        )
        ok, payload, err = _run_oci_command(cmd, dry_run=dry_run)
        return ok, payload, err or None
    finally:
        try:
            os.unlink(shape_file)
        except OSError:
            pass


def interpret_capacity_report(payload: Dict[str, Any], requested_count: int = 1) -> Dict[str, Any]:
    data = _extract_data(payload)
    raw_items = _get_first(data, "shape-availabilities", "shapeAvailabilities", default=[])
    items = raw_items if isinstance(raw_items, list) else []
    result = {
        "available": False,
        "status": "UNKNOWN",
        "available_count": 0,
        "fault_domain": None,
        "raw_items": items,
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(_get_first(item, "availability-status", "availabilityStatus", default="UNKNOWN")).upper()
        available_count = _safe_int(_get_first(item, "available-count", "availableCount", default=0))
        fault_domain = _get_first(item, "fault-domain", "faultDomain", default=None)
        if status == "AVAILABLE" and available_count >= int(requested_count):
            result.update(
                {
                    "available": True,
                    "status": status,
                    "available_count": available_count,
                    "fault_domain": fault_domain,
                }
            )
            return result
        if result["status"] == "UNKNOWN":
            result.update(
                {
                    "status": status,
                    "available_count": available_count,
                    "fault_domain": fault_domain,
                }
            )
    return result


def launch_instance(
    config: Dict[str, Any],
    launch_cfg: Dict[str, Any],
    *,
    fault_domain: Optional[str],
    dry_run: bool = False,
) -> Tuple[bool, Dict[str, Any], str]:
    tenancy = config["tenancy"]
    metadata: Dict[str, Any] = {}
    ssh_key_file = _expand_path(launch_cfg.get("ssh_authorized_keys_file"))
    if ssh_key_file:
        metadata["ssh_authorized_keys"] = ssh_key_file.read_text(encoding="utf-8").strip()

    metadata_payload = dict(launch_cfg.get("metadata") or {})
    metadata_payload.update(metadata)

    shape_config = {
        "ocpus": _safe_float(launch_cfg["ocpus"]),
        "memoryInGBs": _safe_float(launch_cfg["memory_in_gbs"]),
    }

    metadata_file = _write_temp_json(metadata_payload)
    shape_file = _write_temp_json(shape_config)
    try:
        cmd = _build_oci_base_command(tenancy.get("profile"), tenancy.get("region"))
        cmd.extend(
            [
                "compute",
                "instance",
                "launch",
                "--availability-domain",
                tenancy["availability_domain"],
                "--compartment-id",
                launch_cfg["target_compartment_id"],
                "--subnet-id",
                launch_cfg["subnet_id"],
                "--shape",
                launch_cfg["shape"],
                "--image-id",
                launch_cfg["image_id"],
                "--display-name",
                launch_cfg["display_name"],
                "--shape-config",
                f"file://{shape_file}",
                "--metadata",
                f"file://{metadata_file}",
                "--assign-public-ip",
                _bool_flag(bool(launch_cfg.get("assign_public_ip", True))),
                "--output",
                "json",
            ]
        )
        if fault_domain:
            cmd.extend(["--fault-domain", str(fault_domain)])
        if launch_cfg.get("boot_volume_size_in_gbs"):
            cmd.extend(["--boot-volume-size-in-gbs", str(_safe_int(launch_cfg["boot_volume_size_in_gbs"]))])
        if launch_cfg.get("hostname_label"):
            cmd.extend(["--hostname-label", str(launch_cfg["hostname_label"])])
        return _run_oci_command(cmd, dry_run=dry_run)
    finally:
        for temp_path in (metadata_file, shape_file):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _sleep_duration(base_seconds: int, jitter_seconds: int) -> int:
    jitter = random.randint(0, max(0, int(jitter_seconds)))
    return max(1, int(base_seconds) + jitter)


def _hit_max_attempts(attempt: int, max_attempts: int) -> bool:
    return bool(max_attempts) and int(attempt) >= int(max_attempts)


def _log_retry_pause(message: str, sleep_seconds: int, *, will_retry: bool) -> None:
    if will_retry:
        log(f"{message}; sleeping {sleep_seconds}s")
    else:
        log(f"{message}; would sleep {sleep_seconds}s on a repeating run")


def _require_fields(config: Dict[str, Any]) -> None:
    tenancy = config.get("tenancy")
    launch = config.get("launch")
    if not isinstance(tenancy, dict) or not isinstance(launch, dict):
        raise ValueError("Config must contain 'tenancy' and 'launch' objects")

    required_tenancy = ["availability_domain", "root_compartment_id", "region"]
    required_launch = [
        "display_name",
        "target_compartment_id",
        "subnet_id",
        "image_id",
        "shape",
        "ocpus",
        "memory_in_gbs",
        "ssh_authorized_keys_file",
    ]
    missing = [f"tenancy.{name}" for name in required_tenancy if not tenancy.get(name)]
    missing.extend(f"launch.{name}" for name in required_launch if not launch.get(name))
    if missing:
        raise ValueError(f"Missing required config fields: {', '.join(missing)}")


def _artifact_path(config: Dict[str, Any], key: str, default_path: Path) -> Path:
    artifacts = config.get("artifacts") or {}
    custom = artifacts.get(key)
    return _expand_path(custom) if custom else default_path


def _runtime_root(config: Dict[str, Any]) -> Path:
    artifacts = config.get("artifacts") or {}
    custom = artifacts.get("runtime_root")
    return _expand_path(custom) if custom else DEFAULT_RUNTIME_ROOT


def _runtime_script_path(config: Dict[str, Any]) -> Path:
    return _runtime_root(config) / "oracle_free_tier_retry_launch.py"


def _runtime_config_path(config: Dict[str, Any]) -> Path:
    return _runtime_root(config) / "oracle_free_tier_retry_launch.local.json"


def _default_state(config: Dict[str, Any]) -> Dict[str, Any]:
    launch = config.get("launch") or {}
    fallback_cfg = (config.get("retry") or {}).get("fallback") or {}
    return {
        "version": 1,
        "attempt_count": 0,
        "last_started_at": None,
        "last_finished_at": None,
        "last_result": "never_run",
        "last_message": "No attempts yet.",
        "last_exit_code": None,
        "last_capacity_status": None,
        "last_capacity_available_count": None,
        "last_fault_domain": None,
        "last_target_label": "primary",
        "last_target_ocpus": _safe_float(launch.get("ocpus"), 0),
        "last_target_memory_in_gbs": _safe_float(launch.get("memory_in_gbs"), 0),
        "last_run_mode": None,
        "last_success_at": None,
        "secured": False,
        "secured_instance_id": None,
        "secured_lifecycle": None,
        "fallback_enabled": bool(fallback_cfg.get("enabled")),
    }


def _load_state(state_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    if not state_path.exists():
        return _default_state(config)
    try:
        payload = _read_json_file(state_path)
    except Exception:
        return _default_state(config)
    merged = _default_state(config)
    merged.update(payload)
    return merged


def _save_state(state_path: Path, state: Dict[str, Any]) -> None:
    _write_json_file(state_path, state)


def _log_tail(log_path: Path, max_lines: int = 16) -> List[str]:
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _acquire_lock(lock_path: Path, stale_after_seconds: int = 4 * 3600) -> Optional[int]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0
        if age > stale_after_seconds:
            try:
                lock_path.unlink()
            except OSError:
                pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    os.write(fd, f"{os.getpid()} {_now()}\n".encode("utf-8"))
    return fd


def _release_lock(lock_path: Path, fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        lock_path.unlink()
    except OSError:
        pass


def _read_crontab() -> str:
    try:
        proc = subprocess.run(["crontab", "-l"], text=True, capture_output=True)
    except (FileNotFoundError, PermissionError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _preferred_python_path() -> str:
    stable_homebrew = Path("/opt/homebrew/bin/python3")
    if stable_homebrew.exists():
        return str(stable_homebrew)
    return sys.executable


def _start_caffeinate_guard(pid: int) -> Optional[subprocess.Popen[str]]:
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, PermissionError):
        return None
    return proc


def build_cron_line(config_path: Path, log_path: Path, python_path: Optional[str] = None) -> str:
    python_exec = python_path or _preferred_python_path()
    parts = [
        CRON_MARKER,
        shlex.quote(python_exec),
        shlex.quote(str(SCRIPT_PATH)),
        "--config",
        shlex.quote(str(config_path)),
        "--once",
    ]
    command = " ".join(parts)
    return f"*/30 * * * * {command} >> {shlex.quote(str(log_path))} 2>&1"


def _cron_enabled(config_path: Path, crontab_text: Optional[str] = None) -> bool:
    text = _read_crontab() if crontab_text is None else crontab_text
    target = str(config_path)
    for line in text.splitlines():
        if CRON_MARKER in line and target in line:
            return True
    return False


def _remove_legacy_cron_scheduler() -> None:
    current = _read_crontab()
    lines = [line for line in current.splitlines() if CRON_MARKER not in line]
    payload = ("\n".join(lines).strip() + "\n") if lines else ""
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
        fh.write(payload)
        temp_name = fh.name
    try:
        subprocess.run(["crontab", temp_name], check=True, capture_output=True, text=True)
    except (FileNotFoundError, PermissionError, subprocess.CalledProcessError):
        pass
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _scheduler_backend() -> str:
    return "launchd" if sys.platform == "darwin" else "cron"


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_service_target() -> str:
    return f"{_launchd_domain()}/{LAUNCH_AGENT_LABEL}"


def _launchd_environment() -> Dict[str, str]:
    return {
        "ORACLE_RETRY_JOB": "1",
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
    }


def _build_launch_agent_payload(
    script_path: Path,
    config_path: Path,
    log_path: Path,
    python_path: Optional[str] = None,
) -> Dict[str, Any]:
    python_exec = python_path or _preferred_python_path()
    shell_command = " ".join(
        [
            "ORACLE_RETRY_JOB=1",
            shlex.quote(python_exec),
            shlex.quote(str(script_path)),
            "--config",
            shlex.quote(str(config_path)),
            "--once",
            ">>",
            shlex.quote(str(log_path)),
            "2>&1",
        ]
    )
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            "/bin/zsh",
            "-c",
            shell_command,
        ],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": _launchd_environment(),
        "StartCalendarInterval": [
            {"Minute": 0},
            {"Minute": 30},
        ],
        "RunAtLoad": False,
        "ProcessType": "Background",
    }


def _write_launch_agent_plist(script_path: Path, config_path: Path, log_path: Path) -> Path:
    plist_path = _launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _build_launch_agent_payload(script_path, config_path, log_path)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(plist_path.parent)) as fh:
        plistlib.dump(payload, fh, sort_keys=False)
        temp_name = fh.name
    os.replace(temp_name, plist_path)
    return plist_path


def _launchd_enabled() -> bool:
    plist_path = _launch_agent_path()
    if not plist_path.exists():
        return False
    proc = subprocess.run(
        ["launchctl", "print", _launchd_service_target()],
        text=True,
        capture_output=True,
    )
    return proc.returncode == 0


def _prepare_scheduler_runtime(config_path: Path) -> Tuple[Path, Path, Path]:
    config = _read_json_file(config_path)
    runtime_root = _runtime_root(config)
    runtime_root.mkdir(parents=True, exist_ok=True)

    runtime_script = _runtime_script_path(config)
    runtime_config = _runtime_config_path(config)
    runtime_log = runtime_root / "oracle_retry.log"
    runtime_state = runtime_root / "oracle_retry_state.json"
    runtime_lock = runtime_root / "oracle_retry.lock"

    merged_config = json.loads(json.dumps(config))
    artifacts = merged_config.setdefault("artifacts", {})
    artifacts["runtime_root"] = str(runtime_root)
    artifacts["log_file"] = str(runtime_log)
    artifacts["state_file"] = str(runtime_state)
    artifacts["lock_file"] = str(runtime_lock)

    source_log = _artifact_path(config, "log_file", DEFAULT_LOG_PATH)
    source_state = _artifact_path(config, "state_file", DEFAULT_STATE_PATH)
    if source_log.exists() and not runtime_log.exists():
        shutil.copy2(source_log, runtime_log)
    if source_state.exists() and not runtime_state.exists():
        shutil.copy2(source_state, runtime_state)

    shutil.copy2(SCRIPT_PATH, runtime_script)
    os.chmod(runtime_script, 0o755)
    _write_json_file(runtime_config, merged_config)
    _write_json_file(config_path, merged_config)
    return runtime_script, runtime_config, runtime_log


def install_scheduler(config_path: Path, log_path: Path) -> None:
    if _scheduler_backend() == "launchd":
        runtime_script, runtime_config, runtime_log = _prepare_scheduler_runtime(config_path)
        plist_path = _write_launch_agent_plist(runtime_script, runtime_config, runtime_log)
        subprocess.run(
            ["launchctl", "bootout", _launchd_domain(), str(plist_path)],
            text=True,
            capture_output=True,
        )
        subprocess.run(
            ["launchctl", "bootstrap", _launchd_domain(), str(plist_path)],
            check=True,
            text=True,
            capture_output=True,
        )
        _remove_legacy_cron_scheduler()
        return

    current = _read_crontab()
    lines = [line for line in current.splitlines() if CRON_MARKER not in line]
    lines.append(build_cron_line(config_path, log_path))
    payload = "\n".join(lines).strip() + "\n"
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
        fh.write(payload)
        temp_name = fh.name
    try:
        subprocess.run(["crontab", temp_name], check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("crontab is not available on this machine.") from exc
    except PermissionError as exc:
        raise RuntimeError("crontab access is blocked in this environment.") from exc
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def remove_scheduler() -> None:
    if _scheduler_backend() == "launchd":
        plist_path = _launch_agent_path()
        subprocess.run(
            ["launchctl", "bootout", _launchd_domain(), str(plist_path)],
            text=True,
            capture_output=True,
        )
        try:
            plist_path.unlink()
        except FileNotFoundError:
            pass
        _remove_legacy_cron_scheduler()
        return

    current = _read_crontab()
    lines = [line for line in current.splitlines() if CRON_MARKER not in line]
    payload = ("\n".join(lines).strip() + "\n") if lines else ""
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
        fh.write(payload)
        temp_name = fh.name
    try:
        subprocess.run(["crontab", temp_name], check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("crontab is not available on this machine.") from exc
    except PermissionError as exc:
        raise RuntimeError("crontab access is blocked in this environment.") from exc
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _next_half_hour_run(now_ts: Optional[float] = None) -> str:
    ts = now_ts or time.time()
    local = time.localtime(ts)
    minute = local.tm_min
    second = local.tm_sec
    add_minutes = 30 - (minute % 30)
    if add_minutes == 30 and second == 0:
        add_minutes = 0
    next_ts = ts + (add_minutes * 60) - second
    if add_minutes == 0 and second == 0:
        next_ts = ts
    return _now(next_ts)


def _update_state(
    state_path: Path,
    config: Dict[str, Any],
    mutate,
) -> Dict[str, Any]:
    state = _load_state(state_path, config)
    updated = mutate(state) or state
    _save_state(state_path, updated)
    return updated


def _build_status_payload(config_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    state_path = _artifact_path(config, "state_file", DEFAULT_STATE_PATH)
    log_path = _artifact_path(config, "log_file", DEFAULT_LOG_PATH)
    state = _load_state(state_path, config)
    retry_cfg = config.get("retry") or {}
    fallback_cfg = retry_cfg.get("fallback") or {}
    backend = _scheduler_backend()
    if backend == "launchd":
        enabled = _launchd_enabled()
    else:
        crontab_text = _read_crontab()
        enabled = _cron_enabled(config_path, crontab_text)
    return {
        "scheduler_backend": backend,
        "scheduler_enabled": enabled,
        "next_run_at": _next_half_hour_run() if enabled else None,
        "state_path": str(state_path),
        "log_path": str(log_path),
        "attempt_count": _safe_int(state.get("attempt_count"), 0),
        "last_started_at": state.get("last_started_at"),
        "last_finished_at": state.get("last_finished_at"),
        "last_result": state.get("last_result"),
        "last_message": state.get("last_message"),
        "last_exit_code": state.get("last_exit_code"),
        "last_capacity_status": state.get("last_capacity_status"),
        "last_capacity_available_count": state.get("last_capacity_available_count"),
        "last_fault_domain": state.get("last_fault_domain"),
        "last_target_label": state.get("last_target_label"),
        "last_target_ocpus": state.get("last_target_ocpus"),
        "last_target_memory_in_gbs": state.get("last_target_memory_in_gbs"),
        "last_success_at": state.get("last_success_at"),
        "secured": bool(state.get("secured")),
        "secured_instance_id": state.get("secured_instance_id"),
        "secured_lifecycle": state.get("secured_lifecycle"),
        "primary_ocpus": _safe_float(config["launch"].get("ocpus"), 0),
        "primary_memory_in_gbs": _safe_float(config["launch"].get("memory_in_gbs"), 0),
        "fallback_enabled": bool(fallback_cfg.get("enabled")),
        "fallback_ocpus": _safe_float(fallback_cfg.get("ocpus", fallback_cfg.get("fallback_ocpus", 0)), 0),
        "fallback_memory_in_gbs": _safe_float(
            fallback_cfg.get("memory_in_gbs", fallback_cfg.get("fallback_memory_in_gbs", 0)),
            0,
        ),
        "interval_seconds": _safe_int(retry_cfg.get("interval_seconds", DEFAULT_INTERVAL_SECONDS), DEFAULT_INTERVAL_SECONDS),
        "jitter_seconds": _safe_int(retry_cfg.get("jitter_seconds", DEFAULT_JITTER_SECONDS), DEFAULT_JITTER_SECONDS),
        "log_tail": _log_tail(log_path),
    }


def _start_background_run(config_path: Path, log_path: Path) -> None:
    env = os.environ.copy()
    env.pop(CRON_MARKER, None)
    with log_path.open("a", encoding="utf-8") as fh:
        subprocess.Popen(
            [
                str(Path(sys.executable).resolve()),
                str(SCRIPT_PATH),
                "--config",
                str(config_path),
                "--once",
            ],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )


def _update_config_targets(
    config_path: Path,
    primary_ocpus: float,
    primary_memory: float,
    fallback_enabled: bool,
    fallback_ocpus: float,
    fallback_memory: float,
) -> Dict[str, Any]:
    config = _read_json_file(config_path)
    config["launch"]["ocpus"] = primary_ocpus
    config["launch"]["memory_in_gbs"] = primary_memory
    retry_cfg = config.setdefault("retry", {})
    fallback_cfg = retry_cfg.setdefault("fallback", {})
    fallback_cfg["enabled"] = bool(fallback_enabled)
    fallback_cfg["ocpus"] = fallback_ocpus
    fallback_cfg["memory_in_gbs"] = fallback_memory
    _write_json_file(config_path, config)
    return config


def _html_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Oracle Retry Tracker</title>
  <style>
    :root {
      --bg: #f5efe4;
      --card: rgba(255,255,255,0.88);
      --ink: #182027;
      --muted: #5d6a72;
      --line: #d7cfbf;
      --accent: #2f6fed;
      --good: #1f8b4c;
      --warn: #9b5f00;
      --bad: #b33030;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(47,111,237,0.12), transparent 28%),
        radial-gradient(circle at bottom right, rgba(155,95,0,0.10), transparent 30%),
        linear-gradient(180deg, #f8f4ea 0%, var(--bg) 100%);
      min-height: 100vh;
      padding: 24px;
    }
    .shell {
      max-width: 1040px;
      margin: 0 auto;
      display: grid;
      gap: 16px;
    }
    .hero, .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      backdrop-filter: blur(8px);
      box-shadow: 0 18px 40px rgba(24,32,39,0.08);
    }
    .hero { padding: 22px 24px; }
    .hero h1 {
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.05;
    }
    .hero p {
      margin: 0;
      color: var(--muted);
      max-width: 760px;
    }
    .grid {
      display: grid;
      gap: 16px;
      grid-template-columns: 1.15fr 0.85fr;
    }
    .card { padding: 18px; }
    .section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }
    h2 {
      margin: 0;
      font-size: 18px;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 13px;
      background: rgba(47,111,237,0.10);
      color: var(--accent);
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .stat {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.72);
    }
    .label {
      font-size: 12px;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 5px;
    }
    .value {
      font-size: 18px;
      font-weight: 600;
    }
    .muted {
      color: var(--muted);
      font-size: 13px;
    }
    .actions, .form-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }
    button {
      border: 0;
      border-radius: 12px;
      padding: 10px 14px;
      font: inherit;
      cursor: pointer;
      background: var(--accent);
      color: white;
      box-shadow: 0 8px 20px rgba(47,111,237,0.18);
    }
    button.secondary {
      background: rgba(24,32,39,0.08);
      color: var(--ink);
      box-shadow: none;
    }
    button.warn {
      background: var(--bad);
      box-shadow: 0 8px 20px rgba(179,48,48,0.18);
    }
    form {
      display: grid;
      gap: 14px;
    }
    .field-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 14px;
    }
    input[type="number"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 12px;
      font: inherit;
      background: rgba(255,255,255,0.85);
    }
    .toggle {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 14px;
    }
    pre {
      margin: 0;
      padding: 12px;
      background: #151c22;
      color: #e2edf5;
      border-radius: 14px;
      max-height: 320px;
      overflow: auto;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .message {
      min-height: 20px;
      font-size: 13px;
      color: var(--muted);
    }
    .message.good { color: var(--good); }
    .message.warn { color: var(--warn); }
    .message.bad { color: var(--bad); }
    @media (max-width: 860px) {
      .grid, .field-grid, .stats {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <h1>Oracle Capacity Retry</h1>
      <p>This stays local on your machine and only checks Oracle through the official OCI CLI. You can watch the 30-minute schedule, see attempt history, turn fallback on only when you want it, and stop the scheduler once you’re done.</p>
    </section>

    <div class="grid">
      <section class="card">
        <div class="section-title">
          <h2>Live Status</h2>
          <span class="status-pill" id="scheduler-pill">Loading…</span>
        </div>
        <div class="stats">
          <div class="stat"><div class="label">Attempts</div><div class="value" id="attempt-count">-</div></div>
          <div class="stat"><div class="label">Next Run</div><div class="value" id="next-run">-</div></div>
          <div class="stat"><div class="label">Last Result</div><div class="value" id="last-result">-</div></div>
          <div class="stat"><div class="label">Last Target</div><div class="value" id="last-target">-</div></div>
          <div class="stat"><div class="label">Capacity Status</div><div class="value" id="capacity-status">-</div></div>
          <div class="stat"><div class="label">Secured</div><div class="value" id="secured-status">-</div></div>
        </div>
        <div class="actions">
          <button id="start-btn">Start Auto Retry</button>
          <button class="warn" id="stop-btn">Stop Auto Retry</button>
          <button class="secondary" id="run-btn">Run Now</button>
          <button class="secondary" id="refresh-btn">Refresh</button>
        </div>
        <div class="message" id="action-message"></div>
        <div class="muted" id="detail-line" style="margin-top:12px;"></div>
      </section>

      <section class="card">
        <div class="section-title">
          <h2>Capacity Targets</h2>
        </div>
        <form id="target-form">
          <div class="field-grid">
            <label>
              Primary OCPU
              <input id="primary-ocpus" type="number" min="1" step="1" required>
            </label>
            <label>
              Primary Memory (GB)
              <input id="primary-memory" type="number" min="1" step="1" required>
            </label>
          </div>
          <label class="toggle">
            <input id="fallback-enabled" type="checkbox">
            Enable one fallback profile only if I choose it
          </label>
          <div class="field-grid">
            <label>
              Fallback OCPU
              <input id="fallback-ocpus" type="number" min="1" step="1">
            </label>
            <label>
              Fallback Memory (GB)
              <input id="fallback-memory" type="number" min="1" step="1">
            </label>
          </div>
          <div class="form-actions">
            <button type="submit">Save Targets</button>
          </div>
        </form>
        <div class="muted" id="config-line">30-minute schedule with conservative Oracle-safe pacing.</div>
      </section>
    </div>

    <section class="card">
      <div class="section-title">
        <h2>Recent Log</h2>
      </div>
      <pre id="log-box">Loading…</pre>
    </section>
  </div>

  <script>
    async function api(url, options = {}) {
      const response = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
      return response.json();
    }

    function setMessage(text, level = "") {
      const node = document.getElementById("action-message");
      node.textContent = text || "";
      node.className = level ? `message ${level}` : "message";
    }

    function render(status) {
      document.getElementById("scheduler-pill").textContent = status.scheduler_enabled ? "Scheduler running" : "Scheduler stopped";
      document.getElementById("attempt-count").textContent = status.attempt_count;
      document.getElementById("next-run").textContent = status.next_run_at || "Stopped";
      document.getElementById("last-result").textContent = status.last_result || "-";
      document.getElementById("last-target").textContent = `${status.last_target_label || "primary"} · ${status.last_target_ocpus || "-"} / ${status.last_target_memory_in_gbs || "-"} GB`;
      document.getElementById("capacity-status").textContent = status.last_capacity_status || "-";
      document.getElementById("secured-status").textContent = status.secured ? `Yes (${status.secured_lifecycle || "accepted"})` : "No";
      document.getElementById("detail-line").textContent = status.last_message || "No status message yet.";
      document.getElementById("primary-ocpus").value = status.primary_ocpus || 2;
      document.getElementById("primary-memory").value = status.primary_memory_in_gbs || 12;
      document.getElementById("fallback-enabled").checked = !!status.fallback_enabled;
      document.getElementById("fallback-ocpus").value = status.fallback_ocpus || 1;
      document.getElementById("fallback-memory").value = status.fallback_memory_in_gbs || 6;
      document.getElementById("config-line").textContent = `Scheduler: ${status.scheduler_backend}. Interval ${status.interval_seconds}s with up to ${status.jitter_seconds}s jitter.`;
      document.getElementById("log-box").textContent = (status.log_tail || []).join("\\n") || "No log lines yet.";
    }

    async function refresh() {
      try {
        const status = await api("/api/status");
        render(status);
      } catch (error) {
        setMessage(error.message || String(error), "bad");
      }
    }

    document.getElementById("refresh-btn").addEventListener("click", async () => {
      setMessage("Refreshing…");
      await refresh();
      setMessage("Status refreshed.", "good");
    });

    document.getElementById("start-btn").addEventListener("click", async () => {
      setMessage("Starting scheduler…");
      const data = await api("/api/scheduler/start", { method: "POST" });
      render(data.status);
      setMessage("Scheduler started.", "good");
    });

    document.getElementById("stop-btn").addEventListener("click", async () => {
      setMessage("Stopping scheduler…");
      const data = await api("/api/scheduler/stop", { method: "POST" });
      render(data.status);
      setMessage("Scheduler stopped.", "warn");
    });

    document.getElementById("run-btn").addEventListener("click", async () => {
      setMessage("Starting one immediate retry attempt…");
      const data = await api("/api/run-now", { method: "POST" });
      render(data.status);
      setMessage(data.message || "Manual run started.", "good");
    });

    document.getElementById("target-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      setMessage("Saving target sizes…");
      const payload = {
        primary_ocpus: Number(document.getElementById("primary-ocpus").value),
        primary_memory_in_gbs: Number(document.getElementById("primary-memory").value),
        fallback_enabled: document.getElementById("fallback-enabled").checked,
        fallback_ocpus: Number(document.getElementById("fallback-ocpus").value || 1),
        fallback_memory_in_gbs: Number(document.getElementById("fallback-memory").value || 6)
      };
      const data = await api("/api/config", {
        method: "POST",
        body: JSON.stringify(payload)
      });
      render(data.status);
      setMessage("Target sizes saved.", "good");
    });

    refresh();
    setInterval(refresh, 10000);
  </script>
</body>
</html>
"""


class _OracleRetryUIHandler(BaseHTTPRequestHandler):
    config_path: Path

    def _send_json(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str, status: int = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _load_config(self) -> Dict[str, Any]:
        config = _read_json_file(self.config_path)
        _require_fields(config)
        return config

    def do_GET(self) -> None:
        if self.path == "/":
            self._send_html(_html_page())
            return
        if self.path == "/api/status":
            config = self._load_config()
            self._send_json(_build_status_payload(self.config_path, config))
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/scheduler/start":
                config = self._load_config()
                log_path = _artifact_path(config, "log_file", DEFAULT_LOG_PATH)
                install_scheduler(self.config_path, log_path)
                self._send_json({"ok": True, "status": _build_status_payload(self.config_path, config)})
                return
            if self.path == "/api/scheduler/stop":
                remove_scheduler()
                config = self._load_config()
                self._send_json({"ok": True, "status": _build_status_payload(self.config_path, config)})
                return
            if self.path == "/api/run-now":
                config = self._load_config()
                log_path = _artifact_path(config, "log_file", DEFAULT_LOG_PATH)
                _start_background_run(self.config_path, log_path)
                self._send_json(
                    {
                        "ok": True,
                        "message": "Manual attempt launched in the background.",
                        "status": _build_status_payload(self.config_path, config),
                    }
                )
                return
            if self.path == "/api/config":
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                primary_ocpus = _safe_float(payload.get("primary_ocpus"), 0)
                primary_memory = _safe_float(payload.get("primary_memory_in_gbs"), 0)
                fallback_enabled = bool(payload.get("fallback_enabled"))
                fallback_ocpus = _safe_float(payload.get("fallback_ocpus"), 0)
                fallback_memory = _safe_float(payload.get("fallback_memory_in_gbs"), 0)
                if primary_ocpus <= 0 or primary_memory <= 0:
                    self._send_json({"error": "Primary OCPU and memory must be positive."}, status=HTTPStatus.BAD_REQUEST)
                    return
                if fallback_enabled and (fallback_ocpus <= 0 or fallback_memory <= 0):
                    self._send_json({"error": "Fallback values must be positive when fallback is enabled."}, status=HTTPStatus.BAD_REQUEST)
                    return
                config = _update_config_targets(
                    self.config_path,
                    primary_ocpus,
                    primary_memory,
                    fallback_enabled,
                    fallback_ocpus,
                    fallback_memory,
                )
                self._send_json({"ok": True, "status": _build_status_payload(self.config_path, config)})
                return
        except subprocess.CalledProcessError as exc:
            error_text = exc.stderr or exc.stdout or str(exc)
            self._send_json({"error": error_text.strip()}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except Exception as exc:  # pragma: no cover - local UI safety net
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve_ui(config_path: Path, port: int, *, keep_awake: bool = False) -> int:
    _OracleRetryUIHandler.config_path = config_path
    server = ThreadingHTTPServer(("127.0.0.1", port), _OracleRetryUIHandler)
    caffeinate_proc = _start_caffeinate_guard(os.getpid()) if keep_awake else None
    log(f"ui ready at http://127.0.0.1:{port}")
    if keep_awake:
        if caffeinate_proc is not None:
            log("keep-awake enabled with caffeinate while the UI process is running")
        else:
            log("keep-awake was requested, but caffeinate was not available")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("ui interrupted")
        return 130
    finally:
        server.server_close()
        if caffeinate_proc is not None and caffeinate_proc.poll() is None:
            try:
                caffeinate_proc.terminate()
            except OSError:
                pass
    return 0


def _mark_state_running(state: Dict[str, Any], attempt: int, run_mode: str) -> Dict[str, Any]:
    state["attempt_count"] = _safe_int(state.get("attempt_count"), 0) + 1
    state["last_started_at"] = _now()
    state["last_finished_at"] = None
    state["last_result"] = "running"
    state["last_message"] = f"Attempt {attempt} is running."
    state["last_exit_code"] = None
    state["last_run_mode"] = run_mode
    return state


def _finish_state(
    state_path: Path,
    config: Dict[str, Any],
    *,
    exit_code: int,
    result: str,
    message: str,
    profile: Optional[Dict[str, Any]] = None,
    capacity_status: Optional[str] = None,
    capacity_count: Optional[int] = None,
    fault_domain: Optional[str] = None,
    secured: bool = False,
    instance_id: Optional[str] = None,
    lifecycle: Optional[str] = None,
) -> None:
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        state["last_finished_at"] = _now()
        state["last_result"] = result
        state["last_message"] = message
        state["last_exit_code"] = exit_code
        if profile:
            state["last_target_label"] = profile["label"]
            state["last_target_ocpus"] = profile["ocpus"]
            state["last_target_memory_in_gbs"] = profile["memory_in_gbs"]
            state["fallback_enabled"] = bool(profile.get("fallback"))
        if capacity_status is not None:
            state["last_capacity_status"] = capacity_status
        if capacity_count is not None:
            state["last_capacity_available_count"] = capacity_count
        if fault_domain is not None:
            state["last_fault_domain"] = fault_domain
        if secured:
            state["secured"] = True
            state["secured_instance_id"] = instance_id
            state["secured_lifecycle"] = lifecycle
            state["last_success_at"] = _now()
        return state

    _update_state(state_path, config, mutate)


def run_single_attempt(
    config: Dict[str, Any],
    *,
    config_path: Path,
    dry_run: bool = False,
    attempt: int = 1,
    run_mode: str = "once",
) -> int:
    state_path = _artifact_path(config, "state_file", DEFAULT_STATE_PATH)
    lock_path = _artifact_path(config, "lock_file", DEFAULT_LOCK_PATH)
    requested_count = int((config.get("retry") or {}).get("requested_instance_count", 1) or 1)
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        _finish_state(
            state_path,
            config,
            exit_code=8,
            result="already_running",
            message="Another Oracle retry attempt is already in progress.",
        )
        log("another retry attempt is already running; skipping this trigger")
        return 8

    try:
        _update_state(state_path, config, lambda state: _mark_state_running(state, attempt, run_mode))

        existing, existing_err = find_existing_instance(config, dry_run=dry_run)
        if existing_err:
            category = classify_oci_error(existing_err)
            exit_code = 9 if category in SAFE_RETRY_CATEGORIES else 2
            message = f"Existing-instance check failed ({category}): {existing_err}"
            _finish_state(state_path, config, exit_code=exit_code, result=f"{category}_error", message=message)
            log(message)
            return exit_code

        if existing:
            instance_id = _get_first(existing, "id", default="unknown")
            lifecycle = _get_first(existing, "lifecycle-state", "lifecycleState", default="UNKNOWN")
            message = f"Instance already exists: {instance_id} ({lifecycle})"
            _finish_state(
                state_path,
                config,
                exit_code=0,
                result="already_exists",
                message=message,
                secured=True,
                instance_id=str(instance_id),
                lifecycle=str(lifecycle),
            )
            log(message)
            return 0

        profiles = _build_profiles(config)
        last_report: Optional[Dict[str, Any]] = None
        for profile in profiles:
            launch_cfg = _launch_profile_from_config(config, profile)
            ok, report_payload, report_err = create_capacity_report(config, launch_cfg, dry_run=dry_run)
            if not ok:
                category = classify_oci_error(report_err or "")
                exit_code = 4 if category in SAFE_RETRY_CATEGORIES else 2
                message = f"Capacity report failed for {profile['label']} ({category}): {report_err}"
                _finish_state(
                    state_path,
                    config,
                    exit_code=exit_code,
                    result=f"{category}_error",
                    message=message,
                    profile=profile,
                )
                log(message)
                return exit_code

            report = interpret_capacity_report(report_payload, requested_count=requested_count)
            status = str(report.get("status", "UNKNOWN"))
            available_count = int(report.get("available_count", 0) or 0)
            picked_fault_domain = report.get("fault_domain") or launch_cfg.get("fault_domain")
            last_report = {"status": status, "available_count": available_count, "fault_domain": picked_fault_domain}
            log(
                f"{profile['label']} capacity report status={status} "
                f"available_count={available_count} fault_domain={picked_fault_domain or 'auto'} "
                f"shape={launch_cfg['shape']} {profile['ocpus']}/{profile['memory_in_gbs']}GB"
            )

            if not bool(report.get("available")) and not dry_run:
                continue

            ok, launch_payload, launch_err = launch_instance(
                config,
                launch_cfg,
                fault_domain=str(picked_fault_domain) if picked_fault_domain else None,
                dry_run=dry_run,
            )
            if ok:
                data = _extract_data(launch_payload)
                instance_id = _get_first(data, "id", default="dry-run")
                lifecycle = _get_first(data, "lifecycle-state", "lifecycleState", default="ACCEPTED")
                message = (
                    f"Instance launch accepted on {profile['label']}: {instance_id} ({lifecycle})"
                    if not dry_run
                    else f"Dry-run launch wiring is valid for {profile['label']}."
                )
                _finish_state(
                    state_path,
                    config,
                    exit_code=0,
                    result="launch_accepted" if not dry_run else "dry_run_ok",
                    message=message,
                    profile=profile,
                    capacity_status=status,
                    capacity_count=available_count,
                    fault_domain=str(picked_fault_domain) if picked_fault_domain else None,
                    secured=not dry_run,
                    instance_id=str(instance_id),
                    lifecycle=str(lifecycle),
                )
                log(message)
                return 0

            category = classify_oci_error(launch_err)
            exit_code = 6 if category in SAFE_RETRY_CATEGORIES else 2
            message = f"Launch failed for {profile['label']} ({category}): {launch_err}"
            _finish_state(
                state_path,
                config,
                exit_code=exit_code,
                result=f"{category}_error",
                message=message,
                profile=profile,
                capacity_status=status,
                capacity_count=available_count,
                fault_domain=str(picked_fault_domain) if picked_fault_domain else None,
            )
            log(message)
            return exit_code

        capacity_status = (last_report or {}).get("status") or "UNAVAILABLE"
        capacity_count = _safe_int((last_report or {}).get("available_count"), 0)
        message = "No capacity available for the current Oracle target."
        if len(profiles) > 1:
            message += " Fallback was checked too and did not secure a slot."
        _finish_state(
            state_path,
            config,
            exit_code=5,
            result="capacity_unavailable",
            message=message,
            profile=profiles[-1],
            capacity_status=str(capacity_status),
            capacity_count=capacity_count,
            fault_domain=(last_report or {}).get("fault_domain"),
        )
        log(message)
        return 5
    finally:
        _release_lock(lock_path, lock_fd)


def _remove_scheduler_on_success(config_path: Path, config: Dict[str, Any]) -> None:
    if os.environ.get(CRON_MARKER) != "1":
        return
    backend = _scheduler_backend()
    if backend == "launchd":
        if not _launchd_enabled():
            return
    elif not _cron_enabled(config_path):
        return
    try:
        remove_scheduler()
        state_path = _artifact_path(config, "state_file", DEFAULT_STATE_PATH)
        _finish_state(
            state_path,
            config,
            exit_code=0,
            result="scheduler_stopped_after_success",
            message="Capacity secured, so the scheduler removed itself automatically.",
        )
        log(f"success detected from scheduled run; removed {backend} schedule")
    except Exception as exc:
        log(f"warning: instance was secured, but removing the {backend} schedule failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Conservative OCI CLI retry launcher for Always Free A1 capacity hunting."
    )
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--once", action="store_true", help="Run one check/attempt only")
    parser.add_argument("--dry-run", action="store_true", help="Print OCI commands without executing them")
    parser.add_argument("--max-attempts", type=int, default=None, help="Override max attempts from config")
    parser.add_argument("--interval-seconds", type=int, default=None, help="Override retry interval")
    parser.add_argument("--jitter-seconds", type=int, default=None, help="Override retry jitter")
    parser.add_argument("--ui", action="store_true", help="Start the minimal localhost status UI")
    parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT, help="Local port for the status UI")
    parser.add_argument("--keep-awake", action="store_true", help="When used with --ui on macOS, prevent idle sleep while the UI process is running")
    parser.add_argument("--install-schedule", action="store_true", help="Install the 30-minute retry schedule")
    parser.add_argument("--remove-schedule", action="store_true", help="Remove the retry schedule")
    parser.add_argument("--status-json", action="store_true", help="Print current UI/scheduler status as JSON")
    args = parser.parse_args()

    config_path = _expand_path(args.config)
    if config_path is None or not config_path.exists():
        raise SystemExit(f"Config file not found: {args.config}")
    config = _read_json_file(config_path)
    _require_fields(config)

    log_path = _artifact_path(config, "log_file", DEFAULT_LOG_PATH)

    if args.install_schedule:
        install_scheduler(config_path, log_path)
        log(f"installed 30-minute {_scheduler_backend()} schedule")
        return 0

    if args.remove_schedule:
        remove_scheduler()
        log(f"removed {_scheduler_backend()} schedule")
        return 0

    if args.status_json:
        print(json.dumps(_build_status_payload(config_path, config), indent=2))
        return 0

    if args.ui:
        return serve_ui(config_path, args.ui_port, keep_awake=args.keep_awake)

    retry_cfg = config.setdefault("retry", {})
    interval_seconds = int(
        args.interval_seconds if args.interval_seconds is not None else retry_cfg.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    )
    jitter_seconds = int(
        args.jitter_seconds if args.jitter_seconds is not None else retry_cfg.get("jitter_seconds", DEFAULT_JITTER_SECONDS)
    )
    max_attempts = int(args.max_attempts if args.max_attempts is not None else retry_cfg.get("max_attempts", 0) or 0)

    if interval_seconds < 900:
        raise SystemExit("Refusing to run with interval_seconds < 900. Keep retries conservative.")

    log("safe mode enabled: official OCI CLI only, one launch path per run, long backoff, no browser automation")
    if args.dry_run:
        log("dry-run mode enabled")

    attempt = 0
    while True:
        attempt += 1
        exit_code = run_single_attempt(
            config,
            config_path=config_path,
            dry_run=args.dry_run,
            attempt=attempt,
            run_mode="once" if args.once else "loop",
        )
        if exit_code == 0:
            _remove_scheduler_on_success(config_path, config)
            return 0

        if args.once:
            return exit_code

        if _hit_max_attempts(attempt, max_attempts):
            log(f"stopping after max_attempts={max_attempts}")
            return 7

        if exit_code not in RETRYABLE_EXIT_CODES:
            return exit_code

        sleep_seconds = _sleep_duration(interval_seconds, jitter_seconds)
        _log_retry_pause("retryable outcome", sleep_seconds, will_retry=True)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("interrupted")
        raise SystemExit(130)

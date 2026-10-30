#!/usr/bin/env python3
"""Run the complete X5 dashboard refresh from a single machine.

This mirrors .github/workflows/refresh-x5-dashboard.yml so the pipeline can run
on an always-on host with a stable egress IP (and a browser that can pass the
CRM login challenge) instead of a GitHub runner.

Steps, in order:
  1. sync_x5_daily_gross.py      --mode current --apply  (writes Lark)
  2. sync_x5_channel_monthly.py  --apply                 (writes Lark)
  3. export_dashboard_lark_data.py                       (reads Lark)
  4. publish_x5_dashboard_data.py                        (writes outputs/)
  5. git commit + push of the two published files

Usage:
    python scripts/run_refresh_all.py
    python scripts/run_refresh_all.py --attempts 3 --env-file C:\\x5\\refresh.env
    python scripts/run_refresh_all.py --no-push
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = "outputs/lark-dashboard-data.js"
HTML_FILE = "outputs/团队业绩汇总.html"
COMMIT_MESSAGE = "Refresh X5 dashboard snapshot"

# One refresh at a time. The lock lives in work/ (gitignored) and carries the
# holder's pid/phase so a second window can see who is running instead of
# racing it into Lark and git.
LOCK_FILE = ROOT / "work" / ".refresh.lock"
STATUS_FILE = Path(os.environ.get("X5_REFRESH_STATUS") or (ROOT.parent / "REFRESH-STATUS.md"))

STATUS_HEADER = (
    "# X5 刷新状态（公共审查文件）\n\n"
    "> 由 `scripts/run_refresh_all.py` 自动维护，请不要手改。\n"
    "> 两个窗口开工前先读“当前”：状态是 running 就不要再起一轮。\n\n"
)
HISTORY_HEADER = (
    "## 运行记录\n\n"
    "| 开始 | 结束 | 结果 | 提交 | 数据时间 | 说明 |\n"
    "| --- | --- | --- | --- | --- | --- |\n"
)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _pid_alive(pid):
    """True when `pid` is a live process (Windows needs the ctypes dance)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _read_lock():
    """Return the lock payload, or None when the file exists but is unreadable."""
    try:
        return json.loads(LOCK_FILE.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    except ValueError:
        return None


def acquire_lock(log_path=None):
    """Create the lock atomically; refuse when a live process holds it."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            handle = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_lock()
            if holder is None:
                # A half-written lock must not be mistaken for a dead one: only
                # take it over once it is old enough that no writer can be live.
                try:
                    age = time.time() - LOCK_FILE.stat().st_mtime
                except OSError:
                    age = 0
                if age < 120:
                    raise SystemExit(
                        f"a refresh lock exists at {LOCK_FILE} but is unreadable; "
                        f"refusing to start while it may be being written ({age:.0f}s old).")
                holder = {}
            pid = holder.get("pid")
            if pid and _pid_alive(pid):
                raise SystemExit(
                    "another refresh is already running: "
                    f"pid={pid} phase={holder.get('phase', '?')} "
                    f"started={holder.get('started', '?')} log={holder.get('log', '?')}\n"
                    "wait for it to finish; only remove work\\.refresh.lock after "
                    "confirming that pid is gone."
                )
            # Stale lock from a killed run: take it over.
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({
                "pid": os.getpid(),
                "started": _now(),
                "phase": "starting",
                "log": Path(log_path).name if log_path else "",
                "host": os.environ.get("COMPUTERNAME", ""),
            }, stream, ensure_ascii=False)
        return
    raise SystemExit(f"could not acquire {LOCK_FILE}; another refresh keeps winning the race")


def update_lock(phase):
    """Record the current phase; only the holder may write."""
    holder = _read_lock() or {}
    if holder.get("pid") != os.getpid():
        return
    holder["phase"] = phase
    holder["updated"] = _now()
    try:
        LOCK_FILE.write_text(json.dumps(holder, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def release_lock():
    holder = _read_lock() or {}
    if holder.get("pid") == os.getpid():
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def _status_history():
    if not STATUS_FILE.exists():
        return []
    text = STATUS_FILE.read_text(encoding="utf-8")
    if "## 运行记录" not in text:
        return []
    rows = []
    for line in text.split("## 运行记录", 1)[1].splitlines():
        line = line.rstrip()
        if line.startswith("| ") and not line.startswith("| ---") and not line.startswith("| 开始"):
            rows.append(line)
    return rows


def write_status(state, pid=None, phase="", log=None, record=None):
    """Rewrite the shared status file: current holder on top, history below."""
    rows = _status_history()
    if record:
        rows.append(record)
    current = [f"- 状态：**{state}**"]
    if pid:
        current.append(f"- 进程：pid {pid}")
    if phase:
        current.append(f"- 阶段：{phase}")
    if log:
        current.append(f"- 日志：{log}")
    current.append(f"- 更新时间：{_now()}")
    body = (STATUS_HEADER + "## 当前\n\n" + "\n".join(current) + "\n\n"
            + HISTORY_HEADER + "\n".join(rows) + ("\n" if rows else ""))
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATUS_FILE.with_suffix(".tmp")
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, STATUS_FILE)
    except OSError as exc:
        print(f"--- could not write {STATUS_FILE}: {exc}", flush=True)


def published_revision():
    """Return (short head, generatedAt) from the freshly written snapshot."""
    try:
        generated = re.search(r'"generatedAt":"([^"]+)"',
                              (ROOT / DATA_FILE).read_text(encoding="utf-8")).group(1)
    except (OSError, AttributeError):
        generated = "?"
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                          text=True, capture_output=True)
    return (head.stdout.strip() or "?"), generated


def load_env_file(path):
    """Read KEY=VALUE lines; ignore blanks and # comments."""
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            raise SystemExit(f"Invalid line in env file (expected KEY=VALUE): {raw!r}")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def child_env(extra):
    env = {**os.environ, **extra}
    # Keep child output UTF-8 even on a Windows console using a legacy code page.
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def run_step(name, args, env, log_file, phase_hook=None):
    if phase_hook:
        phase_hook(name)
    log_file.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} {name} ===\n")
    log_file.flush()
    print(f"--- {name}", flush=True)
    proc = subprocess.Popen(
        args,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = []
    for line in proc.stdout:
        log_file.write(line)
        tail.append(line)
        del tail[:-40]
        sys.stdout.write(line)
    proc.wait()
    log_file.flush()
    if proc.returncode != 0:
        print(f"--- {name} FAILED (exit {proc.returncode})", flush=True)
        raise subprocess.CalledProcessError(proc.returncode, args, output="".join(tail))


def git(args, env, check=True):
    return subprocess.run(["git", *args], cwd=str(ROOT), env=env, text=True,
                          capture_output=True, check=check)


def publish(args, env, log_file):
    """Commit and push the two generated files; no-op when nothing changed."""
    if args.no_push:
        print("--- publish skipped (--no-push)", flush=True)
        return False
    changed = git(["diff", "--quiet", "--", DATA_FILE, HTML_FILE], env, check=False)
    if changed.returncode == 0:
        print("--- nothing to publish: data files unchanged", flush=True)
        return False
    git(["add", "--", DATA_FILE, HTML_FILE], env)
    git(["commit", "-m", COMMIT_MESSAGE], env)
    pushed = git(["push", "origin", "HEAD:main"], env, check=False)
    if pushed.returncode == 0:
        print("--- published and pushed", flush=True)
        return True
    # Another window (or a manual push) moved main while this run was working.
    # Replay our snapshot commit onto the new base once instead of leaving the
    # checkout diverged, which would make every later run fail the same way.
    print("--- push rejected; rebasing the snapshot onto origin/main", flush=True)
    git(["fetch", "origin"], env)
    rebased = git(["rebase", "origin/main"], env, check=False)
    if rebased.returncode != 0:
        git(["rebase", "--abort"], env, check=False)
        raise RuntimeError(
            "push rejected and the snapshot conflicts with origin/main; "
            "re-run the refresh to regenerate it on top of the latest main")
    pushed = git(["push", "origin", "HEAD:main"], env, check=False)
    if pushed.returncode != 0:
        raise RuntimeError("push rejected twice; another window is publishing at the same time")
    print("--- published and pushed (after rebase)", flush=True)
    return True


def refresh(args, env, log_file, workdir, phase_hook=None):
    x5_export = workdir / "x5-dashboard-export.json"
    dashboard_export = workdir / "dashboard-lark-export.json"
    python = sys.executable
    run_step("sync gross to Lark",
             [python, "scripts/sync_x5_daily_gross.py", "--mode", "current", "--apply",
              "--dashboard-export", str(x5_export)], env, log_file, phase_hook)
    run_step("sync channel to Lark",
             [python, "scripts/sync_x5_channel_monthly.py", "--apply"], env, log_file, phase_hook)
    run_step("export Lark data",
             [python, "scripts/export_dashboard_lark_data.py", "--x5-export", str(x5_export),
              "--output", str(dashboard_export)], env, log_file, phase_hook)
    run_step("publish snapshot",
             [python, "scripts/publish_x5_dashboard_data.py", "--export", str(x5_export),
              "--dashboard-export", str(dashboard_export)], env, log_file, phase_hook)
    return publish(args, env, log_file)


def main():
    parser = argparse.ArgumentParser(description="Run the full X5 dashboard refresh locally.")
    parser.add_argument("--attempts", type=int, default=3,
                        help="whole-pipeline attempts before giving up (default 3)")
    parser.add_argument("--retry-delay", type=int, default=60,
                        help="seconds between attempts (default 60)")
    parser.add_argument("--env-file", help="optional KEY=VALUE file with CRM_/CX_/LARK_ credentials")
    parser.add_argument("--no-push", action="store_true", help="run everything but skip commit/push")
    parser.add_argument("--log-dir", default=str(ROOT / "logs"), help="where to write the run log")
    args = parser.parse_args()

    extra = load_env_file(args.env_file) if args.env_file else {}
    for key in ("CRM_USER", "CRM_PASS", "CRM_TOTP", "CX_USER", "CX_PASS",
                "LARK_APP_ID", "LARK_APP_SECRET", "LARK_APP_TOKEN"):
        if not (extra.get(key) or os.environ.get(key)):
            raise SystemExit(f"missing credential: set {key} in the environment or --env-file")

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"refresh-{stamp}.log"

    env = child_env(extra)
    started_at = _now()
    acquire_lock(log_path)
    status_log = f"logs/{log_path.name}"

    def phase_hook(phase):
        update_lock(phase)
        write_status("running", pid=os.getpid(), phase=phase, log=status_log)

    try:
        phase_hook("git pull --ff-only")
        # Pick up commits published elsewhere (e.g. a manual test on another host)
        # so the later push is a fast-forward. Never fatal: a dirty tree just skips it.
        pulled = git(["pull", "--ff-only"], env, check=False)
        if pulled.returncode != 0:
            print("--- git pull --ff-only skipped:", (pulled.stderr or "").strip()[:160], flush=True)

        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"refresh started {datetime.now().isoformat(timespec='seconds')} "
                           f"attempts={args.attempts} push={not args.no_push}\n")
            for attempt in range(1, max(1, args.attempts) + 1):
                workdir = Path(tempfile.mkdtemp(prefix="x5-refresh-"))
                print(f"=== attempt {attempt}/{args.attempts} (log: {log_path})", flush=True)
                log_file.write(f"\n=== attempt {attempt}/{args.attempts} ===\n")
                log_file.flush()
                try:
                    refreshed = refresh(args, env, log_file, workdir, phase_hook)
                except subprocess.CalledProcessError as exc:
                    print(f"=== attempt {attempt} failed: {exc}\n", flush=True)
                    log_file.write(f"=== attempt {attempt} failed ===\n")
                    log_file.flush()
                    if attempt < args.attempts:
                        time.sleep(args.retry_delay)
                    continue
                print(f"=== attempt {attempt} succeeded (published={refreshed})", flush=True)
                log_file.write(f"=== attempt {attempt} succeeded (published={refreshed}) ===\n")
                commit, generated = published_revision()
                note = f"attempts={attempt}/{args.attempts}"
                if not refreshed:
                    note += "，无变更未推送"
                write_status("idle", log=status_log, record=(
                    f"| {started_at} | {_now()} | 成功 | {commit} | {generated} | {note} |"))
                return 0
            print("=== all attempts failed ===", flush=True)
            log_file.write("=== all attempts failed ===\n")
            write_status("idle", log=status_log, record=(
                f"| {started_at} | {_now()} | 失败 | - | - | attempts={args.attempts} |"))
            return 1
    except BaseException:
        commit, generated = published_revision()
        write_status("idle", log=status_log, record=(
            f"| {started_at} | {_now()} | 异常中断 | {commit} | {generated} | 见 {status_log} |"))
        raise
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())

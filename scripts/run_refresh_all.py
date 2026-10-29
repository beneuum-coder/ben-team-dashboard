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
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = "outputs/lark-dashboard-data.js"
HTML_FILE = "outputs/团队扩编与筛选体系框架-可视化编辑版.html"
COMMIT_MESSAGE = "Refresh X5 dashboard snapshot"


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


def run_step(name, args, env, log_file):
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
    git(["push", "origin", "HEAD:main"], env)
    print("--- published and pushed", flush=True)
    return True


def refresh(args, env, log_file, workdir):
    x5_export = workdir / "x5-dashboard-export.json"
    dashboard_export = workdir / "dashboard-lark-export.json"
    python = sys.executable
    run_step("sync gross to Lark",
             [python, "scripts/sync_x5_daily_gross.py", "--mode", "current", "--apply",
              "--dashboard-export", str(x5_export)], env, log_file)
    run_step("sync channel to Lark",
             [python, "scripts/sync_x5_channel_monthly.py", "--apply"], env, log_file)
    run_step("export Lark data",
             [python, "scripts/export_dashboard_lark_data.py", "--x5-export", str(x5_export),
              "--output", str(dashboard_export)], env, log_file)
    run_step("publish snapshot",
             [python, "scripts/publish_x5_dashboard_data.py", "--export", str(x5_export),
              "--dashboard-export", str(dashboard_export)], env, log_file)
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
                refreshed = refresh(args, env, log_file, workdir)
            except subprocess.CalledProcessError as exc:
                print(f"=== attempt {attempt} failed: {exc}\n", flush=True)
                log_file.write(f"=== attempt {attempt} failed ===\n")
                log_file.flush()
                if attempt < args.attempts:
                    time.sleep(args.retry_delay)
                continue
            print(f"=== attempt {attempt} succeeded (published={refreshed})", flush=True)
            log_file.write(f"=== attempt {attempt} succeeded (published={refreshed}) ===\n")
            return 0
        print("=== all attempts failed ===", flush=True)
        log_file.write("=== all attempts failed ===\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

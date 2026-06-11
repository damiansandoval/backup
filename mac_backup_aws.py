#!/usr/bin/env python3
"""
mac_backup.py — macOS User Backup Tool
Backs up a user's home folder to an encrypted DMG and uploads it to AWS S3.
Designed to run on macOS by Service Desk technicians via MDM deployment.
"""

from __future__ import annotations

import os
import sys
import math
import time
import shutil
import hashlib
import getpass
import logging
import platform
import subprocess
import threading
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Dependency check — must happen before any third-party import
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES = {"rich": "rich", "boto3": "boto3"}
HOMEBREW_INSTALL_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"


def _brew_path() -> Optional[str]:
    for candidate in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("brew")


def _ensure_homebrew() -> bool:
    if _brew_path():
        return True
    print("\n  Homebrew not found — installing...")
    result = subprocess.run(
        ["bash", "-c", f'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL {HOMEBREW_INSTALL_URL})"'],
        capture_output=False,
    )
    return result.returncode == 0


def _install_packages(missing: list) -> bool:
    if not shutil.which("pip3"):
        brew = _brew_path()
        if brew:
            subprocess.run([brew, "install", "python3"], capture_output=False)
    # --break-system-packages is only available on Python 3.11+
    cmd = [sys.executable, "-m", "pip", "install", "-q"] + missing
    if sys.version_info >= (3, 11):
        cmd.insert(4, "--break-system-packages")
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0


def bootstrap_dependencies():
    """Check for missing packages and offer a single yes/no install prompt."""
    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    names = " and ".join(f'"{p}"' for p in missing)
    print(f"\n  Missing dependencies: {names}")
    answer = input("  Install now? [Y/n] ").strip().lower()

    if answer in ("", "y", "yes"):
        if not _ensure_homebrew():
            print("\n  [ERROR] Homebrew installation failed. Install manually:")
            print(f"  pip3 install {' '.join(missing)}")
            sys.exit(1)

        print(f"  Installing {', '.join(missing)} ...")
        if not _install_packages(missing):
            print("\n  [ERROR] Package installation failed. Install manually:")
            print(f"  pip3 install {' '.join(missing)}")
            sys.exit(1)

        import importlib
        for pkg in missing:
            try:
                importlib.import_module(pkg)
            except ImportError:
                print(f"\n  [ERROR] Could not import '{pkg}' after install. Try rerunning.")
                sys.exit(1)

        print("  Dependencies installed.\n")
    else:
        print("\n  Cannot continue without dependencies. Exiting.")
        sys.exit(0)


bootstrap_dependencies()

# ---------------------------------------------------------------------------
# Third-party imports (safe after dep check)
# ---------------------------------------------------------------------------

import boto3
import botocore.exceptions
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import (
    Progress, SpinnerColumn, BarColumn,
    TextColumn, TimeElapsedColumn, FileSizeColumn,
    TransferSpeedColumn, TaskID,
)
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich import box

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOL_VERSION          = "1.1.0"
REQUIRED_BINARIES     = ["hdiutil", "diskutil", "du", "df", "rsync"]
MULTIPART_THRESHOLD_MB = 100      # Use multipart above this size
CHUNK_SIZE_MB          = 50       # Each chunk for multipart upload
VOLUME_OVERHEAD_FACTOR = 1.20     # 20 % extra headroom for staging volume
LOG_DIR                = Path("/var/log/mac_backup")

# ---------------------------------------------------------------------------
# AWS S3 configuration — set these before deploying
# ---------------------------------------------------------------------------
AWS_REGION  = "eu-west-1"
AWS_BUCKET  = "dlocal-eu1-security-live-notebook-backups"
AWS_PREFIX  = "macOS"   # base folder inside the bucket

# ---------------------------------------------------------------------------
# Theme — light-terminal compatible palette
# ---------------------------------------------------------------------------

THEME = {
    "primary":     "#0F6E56",
    "primary_mid": "#1D9E75",
    "success":     "#3B6D11",
    "warning":     "#BA7517",
    "warning_bdr": "#FAC775",
    "error":       "#993C1D",
    "rule":        "#d0cfc8",
}

console = Console(highlight=False)

# Data volume path — needed by multiple functions
DATA_VOLUME_PATH = Path("/System/Volumes/Data")


# ---------------------------------------------------------------------------
# Logging — file only, no Rich output on screen
# ---------------------------------------------------------------------------

def setup_logging(log_path: Path) -> tuple:
    log_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = log_path / f"backup_{timestamp}.log"

    # Get the named logger and configure it directly
    # (avoids basicConfig being ignored if root logger was already initialized)
    log = logging.getLogger("mac_backup")
    log.setLevel(logging.DEBUG)
    log.propagate = False

    # Remove any existing handlers (e.g. from a previous run in same process)
    log.handlers.clear()

    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(handler)
    log.info(f"Log file: {log_file}")
    return log, log_file


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def print_header():
    console.print()
    console.print(Panel.fit(
        Text.from_markup(
            f"[bold]macOS Backup Tool[/]  [dim]v{TOOL_VERSION}[/]\n"
            "[dim]Service Desk — Offboarding Workflow[/]"
        ),
        border_style=THEME["primary"],
        padding=(0, 4),
    ))
    console.print()


def print_step(number: int, title: str):
    console.rule(f"[bold]{title}[/]", style=THEME["rule"], characters="─")
    console.print()


def print_success(msg: str):
    console.print(f"[bold #3B6D11]✓[/]  {msg}")


def print_warning(msg: str):
    console.print(f"[bold #BA7517]⚠[/]  {msg}")


def print_error(msg: str):
    console.print(f"[bold #993C1D]✗[/]  {msg}")


def bytes_to_human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd: list, capture: bool = True, log: logging.Logger = None) -> subprocess.CompletedProcess:
    if log:
        log.debug(f"CMD: {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if log and result.returncode != 0:
        log.debug(f"STDERR: {result.stderr.strip()}")
    return result


def get_serial(log: logging.Logger = None) -> str:
    """Return the hardware serial number via ioreg."""
    result = run_cmd(["ioreg", "-l", "-d", "2", "-c", "IOPlatformExpertDevice"], log=log)
    for line in result.stdout.splitlines():
        if "IOPlatformSerialNumber" in line:
            serial = line.split("=")[-1].strip().strip('"')
            if serial:
                if log:
                    log.info(f"Serial: {serial}")
                return serial
    if log:
        log.warning("Could not read serial number, using UNKNOWN")
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# User email prompt
# ---------------------------------------------------------------------------

def prompt_user_email(log: logging.Logger) -> str:
    """Ask for the email of the user being backed up. Used as part of the S3 folder name."""
    print_step(0, "User Information")
    console.print("[dim]Enter the email address of the user being offboarded.[/]\n")

    while True:
        email = Prompt.ask("[#0F6E56]User email[/]").strip().lower()
        if not email:
            print_warning("Email cannot be empty.")
            continue
        if "@" not in email or "." not in email.split("@")[-1]:
            print_warning("Enter a valid email address.")
            continue
        log.info(f"User email: {email}")
        print_success(f"Email: {email}")
        console.print()
        return email


# ---------------------------------------------------------------------------
# Backup mode selection
# ---------------------------------------------------------------------------

BACKUP_MODE_USER   = "user"
BACKUP_MODE_VOLUME = "volume"


def select_backup_mode(log: logging.Logger) -> str:
    print_step(1, "Select Backup Mode")
    console.print("[dim]Choose what to back up:[/]\n")
    console.print("  [#0F6E56]1[/]  User backup    — home folder of a specific user")
    console.print("  [#0F6E56]2[/]  Volume backup  — full Macintosh HD - Data volume")
    console.print()

    while True:
        choice = Prompt.ask("[#0F6E56]Mode[/]", choices=["1", "2"]).strip()
        if choice == "1":
            log.info("Mode: user backup")
            print_success("Mode: User backup")
            return BACKUP_MODE_USER
        if choice == "2":
            log.info("Mode: full volume backup")
            print_success("Mode: Full volume backup (Macintosh HD - Data)")
            return BACKUP_MODE_VOLUME


# ---------------------------------------------------------------------------
# Time Machine snapshot detection and cleanup
# ---------------------------------------------------------------------------

def _get_data_volume_disk(log: logging.Logger) -> str:
    """Return the disk identifier for /System/Volumes/Data (e.g. disk3s5)."""
    result = run_cmd(["df", str(DATA_VOLUME_PATH)], log=log)
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            # /dev/disk3s5 → disk3s5
            return parts[0].replace("/dev/", "")
    return "disk1s1"   # safe fallback


def detect_time_machine(log: logging.Logger) -> dict:
    result: dict = {"snapshots": [], "local_backups": []}

    # Try diskutil apfs listSnapshots for sizes (more reliable than tmutil)
    disk_id     = _get_data_volume_disk(log)
    disk_result = run_cmd(["diskutil", "apfs", "listSnapshots", disk_id], log=log)
    if disk_result.returncode == 0:
        current_snap, current_size = None, "unknown"
        for line in disk_result.stdout.splitlines():
            line = line.strip()
            if "Name:" in line:
                if current_snap:
                    result["snapshots"].append((current_snap, current_size))
                current_snap  = line.split("Name:")[-1].strip()
                current_size  = "unknown"
            if "Size:" in line:
                current_size = line.split("Size:")[-1].strip()
        if current_snap:
            result["snapshots"].append((current_snap, current_size))
    else:
        # Fallback: tmutil listlocalsnapshots
        snap_result = run_cmd(["tmutil", "listlocalsnapshots", "/"], log=log)
        if snap_result.returncode == 0:
            for line in snap_result.stdout.strip().splitlines():
                line = line.strip()
                if line:
                    result["snapshots"].append((line, "unknown"))

    # Local backup destinations — parse each destination block independently
    dest_result = run_cmd(["tmutil", "destinationinfo"], log=log)
    if dest_result.returncode == 0 and "Kind" in dest_result.stdout:
        # Split output into per-destination blocks separated by blank lines
        blocks = dest_result.stdout.strip().split("\n\n")
        for block in blocks:
            b_name, b_path = None, None
            for line in block.splitlines():
                if "Name" in line and ":" in line:
                    b_name = line.split(":", 1)[-1].strip()
                if "Mount Point" in line and ":" in line:
                    b_path = line.split(":", 1)[-1].strip()
            if b_name and b_path:
                dest_path = Path(b_path)
                if dest_path.exists():
                    size_r = run_cmd(["du", "-sh", str(dest_path)], log=log)
                    size   = size_r.stdout.split()[0] if size_r.returncode == 0 else "unknown"
                    result["local_backups"].append((b_name, b_path, size))

    log.info(f"Time Machine: {len(result['snapshots'])} snapshots, "
             f"{len(result['local_backups'])} local destinations")
    return result


def handle_time_machine(log: logging.Logger) -> bool:
    print_step(2, "Time Machine Check")

    with console.status("[#0F6E56]Scanning for Time Machine data…[/]", spinner="dots", spinner_style="#1D9E75"):
        tm = detect_time_machine(log)

    snapshots     = tm["snapshots"]
    local_backups = tm["local_backups"]

    if not snapshots and not local_backups:
        print_success("No Time Machine snapshots or local backups found.")
        console.print()
        return True

    if snapshots:
        console.print(f"[yellow]Found {len(snapshots)} local APFS snapshot(s):[/]\n")
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        table.add_column("#",        style="dim", width=4)
        table.add_column("Snapshot", style="#0F6E56")
        table.add_column("Size",     style="yellow")
        for i, (snap, size) in enumerate(snapshots, 1):
            table.add_row(str(i), snap, size)
        console.print(table)
        console.print()

        if Confirm.ask("[#0F6E56]Delete all local snapshots to free space?[/]", default=False):
            deleted = 0
            for snap, _ in snapshots:
                r = run_cmd(["tmutil", "deletelocalsnapshots", snap], log=log)
                if r.returncode == 0:
                    deleted += 1
                    log.info(f"Deleted TM snapshot: {snap}")
                else:
                    print_warning(f"Could not delete: {snap}")
                    log.warning(f"TM snapshot delete failed: {snap} — {r.stderr.strip()}")
            print_success(f"Deleted {deleted}/{len(snapshots)} snapshot(s).")
            console.print()

    if local_backups:
        console.print("[yellow]Local Time Machine backup destinations:[/]\n")
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        table.add_column("Name",        style="#0F6E56")
        table.add_column("Mount Point", style="dim")
        table.add_column("Size",        style="yellow")
        for name, path, size in local_backups:
            table.add_row(name, path, size)
        console.print(table)
        console.print(
            "[dim]Local backup destinations are not deleted automatically.\n"
            "Disconnect the drive before backup if you want to reclaim that space.[/]\n"
        )

    return True


# ---------------------------------------------------------------------------
# Dependency validation
# ---------------------------------------------------------------------------

def validate_dependencies(log: logging.Logger) -> bool:
    print_step(1, "Dependency Validation")

    if platform.system() != "Darwin":
        print_error("This script must run on macOS.")
        log.error("Non-macOS platform detected.")
        return False

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Tool",   style="#0F6E56")
    table.add_column("Status")

    all_ok = True
    for binary in REQUIRED_BINARIES:
        path = shutil.which(binary)
        if path:
            table.add_row(binary, f"[green]✓  {path}[/]")
            log.info(f"Binary OK: {binary} → {path}")
        else:
            table.add_row(binary, "[red]✗  Not found[/]")
            log.error(f"Missing binary: {binary}")
            all_ok = False

    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg)
            table.add_row(f"python:{pkg}", "[green]✓  installed[/]")
        except ImportError:
            table.add_row(f"python:{pkg}", "[red]✗  missing[/]")
            all_ok = False

    console.print(table)

    if all_ok:
        print_success("All dependencies satisfied.")
    else:
        print_error("Missing dependencies. Cannot continue.")

    return all_ok


# ---------------------------------------------------------------------------
# User selection
# ---------------------------------------------------------------------------

def get_local_users() -> list:
    users = []
    try:
        result = run_cmd(["dscl", ".", "-list", "/Users", "UniqueID"])
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                name, uid = parts
                if int(uid) >= 500 and not name.startswith("_"):
                    home = Path(f"/Users/{name}")
                    if home.exists():
                        users.append(name)
    except Exception:
        pass
    return sorted(users)


def select_user(log: logging.Logger) -> Optional[str]:
    print_step(2, "Select User to Back Up")
    users = get_local_users()

    if not users:
        print_warning("No local users detected automatically.")
        username = Prompt.ask("[#0F6E56]Enter username manually[/]").strip()
        if not username:
            return None
        home = Path(f"/Users/{username}")
        if not home.exists():
            print_error(f"Home directory not found: {home}")
            log.error(f"User home not found: {home}")
            return None
        log.info(f"Manual user selected: {username}")
        return username

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("#",        style="dim", width=4)
    table.add_column("Username", style="#0F6E56")
    table.add_column("Home",     style="dim")
    for i, u in enumerate(users, 1):
        table.add_row(str(i), u, f"/Users/{u}")
    console.print(table)

    while True:
        choice = Prompt.ask(
            f"[#0F6E56]Select user[/] [dim](1-{len(users)} or type username)[/]"
        ).strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(users):
                selected = users[idx]
                break
            print_warning(f"Enter a number between 1 and {len(users)}.")
        elif choice in users:
            selected = choice
            break
        elif choice:
            home = Path(f"/Users/{choice}")
            if home.exists():
                selected = choice
                break
            print_warning(f"User '{choice}' not found in /Users. Try again.")
        else:
            print_warning("Please enter a selection.")

    log.info(f"User selected: {selected}")
    print_success(f"User selected: [bold]{selected}[/]")
    return selected


# ---------------------------------------------------------------------------
# Exclude lists — single source of truth for size calc + rsync
# ---------------------------------------------------------------------------

# Always excluded — Library has TCC-protected subfolders that break rsync.
RSYNC_EXCLUDES_BASE = [
    "Library",
    ".DS_Store",
    "*.sock",
    ".Spotlight-V100",
    ".fseventsd",
    ".TemporaryItems",
]

# Known virtualization patterns grouped by product.
VM_PATTERNS: list = [
    # Traditional VMs
    ("Parallels",            ["Parallels", "*.macvm", "*.pvm"]),
    ("VMware Fusion",        ["Virtual Machines", "*.vmwarevm", "*.vmx", "*.vmdk"]),
    ("VirtualBox",           ["VirtualBox VMs", "*.vbox", "*.vdi"]),
    ("UTM",                  ["UTM", "*.utm"]),
    ("QEMU",                 ["*.qcow2", "*.qcow", "*.img"]),
    ("Vagrant",              [".vagrant.d"]),
    # Container runtimes
    ("Docker Desktop",       [".docker"]),
    ("Podman",               [".local/share/containers", ".config/containers"]),
    ("Lima",                 [".lima"]),
    ("Colima",               [".colima"]),
    ("Rancher Desktop",      [".rd", ".local/share/rancher-desktop"]),
    ("Podman Desktop",       [".local/share/containers/podman"]),
    ("nerdctl / containerd", [".local/share/nerdctl", ".local/share/containerd"]),
    # Kubernetes
    ("kubectl / kubeconfig", [".kube"]),
    ("k3d",                  [".k3d"]),
    ("kind",                 [".kind"]),
    ("Minikube",             [".minikube"]),
]


def build_excludes(extra: Optional[list] = None) -> list:
    return RSYNC_EXCLUDES_BASE + (extra or [])


# ---------------------------------------------------------------------------
# VM / container detection and selection
# ---------------------------------------------------------------------------

def detect_vm_folders(user: str, log: logging.Logger) -> list:
    import fnmatch
    home     = Path(f"/Users/{user}")
    detected = []

    try:
        entries = {e.name: e for e in home.iterdir()}
    except PermissionError:
        log.warning(f"Cannot scan {home} for VM/container folders.")
        return []

    for display_name, patterns in VM_PATTERNS:
        matched = []
        for name, entry in entries.items():
            for pat in patterns:
                # For patterns with slashes (e.g. ".local/share/containers"),
                # only match against the top-level component (.local) so we
                # detect it correctly — deep path matching is done by rsync.
                top_level_pat = pat.split("/")[0]
                if fnmatch.fnmatch(name, top_level_pat):
                    matched.append(entry)
                    break

        if not matched:
            continue

        total_kb = 0
        for entry in matched:
            r = run_cmd(["du", "-sk", str(entry)], log=log)
            if r.returncode == 0:
                try:
                    total_kb += int(r.stdout.split()[0])
                except (IndexError, ValueError):
                    pass

        size_str = bytes_to_human(total_kb * 1024) if total_kb else "unknown"
        log.info(f"Detected: {display_name} — {size_str}")
        detected.append((display_name, size_str, patterns))

    return detected


def _multiselect(title: str, items: list) -> list:
    """Interactive multi-select using curses.
    Arrow keys to move, Space to toggle inclusion, Enter to confirm.
    Returns list of selected indices (0-based).
    Default: nothing selected (all excluded).
    """
    import curses

    selected = set()   # indices the technician wants to INCLUDE

    def _draw(stdscr, cursor):
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        # Header
        stdscr.addstr(0, 0, title[:w-1], curses.A_BOLD)
        stdscr.addstr(1, 0, "─" * min(w-1, 60))
        stdscr.addstr(2, 0, "↑↓ move   Space = include   Enter = confirm   A = include all   N = include none")

        # Items
        for i, (name, size) in enumerate(items):
            row = 4 + i
            if row >= h - 1:
                break
            marker  = "[x]" if i in selected else "[ ]"
            line    = f"  {marker}  {name:<22} {size}"
            style   = curses.A_REVERSE if i == cursor else curses.A_NORMAL
            stdscr.addstr(row, 0, line[:w-1], style)

        stdscr.refresh()

    def _run(stdscr):
        curses.curs_set(0)
        cursor = 0
        n      = len(items)

        while True:
            _draw(stdscr, cursor)
            key = stdscr.getch()

            if key in (curses.KEY_UP, ord("k")) and cursor > 0:
                cursor -= 1
            elif key in (curses.KEY_DOWN, ord("j")) and cursor < n - 1:
                cursor += 1
            elif key == ord(" "):
                if cursor in selected:
                    selected.discard(cursor)
                else:
                    selected.add(cursor)
            elif key in (ord("a"), ord("A")):
                selected.update(range(n))
            elif key in (ord("n"), ord("N")):
                selected.clear()
            elif key in (10, 13, curses.KEY_ENTER):   # Enter
                break

        return list(selected)

    try:
        return curses.wrapper(_run)
    except Exception:
        # Fallback if curses fails (e.g. non-interactive terminal)
        return []


def select_vm_folders(user: str, detected: list, log: logging.Logger) -> list:
    """Let the technician choose which VM/container folders to INCLUDE.
    Default: all excluded. Space to mark for inclusion, Enter to confirm.
    Returns a list of glob patterns to ADD to the exclude list (i.e. not included).
    """
    console.print()
    console.rule("[bold]Virtualization / Container Data Detected[/]", style=THEME["rule"], characters="─")
    console.print()
    console.print(
        "[dim]The following folders were found in the user's home.\n"
        "By default [bold]all are excluded[/] from the backup.\n"
        "Press [#0F6E56]Space[/] to mark folders you want to [bold green]include[/], "
        "then [#0F6E56]Enter[/] to confirm.[/]\n"
    )

    items         = [(name, size) for name, size, _ in detected]
    title         = "Select folders to INCLUDE in the backup:"
    included_idxs = set(_multiselect(title, items))

    console.print()

    excluded_patterns: list = []
    for i, (name, size, patterns) in enumerate(detected):
        if i in included_idxs:
            log.info(f"Included by technician: {name}")
            print_success(f"Included: {name} ({size})")
        else:
            excluded_patterns.extend(patterns)
            log.info(f"Excluded by technician: {name} ({patterns})")
            print_warning(f"Excluded: {name} ({size})")

    if not excluded_patterns:
        print_success("All VM/container folders will be included in the backup.")
    elif len(excluded_patterns) == sum(len(p) for _, _, p in detected):
        print_warning("All VM/container folders will be excluded from the backup.")

    return excluded_patterns


# ---------------------------------------------------------------------------
# Disk space helpers
# ---------------------------------------------------------------------------

def get_free_space(path: Path = None, log: logging.Logger = None) -> int:
    """Return free bytes on the APFS data volume (or on a given path).

    On macOS with APFS, user data lives on /System/Volumes/Data which shares
    the same pool as /, but statvfs('/') returns a smaller free value than
    the actual available space. Always measure Data unless a path is given.
    """
    if path is None:
        data_vol = Path("/System/Volumes/Data")
        target   = data_vol if data_vol.exists() else Path("/")
    else:
        target = path

    stat = os.statvfs(target)
    free = stat.f_bavail * stat.f_frsize
    if log:
        log.debug(f"get_free_space({target}): {bytes_to_human(free)}")
    return free


def get_folder_size(path: Path, log: logging.Logger, excludes: Optional[list] = None) -> int:
    """Return folder size in bytes, skipping excluded top-level entries.
    Uses the same exclude list as rsync so size calc and copy always match.
    """
    import fnmatch
    active_excludes = excludes if excludes is not None else RSYNC_EXCLUDES_BASE
    log.info(f"Calculating size of {path} (excludes applied)…")

    total_kb = 0
    skipped  = []

    try:
        entries = list(path.iterdir())
    except PermissionError:
        raise RuntimeError(f"Cannot read directory: {path}")

    def _matches(name: str, pat: str) -> bool:
        import fnmatch as _fnmatch
        return _fnmatch.fnmatch(name, pat.split("/")[0])

    for entry in entries:
        if any(_matches(entry.name, pat) for pat in active_excludes):
            skipped.append(entry.name)
            log.debug(f"Size calc — excluded: {entry.name}")
            continue

        r = run_cmd(["du", "-sk", str(entry)], log=log)
        if r.returncode != 0:
            log.warning(f"du failed for {entry}, skipping.")
            continue
        try:
            total_kb += int(r.stdout.split()[0])
        except (IndexError, ValueError):
            log.warning(f"Could not parse du output for {entry}")

    if skipped:
        log.info(f"Size calc skipped {len(skipped)} entries: {skipped}")
    log.info(f"Effective size: {bytes_to_human(total_kb * 1024)}")
    return total_kb * 1024


def prompt_for_external_drive(needed_bytes: int, log: logging.Logger) -> Optional[Path]:
    """Ask for an external drive path and validate it has enough free space."""
    console.print()
    console.print(Panel(
        f"[bold #BA7517]Insufficient space on the internal disk.[/]\n\n"
        f"Insert an external drive (USB, pendrive, or portable disk) with at least "
        f"[bold cyan]{bytes_to_human(needed_bytes)}[/] of free space.\n\n"
        f"External drives are usually mounted under [#0F6E56]/Volumes/[/].\n"
        f"Example: [dim]/Volumes/MyDrive[/]",
        title="[bold #BA7517]⚠  External Drive Required[/]",
        border_style="#FAC775",
    ))
    console.print()

    # Show mounted /Volumes as a hint
    result = run_cmd(["df", "-H", "-l"], log=log)
    hints  = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts and parts[-1].startswith("/Volumes/"):
            hints.append((parts[-1], parts[3] if len(parts) > 3 else "?"))

    if hints:
        console.print("[dim]Currently mounted external volumes:[/]")
        ht = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        ht.add_column("Mount point", style="#0F6E56")
        ht.add_column("Available",   style="green")
        for mount, avail in hints:
            ht.add_row(mount, avail)
        console.print(ht)
        console.print()
    else:
        console.print("[dim]No external volumes detected yet — insert the drive first.[/]\n")

    for _ in range(3):
        raw = Prompt.ask(
            "[#0F6E56]Enter path to the external drive[/] [dim](or 'q' to quit)[/]"
        ).strip()

        if raw.lower() in ("q", "quit", "exit"):
            log.info("Technician quit at external drive prompt.")
            return None

        drive_path = Path(raw)

        if not drive_path.exists() or not drive_path.is_dir():
            print_warning(f"Path not found or not a directory: {drive_path}")
            log.warning(f"Invalid external drive path: {drive_path}")
            continue

        drive_free = get_free_space(drive_path, log=log)

        st = Table(box=box.SIMPLE, show_header=False)
        st.add_column("Label", style="dim",  width=28)
        st.add_column("Value", style="#0F6E56")
        st.add_row("Drive path",    str(drive_path))
        st.add_row("Free on drive", bytes_to_human(drive_free))
        st.add_row("Required",      bytes_to_human(needed_bytes))
        st.add_row("Space check",
                   "[green]✓ Sufficient[/]" if drive_free >= needed_bytes
                   else "[red]✗ Insufficient[/]")
        console.print(st)
        console.print()

        if drive_free >= needed_bytes:
            log.info(f"External drive OK: {drive_path} (free: {bytes_to_human(drive_free)})")
            print_success(f"Using external drive: {drive_path}")
            return drive_path

        shortage = needed_bytes - drive_free
        print_warning(
            f"This drive is {bytes_to_human(shortage)} short. "
            f"Try a drive with at least {bytes_to_human(needed_bytes)} free."
        )
        log.warning(f"Drive too small: {drive_path} — "
                    f"free={bytes_to_human(drive_free)}, needed={bytes_to_human(needed_bytes)}")

    print_error("No valid external drive provided after 3 attempts. Aborting.")
    log.error("External drive selection failed after 3 attempts.")
    return None


def check_disk_space(user: str, log: logging.Logger, excludes: list) -> tuple:
    """Returns (folder_bytes, needed_bytes, staging_dir | None)."""
    print_step(3, "Disk Space Analysis")

    home = Path(f"/Users/{user}")
    with console.status("[#0F6E56]Calculating home folder size…[/]", spinner="dots", spinner_style="#1D9E75"):
        folder_bytes = get_folder_size(home, log, excludes=excludes)

    needed_bytes = int(folder_bytes * VOLUME_OVERHEAD_FACTOR)
    free_bytes   = get_free_space(log=log)

    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Label", style="dim",  width=28)
    table.add_column("Value", style="#0F6E56")
    table.add_row("Home folder size",          bytes_to_human(folder_bytes))
    table.add_row(f"Required (×{VOLUME_OVERHEAD_FACTOR})", bytes_to_human(needed_bytes))
    table.add_row("Available on internal disk", bytes_to_human(free_bytes))
    table.add_row("Space check",
                  "[green]✓ Sufficient[/]" if free_bytes >= needed_bytes
                  else "[red]✗ Insufficient[/]")
    console.print(table)

    if free_bytes >= needed_bytes:
        log.info(f"Space OK — needed: {bytes_to_human(needed_bytes)}, "
                 f"free: {bytes_to_human(free_bytes)}")
        print_success("Internal disk has sufficient space.")
        staging_dir = Path(tempfile.gettempdir())
        log.info(f"Staging dir: {staging_dir}")
        return folder_bytes, needed_bytes, staging_dir

    log.warning(f"Insufficient space — needed: {bytes_to_human(needed_bytes)}, "
                f"free: {bytes_to_human(free_bytes)}")
    print_error("Not enough free space on the internal disk.")
    staging_dir = prompt_for_external_drive(needed_bytes, log)
    return folder_bytes, needed_bytes, staging_dir


# ---------------------------------------------------------------------------
# Staging volume helpers
# ---------------------------------------------------------------------------

def create_sparse_volume(needed_bytes: int, staging_dir: Path, log: logging.Logger) -> Optional[Path]:
    print_step(4, "Creating Temporary Staging Volume")

    tmp_dir     = Path(tempfile.mkdtemp(prefix="mac_backup_", dir=staging_dir))
    volume_path = tmp_dir / "backup_staging.sparseimage"
    size_mb     = math.ceil(needed_bytes / (1024 * 1024))

    log.info(f"Creating sparse image: {volume_path} ({size_mb} MB)")

    cmd = [
        "hdiutil", "create",
        "-size",    f"{size_mb}m",
        "-type",    "SPARSE",
        "-fs",      "APFS",
        "-volname", "BackupStaging",
        str(volume_path.with_suffix("")),   # hdiutil appends .sparseimage
    ]

    with console.status(f"[#0F6E56]Creating {size_mb} MB sparse image…[/]", spinner="dots", spinner_style="#1D9E75"):
        result = run_cmd(cmd, log=log)

    if result.returncode != 0:
        print_error(f"hdiutil create failed: {result.stderr.strip()}")
        log.error(f"hdiutil create: {result.stderr.strip()}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    print_success(f"Sparse volume created ({bytes_to_human(needed_bytes)})")
    log.info(f"Sparse image at {volume_path}")
    return volume_path


def mount_sparse_volume(volume_path: Path, log: logging.Logger) -> Optional[Path]:
    cmd = ["hdiutil", "attach", str(volume_path), "-nobrowse", "-noverify"]
    with console.status("[#0F6E56]Mounting staging volume…[/]", spinner="dots", spinner_style="#1D9E75"):
        result = run_cmd(cmd, log=log)

    if result.returncode != 0:
        print_error(f"hdiutil attach failed: {result.stderr.strip()}")
        log.error(f"hdiutil attach: {result.stderr.strip()}")
        return None

    mount_point = None
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and "/Volumes/" in parts[-1]:
            mount_point = Path(parts[-1].strip())
            break

    if not mount_point:
        print_error("Could not determine mount point.")
        log.error(f"hdiutil attach output:\n{result.stdout}")
        return None

    log.info(f"Staging mounted at {mount_point}")
    print_success(f"Staging volume mounted: {mount_point}")
    return mount_point


def unmount_volume(mount_point: Path, log: logging.Logger) -> bool:
    result = run_cmd(["hdiutil", "detach", str(mount_point), "-force"], log=log)
    if result.returncode == 0:
        log.info(f"Unmounted: {mount_point}")
        return True
    log.warning(f"Unmount failed: {result.stderr.strip()}")
    return False


# ---------------------------------------------------------------------------
# rsync copy
# ---------------------------------------------------------------------------

RSYNC_ACCEPTABLE = {0, 23, 24}


def _run_rsync(cmd: list, label: str, log: logging.Logger) -> int:
    """Run rsync with a progress spinner. Returns the exit code."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    with Progress(
        SpinnerColumn(),
        TextColumn(f"[#0F6E56]{label}[/]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(label, total=None)
        while proc.poll() is None:
            time.sleep(0.5)
        progress.update(task, completed=True)

    _, stderr = proc.communicate()
    rc = proc.returncode

    if rc not in RSYNC_ACCEPTABLE:
        if rc == 11 or "short write" in stderr or "out of space" in stderr.lower():
            print_error("rsync failed: staging volume ran out of space.")
            print_warning("Free up disk space and try again.")
        else:
            print_error(f"rsync failed (code {rc}) — see log for details.")
        log.error(f"rsync failed (code {rc}): {stderr.strip()}")
    elif rc in (23, 24):
        print_warning("Copy finished with warnings — some files skipped (see log).")
        log.warning(f"rsync code {rc}: some files skipped.")
    else:
        log.info(f"rsync completed OK (code {rc}).")

    return rc


def copy_user_home(user: str, mount_point: Path, log: logging.Logger, excludes: list) -> bool:
    src = Path(f"/Users/{user}")
    dst = mount_point / user

    log.info(f"Copying {src} → {dst} | excludes: {excludes}")
    console.print(f"[dim]Copying [#0F6E56]{src}[/] to staging volume…[/]")
    console.print(f"[dim]Excluded: {', '.join(excludes)}[/]\n")

    exclude_args = [arg for e in excludes for arg in ("--exclude", e)]
    cmd = ["rsync", "-aH", "--ignore-errors"] + exclude_args + [f"{src}/", f"{dst}/"]

    rc = _run_rsync(cmd, "Copying files…", log)

    if rc not in RSYNC_ACCEPTABLE:
        return False

    if rc == 0:
        print_success("Home folder copied to staging volume.")
    return True


# ---------------------------------------------------------------------------
# DMG helpers
# ---------------------------------------------------------------------------

def _hdiutil_create_dmg(src_folder: Path, dmg_out: Path, log: logging.Logger) -> Optional[Path]:
    """Create a compressed read-only DMG (no encryption — S3 SSE-KMS handles it).
    Returns the final .dmg path or None on failure.
    """
    dmg_name = dmg_out.stem
    cmd = [
        "hdiutil", "create",
        "-srcfolder", str(src_folder),
        "-volname",   dmg_name,
        "-format",    "UDZO",
        str(dmg_out.with_suffix("")),   # hdiutil adds .dmg itself
    ]

    with console.status("[#0F6E56]Creating compressed DMG…[/] [dim](this may take a while)[/]",
                        spinner="dots", spinner_style="#1D9E75"):
        result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log.error(f"hdiutil create DMG failed: {result.stderr.strip()}")
        return None

    candidate = Path(str(dmg_out.with_suffix("")) + ".dmg")
    if candidate.exists():
        return candidate
    if dmg_out.exists():
        return dmg_out

    log.error("DMG file not found after hdiutil create.")
    return None


def create_dmg(user: str, mount_point: Path, output_dir: Path, log: logging.Logger) -> Optional[Path]:
    """Create a compressed DMG from the staged user home. Encryption is handled by S3 SSE-KMS."""
    print_step(5, "Creating DMG")

    serial    = get_serial(log)
    datestamp = datetime.now().strftime("%Y%m%d")
    dmg_name  = f"{serial}_{user}_{datestamp}"
    dmg_out   = output_dir / f"{dmg_name}.dmg"
    src       = mount_point / user

    if not src.exists():
        print_error(f"Source folder not found in staging: {src}")
        log.error(f"DMG source missing: {src}")
        return None

    log.info(f"Creating DMG: {dmg_out}")
    console.print()

    dmg_path = _hdiutil_create_dmg(src, dmg_out, log)
    if not dmg_path:
        print_error("DMG creation failed — see log for details.")
        return None

    size = dmg_path.stat().st_size
    log.info(f"DMG created: {dmg_path} ({bytes_to_human(size)})")
    print_success(f"DMG created: [bold]{dmg_path.name}[/] ({bytes_to_human(size)})")
    return dmg_path


# ---------------------------------------------------------------------------
# Full volume backup (Macintosh HD - Data)
# ---------------------------------------------------------------------------

VOLUME_EXCLUDES = [
    ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".DS_Store", "*.sock",
    "private/tmp", "private/var/folders",
    # TCC-protected — inaccessible even with sudo
    "private/var/networkd", "private/var/OOPJit",
    "Library/Caches/com.apple.aneuserd", "Library/Caches/com.apple.aned",
    "System/Library/AssetsV2",
    "private/var/db/ConfigurationProfiles",
    "private/var/protected", "private/var/audit", "private/var/at",
]


def backup_full_volume(staging_dir: Path, log: logging.Logger) -> Optional[Path]:
    print_step(5, "Full Volume Backup — Macintosh HD - Data")

    src = DATA_VOLUME_PATH
    log.info(f"Full volume backup: {src}")

    # Measure used space
    with console.status("[#0F6E56]Calculating volume size…[/]", spinner="dots", spinner_style="#1D9E75"):
        df_result = run_cmd(["df", "-k", str(src)], log=log)
        used_kb = 0
        for line in df_result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                try:
                    used_kb = int(parts[2])
                except ValueError:
                    pass

    folder_bytes = used_kb * 1024
    needed_bytes = int(folder_bytes * VOLUME_OVERHEAD_FACTOR)
    free_bytes   = get_free_space(log=log)

    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Label", style="dim",  width=28)
    table.add_column("Value", style="#0F6E56")
    table.add_row("Source",                    str(src))
    table.add_row("Used on volume",            bytes_to_human(folder_bytes))
    table.add_row(f"Required (×{VOLUME_OVERHEAD_FACTOR})", bytes_to_human(needed_bytes))
    table.add_row("Available on disk",         bytes_to_human(free_bytes))
    table.add_row("Space check",
                  "[green]✓ Sufficient[/]" if free_bytes >= needed_bytes
                  else "[red]✗ Insufficient[/]")
    console.print(table)
    console.print()

    if free_bytes < needed_bytes:
        staging_dir = prompt_for_external_drive(needed_bytes, log)
        if staging_dir is None:
            return None

    sparse_volume = create_sparse_volume(needed_bytes, staging_dir, log)
    if not sparse_volume:
        return None

    mount_point = mount_sparse_volume(sparse_volume, log)
    if not mount_point:
        shutil.rmtree(sparse_volume.parent, ignore_errors=True)
        return None

    dst = mount_point / "MacintoshHD-Data"
    dst.mkdir(parents=True, exist_ok=True)

    console.print("[dim]Copying Macintosh HD - Data to staging volume…[/]\n")
    log.info(f"rsync {src} → {dst}")

    exclude_args = [arg for e in VOLUME_EXCLUDES for arg in ("--exclude", e)]
    cmd = ["rsync", "-aH", "--ignore-errors"] + exclude_args + [f"{src}/", f"{dst}/"]

    # Code 1 is acceptable for a live system volume — some paths are TCC-protected
    ACCEPTABLE_VOLUME = {0, 1, 23, 24}
    PERMISSION_MARKERS = ("Operation not permitted", "could not stat", "unreadable directory")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    with Progress(
        SpinnerColumn(), TextColumn("[#0F6E56]Copying volume…[/]"),
        TimeElapsedColumn(), console=console, transient=True,
    ) as progress:
        task = progress.add_task("Copying volume…", total=None)
        while proc.poll() is None:
            time.sleep(0.5)
        progress.update(task, completed=True)

    _, stderr = proc.communicate()
    rc = proc.returncode

    if rc not in ACCEPTABLE_VOLUME:
        if rc == 11 or "short write" in stderr or "out of space" in stderr.lower():
            print_error("rsync failed: staging volume ran out of space.")
        else:
            print_error(f"rsync failed (code {rc}) — see log for details.")
        log.error(f"Volume rsync failed (code {rc}): {stderr.strip()}")
        unmount_volume(mount_point, log)
        shutil.rmtree(sparse_volume.parent, ignore_errors=True)
        return None

    only_perms = rc == 1 and all(
        any(m in line for m in PERMISSION_MARKERS)
        for line in stderr.strip().splitlines() if line.strip()
    )
    if only_perms:
        print_warning("Some system-protected paths were skipped (TCC) — this is expected.")
        log.warning("Volume rsync code 1 — only permission errors, treated as OK.")
    elif rc in (1, 23, 24):
        print_warning("Copy finished with warnings — some files skipped (see log).")
        log.warning(f"Volume rsync code {rc}: {stderr.strip()[:300]}")
    else:
        print_success("Volume copied to staging.")
    log.info(f"Volume rsync complete (code {rc}).")

    # Create DMG — no local encryption, S3 SSE-KMS handles it
    serial    = get_serial(log)
    datestamp = datetime.now().strftime("%Y%m%d")
    dmg_name  = f"{serial}_volume_{datestamp}"
    dmg_out   = staging_dir / f"{dmg_name}.dmg"

    console.print()
    dmg_path = _hdiutil_create_dmg(dst, dmg_out, log)

    # Unmount staging after DMG creation
    unmount_volume(mount_point, log)

    if not dmg_path:
        print_error("DMG creation failed — see log for details.")
        return None

    size = dmg_path.stat().st_size
    print_success(f"DMG created: [bold]{dmg_path.name}[/] ({bytes_to_human(size)})")
    log.info(f"Volume DMG: {dmg_path} ({bytes_to_human(size)})")
    return dmg_path


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def get_aws_credentials(log: logging.Logger, s3_prefix: str) -> Optional[dict]:
    """Ask only for AWS keys. Region and bucket are hardcoded constants."""
    print_step(6, "AWS S3 Credentials")
    console.print(f"[dim]Bucket:[/] [#0F6E56]{AWS_BUCKET}[/]  [dim]Region:[/] [#0F6E56]{AWS_REGION}[/]")
    console.print(f"[dim]Folder:[/] [#0F6E56]{s3_prefix}/[/]\n")

    access_key = Prompt.ask("[#0F6E56]AWS Access Key ID[/]").strip()
    secret_key = getpass.getpass("  AWS Secret Access Key : ").strip()

    if not access_key or not secret_key:
        print_error("Access Key ID and Secret Access Key are required.")
        return None

    log.info(f"S3 target: s3://{AWS_BUCKET}/{s3_prefix}")
    return {
        "access_key": access_key,
        "secret_key": secret_key,
        "region":     AWS_REGION,
        "bucket":     AWS_BUCKET,
        "prefix":     s3_prefix,
    }


class S3UploadProgress:
    def __init__(self, total_size: int, progress: Progress, task_id: TaskID):
        self._lock     = threading.Lock()
        self._progress = progress
        self._task_id  = task_id

    def __call__(self, bytes_amount: int):
        with self._lock:
            self._progress.update(self._task_id, advance=bytes_amount)


def compute_sha256(path: Path, log: logging.Logger) -> str:
    log.info(f"Computing SHA-256 for {path.name}…")
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    log.info(f"SHA-256: {digest}")
    return digest


def upload_to_s3(dmg_path: Path, creds: dict, log: logging.Logger) -> bool:
    file_size     = dmg_path.stat().st_size
    chunk_bytes   = CHUNK_SIZE_MB * 1024 * 1024
    use_multipart = file_size > MULTIPART_THRESHOLD_MB * 1024 * 1024
    key_prefix    = creds["prefix"]
    s3_key        = f"{key_prefix}/{dmg_path.name}".lstrip("/")

    console.print()
    console.print(f"[dim]Destination:[/] [#0F6E56]s3://{creds['bucket']}/{s3_key}[/]")
    console.print(f"[dim]Upload mode:[/] {'Multipart' if use_multipart else 'Single-part'}")
    console.print(f"[dim]Encryption :[/] SSE-KMS (aws/s3)")
    console.print(f"[dim]File size  :[/] {bytes_to_human(file_size)}\n")

    with console.status("[#0F6E56]Computing local SHA-256…[/]", spinner="dots", spinner_style="#1D9E75"):
        local_sha256 = compute_sha256(dmg_path, log)
    print_success(f"Local SHA-256: {local_sha256}")

    # Connect and verify bucket access
    try:
        session = boto3.Session(
            aws_access_key_id=creds["access_key"],
            aws_secret_access_key=creds["secret_key"],
            region_name=creds["region"],
        )
        s3 = session.client("s3")
        s3.head_bucket(Bucket=creds["bucket"])
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        print_error(f"S3 access error ({code}): {e}")
        log.error(f"S3 access: {e}")
        return False
    except Exception as e:
        print_error(f"AWS connection failed: {e}")
        log.error(f"AWS connection: {e}")
        return False

    # Upload
    try:
        with Progress(
            SpinnerColumn(), TextColumn("[#0F6E56]{task.description}"),
            BarColumn(), FileSizeColumn(), TransferSpeedColumn(), TimeElapsedColumn(),
            console=console,
        ) as progress:
            task    = progress.add_task("Uploading to S3…", total=file_size)
            tracker = S3UploadProgress(file_size, progress, task)

            # SSE-KMS — encrypt at rest using the default aws/s3 KMS key
            extra_args = {"ServerSideEncryption": "aws:kms"}

            if use_multipart:
                config = boto3.s3.transfer.TransferConfig(
                    multipart_threshold=MULTIPART_THRESHOLD_MB * 1024 * 1024,
                    multipart_chunksize=chunk_bytes,
                    max_concurrency=4,
                    use_threads=True,
                )
                s3.upload_file(str(dmg_path), creds["bucket"], s3_key,
                               Callback=tracker, Config=config, ExtraArgs=extra_args)
            else:
                s3.upload_file(str(dmg_path), creds["bucket"], s3_key,
                               Callback=tracker, ExtraArgs=extra_args)

        log.info(f"Upload complete: s3://{creds['bucket']}/{s3_key}")
        print_success("Upload complete.")

    except Exception as e:
        print_error(f"Upload failed: {e}")
        log.error(f"Upload error: {e}")
        return False

    # ETag integrity check
    console.print()
    with console.status("[#0F6E56]Validating upload integrity…[/]", spinner="dots", spinner_style="#1D9E75"):
        try:
            head        = s3.head_object(Bucket=creds["bucket"], Key=s3_key)
            etag        = head["ETag"].strip('"')
            remote_size = head["ContentLength"]
            log.info(f"S3 ETag: {etag} | Remote size: {remote_size}")
        except Exception as e:
            print_warning(f"Could not retrieve ETag: {e}")
            log.warning(f"ETag retrieval: {e}")
            return True

    size_match = remote_size == file_size

    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Label", style="dim",  width=22)
    table.add_column("Value", style="#0F6E56")
    table.add_row("S3 ETag",      etag)
    table.add_row("Remote size",  bytes_to_human(remote_size))
    table.add_row("Local size",   bytes_to_human(file_size))
    table.add_row("Local SHA-256", local_sha256)
    table.add_row("Size match",   "[green]✓[/]" if size_match else "[red]✗[/]")
    console.print(table)

    if not size_match:
        print_warning("Size mismatch — verify the upload manually.")
        log.warning(f"Size mismatch: local={file_size}, remote={remote_size}")
    else:
        print_success("Integrity check passed.")
        log.info("Integrity check passed.")

    return True


# ---------------------------------------------------------------------------
# Cleanup and summary
# ---------------------------------------------------------------------------

def cleanup(
    sparse_volume: Optional[Path],
    mount_point:   Optional[Path],
    dmg_path:      Optional[Path],
    log:           logging.Logger,
    log_file:      Path,
):
    print_step(7, "Summary & Cleanup")

    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Item", style="dim",  width=26)
    table.add_column("Path", style="#0F6E56")

    if dmg_path      and dmg_path.exists():
        table.add_row("DMG file",            str(dmg_path))
    if sparse_volume and sparse_volume.exists():
        table.add_row("Sparse staging image", str(sparse_volume))
    if mount_point   and mount_point.exists():
        table.add_row("Staging mount point", str(mount_point))
    table.add_row("Log file", str(log_file))
    console.print(table)
    console.print()

    # Unmount staging volume if still mounted
    if mount_point and mount_point.exists():
        if Confirm.ask("[#0F6E56]Unmount staging volume?[/]", default=True):
            unmount_volume(mount_point, log)

    # Remove temp files
    # The DMG may live inside sparse_volume.parent (same tmp dir).
    # Build a deduplicated list: prefer the parent dir over the individual file.
    items_to_remove = []
    tmp_dir = sparse_volume.parent if sparse_volume and sparse_volume.exists() else None
    if tmp_dir:
        items_to_remove.append(tmp_dir)
    if dmg_path and dmg_path.exists():
        # Only add DMG separately if it lives outside the tmp dir
        if tmp_dir is None or not str(dmg_path).startswith(str(tmp_dir)):
            items_to_remove.append(dmg_path)

    if items_to_remove:
        console.print()
        if Confirm.ask("[#0F6E56]Delete temporary files (sparse image + local DMG)?[/]", default=False):
            for item in items_to_remove:
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    log.info(f"Removed: {item}")
                    print_success(f"Removed: {item}")
                except Exception as e:
                    print_warning(f"Could not remove {item}: {e}")
                    log.warning(f"Remove failed {item}: {e}")
        else:
            print_warning("Temporary files kept — clean up manually when done.")
            log.info("Temporary files kept by user choice.")

    console.print()
    console.print(Panel.fit(
        "[bold #3B6D11]✓  Backup workflow complete.[/]\n"
        f"[dim]Log: {log_file}[/]",
        border_style="#0F6E56",
    ))
    console.print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print_header()

    # Setup logging — fallback to /tmp if /var/log is not writable
    try:
        log, log_file = setup_logging(LOG_DIR)
    except PermissionError:
        fallback = Path(tempfile.gettempdir()) / "mac_backup"
        log, log_file = setup_logging(fallback)
        print_warning(f"No write access to {LOG_DIR} — logging to {fallback}")

    log.info(f"=== mac_backup.py v{TOOL_VERSION} started ===")
    log.info(f"Running as: {getpass.getuser()} | macOS {platform.mac_ver()[0]}")

    sparse_volume: Optional[Path] = None
    mount_point:   Optional[Path] = None
    dmg_path:      Optional[Path] = None

    try:
        # Dependencies
        if not validate_dependencies(log):
            sys.exit(1)

        # Collect user email upfront — used for S3 folder naming
        user_email = prompt_user_email(log)

        # Backup mode
        mode = select_backup_mode(log)

        # Time Machine check (both modes)
        if not handle_time_machine(log):
            sys.exit(1)

        # ── User backup ──────────────────────────────────────────────────
        if mode == BACKUP_MODE_USER:

            user = select_user(log)
            if not user:
                print_error("No user selected. Exiting.")
                sys.exit(1)

            # Detect VM / container folders
            with console.status("[#0F6E56]Scanning for virtualization and container data…[/]",
                                spinner="dots", spinner_style="#1D9E75"):
                detected_vms = detect_vm_folders(user, log)

            vm_excludes: list = []
            if detected_vms:
                vm_excludes = select_vm_folders(user, detected_vms, log)
            else:
                console.print("[dim]No virtualization or container folders detected.[/]\n")
                log.info("No VM/container folders detected.")

            active_excludes = build_excludes(vm_excludes)

            # Disk space check
            folder_bytes, needed_bytes, staging_dir = check_disk_space(
                user, log, excludes=active_excludes
            )
            if staging_dir is None:
                sys.exit(1)

            # Create staging volume and copy
            sparse_volume = create_sparse_volume(needed_bytes, staging_dir, log)
            if not sparse_volume:
                sys.exit(1)

            mount_point = mount_sparse_volume(sparse_volume, log)
            if not mount_point:
                sys.exit(1)

            if not copy_user_home(user, mount_point, log, excludes=active_excludes):
                sys.exit(1)

            dmg_path = create_dmg(user, mount_point, staging_dir, log)

            # Unmount staging after DMG creation
            unmount_volume(mount_point, log)
            mount_point = None   # prevent double-unmount in cleanup

            if not dmg_path:
                sys.exit(1)

        # ── Full volume backup ───────────────────────────────────────────
        else:
            staging_dir = Path(tempfile.gettempdir())
            dmg_path    = backup_full_volume(staging_dir, log)
            if not dmg_path:
                sys.exit(1)

        # S3 upload (both modes) — retry on credential or upload error
        serial     = get_serial(log)
        s3_prefix  = f"{AWS_PREFIX}/{serial}_{user_email}"
        log.info(f"S3 folder: {s3_prefix}")

        while True:
            creds = get_aws_credentials(log, s3_prefix)
            if not creds:
                # Technician left a required field blank
                if not Confirm.ask("[#0F6E56]Try entering credentials again?[/]", default=True):
                    print_warning("Upload skipped — DMG preserved locally.")
                    log.warning("S3 upload skipped by technician.")
                    break
                continue

            if upload_to_s3(dmg_path, creds, log):
                break   # success

            # Upload failed — ask if they want to retry with different credentials
            console.print()
            if Confirm.ask("[#0F6E56]Retry with different credentials?[/]", default=True):
                continue
            print_warning("Upload failed — DMG preserved locally.")
            log.error("S3 upload failed and technician chose not to retry.")
            break

    except KeyboardInterrupt:
        console.print()
        print_warning("Interrupted by user.")
        log.warning("Interrupted by user (KeyboardInterrupt).")

    finally:
        cleanup(sparse_volume, mount_point, dmg_path, log, log_file)


if __name__ == "__main__":
    main()

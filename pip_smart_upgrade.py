#!/usr/bin/env python3
"""Upgrade outdated top-level packages with dependency conflict resolution.

- Version checks target only top-level packages (pipdeptree graph roots) by
  default, or explicit names passed on the CLI -- far cheaper than
  `pip list --outdated`, which queries every installed package.
- Constraints read locally from pipdeptree (no PyPI queries).
- Pre-flight: `pip install --dry-run --report <tempfile>` resolves the whole
  batch and is checked against every installed package's requirements, closing
  the hole where pip ignores already-installed pinners; unsafe targets pruned.
  (Report goes to a temp file, not "-": pip 26 renders stdout reports via rich,
  which crashes on non-GBK chars under a legacy Windows console.)
- Shared-dep conflicts blocked or confirmed interactively.
- Batch pip install; output streams live with elapsed tags, per-package noise
  (Collecting/Downloading/Uninstalling) folded into a ~3s ticker that names the
  packages and phase, unless `--verbose`; Windows locks (WinError 5) killed/
  retried individually.
- Post-upgrade: pip check + auto-fix, max 3 rounds.
- `~*` leftover cleanup is skipped while another pip upgrade is running (its
  in-flight staging dirs would otherwise be deleted mid-install).
- Terse by default: the plan line shows versions (`pkg old -> new`), and
  skip/conflict lines are compacted; `--verbose`/`-v` expands the full
  per-package and per-constraint detail. Every long step shows live progress
  (counters or an elapsed timer), so nothing looks hung.

Usage:
    python pip_smart_upgrade.py                 # top-level: analyze + pre-flight + upgrade + fix
    python pip_smart_upgrade.py --dry-run       # analyze + pre-flight, no install
    python pip_smart_upgrade.py -v              # verbose: full per-package / constraint dump
    python pip_smart_upgrade.py requests rich   # only the named packages
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Chinese PyPI mirrors (fallback)
PYPI_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/pypi",  # Tsinghua
    "https://mirrors.aliyun.com/pypi/pypi",  # Alibaba
    "https://pypi.doubanio.com/pypi",  # Douban
    "https://pypi.mirrors.ustc.edu.cn/simple",  # USTC
]


class PkgError(Exception):
    pass


def run_pip(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


_PIP_NOISE_RE = re.compile(
    r"^\s*(?:-{5,}|Collecting |Using cached |Downloading |Attempting uninstall: "
    r"|Found existing installation: |Uninstalling |Successfully uninstalled "
    r"|Requirement already satisfied: )",
    re.I,
)
_PIP_KEEP_RE = re.compile(r"^\s*(?:Successfully installed\b|Installing collected packages\b)", re.I)
_COLLECT_RE = re.compile(r"^\s*Collecting\s+(\S+)", re.I)
_DOWNLOAD_RE = re.compile(r"^\s*Downloading\s+", re.I)
_REMOVE_RE = re.compile(r"^\s*Attempting uninstall:\s+(\S+)", re.I)


def _pip_line_kind(text: str) -> str:
    """Streaming pip 日志行分类：keep（结果/里程碑）/ noise（逐包噪音）/ other。"""
    if _PIP_KEEP_RE.match(text):
        return "keep"
    if _PIP_NOISE_RE.match(text):
        return "noise"
    return "other"


def run_pip_streaming(
    *args: str, indent: str = "    ", compact: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run pip while echoing its output live (long installs must not look hung).

    stdout is printed line-by-line with an elapsed-seconds tag; stderr is drained
    on a side thread. Both streams are still captured for later parsing.

    compact=True folds the per-package noise (Collecting/Downloading/Uninstalling
    lines) into a ~3s ticker that names the packages being processed and their
    phase (collecting/downloading/removing), so the user always sees what is
    happening without being flooded. Milestones ("Installing collected
    packages", "Successfully installed") and errors/warnings stay visible; long
    package lists are truncated to N names + "(+M more)". --verbose keeps every
    raw pip line.
    """
    env = {**os.environ, "PYTHONUTF8": "1"}
    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "pip", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    out_lines: list[str] = []
    err_lines: list[str] = []

    def _drain(stream: Any, sink: list[str]) -> None:
        for line in stream:
            sink.append(line)

    err_thread = threading.Thread(target=_drain, args=(proc.stderr, err_lines), daemon=True)
    err_thread.start()
    assert proc.stdout is not None
    hidden = 0  # noise lines folded since the last tick
    phase = ""  # latest activity keyword (collecting/downloading/removing)
    new_names: list[str] = []
    last_tick = start

    def _emit_tick(now: float, force: bool = False) -> None:
        nonlocal hidden, last_tick
        if not compact:
            return
        if force:
            if not new_names:
                return  # keep/tail: only name a pending burst, don't repeat msgs
        elif now - last_tick < 3.0 or not (new_names or hidden):
            return
        last_tick = now
        label = phase or "working"
        shown = new_names[:6]
        extra = len(new_names) - len(shown)
        if shown:
            body = f"{label}: {', '.join(shown)}"
            if extra:
                body += f" ... (+{extra} more)"
        else:
            body = f"{label} ... ({hidden} msgs)"
        print(
            f"{indent}[{int(now - start):>4}s] {body} (use --verbose to expand)",
            flush=True,
        )
        new_names.clear()
        hidden = 0

    for line in proc.stdout:
        out_lines.append(line)
        text = line.rstrip()
        tag = f"{int(time.monotonic() - start):>4}s"
        if not text:
            continue
        kind = _pip_line_kind(text)
        if compact and kind == "noise":
            hidden += 1
            collect_m = _COLLECT_RE.match(text)
            if collect_m:
                phase = "collecting"
                name = collect_m.group(1)
                if name not in new_names:
                    new_names.append(name)
            else:
                remove_m = _REMOVE_RE.match(text)
                if remove_m:
                    phase = "removing"
                    name = remove_m.group(1)
                    if name not in new_names:
                        new_names.append(name)
                elif _DOWNLOAD_RE.match(text):
                    phase = "downloading"
            _emit_tick(time.monotonic())
            continue
        if kind == "keep" and compact:
            _emit_tick(time.monotonic(), force=True)
            if len(text) > 160:
                parts = text.split()
                header_end = 3 if parts and parts[0].lower() == "installing" else 2
                total = max(0, len(parts) - header_end)
                shown_names: list[str] = []
                width = 0
                for part in parts[header_end:]:
                    if width + len(part) + 2 > 150:
                        break
                    shown_names.append(part)
                    width += len(part) + 2
                text = (
                    " ".join(parts[:header_end])
                    + " "
                    + ", ".join(shown_names)
                    + f" ... (+{total - len(shown_names)} more)"
                )
        print(f"{indent}[{tag}] {text}", flush=True)
    _emit_tick(time.monotonic(), force=True)
    proc.wait()
    err_thread.join(timeout=5)
    return subprocess.CompletedProcess(
        proc.args, proc.returncode, "".join(out_lines), "".join(err_lines)
    )


def _fmt_elapsed(seconds: float) -> str:
    return f"{seconds:.0f}s"


def _compact(items: list[str], limit: int = 12) -> str:
    """把列表压成 `a, b, ... (+N more)`，避免刷屏。"""
    if len(items) <= limit:
        return ", ".join(items)
    return f"{', '.join(items[:limit])} (+{len(items) - limit} more)"


def format_summary(
    outdated: dict[str, tuple[str, str]],
    succeeded: list[str],
    failed: list[str],
    skipped: list[tuple[str, str]],
    check_ok: bool,
) -> str:
    """生成结果总结一句话：实际升级/失败/跳过/依赖状态。"""
    upgraded = sorted(set(succeeded))
    failed_pkgs = sorted(set(failed))
    skipped_pkgs = sorted(p for p, _ in skipped)
    bits: list[str] = []
    if upgraded:
        bits.append(
            f"upgraded {len(upgraded)} "
            f"({_compact([f'{p} {outdated[p][0]} -> {outdated[p][1]}' for p in upgraded], 6)})"
        )
    else:
        bits.append("upgraded 0")
    if failed_pkgs:
        bits.append(f"failed {len(failed_pkgs)} ({_compact(failed_pkgs, 6)})")
    if skipped_pkgs:
        bits.append(
            f"skipped {len(skipped_pkgs)} "
            f"({_compact([f'{p} ({reason})' for p, reason in skipped if p in skipped_pkgs], 6)})"
        )
    bits.append("pip check clean" if check_ok else "pip check FAILED")
    return "Summary: " + ", ".join(bits) + "."


class Pulse:
    """单行原地状态显示；非 TTY（重定向/管道）时退化为每次一行。"""

    def __init__(self, label: str) -> None:
        self._label = label
        self._tty = sys.stdout.isatty()
        self._width = 0

    def _render(self, text: str, end: str) -> None:
        if self._tty:
            pad = " " * max(0, self._width - len(text))
            print(f"\r{text}{pad}", end=end, flush=True)
            self._width = len(text)
        else:
            print(text, end=end, flush=True)
            if end == "\n":
                self._width = len(text)

    def set(self, detail: str = "") -> None:
        # 非 TTY 无法原地刷新，逐次打印只会刷屏；交给 done() 收尾输出一行即可。
        if not self._tty:
            return
        self._render(f"{self._label}{detail}", "")

    def done(self, detail: str = "") -> None:
        self._render(f"{self._label}{detail}", "\n")


class Working:
    """上下文管理器：为不透明长调用持续刷新耗时，确保"一直在动"。"""

    def __init__(self, label: str, interval: float = 3.0) -> None:
        self._pulse = Pulse(label)
        self._interval = interval
        self._stop = threading.Event()
        self._start = 0.0
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Working:
        self._start = time.monotonic()
        self._pulse.set()
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()
        return self

    def _tick(self) -> None:
        while not self._stop.wait(self._interval):
            self._pulse.set(f" {_fmt_elapsed(time.monotonic() - self._start)}")

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._pulse.done(f" {_fmt_elapsed(time.monotonic() - self._start)}")


def site_packages_dir() -> Path:
    result = run_pip("show", "pip")
    for line in result.stdout.splitlines():
        if line.startswith("Location:"):
            return Path(line.split(":", 1)[1].strip())
    raise PkgError("Cannot locate site-packages directory")


_PIP_CMD_RE = re.compile(
    r"(?:^|\s)-m\s+pip(?:\s|$)"
    r"|(?:^|[\\/\s\"'])pip(?:\.exe)?[\"']?\s+(?:install|uninstall|download)\b",
    re.IGNORECASE,
)


def _iter_pip_cmdlines(text: str, me_pid: int) -> list[str]:
    """从 `pid<TAB>cmdline` 文本中挑出其他 pip 进程（排除 me_pid）。

    只认真正的 pip 调用（`python -m pip ...` / `pip install ...`），避免把
    pyright/grep 等命令行里恰好出现本脚本文件名的进程误判为 pip。
    """
    active: list[str] = []
    for line in text.splitlines():
        pid_str, sep, cmd = line.partition("\t")
        if not sep:
            continue
        with contextlib.suppress(ValueError):
            if int(pid_str.strip()) == me_pid:
                continue
        if _PIP_CMD_RE.search(cmd):
            active.append(cmd.strip())
    return active


def active_pip_processes() -> list[str]:
    """其他正在运行的 pip / 本脚本进程的命令行（排除自身）。

    `~*` 目录可能是这些进程正在做卸载/安装的在途 staging；此时清理会装坏包，
    故清理前先探测。非 Windows 或探测失败时返回 []（不做拦截）。
    """
    if os.name != "nt":
        return []
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return _iter_pip_cmdlines(result.stdout, os.getpid())


def cleanup_invalid_dists() -> list[Path]:
    """Clean up pip leftover directories (tilde-prefixed) from all site-packages dirs.

    Skips entirely (with a warning) when another pip upgrade is running, since those
    `~*` dirs may be its in-flight staging. Returns the list of removed paths.
    """
    import site

    active = active_pip_processes()
    if active:
        print(
            f"[!] Detected {len(active)} active pip upgrade process(es); skipping ~* cleanup "
            "to avoid deleting in-flight staging dirs:"
        )
        for cmd in active[:3]:
            print(f"      {cmd}")
        return []

    site_dirs: set[Path] = {site_packages_dir()}
    with contextlib.suppress(Exception):
        site_dirs.update(Path(d) for d in site.getsitepackages())
    with contextlib.suppress(Exception):
        site_dirs.add(Path(site.getusersitepackages()))

    removed: list[Path] = []
    for site_dir in site_dirs:
        if not site_dir.is_dir():
            continue
        for entry in site_dir.iterdir():
            if entry.name.startswith("~"):
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                    removed.append(entry)
                except OSError as e:
                    print(f"[!] Cannot cleanup {entry}: {e}", file=sys.stderr)
    return removed


def _is_local_install(pkg: str) -> bool:
    """是否为 editable 或从本地目录/文件安装（direct_url.json 判定）。

    这类包不应被当成 PyPI 包查询/升级（如项目自身的 `netmind` 编辑安装）。
    """
    import importlib.metadata as imd

    try:
        raw = imd.distribution(pkg).read_text("direct_url.json")
    except (imd.PackageNotFoundError, OSError):
        return False
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if (data.get("dir_info") or {}).get("editable"):
        return True
    return str(data.get("url", "")).startswith("file:")


def _pip_index_latest(pkg: str) -> tuple[str, str] | None:
    """查询单个包的 INSTALLED/LATEST（`pip index versions`）。

    比 `pip list --outdated` 全量扫描（对每个已装包都发请求）省得多。
    返回 (installed, latest)；不可查、未装、或本地/editable 安装（显式判定，
    且当前版本不在索引版本列表中，双保险防用 PyPI 同名包误判升级）则返回 None。
    """
    if _is_local_install(pkg):
        return None
    result = run_pip("index", "versions", pkg)
    if result.returncode != 0:
        return None
    installed = latest = ""
    available: set[str] = set()
    for line in result.stdout.splitlines():
        s = line.strip()
        if s.startswith("INSTALLED:"):
            installed = s.split(":", 1)[1].strip()
        elif s.startswith("LATEST:"):
            latest = s.split(":", 1)[1].strip()
        elif s.startswith("Available versions:"):
            available = {v.strip() for v in s.split(":", 1)[1].split(",")}
    if not installed or not latest:
        return None
    if installed not in available:
        return None
    return installed, latest


def get_top_level_packages(
    dep_specs: dict[str, dict[str, str]],
    reverse_deps: dict[str, list[str]],
) -> list[str]:
    """顶层包 = 依赖图的根（无任何包依赖它），排除本地/editable 安装。"""
    return sorted(p for p in dep_specs if not reverse_deps.get(p) and not _is_local_install(p))


def get_outdated_packages(targets: list[str]) -> dict[str, tuple[str, str]]:
    """返回 targets 中过时包的 {包名: (当前, 最新)}。

    只对 targets（默认顶层包，或命令行显式点名）并行查索引，替代会扫描
    每个已装包的 `pip list --outdated`。
    """
    outdated: dict[str, tuple[str, str]] = {}
    total = len(targets)
    pulse = Pulse(f"[2/4] checking {total} targets for updates...")
    pulse.set()
    workers = min(16, max(1, total))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_pip_index_latest, t): t for t in targets}
        for fut in as_completed(futures):
            done += 1
            with contextlib.suppress(Exception):
                res = fut.result()
                if res and res[0] and res[1] and res[0] != res[1]:
                    outdated[normalize(futures[fut])] = (res[0], res[1])
            pulse.set(f" {done}/{total}, {len(outdated)} outdated")
    detail = ""
    if outdated:
        detail = f" ({_compact(sorted(outdated), 6)})"
    pulse.done(f" {total} checked, {len(outdated)} outdated{detail}")
    return outdated


def satisfies_constraint(version: str, constraint: str) -> bool:
    """Check if a version satisfies a constraint using packaging."""
    try:
        from packaging.requirements import Requirement

        req = Requirement(f"dummy{constraint}")
        return bool(req.specifier.contains(version, prereleases=True))
    except Exception as e:
        print(f"[!] Constraint parse failed {constraint}: {e}", file=sys.stderr)
        return True


def compute_version_intersection(all_specs: list[str]) -> str:
    """用 packaging.SpecifierSet 合并版本约束。

    手写正则解析无法正确处理 `~=`/`!=`/多段约束，这里交给 packaging。
    返回合并后的约束串；无有效约束时返回 "any"。
    """
    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    combined = SpecifierSet()
    for spec in all_specs:
        if not spec:
            continue
        try:
            combined &= SpecifierSet(spec)
        except InvalidSpecifier as e:
            print(f"[!] Constraint parse failed {spec}: {e}", file=sys.stderr)
    text = str(combined).strip().strip(",")
    return text if text else "any"


def _tighter_lower(a: tuple[Any, bool], b: tuple[Any, bool]) -> tuple[Any, bool]:
    """取更紧的下界（版本大者优先；同版本时闭区间优先）。"""
    if a[0] != b[0]:
        return a if a[0] > b[0] else b
    return a if a[1] else b


def _tighter_upper(a: tuple[Any, bool], b: tuple[Any, bool]) -> tuple[Any, bool]:
    """取更紧的上界（版本小者优先；同版本时闭区间优先）。"""
    if a[0] != b[0]:
        return a if a[0] < b[0] else b
    return a if a[1] else b


def specifier_conflicts(all_specs: list[str]) -> tuple[bool, str]:
    """尽力判定合并约束是否无解（覆盖 ==/!=/>=/>/<=/</~=）。

    纯区间推理，无法识别的运算符按「无约束」处理；权威判定仍以 pip 解析器的
    --dry-run 预演为准。返回 (是否矛盾, 原因)。
    """
    from packaging.version import InvalidVersion, Version

    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    exact: Version | None = None
    excluded: set[Version] = set()

    for spec in all_specs:
        for part in spec.split(","):
            m = re.match(r"^\s*(>=|>|<=|<|==|~=|!=)\s*([^\s,]+)\s*$", part)
            if not m:
                continue
            op, vstr = m.group(1), m.group(2)
            try:
                v = Version(vstr)
            except InvalidVersion:
                continue
            if op == "==":
                if exact is not None and exact != v:
                    return True, f"=={exact} 与 =={v} 互斥"
                exact = v
            elif op == "!=":
                excluded.add(v)
            elif op == ">=":
                lower = (v, True) if lower is None else _tighter_lower(lower, (v, True))
            elif op == ">":
                lower = (v, False) if lower is None else _tighter_lower(lower, (v, False))
            elif op == "<=":
                upper = (v, True) if upper is None else _tighter_upper(upper, (v, True))
            elif op == "<":
                upper = (v, False) if upper is None else _tighter_upper(upper, (v, False))
            elif op == "~=":
                lower = (v, True) if lower is None else _tighter_lower(lower, (v, True))
                rel = list(v.release)
                if len(rel) >= 2:
                    rel[-2] += 1
                    up = Version(".".join(str(x) for x in rel[:-1]))
                else:
                    up = Version(str(rel[0] + 1))
                upper = (up, False) if upper is None else _tighter_upper(upper, (up, False))

    if exact is not None:
        if exact in excluded:
            return True, f"=={exact} 被 != 排除"
        if lower and (exact < lower[0] or (exact == lower[0] and not lower[1])):
            return True, f"=={exact} 低于下界 {lower[0]}"
        if upper and (exact > upper[0] or (exact == upper[0] and not upper[1])):
            return True, f"=={exact} 高于上界 {upper[0]}"
        return False, ""

    if lower and upper:
        if lower[0] > upper[0]:
            return True, f"下界 {lower[0]} > 上界 {upper[0]}"
        if lower[0] == upper[0] and not (lower[1] and upper[1]):
            return True, f"区间在 {lower[0]} 处为空（一端为开区间）"
    return False, ""


def batch_install(packages: list[str], *, compact: bool = True) -> tuple[list[str], list[str]]:
    """Install all packages in one batch via pip resolver.

    If the batch fails because a Windows executable is locked (WinError 5),
    identify the offending package, remove it from the batch, and retry.
    Remaining locked packages are retried individually.

    If the batch fails for a genuine conflict, fall back to individual installs.

    Returns (succeeded, failed).
    """
    if not packages:
        return [], []

    print(f"[*] Batch installing {len(packages)} packages via pip resolver...")
    print("    (pip output streams below with elapsed seconds -- this is the slow step)")
    remaining = list(packages)
    succeeded: list[str] = []
    locked_packages: list[str] = []

    while remaining:
        start = time.monotonic()
        result = install(*remaining, stream=True, compact=compact)
        elapsed = time.monotonic() - start
        if result.returncode == 0:
            print(f"    [OK] batch install succeeded ({len(remaining)} packages, {elapsed:.0f}s)")
            succeeded.extend(remaining)
            break

        if "WinError 5" not in result.stderr and "拒绝访问" not in result.stderr:
            print(
                f"    [!] Batch install failed after {elapsed:.0f}s: {result.stderr.strip()[:200]}"
            )
            print("[*] Falling back to individual install...")
            s2, f2 = parallel_install(remaining)
            succeeded.extend(s2)
            locked_packages.extend(f2)
            break

        locked = identify_locked_packages(result.stderr, remaining)
        if not locked:
            print(
                "    [!] Could not identify locked package; falling back to individual install..."
            )
            s2, f2 = parallel_install(remaining)
            succeeded.extend(s2)
            locked_packages.extend(f2)
            break

        remaining = [p for p in remaining if p not in locked]
        locked_packages.extend(locked)
        print(
            f"    [!] Windows lock on {locked[0]}.exe; excluded {', '.join(locked)} from batch, "
            f"retrying with {len(remaining)}"
        )

    if locked_packages:
        print(f"[*] Retrying locked packages individually: {locked_packages}")
        s2, f2 = parallel_install(locked_packages)
        succeeded.extend(s2)
        failed = f2
    else:
        failed = []

    return succeeded, failed


def parallel_install(packages: list[str], max_workers: int = 4) -> tuple[list[str], list[str]]:
    """Install packages in parallel. Returns (succeeded, failed)."""
    succeeded: list[str] = []
    failed: list[str] = []
    total = len(packages)
    completed = 0
    print(f"[*] Installing {total} package(s) individually (up to {max_workers} in parallel)...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(install, pkg): pkg for pkg in packages}
        for future in as_completed(futures):
            pkg = futures[future]
            completed += 1
            try:
                result = future.result()
                if result.returncode == 0:
                    print(f"    [{completed}/{total}] [OK] {pkg}")
                    succeeded.append(pkg)
                else:
                    print(f"    [{completed}/{total}] [!] {pkg}: {result.stderr.strip()[:80]}")
                    failed.append(pkg)
            except Exception as e:
                print(f"    [{completed}/{total}] [!] {pkg} error: {e}")
                failed.append(pkg)

    return succeeded, failed


def kill_processes_by_name(name: str) -> int:
    """Kill processes by executable name. Returns kill count."""
    count = 0
    result = subprocess.run(
        ["tasklist", "/fo", "csv"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    exe = f"{name}.exe"
    pids: list[int] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split('","')
        if len(parts) >= 2 and exe.lower() in parts[0].lower():
            with contextlib.suppress(ValueError):
                pids.append(int(parts[1]))
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        count += 1
    return count


def extract_locked_exe_name(stderr: str) -> str | None:
    """从 pip 的 WinError 5 报错中提取被锁定的 .exe 名称（不含扩展名）。"""
    for token in re.findall(r"\S+\.exe", stderr, re.IGNORECASE):
        token = token.strip("'\"").lower()
        if "scripts" in token:
            return Path(token).stem
    return None


def get_package_scripts(pkg: str) -> set[str]:
    """获取某个已安装包提供的 console_scripts 名称集合。"""
    try:
        import importlib.metadata as imd

        dist = imd.distribution(pkg)
        eps = dist.entry_points
        if hasattr(eps, "select"):
            return {ep.name for ep in eps.select(group="console_scripts")}
        return {ep.name for ep in eps if getattr(ep, "group", None) == "console_scripts"}
    except Exception:
        return set()


def identify_locked_packages(stderr: str, candidates: list[str]) -> list[str]:
    """根据 WinError 5 的 .exe 路径，找出 candidates 中可能是罪魁祸首的包。"""
    exe = extract_locked_exe_name(stderr)
    if not exe:
        return []
    for pkg in candidates:
        if pkg.lower() == exe:
            return [pkg]
    for pkg in candidates:
        if exe in get_package_scripts(pkg):
            return [pkg]
    return []


def install(
    *pkg_specs: str, stream: bool = False, compact: bool = False
) -> subprocess.CompletedProcess[str]:
    """Install/upgrade/downgrade packages with process-kill retry on Windows.

    stream=True echoes pip output live (used for the long batch attempt);
    compact=True collapses the per-package noise while keeping errors and the
    final result line.
    """
    if stream:

        def runner(*inner_args: str) -> subprocess.CompletedProcess[str]:
            return run_pip_streaming(*inner_args, compact=compact)
    else:

        def runner(*inner_args: str) -> subprocess.CompletedProcess[str]:
            return run_pip(*inner_args)

    result = runner("install", "-U", *pkg_specs)
    if result.returncode != 0 and ("WinError 5" in result.stderr or "拒绝访问" in result.stderr):
        exe = extract_locked_exe_name(result.stderr)
        pkg_name = pkg_specs[0].split("==")[0].split(">=")[0].split("<=")[0].strip()
        kill_name = exe if exe else pkg_name
        if kill_name:
            killed = kill_processes_by_name(kill_name)
            if killed:
                print(f"[*] Killed {killed} {kill_name} process(es), retrying")
                result = runner("install", "-U", *pkg_specs)
    return result


def normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def dry_run_resolution(packages: list[str]) -> tuple[dict[str, Any] | None, str]:
    """用 pip 解析器预演整批升级（不安装），返回 (report, error)。

    report 为 pip --report 的 JSON（含真实解析结果 install 列表）；低版本 pip
    不支持时返回 (None, error)，调用方降级为直接批量安装。

    报告写入临时文件而非 "-"：pip 26 对 stdout 报告用 rich.print_json，
    在 legacy Windows 控制台按 GBK 编码非 GBK 字符会抛 UnicodeEncodeError；
    写文件则走普通 UTF-8 I/O。
    """
    if not packages:
        return None, ""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    fd, report_path = tempfile.mkstemp(suffix=".json", prefix="pip_report_")
    os.close(fd)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--report",
                report_path,
                "-q",
                "-U",
                *packages,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
        if result.returncode != 0:
            return None, (result.stderr.strip() or result.stdout.strip())[:400]
        out = Path(report_path).read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"dry-run 执行失败: {exc}"
    finally:
        with contextlib.suppress(OSError):
            Path(report_path).unlink()
    try:
        return json.loads(out), ""
    except json.JSONDecodeError:
        idx = out.find("{")
        if idx >= 0:
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(out[idx:]), ""
        return None, "无法解析 pip --report 输出"


def check_report_violations(report: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """校验解析结果是否会破坏「已装但未参与本次升级」包的约束。

    pip 解析器只保证本次请求集合自洽，不会约束其它已装包（这正是 kimi-cli
    被顶掉的根因）；这里补上这一层。已参与升级的包改用 report 里的新元数据。
    返回 [(requirer, dep, spec, planned_version), ...]。
    """
    from packaging.requirements import Requirement

    resolved: dict[str, str] = {}
    planned_requires: dict[str, list[str]] = {}
    for item in report.get("install", []):
        meta = item.get("metadata") or {}
        name = normalize(meta.get("name") or "")
        if not name:
            continue
        resolved[name] = meta.get("version", "")
        planned_requires[name] = meta.get("requires_dist") or []

    import importlib.metadata as imd

    violations: list[tuple[str, str, str, str]] = []
    for dist in imd.distributions():
        requirer = normalize(dist.name or "")
        if not requirer:
            continue
        reqs = planned_requires.get(requirer)
        if reqs is None:
            reqs = dist.requires or []
        for req_str in reqs:
            try:
                req = Requirement(req_str)
            except Exception:
                continue
            if req.marker is not None:
                with contextlib.suppress(Exception):
                    if not req.marker.evaluate():
                        continue
            dep = normalize(req.name)
            if dep not in resolved or not req.specifier:
                continue
            try:
                ok = req.specifier.contains(resolved[dep], prereleases=True)
            except Exception:
                continue
            if not ok:
                violations.append((requirer, dep, str(req.specifier), resolved[dep]))
    return violations


def unsafe_targets(
    report: dict[str, Any],
    violations: list[tuple[str, str, str, str]],
    to_upgrade: list[str],
) -> set[str]:
    """由违规项反推应从升级批次中剔除的包（归一化名）。

    优先剔除「被升级且违反约束的依赖」；若该依赖是解析器顺带升级的传递依赖，
    则剔除所有把它拉进计划的批次包。
    """
    from packaging.requirements import Requirement

    upgraded = {normalize(p) for p in to_upgrade}
    planned_requires: dict[str, list[str]] = {}
    for item in report.get("install", []):
        meta = item.get("metadata") or {}
        name = normalize(meta.get("name") or "")
        if name:
            planned_requires[name] = meta.get("requires_dist") or []

    bad: set[str] = set()
    for _requirer, dep, _spec, _ver in violations:
        if dep in upgraded:
            bad.add(dep)
            continue
        for pkg, reqs in planned_requires.items():
            if pkg not in upgraded:
                continue
            for req_str in reqs:
                try:
                    if normalize(Requirement(req_str).name) == dep:
                        bad.add(pkg)
                        break
                except Exception:
                    continue
    return bad


def parse_pip_check(output: str) -> dict[str, dict[str, str]]:
    """Parse pip check output: {dep: {requiring_pkg: version_spec}}.

    兼容旧版措辞（"has requirement dep<spec>, but you have dep ver."）与新 pip
    （"requires dep<spec>, but you have dep ver which is incompatible."）。
    """
    conflicts: dict[str, dict[str, str]] = {}
    head_re = re.compile(r"^(\S+)\s+\S+\s+(?:has requirement|requires)\s+(.+?)\s*$")
    req_re = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*((?:[<>=!~][^\s,]*)(?:,[^\s,]*)*)\s*$")
    for raw in output.splitlines():
        line = raw.strip()
        if "but you have" not in line:
            continue
        head = line.partition("but you have")[0].strip().rstrip(",").strip()
        m = head_re.match(head)
        if not m:
            continue
        rm = req_re.match(m.group(2).strip())
        if not rm:
            continue
        dep = rm.group(1).lower().replace("_", "-")
        conflicts.setdefault(dep, {})[m.group(1)] = rm.group(2).strip()
    return conflicts


def try_resolve_conflicts(fix_conflicts: bool) -> dict[str, Any]:
    """Run pip check and optionally auto-fix conflicts."""
    result = run_pip("check")
    if result.returncode == 0:
        return {"ok": True, "output": result.stdout}

    conflicts = parse_pip_check(result.stdout)
    if not fix_conflicts or not conflicts:
        return {"ok": False, "output": result.stdout, "conflicts": conflicts}

    fixed: list[str] = []
    failed: list[str] = []

    for round_num in range(1, 4):
        if not conflicts:
            break
        print(f"[*] Conflict fix round {round_num}...")
        for dep, req_by in conflicts.items():
            for requiring_pkg, version_spec in req_by.items():
                if not version_spec:
                    continue
                target_dep = f"{dep}{version_spec}"
                specs = [target_dep, requiring_pkg]
                print(f"[*] Trying: {specs}")
                install_result = install(*specs)
                if install_result.returncode == 0:
                    fixed.append(f"{target_dep} + {requiring_pkg}")
                else:
                    failed.append(f"{target_dep} + {requiring_pkg}")

        result = run_pip("check")
        if result.returncode == 0:
            conflicts = {}
            break
        conflicts = parse_pip_check(result.stdout)

    return {
        "ok": result.returncode == 0,
        "output": result.stdout,
        "conflicts": conflicts,
        "fixed": fixed,
        "failed": failed,
    }


def format_manual_suggestions(conflicts: dict[str, dict[str, str]]) -> list[str]:
    """Turn remaining pip check conflicts into deduped manual fix suggestions."""
    seen: set[str] = set()
    lines: list[str] = []
    for dep, req_by in sorted(conflicts.items()):
        for requiring_pkg, version_spec in sorted(req_by.items()):
            target = f"{dep}{version_spec}" if version_spec else dep
            cmd = f'pip install "{target}" "{requiring_pkg}"'
            if cmd not in seen:
                seen.add(cmd)
                lines.append(cmd)
    return lines


def report_remaining_conflicts(check: dict[str, Any]) -> None:
    """Print pip check output; if unresolved, append deduped manual-fix suggestions."""
    print(check["output"])
    if check["ok"]:
        return
    suggestions = format_manual_suggestions(check.get("conflicts") or {})
    if suggestions:
        print("Suggested manual fixes:")
        for line in suggestions:
            print(f"    {line}")
    else:
        print("Unrecognized conflicts; inspect pip check output above.")


def parse_args() -> dict[str, Any]:
    """Parse CLI args: --dry-run, --verbose/-v, plus optional explicit package names."""
    return {
        "dry_run": "--dry-run" in sys.argv,
        "verbose": "--verbose" in sys.argv or "-v" in sys.argv,
        "packages": [a for a in sys.argv[1:] if not a.startswith("-")],
    }


def can_upgrade_safely(
    pkg: str,
    latest: str,
    reverse_deps: dict[str, list[str]],
    dep_specs: dict[str, dict[str, str]],
) -> tuple[bool, str]:
    """Check if a package can be safely upgraded to the latest version.

    Uses dependency constraints from pipdeptree directly (no PyPI queries).
    Checks each constraint individually to avoid missing ~= operators.

    Returns (can_upgrade, reason)
    """
    dependers = reverse_deps.get(pkg, [])

    # No dependents = top-level package, safe to upgrade
    if not dependers:
        return True, "top-level, no dependents"

    # Collect all dependents' version constraints on this package
    all_specs: list[tuple[str, str]] = []
    for depender in dependers:
        spec = dep_specs.get(depender, {}).get(pkg, "")
        if spec:
            all_specs.append((depender, spec))

    # No constraints = safe to upgrade
    if not all_specs:
        return True, "dependents unconstrained"

    # Check if latest satisfies each constraint individually
    for depender, spec in all_specs:
        if not satisfies_constraint(latest, spec):
            return False, f"{depender} needs {spec}"

    return True, f"all constraints met: {', '.join(s for _, s in all_specs)}"


def analyze_shared_dep_intersection(
    to_upgrade: list[str],
    top_level: set[str],
    outdated: dict[str, tuple[str, str]],
    reverse_deps: dict[str, list[str]],
    dep_specs: dict[str, dict[str, str]],
) -> tuple[set[str], dict[str, dict[str, Any]], list[str]]:
    """分析待升级包的共享依赖版本约束交集。

    使用 pipdeptree 提取的 dep_specs，无需查 PyPI。
    约束来源覆盖「所有」依赖方（含未过时的锁定方，如 kimi-cli），不再只看
    过时集合，避免漏掉精确锁定造成的矛盾；展示仍聚焦于被多个升级包共享
    或确实矛盾的依赖，防止输出爆炸。

    返回 (blocked_pkgs, intersections, conflicts)
    """
    intersections: dict[str, dict[str, Any]] = {}
    blocked_pkgs: set[str] = set()
    conflicts: list[str] = []
    checked: set[str] = set()
    to_upgrade_set = set(to_upgrade)

    for pkg in to_upgrade:
        for dep_name in dep_specs.get(pkg, {}):
            if dep_name in checked:
                continue
            checked.add(dep_name)

            requiring = sorted(set(reverse_deps.get(dep_name, [])))
            if len(requiring) <= 1:
                continue

            other_specs: dict[str, str] = {}
            for other_pkg in requiring:
                spec = dep_specs.get(other_pkg, {}).get(dep_name, "")
                if spec:
                    other_specs[other_pkg] = spec

            all_specs = list(other_specs.values())
            intersection = compute_version_intersection(all_specs) if all_specs else "any"
            is_conflict, reason = specifier_conflicts(all_specs)

            upgraders = [p for p in requiring if p in to_upgrade_set]
            if len(upgraders) < 2 and not is_conflict:
                continue

            info: dict[str, Any] = {
                "specs": other_specs,
                "requiring": requiring,
                "intersection": intersection,
                "conflict": is_conflict,
                "reason": reason,
            }
            if dep_name in outdated:
                info["outdated"] = outdated[dep_name]

            intersections[dep_name] = info

            if is_conflict:
                conflicts.append(dep_name)
                for p in upgraders:
                    blocked_pkgs.add(p)

    return blocked_pkgs, intersections, conflicts


# Script dependencies (non-stdlib) that must be present before running
SCRIPT_DEPS = ["packaging", "pipdeptree"]


def ensure_dependencies() -> None:
    """Ensure this script's own dependencies are installed; auto-install if missing."""
    for dep in SCRIPT_DEPS:
        try:
            __import__(dep)
        except ImportError:
            print(f"[*] {dep} not found, installing...")
            result = run_pip("install", dep)
            if result.returncode != 0:
                raise PkgError(f"Failed to install {dep}: {result.stderr}") from None


def build_dep_constraints_from_pipdeptree() -> tuple[
    dict[str, list[str]], dict[str, dict[str, str]]
]:
    """从 pipdeptree JSON 构建反向依赖图和依赖约束。

    pipdeptree 已包含每个依赖的 required_version，无需再查 PyPI。

    返回 (reverse_deps, dep_specs)
    - reverse_deps: {依赖包: [依赖它的包列表]}
    - dep_specs: {依赖方: {依赖名: 版本约束}}
    """
    import subprocess as sp

    result = sp.run(
        [sys.executable, "-m", "pipdeptree", "--json-tree", "--warn", "silence"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PkgError(f"pipdeptree failed: {result.stderr}")

    tree = json.loads(result.stdout)
    reverse_deps: dict[str, list[str]] = {}
    dep_specs: dict[str, dict[str, str]] = {}

    def process_item(item: dict) -> None:
        pkg = item["package_name"].lower().replace("_", "-")
        deps_map: dict[str, str] = {}
        for dep in item.get("dependencies") or []:
            dep_name = dep["package_name"].lower().replace("_", "-")
            spec = dep.get("required_version", "")
            if spec == "Any":
                spec = ""
            deps_map[dep_name] = spec
            reverse_deps.setdefault(dep_name, []).append(pkg)
            process_item(dep)
        dep_specs[pkg] = deps_map

    for item in tree:
        process_item(item)

    return reverse_deps, dep_specs


def main() -> int:
    try:
        return _main_impl()
    except PkgError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        return 1


def _main_impl() -> int:
    args = parse_args()
    dry_run = args["dry_run"]
    verbose = args["verbose"]
    explicit = args["packages"]

    # 0) Ensure this script's own dependencies are available first
    ensure_dependencies()

    # 清理残留
    removed = cleanup_invalid_dists()
    if removed:
        print(f"Cleaned up: {', '.join(p.name for p in removed)}")

    # 1) 构建依赖关系图（从 pipdeptree 直接提取约束，本地，不查 PyPI）
    #    build_dep_constraints_from_pipdeptree() raises PkgError on failure so
    #    later steps cannot proceed with invalid/empty dep data.
    graph_start = time.monotonic()
    graph_pulse = Pulse("[1/4] building dep graph...")
    graph_pulse.set()
    reverse_deps, dep_specs = build_dep_constraints_from_pipdeptree()

    # 2) 只对顶层包（或命令行显式点名的包）查版本；
    #    替代会扫描每个已装包的 `pip list --outdated`
    if explicit:
        targets = [normalize(p) for p in explicit]
        targets_label = f"{len(targets)} targeted"
    else:
        targets = get_top_level_packages(dep_specs, reverse_deps)
        targets_label = f"{len(targets)} top-level"
        if not targets:
            raise PkgError("pipdeptree graph yielded no top-level packages")
    graph_pulse.done(
        f" {len(dep_specs)} installed, {targets_label} "
        f"({_fmt_elapsed(time.monotonic() - graph_start)})"
    )
    outdated = get_outdated_packages(targets)

    if not outdated:
        print("All packages up to date")
        check = try_resolve_conflicts(True)
        report_remaining_conflicts(check)
        return 0 if check["ok"] else 1

    # 4) 中庸策略：逐个检查所有过时包（仅本地依赖约束，不查 PyPI）
    to_upgrade: list[str] = []
    skipped: list[tuple[str, str]] = []
    total_outdated = len(outdated)
    analyze_pulse = Pulse(f"[3/4] analyzing upgradability ({total_outdated} packages)...")
    analyze_pulse.set()
    for idx, pkg in enumerate(sorted(outdated.keys()), 1):
        current, latest = outdated[pkg]
        can_upgrade, reason = can_upgrade_safely(
            pkg,
            latest,
            reverse_deps,
            dep_specs,
        )
        if can_upgrade:
            to_upgrade.append(pkg)
        else:
            skipped.append((pkg, reason))
        analyze_pulse.set(f" {idx}/{total_outdated}")
    analyze_pulse.done(
        f" {total_outdated} analyzed -> {len(to_upgrade)} safe, {len(skipped)} skipped"
    )

    if to_upgrade:
        if verbose:
            print("\nPlan (safe to upgrade):")
            for pkg in sorted(to_upgrade):
                current, latest = outdated[pkg]
                print(f"    {pkg}: {current} -> {latest}")
        else:
            plan_items = [f"{p} {outdated[p][0]} -> {outdated[p][1]}" for p in sorted(to_upgrade)]
            print(f"Plan ({len(to_upgrade)}): {_compact(plan_items, 10)}")

    if skipped:
        if verbose:
            print("\nSkipped (dependents pinning):")
            for pkg, reason in skipped:
                current, latest = outdated[pkg]
                print(f"    {pkg}: {current} -> {latest}: {reason}")
        else:
            print(
                f"Skipped ({len(skipped)}): "
                f"{_compact([f'{pkg} ({reason})' for pkg, reason in skipped], 8)}"
            )

    if not to_upgrade:
        print("\nNo safe upgrades available")
        check = try_resolve_conflicts(True)
        report_remaining_conflicts(check)
        return 0 if check["ok"] else 1

    # 5) 分析共享依赖版本约束交集（检测矛盾）
    blocked_pkgs, intersections, conflicts = analyze_shared_dep_intersection(
        to_upgrade,
        set(outdated.keys()),
        outdated,
        reverse_deps,
        dep_specs,
    )

    print(f"[4/4] shared deps: {len(intersections)} analyzed, {len(conflicts)} conflicts")
    for dep in conflicts:
        info = intersections.get(dep) or {}
        print(f"    [!] {dep}: {info.get('reason') or 'conflicting constraints'}")

    if verbose:
        for dep, info in sorted(intersections.items(), key=lambda x: -len(x[1]["specs"])):
            specs = info["specs"]
            intersection = info["intersection"]
            requiring = ", ".join(sorted(info.get("requiring") or specs.keys()))
            print(f"\n    {dep} (required by {len(info.get('requiring') or specs)}: {requiring})")
            for pkg, spec in sorted(specs.items()):
                print(f"      {pkg} requires: {dep}{spec or '(unconstrained)'}")
            label = dep if intersection == "any" else f"{dep}{intersection}"
            print(f"      >>> intersection: {label}")
            if info.get("conflict"):
                print(f"      >>> CONFLICT: {info.get('reason')}")
            if "outdated" in info:
                cur, lat = info["outdated"]
                print(f"      version: {cur} -> {lat}")

    # 移除被 blocked 的包
    if blocked_pkgs:
        to_upgrade = [p for p in to_upgrade if p not in blocked_pkgs]
        print(
            f"\nRemoved {len(blocked_pkgs)} (shared dep conflicts): "
            f"{', '.join(sorted(blocked_pkgs))}"
        )

    # 5.5) Pre-flight：用 pip 解析器预演整批，捕获「已装包精确锁定」被打破的情况
    print()
    with Working(
        f"[*] pre-flight: resolving {len(to_upgrade)} packages via pip (no install)...",
        2.0,
    ):
        report, err = dry_run_resolution(to_upgrade)
    if err:
        print(f"    [!] dry-run skipped: {err}")
    elif report is not None:
        violations = check_report_violations(report)
        for _ in range(2):
            if not violations:
                break
            print(f"    [!] Plan would break {len(violations)} installed requirement(s):")
            for requirer, dep, spec, ver in violations[:20]:
                print(f"        - {requirer} requires {dep}{spec}, plan resolves {dep} {ver}")
            bad = unsafe_targets(report, violations, to_upgrade)
            if not bad:
                print("    [!] Cannot identify offending target(s); proceeding may break packages")
                break
            to_upgrade = [p for p in to_upgrade if normalize(p) not in bad]
            print(f"    [!] Pruned {len(bad)} unsafe target(s): {', '.join(sorted(bad))}")
            with Working(
                f"    pre-flight: re-resolving pruned plan ({len(to_upgrade)} pkgs)...", 2.0
            ):
                report, err = dry_run_resolution(to_upgrade)
            if err or not report:
                violations = []
                break
            violations = check_report_violations(report)
        if not to_upgrade:
            print("\nNo safe upgrades available after pre-flight")
            check = try_resolve_conflicts(True)
            report_remaining_conflicts(check)
            return 0 if check["ok"] else 1
        if violations:
            print(f"    [!] {len(violations)} potential conflict(s) remain in plan")
        else:
            print(f"    [OK] resolver plan clean ({len(to_upgrade)} packages)")

    # 检测到矛盾：交互模式
    if conflicts:
        print(f"\nFound {len(conflicts)} conflicts:")
        for dep in conflicts:
            info = intersections[dep]
            specs = info["specs"]
            print(f"\n    {dep}:")
            for pkg, spec in sorted(specs.items()):
                print(f"      {pkg} requires: {dep}{spec}")
            print("      >>> CONFLICT (no solution)")

        if dry_run:
            print("\nDry run, no changes made")
            return 0

        print("\n[?] Force upgrade? (pip will try to resolve, may need manual fix)")
        print("    1. Continue")
        print("    2. Cancel")
        choice = input("    Choose [1/2]: ").strip()
        if choice != "1":
            print("Cancelled")
            return 0
    else:
        if dry_run:
            print("\nDry run, no changes made")
            return 0

    # 6) Upgrade safe packages
    print(f"\n[*] Upgrading {len(to_upgrade)} packages...")
    upgrade_start = time.monotonic()
    succeeded, failed = batch_install(to_upgrade, compact=not verbose)
    print(f"[*] Upgrade step finished in {_fmt_elapsed(time.monotonic() - upgrade_start)}")

    removed = cleanup_invalid_dists()
    if removed:
        print(f"Cleaned up install leftovers: {', '.join(p.name for p in removed)}")

    # 7) 循环 pip check + 修复（最多 3 轮）
    max_rounds = 3
    check = {"ok": True, "output": "", "fixed": [], "failed": []}
    for round_num in range(1, max_rounds + 1):
        removed = cleanup_invalid_dists()
        if removed:
            print(
                f"Cleaned up before pip check round {round_num}: "
                f"{', '.join(p.name for p in removed)}"
            )
        print(f"\npip check (round {round_num})...")
        check = try_resolve_conflicts(True)
        print(check["output"])

        if check["ok"]:
            print("All requirements satisfied")
            break

        if check.get("fixed"):
            print(f"Auto-fixed: {', '.join(check['fixed'])}")
        if check.get("failed"):
            print(f"Auto-fix failed: {', '.join(check['failed'])}")

        if round_num == max_rounds:
            print(f"Max rounds ({max_rounds}) reached, unresolved conflicts may remain")
            break

    # 8) 汇总
    print("\n" + "=" * 50)
    print(format_summary(outdated, succeeded, failed, skipped, check["ok"]))

    failed_pkgs = sorted(set(failed))
    if failed_pkgs:
        print("Failed:")
        for pkg in failed_pkgs:
            print(f"    - {pkg}")
    if not check["ok"]:
        print("Dependency conflicts remain, manual intervention needed")
        suggestions = format_manual_suggestions(check.get("conflicts") or {})
        if suggestions:
            print("Suggested manual fixes:")
            for line in suggestions:
                print(f"    {line}")
        else:
            print("Inspect pip check output above.")
        return 1
    return 1 if failed_pkgs else 0


if __name__ == "__main__":
    sys.exit(main())

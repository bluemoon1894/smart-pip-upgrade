#!/usr/bin/env python3
"""Upgrade outdated packages with dependency conflict resolution.

- Constraints read locally from pipdeptree (no PyPI queries).
- Top-level packages always upgrade; others only if latest meets all
  dependents' constraints (including non-outdated pinners, e.g. kimi-cli).
- Pre-flight: `pip install --dry-run --report <tempfile>` resolves the whole
  batch and is checked against every installed package's requirements, closing
  the hole where pip ignores already-installed pinners; unsafe targets pruned.
  (Report goes to a temp file, not "-": pip 26 renders stdout reports via rich,
  which crashes on non-GBK chars under a legacy Windows console.)
- Shared-dep conflicts blocked or confirmed interactively.
- Batch pip install; Windows locks (WinError 5) killed/retried individually.
- Post-upgrade: pip check + auto-fix, max 3 rounds.

Usage:
    python pip_smart_upgrade.py              # analyze + pre-flight + upgrade + fix
    python pip_smart_upgrade.py --dry-run    # analyze + pre-flight, no install
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


def site_packages_dir() -> Path:
    result = run_pip("show", "pip")
    for line in result.stdout.splitlines():
        if line.startswith("Location:"):
            return Path(line.split(":", 1)[1].strip())
    raise PkgError("Cannot locate site-packages directory")


def cleanup_invalid_dists() -> list[Path]:
    """Clean up pip leftover directories (tilde-prefixed) from all site-packages dirs.

    Returns the list of removed paths.
    """
    import site

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


def get_outdated_packages() -> dict[str, tuple[str, str]]:
    print("[1/4] Checking outdated packages (queries PyPI, may be slow)...", end="", flush=True)
    result = run_pip("list", "--outdated", "--format=json")
    if result.returncode != 0:
        raise PkgError(f"pip list --outdated failed: {result.stderr}")
    outdated = {
        p["name"].lower().replace("_", "-"): (p["version"], p["latest_version"])
        for p in json.loads(result.stdout)
    }
    print(f" Found {len(outdated)}")
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


def batch_install(packages: list[str]) -> tuple[list[str], list[str]]:
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
    remaining = list(packages)
    succeeded: list[str] = []
    locked_packages: list[str] = []

    while remaining:
        result = install(*remaining)
        if result.returncode == 0:
            print(f"    [OK] batch install succeeded ({len(remaining)} packages)")
            succeeded.extend(remaining)
            break

        if "WinError 5" not in result.stderr and "拒绝访问" not in result.stderr:
            print(f"    [!] Batch install failed: {result.stderr.strip()[:200]}")
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


def install(*pkg_specs: str) -> subprocess.CompletedProcess[str]:
    """Install/upgrade/downgrade packages with process-kill retry on Windows."""
    result = run_pip("install", "-U", *pkg_specs)
    if result.returncode != 0 and ("WinError 5" in result.stderr or "拒绝访问" in result.stderr):
        exe = extract_locked_exe_name(result.stderr)
        pkg_name = pkg_specs[0].split("==")[0].split(">=")[0].split("<=")[0].strip()
        kill_name = exe if exe else pkg_name
        if kill_name:
            killed = kill_processes_by_name(kill_name)
            if killed:
                print(f"[*] Killed {killed} {kill_name} process(es), retrying")
                result = run_pip("install", "-U", *pkg_specs)
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


def parse_args() -> dict[str, bool]:
    """Parse CLI args. Only --dry-run."""
    return {
        "dry_run": "--dry-run" in sys.argv,
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
    all_specs: list[str] = []
    for depender in dependers:
        spec = dep_specs.get(depender, {}).get(pkg, "")
        if spec:
            all_specs.append(spec)

    # No constraints = safe to upgrade
    if not all_specs:
        return True, "dependents unconstrained"

    # Check if latest satisfies each constraint individually
    for spec in all_specs:
        if not satisfies_constraint(latest, spec):
            return False, f"latest {latest} violates constraint {spec}"

    return True, f"all constraints met: {', '.join(all_specs)}"


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

    print("[2/4] Building dep graph...", end="", flush=True)
    result = sp.run(
        [sys.executable, "-m", "pipdeptree", "--json-tree", "--warn", "silence"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        print(" FAILED")
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

    print(" done")
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

    # 0) Ensure this script's own dependencies are available first
    ensure_dependencies()

    # 1) 清理残留
    removed = cleanup_invalid_dists()
    if removed:
        print(f"Cleaned up: {', '.join(p.name for p in removed)}")

    # 2) 获取过时包
    outdated = get_outdated_packages()

    if not outdated:
        print("All packages up to date")
        check = try_resolve_conflicts(True)
        report_remaining_conflicts(check)
        return 0 if check["ok"] else 1

    # 3) 构建依赖关系图（从 pipdeptree 直接提取约束，无需查 PyPI）
    #    build_dep_constraints_from_pipdeptree() raises PkgError on failure so
    #    step 4 cannot proceed with invalid/empty dep data.
    reverse_deps, dep_specs = build_dep_constraints_from_pipdeptree()

    # 4) 中庸策略：逐个检查所有过时包（仅本地依赖约束，不查 PyPI）
    print("[3/4] Analyzing upgradability... (local constraints, please wait)", flush=True)
    to_upgrade: list[str] = []
    skipped_unsafe: list[str] = []

    total_outdated = len(outdated)
    for idx, pkg in enumerate(sorted(outdated.keys()), 1):
        print(f"[3/4] Analyzing {pkg} ({idx}/{total_outdated})...", end="\r", flush=True)
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
            skipped_unsafe.append(f"{pkg} ({current} -> {latest}: {reason})")

    print(f"[3/4] Analyzing upgradability: completed {total_outdated} packages{' ' * 10}")

    print(f"\n{'=' * 50}")
    print(f"Done: {len(to_upgrade)} safe, {len(skipped_unsafe)} skipped")

    if to_upgrade:
        print("\nSafe to upgrade:")
        for pkg in to_upgrade:
            current, latest = outdated[pkg]
            print(f"    {pkg}: {current} -> {latest}")

    if skipped_unsafe:
        print("\nSkipped (dependents pinning):")
        for item in skipped_unsafe:
            print(f"    {item}")

    if not to_upgrade:
        print("\nNo safe upgrades available")
        check = try_resolve_conflicts(True)
        report_remaining_conflicts(check)
        return 0 if check["ok"] else 1

    # 5) 分析共享依赖版本约束交集（检测矛盾）
    print("[4/4] Analyzing shared dep conflicts...")
    blocked_pkgs, intersections, conflicts = analyze_shared_dep_intersection(
        to_upgrade,
        set(outdated.keys()),
        outdated,
        reverse_deps,
        dep_specs,
    )

    if intersections:
        print(f"\nFound {len(intersections)} shared deps:")
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
    else:
        print("    No shared dep conflicts")

    # 移除被 blocked 的包
    if blocked_pkgs:
        to_upgrade = [p for p in to_upgrade if p not in blocked_pkgs]
        print(
            f"\nRemoved {len(blocked_pkgs)} (shared dep conflicts): "
            f"{', '.join(sorted(blocked_pkgs))}"
        )

    # 5.5) Pre-flight：用 pip 解析器预演整批，捕获「已装包精确锁定」被打破的情况
    print("\n[*] Pre-flight resolver dry-run...")
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
    print(f"\n[4/4] Upgrading {len(to_upgrade)} packages...")
    succeeded, failed = batch_install(to_upgrade)

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
    print(f"Upgraded: {len(succeeded)} ok, {len(failed)} failed")
    if failed:
        print("Failed:")
        for pkg in failed:
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

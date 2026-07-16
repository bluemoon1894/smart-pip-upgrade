#!/usr/bin/env python3
"""Upgrade outdated packages with automatic dependency conflict resolution.

Strategy: check all outdated packages (including transitive deps). Upgrade only
if the latest version satisfies all dependents' version constraints.

Design:
- Analyze all dependents' version constraints for each outdated package.
- Upgrade if latest falls within constraints; skip otherwise.
- Top-level packages (no dependents) upgrade unconditionally.
- Detect shared dependency constraint intersections and conflicts.
- Post-upgrade: loop pip check + auto-fix (max 3 rounds).

Usage:
    python update_outdated_top_level.py              # analyze + upgrade + fix
    python update_outdated_top_level.py --dry-run    # analyze only
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import sys
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


def parse_version_spec(spec: str) -> list[tuple[str, str]]:
    """Parse version constraint string. Returns [(operator, version), ...]."""
    specs = []
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    for part in parts:
        m = re.match(r"^(>=|>|<=|<|==|!=|~=)\s*(.+)$", part)
        if m:
            specs.append((m.group(1), m.group(2).strip()))
    return specs


def compute_version_intersection(all_specs: list[str]) -> str:  # noqa: C901
    """Compute intersection of version constraints. Returns 'CONFLICT' if unsolvable."""
    from packaging.version import Version

    if not all_specs:
        return "any"

    parsed = []
    for spec in all_specs:
        parsed.extend(parse_version_spec(spec))

    if not parsed:
        return "any"

    # 分类
    lower_bounds = []
    upper_bounds = []

    for op, ver in parsed:
        if op in (">=", ">"):
            lower_bounds.append((op, ver))
        elif op in ("<=", "<"):
            upper_bounds.append((op, ver))
        elif op == "==":
            return f"=={ver}"

    # 找最大的下界（用语义版本比较）
    if lower_bounds:
        max_lower = max(lower_bounds, key=lambda x: Version(x[1]))
        lower = f"{max_lower[0]}{max_lower[1]}"
    else:
        lower = ""

    # 找最小的上界（用语义版本比较）
    if upper_bounds:
        min_upper = min(upper_bounds, key=lambda x: Version(x[1]))
        upper = f"{min_upper[0]}{min_upper[1]}"
    else:
        upper = ""

    # 检测矛盾：下界 > 上界（用语义版本比较）
    if lower_bounds and upper_bounds:
        max_lower_ver = Version(max(lower_bounds, key=lambda x: Version(x[1]))[1])
        min_upper_ver = Version(min(upper_bounds, key=lambda x: Version(x[1]))[1])
        if max_lower_ver > min_upper_ver:
            return "CONFLICT"

    # 组合
    if lower and upper:
        return f"{lower},{upper}"
    elif lower:
        return lower
    elif upper:
        return upper
    else:
        return "any"


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


def parse_pip_check(output: str) -> dict[str, dict[str, str]]:
    """Parse pip check output: {dep: {requiring_pkg: version_spec}}."""
    conflicts: dict[str, dict[str, str]] = {}
    pattern = re.compile(
        r"^(\S+)\s+\S+\s+has requirement\s+(\S+?)\s*([<>!=~]+\S+)?\s*,\s*but you have\s+\S+\s+\S+\."
    )
    for line in output.splitlines():
        m = pattern.match(line.strip())
        if m:
            req_by = m.group(1)
            dep = m.group(2).lower()
            version_spec = m.group(3) or ""
            conflicts.setdefault(dep, {})[req_by] = version_spec
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


def analyze_shared_dep_intersection(  # noqa: C901
    to_upgrade: list[str],
    top_level: set[str],
    outdated: dict[str, tuple[str, str]],
    reverse_deps: dict[str, list[str]],
    dep_specs: dict[str, dict[str, str]],
) -> tuple[set[str], dict[str, dict[str, Any]], list[str]]:
    """分析待升级包的共享依赖版本约束交集。

    使用 pipdeptree 提取的 dep_specs，无需查 PyPI。

    返回 (blocked_pkgs, intersections, conflicts)
    """
    intersections: dict[str, dict[str, Any]] = {}
    blocked_pkgs: set[str] = set()
    conflicts: list[str] = []
    checked: set[str] = set()

    for pkg in to_upgrade:
        for dep_name, _ in dep_specs.get(pkg, {}).items():
            if dep_name in checked:
                continue
            checked.add(dep_name)

            # 找所有依赖这个共享依赖的包
            requiring = [p for p in reverse_deps.get(dep_name, []) if p in top_level]
            if len(requiring) <= 1:
                continue

            # 收集每个包对这个共享依赖的版本约束
            other_specs: dict[str, str] = {}
            for other_pkg in requiring:
                spec = dep_specs.get(other_pkg, {}).get(dep_name, "")
                if spec:
                    other_specs[other_pkg] = spec

            all_specs = [s for s in other_specs.values() if s]
            intersection = compute_version_intersection(all_specs) if all_specs else "any"

            info: dict[str, Any] = {
                "specs": other_specs,
                "intersection": intersection,
            }
            if dep_name in outdated:
                info["outdated"] = outdated[dep_name]

            intersections[dep_name] = info

            if intersection == "CONFLICT":
                conflicts.append(dep_name)
                for p in requiring:
                    if p in to_upgrade:
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


def _main_impl() -> int:  # noqa: C901
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
        print(check["output"])
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
        print(check["output"])
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
            requiring = ", ".join(sorted(specs.keys()))
            print(f"\n    {dep} (required by {len(specs)}: {requiring})")
            for pkg, spec in sorted(specs.items()):
                print(f"      {pkg} requires: {dep}{spec or '(unconstrained)'}")
            print(f"      >>> intersection: {dep}{intersection}")
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
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

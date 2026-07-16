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

Future Enhancement:
- When latest violates constraints, query PyPI for all versions and find the
  highest version that satisfies dependents' constraints. Low priority: most
  dependents use == pinning, so intermediate versions rarely help; adds ~15-50s
  for 26 extra PyPI requests.
"""

from __future__ import annotations

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
    """Clean up pip leftover directories (tilde-prefixed). Returns removed list."""
    site = site_packages_dir()
    removed: list[Path] = []
    for entry in site.iterdir():
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
        p["name"].lower().replace("_", "-"): (p["version"], p["latest_version"]) for p in json.loads(result.stdout)
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


def check_new_version_deps(
    pkg: str,
    latest: str,
    installed_versions: dict[str, str],
    reverse_deps: dict[str, list[str]],
    dep_specs: dict[str, dict[str, str]],
    outdated: dict[str, tuple[str, str]],
) -> tuple[bool, str]:
    """Check if target version's new deps conflict with current environment.

    Fetches requires_dist from PyPI for the target version and verifies
    each required dependency is satisfied by installed packages.

    Returns (ok, reason)
    """
    import urllib.request as ureq

    url = f"https://pypi.org/pypi/{pkg}/{latest}/json"
    try:
        with ureq.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except Exception:
        return True, ""  # fetch failed — assume safe (conservative)

    requires_dist = data.get("info", {}).get("requires_dist")
    if not requires_dist:
        return True, "no new deps in target"

    from packaging.requirements import Requirement

    problematic: list[str] = []
    for req_str in requires_dist:
        if not req_str:
            continue
        try:
            req = Requirement(req_str)
        except Exception:
            continue
        # Skip optional dependencies (extra markers)
        if req.marker:
            # packaging.Marker has no public markers iterator; use string form
            if re.search(r"\bextra\b", str(req.marker)):
                continue
            # Skip non-matching platform markers
            if not req.marker.evaluate():
                continue

        dep_name = req.name.lower().replace("_", "-")
        installed_ver = installed_versions.get(dep_name)
        # Brand-new dep not currently installed
        if installed_ver is None:
            problematic.append(f"{dep_name} (missing)")
            continue

        # Check if installed version satisfies the constraint
        spec_str = str(req.specifier) if req.specifier else ""
        if spec_str and not satisfies_constraint(installed_ver, spec_str):
            # Check if upgrading this dep is possible
            dep_info = outdated.get(dep_name)
            if dep_info:
                dep_latest = dep_info[1]
                if satisfies_constraint(dep_latest, spec_str):
                    # Check if upgrading this dep is safe
                    safe, _ = can_upgrade_safely(dep_name, dep_latest, reverse_deps, dep_specs)
                    if safe:
                        continue
            problematic.append(f"{dep_name} (installed {installed_ver} fails {dep_name}{spec_str})")

    if problematic:
        return False, f"new version deps conflict: {'; '.join(problematic)}"

    return True, "all new deps satisfied"


def parse_version_spec(spec: str) -> list[tuple[str, str]]:
    """Parse version constraint string. Returns [(operator, version), ...]."""
    specs = []
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    for part in parts:
        m = re.match(r"^(>=|>|<=|<|==|!=|~=)\s*(.+)$", part)
        if m:
            specs.append((m.group(1), m.group(2).strip()))
    return specs


def compute_version_intersection(all_specs: list[str]) -> str:
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


def get_installed_versions() -> dict[str, str]:
    """Get all installed package versions."""
    result = run_pip("list", "--format=json")
    if result.returncode != 0:
        return {}
    return {p["name"].lower().replace("_", "-"): p["version"] for p in json.loads(result.stdout)}


def parallel_install(packages: list[str], max_workers: int = 4) -> tuple[list[str], list[str]]:
    """Install packages in parallel. Returns (succeeded, failed)."""
    succeeded: list[str] = []
    failed: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(install, pkg): pkg for pkg in packages}
        for future in as_completed(futures):
            pkg = futures[future]
            try:
                result = future.result()
                if result.returncode == 0:
                    print(f"    [OK] {pkg}")
                    succeeded.append(pkg)
                else:
                    print(f"    [!] {pkg}: {result.stderr.strip()[:80]}")
                    failed.append(pkg)
            except Exception as e:
                print(f"    [!] {pkg} error: {e}")
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
            import contextlib

            with contextlib.suppress(ValueError):
                pids.append(int(parts[1]))
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        count += 1
    return count


def install(*pkg_specs: str) -> subprocess.CompletedProcess[str]:
    """Install/upgrade/downgrade packages with process-kill retry on Windows."""
    result = run_pip("install", "-U", *pkg_specs)
    if result.returncode != 0 and ("WinError 5" in result.stderr or "拒绝访问" in result.stderr):
        pkg_name = pkg_specs[0].split("==")[0].split(">=")[0].split("<=")[0].strip()
        killed = kill_processes_by_name(pkg_name)
        if killed:
            print(f"[*] Killed {killed} {pkg_name} process(es), retrying")
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


def analyze_shared_dep_intersection(
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
        return {}, {}

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
    args = parse_args()
    dry_run = args["dry_run"]

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
    reverse_deps, dep_specs = build_dep_constraints_from_pipdeptree()

    # 4) 中庸策略：逐个检查所有过时包
    print("[3/4] Analyzing upgradability...", flush=True)
    to_upgrade: list[str] = []
    skipped_unsafe: list[str] = []

    # 获取已安装版本用于 PyPI 依赖验证
    installed_versions = get_installed_versions()

    for pkg in sorted(outdated.keys()):
        current, latest = outdated[pkg]
        can_upgrade, reason = can_upgrade_safely(
            pkg,
            latest,
            reverse_deps,
            dep_specs,
        )
        if can_upgrade:
            # 额外验证：检查目标版本的新依赖是否冲突
            deps_ok, deps_reason = check_new_version_deps(
                pkg, latest, installed_versions,
                reverse_deps, dep_specs, outdated,
            )
            if deps_ok:
                to_upgrade.append(pkg)
            else:
                skipped_unsafe.append(f"{pkg} ({current} -> {latest}: {deps_reason})")
        else:
            skipped_unsafe.append(f"{pkg} ({current} -> {latest}: {reason})")

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
        print(f"\nRemoved {len(blocked_pkgs)} (shared dep conflicts): {', '.join(sorted(blocked_pkgs))}")

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
    succeeded, failed = parallel_install(to_upgrade)

    # 7) 循环 pip check + 修复（最多 3 轮）
    max_rounds = 3
    for round_num in range(1, max_rounds + 1):
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

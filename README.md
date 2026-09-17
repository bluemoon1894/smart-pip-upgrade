# Smart Pip Upgrade

English | [中文](#中文)

A safe, dependency-aware pip batch upgrade helper.

Tired of `pip install --upgrade` breaking your environment because a transitive
dependency got bumped too far? This script checks every outdated package against
**all** dependent packages' version constraints, then upgrades only when it is
truly safe.

## Features

- Checks **only top-level packages** (dependency-graph roots) for updates by default, or
  explicit names passed on the CLI — far cheaper than `pip list --outdated`, which queries
  every installed package (e.g. 77 instead of 317 on this box)
- Builds the full dependency graph locally with `pipdeptree --json-tree`
- Queries the index in parallel (`pip index versions`), respecting pip's mirror/proxy
  config, and skips local/editable installs so a same-named PyPI package can't hijack them
- Collects every dependent's constraint ahead of the pre-flight — dependents that pin an
  exact version (e.g. `kimi-cli`) are what plain `pip` silently breaks
- Upgrades only if the latest version satisfies all constraints; otherwise skips
  with an explicit reason
- Analyzes shared-dependency intersections (`SpecifierSet`, so `~=` / `!=` are
  handled) to detect real conflicts and block the offending upgraders
- **Pre-flight resolution**: resolves the whole batch with
  `pip install --dry-run --report <tempfile>` and validates the plan against every
  installed package's requirements — closing the hole where pip ignores
  already-installed pinners — then auto-prunes unsafe targets (up to 2 rounds)
- Windows file-lock handling: on `WinError 5` it kills the locking process and retries
- Terse by default: the plan line shows versions (`pkg old -> new`); skipped and
  shared-dep sections are compacted to a few lines; `--verbose`/`-v` expands the full
  per-package and per-constraint detail, and the run ends with a one-line summary of
  what was actually upgraded / failed / skipped
- Live progress: every long step shows motion — counters while checking/analyzing, the
  batch install streams pip's output with elapsed tags (per-package
  Collecting/Downloading/Uninstalling noise collapsed to a heart-beat unless
  `--verbose`), and the pre-flight shows a ticking timer, so no step ever looks hung
- Concurrency-safe cleanup: `~*` leftover dirs are left alone whenever another pip
  upgrade is running, so it never deletes an in-flight staging directory
- Post-upgrade: runs `pip check` and auto-fixes issues (max 3 rounds); if conflicts
  remain, prints deduplicated manual-fix suggestions
- `--dry-run` mode to preview all decisions without installing anything

## Usage

```bash
# Preview what would be upgraded or skipped (runs the resolver pre-flight too)
python scripts/pip_smart_upgrade.py --dry-run

# Actually upgrade the safe packages
python scripts/pip_smart_upgrade.py

# Verbose: full per-package versions and per-constraint detail
python scripts/pip_smart_upgrade.py -v

# Only the named packages (bypasses the top-level default)
python scripts/pip_smart_upgrade.py requests ruff
```

## Requirements

- Python 3.11+
- pip
- pipdeptree (auto-installed if missing)
- packaging (auto-installed if missing)

## How it works

1. `pipdeptree --json-tree` extracts the full dependency graph (local — no PyPI queries)
   and yields the top-level roots
2. Query each target's latest version in parallel (`pip index versions`); targets are the
   roots by default, or the explicit names you pass
3. For each candidate, verify **every** dependent's constraint, including non-outdated
   exact pinners; unsafe candidates are skipped with a reason
4. Analyze shared dependencies across the remaining upgrade set; detect contradictions
   and block the packages responsible
5. Pre-flight: resolve the whole batch with `pip install --dry-run --report` and check
   the plan against all installed packages; prune targets whose upgrade would break
   something, then re-resolve
6. Batch install the survivors, streaming pip output with elapsed tags; per-package
   noise is collapsed unless `--verbose` (Windows lock kill-and-retry)
7. Run `pip check`; auto-fix remaining conflicts for up to 3 rounds, then report manual
   suggestions if anything is still broken

> Note: the pre-flight report is written to a temporary file rather than stdout.
> pip 26 renders stdout reports via `rich`, which raises `UnicodeEncodeError` on
> non-GBK characters under a legacy Windows console.

## Warning

This script is a helper, not a guarantee. Always run `--dry-run` first, and make sure
you have backups or virtual environments for critical systems.

## 中文

一个安全的、考虑依赖关系的 pip 批量升级助手。

厌倦了 `pip install --upgrade` 因为某个间接依赖版本过高而破坏环境？这个脚本会检查
每个过时包是否满足**所有**依赖方的版本约束，只有在真正安全时才执行升级。

### 功能

- 默认**只检查顶层包**（依赖图的根），或命令行显式点名的包——比 `pip list --outdated`
  扫描每个已装包省得多（本机 77 个 vs 317 个）
- 用 `pipdeptree --json-tree` 在本地解析完整依赖图
- 用 `pip index versions` **并行**查版本，尊重 pip 的镜像/代理配置；自动跳过本地/
  editable 安装，避免被 PyPI 同名包误判升级
- 预演前收集**全部**依赖方的约束——正是 `kimi-cli` 这类精确锁定会被裸 `pip` 悄悄破坏
- 只有最新版本满足所有约束时才升级，否则跳过并给出明确原因
- 分析共享依赖的版本交集（基于 `SpecifierSet`，正确处理 `~=` / `!=`），检测真矛盾
  并拦下肇事包
- **预演解析（Pre-flight）**：用 `pip install --dry-run --report <临时文件>` 解析整批，
  再对照全部已装包的约束校验——补上 pip 不约束"已装但未参与本次升级"包的漏洞——
  并自动剔除不安全的目标（最多 2 轮）
- Windows 文件占用处理：遇 `WinError 5` 时结束占用进程并重试
- 默认精简：计划/跳过/共享依赖各压成几行；`--verbose`/`-v` 展开逐包、逐约束明细，
  运行结束用一行总结实际升级/失败/跳过了哪些包
- 实时进度：每个长步骤都有动静——查版本/分析时是计数器，批量安装流式打印 pip 输出
  并带耗时秒标，预演有跳秒计时，任何步骤都不会像卡死
- 并发安全清理：只要有其他 pip 升级在运行，就完全不碰 `~*` 残留目录，绝不删除
  在途 staging
- 升级后运行 `pip check` 并自动修复（最多 3 轮）；仍有冲突时输出去重的手动修复建议
- `--dry-run` 模式预览全部决策，不实际安装

### 使用方法

```bash
# 预览哪些会被升级、哪些被跳过（同时跑解析器预演）
python scripts/pip_smart_upgrade.py --dry-run

# 执行安全升级
python scripts/pip_smart_upgrade.py

# 详细模式：展开逐包版本与逐约束明细
python scripts/pip_smart_upgrade.py -v

# 只升级点名的包（绕开默认的顶层过滤）
python scripts/pip_smart_upgrade.py requests ruff
```

### 依赖要求

- Python 3.11+
- pip
- pipdeptree（缺失时自动安装）
- packaging（缺失时自动安装）

### 工作原理

1. 用 `pipdeptree --json-tree` 解析完整依赖图（本地，不查 PyPI），得到顶层根节点
2. 并行查询各目标的最新版本（`pip index versions`）；目标默认为顶层包，或你点名的包
3. 对每个候选包验证**所有**依赖方约束（含未过时的精确锁定方），不安全则跳过并说明
4. 分析剩余升级集内共享依赖的版本交集，检测矛盾并拦下肇事包
5. 预演：用 `pip install --dry-run --report` 解析整批，对照全部已装包校验，剔除会破坏
   环境的目标后重新解析
6. 批量安装幸存者，实时流式打印 pip 输出并带耗时秒标（含 Windows 占用进程结束并重试）
7. 运行 `pip check`，自动修复冲突最多 3 轮；仍损坏则给出可直接执行的手动修复建议

> 说明：预演报告写入临时文件而非 stdout——pip 26 用 `rich` 渲染 stdout 报告，
> 在 legacy Windows 控制台遇非 GBK 字符会抛 `UnicodeEncodeError`。

### 警告

此脚本只是辅助工具，无法保证 100% 安全。请务必先用 `--dry-run` 测试，并确保重要
环境有备份或使用虚拟环境。

## License / 许可

Apache-2.0

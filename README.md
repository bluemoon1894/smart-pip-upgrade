# Smart Pip Upgrade

English | [中文](#中文)

A safe, dependency-aware pip batch upgrade helper.

Tired of `pip install --upgrade` breaking your environment because a transitive
dependency got bumped too far? This script checks every outdated package against
**all** dependent packages' version constraints, then upgrades only when it is
truly safe.

## Features

- Scans every outdated package with `pip list --outdated` (transitive deps included)
- Builds the full dependency graph locally with `pipdeptree --json-tree`
- For each candidate, collects **every** dependent's constraint — including dependents
  that are *not* outdated but pin an exact version (e.g. `kimi-cli`), which plain
  `pip` would happily break
- Upgrades only if the latest version satisfies all constraints; otherwise skips
  with an explicit reason
- Analyzes shared-dependency intersections (`SpecifierSet`, so `~=` / `!=` are
  handled) to detect real conflicts and block the offending upgraders
- **Pre-flight resolution**: resolves the whole batch with
  `pip install --dry-run --report <tempfile>` and validates the plan against every
  installed package's requirements — closing the hole where pip ignores
  already-installed pinners — then auto-prunes unsafe targets (up to 2 rounds)
- Windows file-lock handling: on `WinError 5` it kills the locking process and retries
- Post-upgrade: runs `pip check` and auto-fixes issues (max 3 rounds); if conflicts
  remain, prints deduplicated manual-fix suggestions
- `--dry-run` mode to preview all decisions without installing anything

## Usage

```bash
# Preview what would be upgraded or skipped (runs the resolver pre-flight too)
python scripts/pip_smart_upgrade.py --dry-run

# Actually upgrade the safe packages
python scripts/pip_smart_upgrade.py
```

## Requirements

- Python 3.11+
- pip
- pipdeptree (auto-installed if missing)
- packaging (auto-installed if missing)

## How it works

1. `pip list --outdated` gets candidate packages
2. `pipdeptree --json-tree` extracts the full dependency graph (constraints are read
   locally — no PyPI queries at this stage)
3. For each candidate, verify **every** dependent's constraint, including non-outdated
   exact pinners; unsafe candidates are skipped with a reason
4. Analyze shared dependencies across the remaining upgrade set; detect contradictions
   and block the packages responsible
5. Pre-flight: resolve the whole batch with `pip install --dry-run --report` and check
   the plan against all installed packages; prune targets whose upgrade would break
   something, then re-resolve
6. Batch install the survivors (with Windows lock kill-and-retry)
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

- 用 `pip list --outdated` 扫描所有过时包（含间接依赖）
- 用 `pipdeptree --json-tree` 在本地解析完整依赖图
- 对每个候选包收集**全部**依赖方的约束，包括**未过时但精确锁定**版本的依赖方
  （如 `kimi-cli`）——这类锁定正是裸 `pip` 会直接破坏的
- 只有最新版本满足所有约束时才升级，否则跳过并给出明确原因
- 分析共享依赖的版本交集（基于 `SpecifierSet`，正确处理 `~=` / `!=`），检测真矛盾
  并拦下肇事包
- **预演解析（Pre-flight）**：用 `pip install --dry-run --report <临时文件>` 解析整批，
  再对照全部已装包的约束校验——补上 pip 不约束"已装但未参与本次升级"包的漏洞——
  并自动剔除不安全的目标（最多 2 轮）
- Windows 文件占用处理：遇 `WinError 5` 时结束占用进程并重试
- 升级后运行 `pip check` 并自动修复（最多 3 轮）；仍有冲突时输出去重的手动修复建议
- `--dry-run` 模式预览全部决策，不实际安装

### 使用方法

```bash
# 预览哪些会被升级、哪些被跳过（同时跑解析器预演）
python scripts/pip_smart_upgrade.py --dry-run

# 执行安全升级
python scripts/pip_smart_upgrade.py
```

### 依赖要求

- Python 3.11+
- pip
- pipdeptree（缺失时自动安装）
- packaging（缺失时自动安装）

### 工作原理

1. 用 `pip list --outdated` 获取候选包
2. 用 `pipdeptree --json-tree` 解析完整依赖图（约束本地读取，此阶段不查 PyPI）
3. 对每个候选包验证**所有**依赖方约束（含未过时的精确锁定方），不安全则跳过并说明
4. 分析剩余升级集内共享依赖的版本交集，检测矛盾并拦下肇事包
5. 预演：用 `pip install --dry-run --report` 解析整批，对照全部已装包校验，剔除会破坏
   环境的目标后重新解析
6. 批量安装幸存者（含 Windows 占用进程结束并重试）
7. 运行 `pip check`，自动修复冲突最多 3 轮；仍损坏则给出可直接执行的手动修复建议

> 说明：预演报告写入临时文件而非 stdout——pip 26 用 `rich` 渲染 stdout 报告，
> 在 legacy Windows 控制台遇非 GBK 字符会抛 `UnicodeEncodeError`。

### 警告

此脚本只是辅助工具，无法保证 100% 安全。请务必先用 `--dry-run` 测试，并确保重要
环境有备份或使用虚拟环境。

## License / 许可

Apache-2.0

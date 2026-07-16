# Smart Pip Upgrade

English | 中文

## English

A safe, dependency-aware pip upgrade helper.

Tired of `pip install --upgrade` breaking your environment because a transitive dependency got bumped too far? This script checks every outdated package against all dependent packages' version constraints, then only upgrades when it's truly safe.

### Features

- Scans all outdated packages (including transitive dependencies)
- For each candidate, collects every dependent's version requirement
- Upgrades only if the latest version satisfies all constraints
- Detects shared-dependency conflicts between packages
- Post-upgrade: runs `pip check` and auto-fixes issues (max 3 rounds)
- Dry-run mode to preview decisions first

### Usage

```bash
# Preview what would be upgraded or skipped
python update_outdated_top_level.py --dry-run

# Actually upgrade safe packages
python update_outdated_top_level.py
```

### Requirements

- Python 3.12+
- pip
- pipdeptree
- packaging

### How it works

1. `pip list --outdated` gets candidate packages
2. `pipdeptree --json-tree` extracts the full dependency graph
3. For each candidate, verify every dependent's constraint
4. If safe, upgrade; otherwise skip and explain why
5. After upgrade, run `pip check` and try to resolve any remaining conflicts

### Warning

This script is a helper, not a guarantee. Always run `--dry-run` first, and make sure you have backups or virtual environments for critical systems.

## 中文

一个安全的、考虑依赖关系的 pip 批量升级助手（懒人）。

厌倦了 `pip install --upgrade` 因为某个间接依赖版本过高而破坏环境？这个脚本会检查每个过时包是否满足所有依赖方的版本约束，只有在真正安全时才执行升级。

### 功能

- 扫描所有过时包（包括间接依赖）
- 对每个候选包，收集所有依赖方的版本要求
- 只有最新版本满足所有约束时才升级
- 检测共享依赖冲突
- 升级后运行 `pip check` 并自动修复（最多 3 轮）
- 支持 dry-run 模式预览结果

### 使用方法

```bash
# 预览哪些会被升级、哪些会被跳过
python update_outdated_top_level.py --dry-run

# 执行安全的升级
python update_outdated_top_level.py
```

### 依赖要求

- Python 3.12+
- pip
- pipdeptree
- packaging

### 工作原理

1. 用 `pip list --outdated` 获取候选包
2. 用 `pipdeptree --json-tree` 解析完整依赖图
3. 对每个候选包，验证所有依赖方的约束
4. 安全则升级，否则跳过并说明原因
5. 升级后运行 `pip check` 并尝试修复冲突

### 警告

此脚本只是辅助工具，无法保证 100% 安全。请务必先用 `--dry-run` 测试，并确保重要环境有备份或使用虚拟环境。

## License / 许可

MIT

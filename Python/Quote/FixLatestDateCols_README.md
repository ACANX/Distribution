# FixLatestDateCols — Date/Time 列修复工具

## 用途

修复 `Latest.mvsv` 及归档文件中 Date/Time 列相关的数据一致性问题。

### 可修复的问题

| 问题 | 示例 | 原因 |
|---|---|---|
| **6 列行（缺 Date/Time）** | `1780959660\|4320\|0\|0\|-0.00\|-0.15` | 旧版聚合产生的数据 |
| **异常列宽（如 10 列）** | `...\|20260609\|065900\|20260609\|065900\|4320\|...` | 旧版去重逻辑未逐行检查，重复添加 Date/Time |
| **元数据与数据不匹配** | `# 字段 : ts\|c\|v\|t\|r\|cp` 但数据有 8 列 | 数据已修复但元数据未更新 |

## 用法

### 1. 确认当前分支

```bash
git branch
```

确保在需要修复的分支上（通常是 `quote`）：

```bash
git checkout quote
```

### 2. 运行修复

**仅修复 Latest.mvsv（默认）：**

```bash
python3 Python/Quote/FixLatestDateCols.py
```

**包括归档文件：**

```bash
python3 Python/Quote/FixLatestDateCols.py --all
```

输出示例：

```
  ✅ GDX/Latest.mvsv: 修复 390 行
  ✅ GLD/Latest.mvsv: 修复 390 行
  ✅ IAU/Latest.mvsv: 修复 390 行

完成: 检查 20 个文件，修复 3 个文件共 1170 行
```

### 3. 查看变更

```bash
git status --short
```

确认只有需要修复的文件被修改。

### 4. 提交并推送

```bash
git add -A
git commit -m "[quote] fix Date/Time column inconsistencies"
git push origin quote
```

> 如果远程分支有新的提交导致推送被拒，使用 `--force`：
> ```bash
> git push --force origin quote
> ```

## 安全机制

| 机制 | 说明 |
|---|---|
| **逐行检查** | 每行独立判断，正常行不改动 |
| **修复后校验** | 自动检查修复后行列数一致、元数据匹配 |
| **原子写** | tmp + rename，中断不产生半成品文件 |

## 适用场景

| 场景 | 说明 |
|---|---|
| 首次升级后 | 从旧版升级到新版 Date/Time 格式后运行一次 |
| 版本回退后再升级 | 回退后重新升级时，存量数据需要修复 |
| 日常巡检 | 定期运行 `--all` 检查归档文件完整性 |


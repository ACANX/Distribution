# Latest.mvsv 列宽修复工具

## 用途

修复 `Latest.mvsv` 中 6 列与 8 列行混排的数据不一致问题。

### 背景

从旧版聚合（无 Date/Time 列）升级到新版后，部分 Latest.mvsv 存在列数不一致的记录：

```
# 正常 8 列行
1780959540|20260609|065900|4320.15|0|0|0.23|10.12
# 异常 6 列行（缺失 Date/Time）
1780959660|4320|0|0|-0.00|-0.15
```

此工具自动扫描所有 Latest.mvsv，对 6 列的行按 ts（UTC 秒级时间戳）计算北京时间日期和时间，补全为 8 列。

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

```bash
python3 Python/Quote/RepairMixedCols.py
```

输出示例：

```
PAXG: 修复 7517 行
GDX: 修复 1950 行
IAU: 修复 1950 行
GCMain: 无需修复

完成: 共修复 11417 行
```

### 3. 查看变更

```bash
git status --short
```

确认只有 `Latest.mvsv` 文件被修改。

### 4. 提交并推送

```bash
git add Data/Finv/SecuQuote/*/Latest.mvsv
git commit -m "[quote] repair mixed column rows in Latest.mvsv"
git push origin quote
```

> 如果远程分支有新的提交导致推送被拒，使用 `--force`：
> ```bash
> git push --force origin quote
> ```

## 安全机制

| 机制 | 说明 |
|---|---|
| **列数检测** | 只修复 6 列行，8 列行保持不变（幂等） |
| **原子写** | tmp + rename，中断不产生半成品文件 |
| **仅读 Latest** | 不修改原始采集文件、日归档、月归档 |

## 适用场景

| 场景 | 说明 |
|---|---|
| 首次升级后 | 从旧版聚合升级到新版 Date/Time 格式后运行一次 |
| 版本回退后再升级 | 回退后重新升级时，存量数据需要修复 |
| CI 检测到异常 | 可通过 GitHub Actions 手动触发此脚本 |

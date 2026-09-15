#!/bin/bash
set -e

BRANCH="$1"
N="$2"
EXCLUDED_AUTHOR="$3"
COMMIT_MESSAGE="$4"

# 如果分支未指定，则使用当前分支
if [ -z "$BRANCH" ]; then
    BRANCH=$(git rev-parse --abbrev-ref HEAD)
    echo "No branch specified, using current branch: $BRANCH"
fi

# 检查工作区是否干净（避免 rebase 失败）
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Error: Working directory has uncommitted changes. Please commit or stash them first."
    exit 1
fi

# 检查分支是否存在
if ! git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    echo "Error: Branch '$BRANCH' does not exist."
    exit 1
fi

# 检查必要参数
if [ -z "$N" ] || [ -z "$EXCLUDED_AUTHOR" ] || [ -z "$COMMIT_MESSAGE" ]; then
    echo "Usage: $0 [<branch>] <N> <excluded_author> <commit_message>"
    echo "  If <branch> is omitted, current branch is used."
    exit 1
fi

git checkout "$BRANCH"

# 收集最近 N 个提交的哈希和作者邮箱（从新到旧）
mapfile -t commits < <(git log -n "$N" --format="%H" "$BRANCH")
mapfile -t authors < <(git log -n "$N" --format="%ae" "$BRANCH")

len=${#commits[@]}
if [ $len -eq 0 ]; then
    echo "No commits found in the last $N."
    exit 0
fi

# 寻找最长连续不包含排除作者的段（索引从新到旧）
best_start=0
best_len=0
curr_start=0
curr_len=0

for ((i=0; i<len; i++)); do
    if [[ "${authors[i]}" == "$EXCLUDED_AUTHOR" ]]; then
        if [ $curr_len -gt $best_len ]; then
            best_len=$curr_len
            best_start=$curr_start
        fi
        curr_len=0
    else
        if [ $curr_len -eq 0 ]; then
            curr_start=$i
        fi
        ((curr_len++))
    fi
done
if [ $curr_len -gt $best_len ]; then
    best_len=$curr_len
    best_start=$curr_start
fi

if [ $best_len -le 1 ]; then
    echo "No segment with >1 commit without excluded author. Nothing to squash."
    exit 0
fi

echo "Found longest segment: $best_len commits starting at index $best_start."

# 段内最旧（OLDEST）和最新（NEWEST）提交（NEWEST 更靠近 HEAD）
oldest_idx=$((best_start + best_len - 1))
OLDEST=${commits[$oldest_idx]}
NEWEST=${commits[$best_start]}

# 检查 OLDEST 是否有父提交（若为根提交则无法 rebase）
parent=$(git rev-parse "$OLDEST"^ 2>/dev/null) || true
if [ -z "$parent" ]; then
    echo "Error: $OLDEST is root commit. Cannot rebase. Exiting."
    exit 1
fi

# 导出变量供序列编辑器脚本使用
export OLDEST_HASH="$OLDEST"
export BEST_LEN="$best_len"

# 创建非交互式 rebase 序列编辑器：将段内除第一个外的提交标记为 squash
cat > /tmp/edit_rebase.sh << 'EOF'
#!/bin/bash
TODO_FILE="$1"

LINE_NUM=$(grep -n "^pick $OLDEST_HASH " "$TODO_FILE" | cut -d: -f1)
if [ -z "$LINE_NUM" ]; then
    echo "Error: $OLDEST_HASH not found in todo list" >&2
    exit 1
fi

for ((i=LINE_NUM+1; i<LINE_NUM+BEST_LEN; i++)); do
    if sed -n "${i}p" "$TODO_FILE" | grep -q "^pick "; then
        sed -i "${i}s/^pick /squash /" "$TODO_FILE"
    else
        echo "Warning: line $i is not a pick line, skipping" >&2
    fi
done
EOF
chmod +x /tmp/edit_rebase.sh

export GIT_SEQUENCE_EDITOR=/tmp/edit_rebase.sh
export GIT_EDITOR=true   # 避免弹出合并信息编辑器（稍后 amend）

# 执行变基
if git rebase -i "$OLDEST"^ ; then
    echo "Rebase succeeded."
    # 覆盖合并提交的 message
    git commit --amend -m "$COMMIT_MESSAGE"
    echo "Amended commit message."
else
    echo "Rebase failed. Please resolve conflicts manually." >&2
    exit 1
fi

# 强制推送（若失败则打印错误并退出）
if git push --force origin "$BRANCH"; then
    echo "Force push succeeded."
else
    echo "Force push failed." >&2
    exit 1
fi

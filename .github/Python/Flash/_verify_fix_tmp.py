# -*- coding: utf-8 -*-
"""临时验证脚本: 修复后 JSON 的数据完整性检查(不写回)。
从 _fix_flashfutu_tmp 复用 fix_json_text, 生成修复文本后验证。"""
import glob
import json
import os

from _fix_flashfutu_tmp import fix_json_text

d = "Data/Finv/News/FlashFutu/2026"
REQUIRED = {"id", "time", "news_type", "url", "content", "title", "dt"}

allok = True
for p in sorted(glob.glob(os.path.join(d, "*.json"))):
    name = os.path.basename(p)
    with open(p, encoding="utf-8") as f:
        text = f.read()
    fixed_text, _ = fix_json_text(text)
    try:
        data = json.loads(fixed_text)
    except Exception as e:  # noqa: BLE001
        print("[%s] 修复后解析失败: %s" % (name, e))
        allok = False
        continue
    if not isinstance(data, list):
        print("[%s] 非数组" % name)
        allok = False
        continue
    ids = []
    field_issues = 0
    for it in data:
        if not isinstance(it, dict):
            field_issues += 1
            continue
        ids.append(it.get("id"))
        if not REQUIRED.issubset(it.keys()):
            field_issues += 1
    dup = len(ids) - len(set(ids))
    ok = (field_issues == 0) and (dup == 0)
    allok = allok and ok
    status = "OK" if ok else "FIELD_ISSUES=%d DUP=%d" % (field_issues, dup)
    print("[%s] %s 元素=%d 唯一id=%d %s"
          % (name, status, len(data), len(set(ids)),
             "字段全/无重复" if ok else ""))

print("\n===== 数据完整性:", "全部通过" if allok else "存在问题", "=====")

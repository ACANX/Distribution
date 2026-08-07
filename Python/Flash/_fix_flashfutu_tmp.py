# -*- coding: utf-8 -*-
"""临时修复脚本: 状态机修复 FlashFutu JSON 字符串内未转义引号与控制字符。

背景: Data/Finv/News/FlashFutu/<year>/ 下部分 JSON 文件因拼接存在语法错误,
常见两类:
    1. 字符串内部出现未转义的 ASCII 半角引号(本应为中文全角引号),
       导致 json.load 报 "Expecting ',' delimiter";
    2. 字符串内部出现字面换行/制表符等控制字符,
       导致 json.load 报 "Invalid control character"。

本脚本用状态机逐字符扫描, 只修复字符串内部的问题:
    - 内容引号 -> 补反斜杠转义 (\" )
    - 字面 \n / \r / \t -> 转义为 \\n / \\r / \\t

先用 --verify(默认) 只验证不写回; 通过后再用 --apply 写回原文件。

用法:
    python3 _fix_flashfutu_tmp.py [--apply] [--dir <目录>]
"""
import argparse
import glob
import json
import os

END_CHARS = set(",}]:")

BACKSLASH = "\\"


def fix_json_text(text):
    """状态机修复: 仅处理 JSON 字符串内部的未转义引号与字面控制字符。"""
    out = []
    i, n = 0, len(text)
    in_string = False
    fixed = 0
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
        else:
            if ch == BACKSLASH:
                out.append(ch)
                i += 1
                if i < n:
                    out.append(text[i])
                    i += 1
                continue
            if ch == '"':
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                end = False
                if j < n:
                    nxt = text[j]
                    if nxt in "}]:":  # 后随 } ] : 直接视为结构结束
                        end = True
                    elif nxt == ",":  # 后随逗号: 需再判断 , 之后是否为键/对象
                        k = j + 1
                        while k < n and text[k] in " \t\n\r":
                            k += 1
                        if k < n and text[k] in '"}':
                            end = True
                if end:
                    in_string = False  # 真正的字符串结束符
                    out.append(ch)
                    i += 1
                else:
                    out.append(BACKSLASH)  # 内容引号 -> 转义
                    out.append(ch)
                    fixed += 1
                    i += 1
            elif ch in "\n\r\t":
                out.append(BACKSLASH + {"\n": "n", "\r": "r", "\t": "t"}[ch])
                fixed += 1
                i += 1
            else:
                out.append(ch)
                i += 1
    return "".join(out), fixed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="写回原文件(默认只验证不写回)")
    parser.add_argument("--dir", default="Data/Finv/News/FlashFutu/2026")
    args = parser.parse_args()

    bad = []
    for p in sorted(glob.glob(os.path.join(args.dir, "*.json"))):
        with open(p, encoding="utf-8") as f:
            try:
                json.load(f)
            except ValueError:
                bad.append(p)

    print("待修复文件: %d" % len(bad))
    allok = True
    for p in bad:
        name = os.path.basename(p)
        with open(p, encoding="utf-8") as f:
            text = f.read()
        fixed_text, nfix = fix_json_text(text)
        try:
            data = json.loads(fixed_text)
            ok = isinstance(data, list) and len(data) > 0
            msg = "OK  元素数=%d, 修复点=%d" % (len(data), nfix)
        except Exception as e:  # noqa: BLE001
            ok = False
            msg = "FAIL %s" % str(e)[:90]
        allok = allok and ok
        print("[%s] %s" % (name, msg))
        if args.apply and ok:
            with open(p, "w", encoding="utf-8") as f:
                f.write(fixed_text)

    print("\n===== 全部可修复:", "是" if allok else "否", "=====")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())

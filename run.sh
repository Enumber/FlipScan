#!/bin/bash
# Copyright (C) 2026 ENum
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY; see the GNU General
# Public License (LICENSE) for details.

# 高拍仪自动拍照 / 试卷分析 启动脚本（相对路径版）
BASE="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE"
if [ -f "$BASE/gemini_env/bin/activate" ]; then
    source "$BASE/gemini_env/bin/activate"
fi
if [ "$1" = "--capture" ]; then
    shift
    exec python "$BASE/flipscan.py" "$@"
fi
exec python "$BASE/analyze_papers.py" "$@"

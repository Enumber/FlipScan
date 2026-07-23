#!/usr/bin/env python3
# Copyright (C) 2026 ENum
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY; see the GNU General
# Public License (LICENSE) for details.

"""
试卷分析工具 - 支持图片和视频
使用方法:
  分析图片: python analyze_papers.py --images 试卷1.jpg 试卷2.jpg
  分析视频: python analyze_papers.py --video 录像.mp4
"""

import os
import sys
import time
import argparse
import google.generativeai as genai

# ===== 在这里填入你的 API Key =====
API_KEY = "在这里粘贴你的API_KEY"
# ==================================

PROMPT = """
请仔细分析这份试卷，提供以下内容：
1. 题目列表（题型和题目内容）
2. 涉及的知识点
3. 重点和难点
4. 常见易错点
请用中文回答，条理清晰。
"""

def setup():
    if API_KEY == "在这里粘贴你的API_KEY":
        print("❌ 请先在脚本中填入你的 API Key！")
        print("   获取地址: https://aistudio.google.com → Get API key")
        sys.exit(1)
    genai.configure(api_key=API_KEY)

def analyze_images(image_paths):
    model = genai.GenerativeModel("gemini-2.5-flash")
    for path in image_paths:
        if not os.path.exists(path):
            print(f"⚠️  文件不存在: {path}")
            continue
        print(f"\n📄 正在分析: {path}")
        with open(path, "rb") as f:
            image_data = f.read()
        ext = path.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}.get(ext, "image/jpeg")
        response = model.generate_content([
            {"mime_type": mime, "data": image_data},
            PROMPT
        ])
        print(response.text)
        print("\n" + "="*60)
        time.sleep(2)

def analyze_video(video_path):
    if not os.path.exists(video_path):
        print(f"❌ 文件不存在: {video_path}")
        sys.exit(1)
    print(f"⬆️  正在上传视频: {video_path}（可能需要一点时间）")
    video_file = genai.upload_file(video_path)
    print("⏳ 等待视频处理...")
    while video_file.state.name == "PROCESSING":
        time.sleep(3)
        video_file = genai.get_file(video_file.name)
    if video_file.state.name == "FAILED":
        print("❌ 视频处理失败")
        sys.exit(1)
    print("✅ 视频处理完成，开始分析...")
    model = genai.GenerativeModel("gemini-2.5-flash")
    response = model.generate_content([video_file, PROMPT])
    print("\n📊 分析结果：")
    print(response.text)

def main():
    parser = argparse.ArgumentParser(description="试卷AI分析工具")
    parser.add_argument("--images", nargs="+", help="图片路径（支持多张）")
    parser.add_argument("--video", help="视频路径")
    args = parser.parse_args()

    if not args.images and not args.video:
        parser.print_help()
        sys.exit(1)

    setup()

    if args.images:
        analyze_images(args.images)
    elif args.video:
        analyze_video(args.video)

if __name__ == "__main__":
    main()

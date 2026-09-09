# -*- coding: utf-8 -*-
"""
set_password.py - 一鍵更新 GitHub Pages 看盤密碼
使用方式：
    python set_password.py 你的新密碼
例如：
    python set_password.py mytrade888
"""
import sys
import os
import hashlib
import base64
import subprocess
import re

def main():
    if len(sys.argv) < 2:
        print("請提供要設定的密碼，例如：")
        print("    python set_password.py 123456")
        return

    new_pwd = sys.argv[1].strip()
    pwd_hash = hashlib.sha256(new_pwd.encode("utf-8")).hexdigest()
    pwd_b64 = base64.b64encode(new_pwd.encode("utf-8")).decode("utf-8")
    print(f"🔒 新密碼: {new_pwd}")
    print(f"🔑 計算 SHA-256: {pwd_hash}")
    print(f"🔑 計算 Base64: {pwd_b64}")

    html_file = "index.html"
    if not os.path.exists(html_file):
        print(f"❌ 找不到 {html_file}")
        return

    with open(html_file, "r", encoding="utf-8") as f:
        content = f.read()

    # 替換 const PWD_HASH 與 const PWD_B64
    content = re.sub(r'const PWD_HASH = "[^"]+";', f'const PWD_HASH = "{pwd_hash}";', content)
    content = re.sub(r'const PWD_B64 = "[^"]+";', f'const PWD_B64 = "{pwd_b64}";', content)

    with open(html_file, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"✅ 已成功更新 {html_file} 中的密碼金鑰！")

    # 自動提交並推送到 GitHub
    print("🚀 正在推送到 GitHub...")
    try:
        subprocess.run(["git", "add", "index.html"], check=True)
        subprocess.run(["git", "commit", "-m", f"Update dashboard password hash"], check=True)
        subprocess.run(["git", "push"], check=True)
        print("🎉 密碼已成功更新並推送到 GitHub Pages！約 1 分鐘後即可用新密碼登入。")
    except Exception as e:
        print(f"⚠️ Git 推送失敗: {e}，請手動執行 git push")

if __name__ == "__main__":
    main()

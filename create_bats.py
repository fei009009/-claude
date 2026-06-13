"""Generate V2.0 desktop BAT files (3 files, UTF-8 no BOM)"""
import os

DESKTOP = os.path.join(os.environ["USERPROFILE"], "Desktop")
PY = r"C:\Program Files\Python312\python.exe"
DIR = r"D:\ZTFHQ\分仓之神V2.0"

bats = {
    "V2_控制台.bat": f'''@echo off
chcp 65001 >nul
cd /d "{DIR}"
"{PY}" console.py
pause
''',

    "V2_仪表盘.bat": f'''@echo off
chcp 65001 >nul
cd /d "{DIR}"
echo http://127.0.0.1:8766
"{PY}" main.py dashboard
pause
''',

    "V2_一键分析.bat": f'''@echo off
chcp 65001 >nul
cd /d "{DIR}"
echo ============================================
echo   FenCangZhiShen V2.0
echo ============================================
echo.
"{PY}" main.py run --push
echo.
echo Done.
pause
''',
}

for name, content in bats.items():
    path = os.path.join(DESKTOP, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"OK: {name}")

print(f"\n{len(bats)} BAT files on Desktop.")

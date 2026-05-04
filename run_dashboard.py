import os
import sys
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
#dashboard_path = os.path.join(BASE_DIR, "app", "dashboard.py")
dashboard_path = os.path.join(BASE_DIR, "app", "dashboard_clean.py")
subprocess.run(
    [sys.executable, "-m", "streamlit", "run", dashboard_path],
    check=True
)
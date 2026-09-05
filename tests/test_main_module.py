import subprocess
import sys


def test_python_dash_m_entry_point():
    proc = subprocess.run(
        [sys.executable, "-m", "evtxtoelk", "--version"], capture_output=True, text=True, check=True
    )
    assert proc.stdout.startswith("evtxtoelk 2.")

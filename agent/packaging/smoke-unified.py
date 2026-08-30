"""Run only on disposable Windows CI: no inventory/network scan/install."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

exe = Path(sys.argv[1]).resolve()
temporary = Path(tempfile.mkdtemp(prefix="cmdb-unified-smoke-"))
environment = os.environ.copy()
environment["ProgramData"] = str(temporary)
environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
flags = subprocess.CREATE_NO_WINDOW


def command(*args):
    return subprocess.run([str(exe), *args], capture_output=True, timeout=60,
                          creationflags=flags, env=environment)


version = command("--version")
assert version.returncode == 0 and b"0.5.9" in version.stdout, (version.returncode, version.stdout, version.stderr)
status = command("status", "--json")
assert status.returncode == 0, status.stderr
assert isinstance(json.loads(status.stdout), dict)
invalid = command("--unknown-option")
assert invalid.returncode != 0 and invalid.stderr, "CLI errors/exit codes must survive windowed packaging"
worker = command("run")  # no configuration: must fail, not open a GUI or contact a server
assert worker.returncode != 0 and worker.stderr

tray = subprocess.Popen([str(exe)], env=environment)
try:
    time.sleep(12)
    assert tray.poll() is None, "Tray exited"
    # Enumerate the bootloader AND Python child (same executable path).
    def processes():
        output = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command",
            "@(Get-Process -Name cmdb-agent -ErrorAction SilentlyContinue | "
            "Select-Object Id,@{Name='Visible';Expression={$_.MainWindowHandle -ne 0}}) | ConvertTo-Json -Compress"], creationflags=flags)
        return json.loads(output or b"[]")
    before = {row["Id"] for row in processes()}
    assert before and all(not row["Visible"] for row in processes())
    duplicate = command("gui")
    assert duplicate.returncode == 0, duplicate.stderr
    assert {row["Id"] for row in processes()} == before, "Second tray instance remained alive"
    # The same EXE remains usable as a worker while the tray holds it open.
    assert command("--version").returncode == 0
    old = exe.with_suffix(".exe.old")
    replacement = exe.with_suffix(".exe.new")
    shutil.copy2(exe, replacement)
    os.utime(replacement, (time.time() + 5, time.time() + 5))
    exe.rename(old)
    replacement.rename(exe)
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline and tray.poll() is None:
        time.sleep(1)
    assert tray.poll() == 0, "Old tray did not exit after updating the EXE"
    time.sleep(8)
    after = processes()
    assert after and {row["Id"] for row in after}.isdisjoint(before), "New tray not started"
    assert all(not row["Visible"] for row in after)
    assert command("--version").returncode == 0
    print("Unified EXE: CLI pipes, errors, worker, silent tray, singleton and hot replacement OK")
finally:
    subprocess.run(["powershell.exe", "-NoProfile", "-Command",
        "Get-Process -Name cmdb-agent -ErrorAction SilentlyContinue | Stop-Process -Force"], creationflags=flags)

# python3
# auto_installer.py
# v.0.3
# currently using for code test, API 3.4 Indigo when released should do this automatically and can remove this code

import subprocess
from pathlib import Path
import sys
import os, sys, subprocess, pathlib


try:
    import indigo
except:
    pass

from pathlib import Path
import os
import sys
import time
import subprocess
import importlib

def install_requirements_manual():
    """
    Installs packages from requirements_manual.txt into ../Packages using pip,
    with macOS SDK sysroot injected so native builds (e.g. netifaces) can compile.
    """

    current_directory = Path.cwd()              # .../Contents/Server Plugin
    parent_directory  = current_directory.parent  # .../Contents
    pip_path = f"/Library/Frameworks/Python.framework/Versions/{sys.version_info.major}.{sys.version_info.minor}/bin/pip{sys.version_info.major}.{sys.version_info.minor}"
    requirements_file = current_directory / "requirements_manual.txt"
    install_dir       = parent_directory / "Packages"
    success_log  = install_dir / "pip-install-success.txt"

    if success_log.exists():
        indigo.server.log(f"Libraries already installed (found {success_log}). Skipping reinstall.")
        return f"Skipped install: {success_log} exist"

    installation_output = f"Installing requirements Libraries into '{install_dir}'\n"

    indigo.server.log("Processing requirements_manual.txt (manual Library install).")
    indigo.server.log(installation_output)
    indigo.server.log("... Please Wait ....")
    # If we've already installed successfully, do nothing

    if not requirements_file.exists():
        indigo.server.log(f"requirements_manual.txt not found: {requirements_file}")
        sys.exit(1)

    install_dir.mkdir(parents=True, exist_ok=True)

    # Inject sysroot so clang can find stdlib.h when Indigo/launchd env is minimal
    try:
        sdk = subprocess.check_output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
            text=True
        ).strip()
    except Exception:
        sdk = "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"

    env = os.environ.copy()
    env["SDKROOT"] = sdk
    env.setdefault("DEVELOPER_DIR", "/Library/Developer/CommandLineTools")
    env["CFLAGS"]   = (f"-isysroot {sdk} " + env.get("CFLAGS", "")).strip()

    try:
        result = subprocess.run(
            [
                pip_path, "install",
                "-r", str(requirements_file),
                "--upgrade",
                "-t", str(install_dir),
                "--disable-pip-version-check"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=False,
        )

        out_bytes = result.stdout or b""
        pip_out = out_bytes.decode("utf-8", errors="replace")

        indigo.server.log(f"pip return code: {result.returncode}", level=10)
        indigo.server.log("--- pip output (tail) ---", level=10)
        indigo.server.log(pip_out[-3000:] if len(pip_out) > 3000 else pip_out, level=10)

        installation_output =  "\n--- pip output ---\n" + pip_out + f"\n--- pip return code: {result.returncode} ---\n"

        if result.returncode != 0:
            indigo.server.log("ERROR: pip install failed for requirements_manual.txt")
            return installation_output

        try:
            success_log.write_text(installation_output, encoding="utf-8")
            indigo.server.log(f"Wrote install log: {success_log}")
            installation_output += f"\nWrote install log: {success_log}\n"
        except Exception as e:
            indigo.server.log(f"WARNING: install succeeded but could not write success log: {e}")
            installation_output += f"\nWARNING: install succeeded but could not write success log: {e}\n"


        indigo.server.log("Library install completed successfully.")
        # Invalidate import caches so the next run sees new files
        importlib.invalidate_caches()
        time.sleep(5)
        return installation_output

    except FileNotFoundError as e:
        error_message = f"File not found error: {e}"
        indigo.server.log(error_message)
        sys.exit(1)
    except Exception as e:
        msg = str(e).encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        indigo.server.log(f"An unexpected error occurred: {msg}")
        return f"\nERROR: {msg}\n"

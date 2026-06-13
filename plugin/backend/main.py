import os
import platform
import subprocess
import threading

import Millennium  # provided by the Millennium runtime


def _paths():
    """Repo clone + Millennium quickcss path, per OS.
    NOTE: Windows paths are best-effort and need verifying on a real Windows
    install (Millennium location can vary)."""
    if platform.system() == "Windows":
        repo = os.path.join(os.environ.get("USERPROFILE", ""), "discord-ish-steam")
        quickcss = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Millennium", "quickcss.css")
    else:
        repo = os.path.expanduser("~/steam-reskin")
        quickcss = os.path.expanduser("~/.config/millennium/quickcss.css")
    return repo, quickcss


def _update():
    repo, quickcss = _paths()
    try:
        subprocess.run(["git", "-C", repo, "pull", "--ff-only"],
                       timeout=25, capture_output=True)
    except Exception:
        pass
    try:
        with open(os.path.join(repo, "theme", "friends.custom.css")) as f:
            css = f.read()
        os.makedirs(os.path.dirname(quickcss), exist_ok=True)
        with open(quickcss, "w") as f:
            f.write("/* Quick CSS file created by Millennium */\n"
                    "/* discord-ish (auto-updated on boot) */\n" + css)
    except Exception:
        pass


class Plugin:
    def _front_end_loaded(self):
        pass

    def _load(self):
        # background thread so a slow git pull never delays Steam startup
        threading.Thread(target=_update, daemon=True).start()
        Millennium.ready()

    def _unload(self):
        pass

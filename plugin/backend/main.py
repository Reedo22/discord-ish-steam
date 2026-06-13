import os
import subprocess
import threading

import Millennium  # provided by the Millennium runtime

# Linux dev paths (Windows would need its own paths — see README v2 notes).
REPO = os.path.expanduser("~/steam-reskin")
QUICKCSS = os.path.expanduser("~/.config/millennium/quickcss.css")


def _update():
    # pull latest (fast-forward only; ignore failures e.g. offline / local edits)
    try:
        subprocess.run(["git", "-C", REPO, "pull", "--ff-only"],
                       timeout=25, capture_output=True)
    except Exception:
        pass
    # refresh quickcss from the repo theme (applies on next Steam start)
    try:
        with open(os.path.join(REPO, "theme", "friends.custom.css")) as f:
            css = f.read()
        with open(QUICKCSS, "w") as f:
            f.write("/* Quick CSS file created by Millennium */\n"
                    "/* discord-ish (auto-updated on boot) */\n" + css)
    except Exception:
        pass


class Plugin:
    def _front_end_loaded(self):
        pass

    def _load(self):
        # run in background so a slow git pull never delays Steam startup
        threading.Thread(target=_update, daemon=True).start()
        Millennium.ready()

    def _unload(self):
        pass

import os
import shutil

import Millennium  # provided by the Millennium runtime

PLUGIN = "DiscordishChat"
# Use fixed, known paths — Millennium's runtime sets __file__ oddly, so deriving
# paths from os.path.dirname(__file__) is unreliable here.
STEAMUI = os.path.expanduser("~/.local/share/Steam/steamui")
SRC = os.path.expanduser(
    "~/.local/share/millennium/plugins/discordish-chat/webkit/discordish.js"
)


def _deploy():
    """Copy our plain webkit JS into steamui so Millennium can inject it."""
    dst_dir = os.path.join(STEAMUI, PLUGIN)
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy(SRC, os.path.join(dst_dir, "discordish.js"))


class Plugin:
    def _front_end_loaded(self):
        _deploy()

    def _load(self):
        _deploy()
        # inject into Steam's webkit/browser pages (incl. the friends popup, we hope)
        Millennium.add_browser_js(f"{PLUGIN}/discordish.js")
        Millennium.ready()

    def _unload(self):
        pass

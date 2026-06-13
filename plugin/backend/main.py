import os
import shutil

import Millennium  # provided by the Millennium runtime

PLUGIN = "DiscordishChat"
# Steam path is stable on this install; avoids depending on an uncertain API name.
STEAMUI = os.path.expanduser("~/.local/share/Steam/steamui")


def _deploy():
    """Copy our plain webkit JS into steamui so Millennium can inject it."""
    src = os.path.join(os.path.dirname(__file__), "..", "webkit", "discordish.js")
    dst_dir = os.path.join(STEAMUI, PLUGIN)
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy(src, os.path.join(dst_dir, "discordish.js"))


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

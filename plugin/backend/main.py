import Millennium  # provided by the Millennium runtime


class Plugin:
    def _front_end_loaded(self):
        pass

    def _load(self):
        # All UI work happens in public/discordish.js, injected via the
        # plugin.json "include" field. Backend just signals readiness.
        Millennium.ready()

    def _unload(self):
        pass

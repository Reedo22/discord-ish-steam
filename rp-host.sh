#!/usr/bin/env bash
# Tiny long-running host so Steam has a "game" to anchor a Remote Play Together
# group while we stream the desktop. No window needed (desktop streaming captures
# the screen, not this process). Added as a non-Steam shortcut by the plugin.
exec sleep 1000000

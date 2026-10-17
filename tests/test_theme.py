"""The reskin must stay themeable: every colour in theme/friends.custom.css comes from a
--dc-* token, so a theme (Appearance panel / custom CSS) only overrides variables."""
import pathlib
import re
import unittest

CSS = (pathlib.Path(__file__).resolve().parent.parent / "theme" / "friends.custom.css").read_text()
COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


class ThemeTokens(unittest.TestCase):
    def test_colours_only_in_token_block(self):
        tokens = re.search(r":root\s*\{[^}]*--dc-sidebar[^}]*\}", CSS)
        self.assertIsNotNone(tokens, "token :root block missing")
        rest = CSS[:tokens.start()] + CSS[tokens.end():]
        bad = []
        for n, line in enumerate(rest.splitlines(), 1):
            code = re.sub(r"/\*.*?\*/", "", line)
            if COLOR.search(code):
                bad.append(f"{n}: {line.strip()}")
        self.assertEqual(bad, [], "raw colours outside the :root tokens (use var(--dc-...))")

    def test_every_used_token_is_defined(self):
        defined = set(re.findall(r"(--dc-[\w-]+)\s*:", CSS))
        used = set(re.findall(r"var\((--dc-[\w-]+)", CSS))
        self.assertEqual(used - defined, set())


if __name__ == "__main__":
    unittest.main()

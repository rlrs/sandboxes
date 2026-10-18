import hashlib
from html.parser import HTMLParser
from importlib.resources import files
import re
import shutil
import subprocess
import unittest

from sandboxes.dashboard import dashboard_asset

DASHBOARD_HTML = dashboard_asset("/dashboard").body.decode("utf-8")
DASHBOARD_JS = dashboard_asset("/dashboard/dashboard.js").body.decode("utf-8")


class _DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.tablists: list[dict[str, str | None]] = []
        self.tabs: list[dict[str, str | None]] = []
        self.panels: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.ids.append(element_id)
        if values.get("role") == "tablist":
            self.tablists.append(values)
        if values.get("role") == "tab":
            self.tabs.append(values)
        if values.get("role") == "tabpanel" and element_id:
            self.panels[element_id] = values


class DashboardTests(unittest.TestCase):
    def test_dashboard_ids_and_tab_relationships_are_valid(self) -> None:
        parser = _DashboardParser()
        parser.feed(DASHBOARD_HTML)

        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertEqual(len(parser.tablists), 1)
        self.assertGreater(len(parser.tabs), 0)
        self.assertEqual(
            sum(tab.get("aria-selected") == "true" for tab in parser.tabs),
            1,
        )
        for tab in parser.tabs:
            target = tab.get("aria-controls")
            self.assertIn(target, parser.panels)
            self.assertEqual(
                parser.panels[str(target)].get("aria-labelledby"),
                tab.get("id"),
            )

    def test_every_bootstrapped_element_id_exists(self) -> None:
        parser = _DashboardParser()
        parser.feed(DASHBOARD_HTML)
        match = re.search(
            r"for \(const id of \[(.*?)\]\) \{\s*els\[id\]",
            DASHBOARD_JS,
            re.DOTALL,
        )

        self.assertIsNotNone(match)
        boot_ids = re.findall(r'"([^"]+)"', match.group(1) if match else "")
        self.assertTrue(boot_ids)
        self.assertEqual(set(boot_ids) - set(parser.ids), set())

    def test_dashboard_javascript_parses(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        script = files("sandboxes.dashboard").joinpath("dashboard.js")
        completed = subprocess.run(
            [node, "--check", str(script)],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_served_assets_are_byte_identical_to_the_inline_strings(self) -> None:
        # SHA-256 of the bytes served before the assets moved out of
        # dashboard.py. Update these only for an intended dashboard change.
        expected = {
            "/": ("baf08a290628ab99682a4250f99043248c4236644838d49dd446b563c9ee8f6e", "text/html; charset=utf-8"),
            "/dashboard": ("baf08a290628ab99682a4250f99043248c4236644838d49dd446b563c9ee8f6e", "text/html; charset=utf-8"),
            "/dashboard/": ("baf08a290628ab99682a4250f99043248c4236644838d49dd446b563c9ee8f6e", "text/html; charset=utf-8"),
            "/dashboard/dashboard.css": ("ed109eb5c92abad90c8b5bd8deb3535359dfbd7e7381bc37238c832782200a6f", "text/css; charset=utf-8"),
            "/dashboard/dashboard.js": ("56bdf6843ba8b3ca681a4dca5a17a33747336bf0afd3faad7ce75571e3205966", "application/javascript; charset=utf-8"),
        }
        for path, (digest, content_type) in expected.items():
            with self.subTest(path=path):
                asset = dashboard_asset(path)
                self.assertIsNotNone(asset)
                assert asset is not None
                self.assertEqual(hashlib.sha256(asset.body).hexdigest(), digest)
                self.assertEqual(asset.content_type, content_type)
        self.assertIsNone(dashboard_asset("/dashboard/index.html"))

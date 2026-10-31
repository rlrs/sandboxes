import importlib.util
import json
import unittest
from pathlib import Path

GRAFANA = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "grafana"


def generated() -> dict:
    spec = importlib.util.spec_from_file_location("generate_dashboard", GRAFANA / "generate_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


class DashboardTests(unittest.TestCase):
    def test_committed_dashboard_is_the_generated_one(self) -> None:
        committed = json.loads((GRAFANA / "dashboards" / "sandboxes.json").read_text())

        self.assertEqual(committed, generated())

    def test_controls_only_call_the_ops_proxy(self) -> None:
        def panels(items):
            for panel in items:
                yield panel
                yield from panels(panel.get("panels", []))

        urls = [action["fetch"]["url"] for panel in panels(generated()["panels"])
                for action in panel.get("fieldConfig", {}).get("defaults", {}).get("actions", [])]

        self.assertEqual(len(urls), 6)
        # Grafana prefixes its sub-path (/grafana) to an action's URL.
        self.assertTrue(all(url.startswith("/ops/v1/") for url in urls), urls)


if __name__ == "__main__":
    unittest.main()

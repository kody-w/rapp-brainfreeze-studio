"""Offline tests for from-catalog / catalog-check: what is refused before anything runs, and that a red
gauntlet stops the pipeline before build and deploy."""
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brainfreeze_studio import catalog  # noqa: E402


class CatalogTests(unittest.TestCase):
    def test_refuses_plain_http(self):
        with self.assertRaises(catalog.CatalogError):
            catalog._get("http://example.com/x.egg")

    def test_rar_eggs_lists_only_eggs(self):
        tree = {"tree": [{"path": "stacks/a/a.egg"}, {"path": "agents/@x/y_agent.py"}, {"path": "b.egg"}]}
        fake = unittest.mock.MagicMock()
        fake.__enter__.return_value = io.BytesIO(json.dumps(tree).encode())
        with unittest.mock.patch("urllib.request.urlopen", return_value=fake):
            self.assertEqual(catalog.rar_eggs(), [catalog.RAR_RAW + "stacks/a/a.egg", catalog.RAR_RAW + "b.egg"])

    def test_a_red_gauntlet_builds_and_deploys_nothing(self):
        red = {"egg": "https://x/y.egg", "green": False, "steps": [{"step": "verify rapp/1", "ok": False}]}
        with tempfile.TemporaryDirectory() as out, \
                unittest.mock.patch.object(catalog, "gauntlet", return_value=red), \
                unittest.mock.patch.object(catalog, "_run") as run:
            with self.assertRaises(catalog.CatalogError) as e:
                catalog.ship("https://x/y.egg", "https://org.crm.dynamics.com/", out)
            self.assertIn("nothing was built or deployed", str(e.exception))
            run.assert_not_called()
            self.assertFalse((Path(out) / "build").exists())

    def test_gauntlet_is_red_for_something_that_is_not_an_egg(self):
        with tempfile.TemporaryDirectory() as out, \
                unittest.mock.patch.object(catalog, "_get", return_value=b"class NotAnEgg: pass\n"):
            rep = catalog.gauntlet("https://x/not.egg", out)
        self.assertFalse(rep["green"])
        self.assertEqual([s["step"] for s in rep["steps"]], ["fetch", "verify rapp/1"])


if __name__ == "__main__":
    unittest.main()

"""Fast checks for serialization and the user-only installer boundaries."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


renderer = load_module("preview_renderer", ROOT / "mdpreview/renderer.py")
manager = load_module("preview_manager", ROOT / "manage.py")


class RenderContractTests(unittest.TestCase):
    def test_document_round_trips_as_json_without_code_interpolation(self) -> None:
        source = '</script>"; window.injected = true; //\n\u2028\u2029\\x00'
        options = {"baseUri": "file:///tmp/a%20directory/", "name": "quoted\".md"}
        script = renderer.document_script(source, options)
        payload = script.removeprefix("window.mdPreview.render(").removesuffix(");void 0;")
        parsed_source, end = json.JSONDecoder().raw_decode(payload)
        self.assertEqual(parsed_source, source)
        self.assertEqual(json.loads(payload[end + 1:]), options)

    def test_limit_counts_encoded_bytes(self) -> None:
        with patch.object(renderer, "MAX_DOCUMENT_BYTES", 5):
            renderer.document_script("12345", {})
            with self.assertRaisesRegex(ValueError, "up to 4 MB"):
                renderer.document_script("123456", {})
            with self.assertRaises(ValueError):
                renderer.document_script("\u2603\u2603", {})

    def test_shell_has_local_assets_and_restricts_active_resources(self) -> None:
        shell = renderer.preview_html("dark")
        self.assertIn('data-theme="dark"', shell)
        self.assertIn("connect-src &#x27;none&#x27;", shell)
        self.assertIn("frame-src &#x27;none&#x27;", shell)
        self.assertNotIn("https://", shell)
        self.assertLess(shell.index("vendor.js"), shell.index("preview.js"))
        with self.assertRaises(ValueError):
            renderer.preview_html('\" onload=alert(1)')


class InstallerContractTests(unittest.TestCase):
    def test_install_update_uninstall_preserves_other_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            destination = data / "gedit/plugins"
            destination.mkdir(parents=True)
            unrelated = destination / "other.plugin"
            unrelated.write_text("owner data", encoding="utf-8")
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}):
                self.assertEqual(manager.install(), 0)
                self.assertTrue(manager.owned_manifest(destination / "mdpreview.plugin"))
                self.assertEqual(manager.install(), 0)
                self.assertEqual(manager.uninstall(), 0)
                self.assertEqual(unrelated.read_text(encoding="utf-8"), "owner data")

    def test_foreign_installation_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            (destination / "mdpreview").mkdir()
            with self.assertRaisesRegex(RuntimeError, "unrecognized"):
                manager.check_destination(destination)
            manifest = destination / "mdpreview.plugin"
            manifest.write_text("[Plugin]\nModule=mdpreview\n", encoding="utf-8")
            self.assertFalse(manager.owned_manifest(manifest))
            manifest.write_text(manager.MARKER + "\n[Plugin]\nModule=other\n", encoding="utf-8")
            self.assertFalse(manager.owned_manifest(manifest))

    def test_symlinked_plugin_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            (destination / "mdpreview.plugin").write_text((ROOT / "mdpreview.plugin").read_text(), encoding="utf-8")
            (destination / "mdpreview").symlink_to(ROOT / "mdpreview", target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                manager.check_destination(destination)

    def test_every_python_function_has_type_annotations(self) -> None:
        import ast
        for path in [ROOT / "manage.py", *sorted((ROOT / "mdpreview").glob("*.py"))]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertIsNotNone(node.returns, f"{path.name}:{node.name} return")
                    for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                        if argument.arg not in {"self", "cls"}:
                            self.assertIsNotNone(argument.annotation, f"{path.name}:{node.name}:{argument.arg}")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Build, diagnose, and install the current-user gedit Markdown preview plugin."""

from __future__ import annotations

import argparse
import configparser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any


PROJECT = Path(__file__).resolve().parent
MARKER = "# Managed by gedit-md-preview-plugin"
MODULE = "mdpreview"
DEPENDENCIES = {
    "mermaid": "11.17.2",
    "markdown-it": "14.1.0",
    "markdown-it-task-lists": "2.1.1",
    "dompurify": "3.4.17",
    "highlight.js": "11.11.1",
    "esbuild": "0.25.11",
}
ASSETS = ("preview.css", "preview.js", "vendor.js", "THIRD_PARTY_LICENSES.txt")


def plugin_directory() -> Path:
    """Honor XDG_DATA_HOME so installation can also be tested in isolation."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    if not data_home.is_absolute():
        raise RuntimeError("XDG_DATA_HOME must be an absolute path.")
    return data_home / "gedit/plugins"


def owned_manifest(path: Path) -> bool:
    """Recognize only our explicit marker and expected gedit module together."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        contents = path.read_text(encoding="utf-8")
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(contents)
        return MARKER in contents.splitlines() and config.get("Plugin", "Module") == MODULE
    except (OSError, UnicodeError, configparser.Error):
        return False


def check_destination(directory: Path) -> None:
    manifest = directory / "mdpreview.plugin"
    package = directory / MODULE
    if not manifest.exists() and not manifest.is_symlink() and not package.exists() and not package.is_symlink():
        return
    if not owned_manifest(manifest):
        raise RuntimeError(f"Refusing an unrecognized plugin installation: {manifest}")
    if package.is_symlink() or (package.exists() and not package.is_dir()):
        raise RuntimeError(f"Refusing an unsafe plugin package: {package}")
    if package.exists() and any(item.is_symlink() for item in package.rglob("*")):
        raise RuntimeError(f"Refusing a plugin package containing symbolic links: {package}")


def check_sources() -> None:
    if not owned_manifest(PROJECT / "mdpreview.plugin"):
        raise RuntimeError("The source plugin manifest is missing its ownership marker or module.")
    required = [PROJECT / MODULE / "__init__.py", PROJECT / MODULE / "renderer.py"]
    required.extend(PROJECT / MODULE / "assets" / filename for filename in ASSETS)
    missing = [str(path.relative_to(PROJECT)) for path in required if not path.is_file() or not path.stat().st_size]
    if missing:
        raise RuntimeError("Missing or empty project files: " + ", ".join(missing))


def doctor() -> int:
    """Inspect runtime dependencies with Ubuntu's Python, not a virtualenv."""
    problems = 0
    gedit = shutil.which("gedit")
    if gedit:
        result = subprocess.run([gedit, "--version"], capture_output=True, text=True, check=False)
        version_text = (result.stdout + result.stderr).strip()
        match = re.search(r"\b(\d+)\.(\d+)(?:\.\d+)?\b", version_text)
        if result.returncode == 0 and match and 41 <= int(match.group(1)) <= 46:
            print(f"OK: {version_text}")
        else:
            print(f"ERROR: Unsupported gedit version: {version_text or 'unknown'}.")
            print("This Python plugin targets gedit 41–46; gedit 48.2+ removed Python plugin support.")
            problems += 1
    else:
        print("ERROR: gedit was not found. GNOME Text Editor cannot load gedit plugins.")
        problems += 1

    typelib_dirs = [Path("/usr/lib/gedit/girepository-1.0")]
    typelib_dirs.extend(Path("/usr/lib").glob("*/gedit/girepository-1.0"))
    library_dirs = [path.parent for path in typelib_dirs if path.is_dir()]
    env = os.environ.copy()
    for name, paths in (("GI_TYPELIB_PATH", typelib_dirs), ("LD_LIBRARY_PATH", library_dirs)):
        additions = [str(path) for path in paths if path.is_dir()]
        if env.get(name):
            additions.append(env[name])
        env[name] = os.pathsep.join(additions)
    probe = """
import json
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gedit', '3.0')
from gi.repository import Gtk, Gedit
for version in ('4.1', '4.0'):
    try:
        gi.require_version('WebKit2', version)
        from gi.repository import WebKit2
        print(json.dumps({'webkit': version, 'gtk': Gtk.MAJOR_VERSION}))
        break
    except (ValueError, ImportError):
        continue
else:
    raise RuntimeError('WebKit2 4.1 or 4.0 introspection bindings are missing')
"""
    try:
        result = subprocess.run(["/usr/bin/python3", "-c", probe], env=env, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "GI dependency check failed")
        details = json.loads(result.stdout)
        print(f"OK: /usr/bin/python3, GTK {details['gtk']}, Gedit 3.0, WebKit2 {details['webkit']}")
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"ERROR: {error}")
        print("Ubuntu packages: gedit, python3-gi, and gir1.2-webkit2-4.0 (22.04) or gir1.2-webkit2-4.1 (24.04).")
        problems += 1
    try:
        check_sources()
        print("OK: plugin sources and bundled offline preview assets are present.")
    except RuntimeError as error:
        print(f"ERROR: {error}")
        problems += 1
    print(f"Plugin destination: {plugin_directory()}")
    return int(bool(problems))


def install() -> int:
    check_sources()
    destination = plugin_directory()
    destination.mkdir(parents=True, exist_ok=True)
    check_destination(destination)
    with tempfile.TemporaryDirectory(prefix=".mdpreview-install-", dir=destination) as temporary:
        staging = Path(temporary)
        shutil.copytree(PROJECT / MODULE, staging / MODULE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(PROJECT / "mdpreview.plugin", staging / "mdpreview.plugin")
        backed_up: list[str] = []
        installed: list[str] = []
        try:
            for name in (MODULE, "mdpreview.plugin"):
                target = destination / name
                if target.exists():
                    target.rename(staging / (name + ".backup"))
                    backed_up.append(name)
                (staging / name).rename(target)
                installed.append(name)
        except OSError:
            for name in reversed(installed):
                target = destination / name
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            for name in backed_up:
                (staging / (name + ".backup")).rename(destination / name)
            raise
    print(f"Installed Markdown Preview into {destination}")
    print("Restart gedit yourself, then enable Markdown Preview in Preferences → Plugins.")
    print("The installer does not change gedit's active plugins or close any editor windows.")
    return 0


def uninstall() -> int:
    destination = plugin_directory()
    check_destination(destination)
    package = destination / MODULE
    manifest = destination / "mdpreview.plugin"
    if not package.exists() and not manifest.exists():
        print("Markdown Preview is not installed in this user's plugin directory.")
        return 0
    if package.exists():
        shutil.rmtree(package)
    manifest.unlink()
    print("Removed only mdpreview/ and mdpreview.plugin from " + str(destination))
    print("Disable Markdown Preview in gedit Preferences → Plugins, then restart gedit yourself.")
    print("The active plugins setting was left unchanged.")
    return 0


def package_roots(node_modules: Path) -> list[Path]:
    """Collect actual installed packages, including nested/scoped dependencies."""
    packages: list[Path] = []
    for entry in sorted(node_modules.iterdir()):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        candidates = sorted(entry.iterdir()) if entry.name.startswith("@") else [entry]
        for package in candidates:
            if (package / "package.json").is_file():
                packages.append(package)
                nested = package / "node_modules"
                if nested.is_dir():
                    packages.extend(package_roots(nested))
    return packages


def collect_licenses(node_modules: Path) -> str:
    sections = ["Bundled preview dependencies and build dependency notices.\n"]
    for package in package_roots(node_modules):
        metadata: dict[str, Any] = json.loads((package / "package.json").read_text(encoding="utf-8"))
        heading = f"{metadata.get('name', package.name)} {metadata.get('version', 'unknown')}"
        sections.append("\n" + "=" * 72 + "\n" + heading)
        sections.append("Declared license: " + json.dumps(metadata.get("license", metadata.get("licenses", "not declared"))))
        notices = [path for path in sorted(package.iterdir()) if path.is_file() and re.match(r"^(licen[cs]e|copying|notice)([._-].*)?$", path.name, re.IGNORECASE)]
        for notice in notices:
            sections.append(f"\n--- {notice.name} ---\n" + notice.read_text(encoding="utf-8", errors="replace"))
        if not notices:
            sections.append("No separate root license/notice file was included in the installed package.")
    return "\n".join(sections) + "\n"


def build_assets() -> int:
    if not shutil.which("node") or not shutil.which("npm"):
        raise RuntimeError("Building assets requires Node.js and npm. Installed plugin use does not.")
    assets = PROJECT / MODULE / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gedit-mdpreview-build-") as temporary:
        work = Path(temporary)
        (work / "package.json").write_text(json.dumps({"name": "gedit-mdpreview-assets", "version": "1.0.0", "private": True, "dependencies": DEPENDENCIES}, indent=2) + "\n", encoding="utf-8")
        subprocess.run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", "--save-exact"], cwd=work, check=True)
        for name, expected in DEPENDENCIES.items():
            installed = json.loads((work / "node_modules" / name / "package.json").read_text(encoding="utf-8"))
            if installed["version"] != expected:
                raise RuntimeError(f"Unexpected {name} version: {installed['version']} (expected {expected}).")
        (work / "entry.js").write_text("""import markdownit from 'markdown-it';
import taskLists from 'markdown-it-task-lists';
import DOMPurify from 'dompurify';
import hljs from 'highlight.js/lib/common';
import mermaid from 'mermaid';
window.PreviewVendors = { markdownit, taskLists, DOMPurify, hljs, mermaid };
""", encoding="utf-8")
        subprocess.run([
            str(work / "node_modules/.bin/esbuild"), "entry.js", "--bundle", "--format=iife",
            "--platform=browser", "--target=safari15.4", "--minify", "--legal-comments=inline",
            "--outfile=vendor.js", "--metafile=build-meta.json",
        ], cwd=work, check=True)
        licenses = collect_licenses(work / "node_modules")
        shutil.copyfile(work / "vendor.js", assets / "vendor.js")
        (assets / "THIRD_PARTY_LICENSES.txt").write_text(licenses, encoding="utf-8")
    print("Built mdpreview/assets/vendor.js and THIRD_PARTY_LICENSES.txt using pinned dependencies.")
    print("No Node.js runtime, development server, or CDN is needed by the installed plugin.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "install", "uninstall", "build-assets"))
    arguments = parser.parse_args()
    commands = {"doctor": doctor, "install": install, "uninstall": uninstall, "build-assets": build_assets}
    try:
        return commands[arguments.command]()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

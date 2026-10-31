"""Lazy, read-only Markdown preview integration for gedit 41 and 46."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import gi

gi.require_version("Gedit", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gedit, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .renderer import ASSETS, document_script, preview_html

LOGGER = logging.getLogger(__name__)
ACTION = "md-preview"
SHORTCUT = "<Primary><Alt>m"


class MarkdownPreviewApp(GObject.Object, Gedit.AppActivatable):
    """Expose the preview in gedit's normal Tools menu and key bindings."""

    __gtype_name__ = "MarkdownPreviewApp"
    app = GObject.Property(type=Gedit.App)

    def do_activate(self) -> None:
        self._menu = self.extend_menu("tools-section-1")
        self._menu.append_menu_item(Gio.MenuItem.new("Markdown Preview", "win." + ACTION))
        self.app.set_accels_for_action("win." + ACTION, [SHORTCUT])

    def do_deactivate(self) -> None:
        self.app.set_accels_for_action("win." + ACTION, [])
        self._menu.remove_items()
        self._menu = None


class MarkdownPreviewWindow(GObject.Object, Gedit.WindowActivatable):
    """Own the preview split without changing documents or other panels."""

    __gtype_name__ = "MarkdownPreviewWindow"
    window = GObject.Property(type=Gedit.Window)

    def do_activate(self) -> None:
        self._box: Any = None
        self._web: Any = None
        self._manager: Any = None
        self._split: Any = None
        self._outer: Any = None
        self._notebook: Any = None
        self._document: Any = None
        self._view: Any = None
        self._adjustment: Any = None
        self._buffer_handlers: list[tuple[Any, int]] = []
        self._header_handlers: list[tuple[Any, int]] = []
        self._header_document: Any = None
        self._header_button: Any = None
        self._zoom_group: Any = None
        self._zoom_button: Any = None
        self._editor_zoom_styles: dict[Any, Any] = {}
        self._font_handlers: list[tuple[Any, int]] = []
        self._editor_zoom_pending = 0
        self._editor_width: int | None = None
        self._split_gap = 0
        self._split_side = "right"
        self._layout_pending = 0
        self._layout_settling = False
        self._layout_positioned = False
        self._updating_layout = False
        self._side_control: Any = None
        self._full_control: Any = None
        self._dark_control: Any = None
        self._pending = 0
        self._scroll_pending = 0
        self._scroll_unlock = 0
        self._syncing = False
        self._page_ready = False
        self._fallback = False
        self._theme = "dark"
        self._layout = "right"
        self._zoom = 1.0
        self._outline_enabled = False
        self._sync_enabled = True
        self._action = Gio.SimpleAction.new_stateful(ACTION, None, GLib.Variant.new_boolean(False))
        self._action.connect("activate", self._toggle)
        self.window.add_action(self._action)
        self._tab_handler = self.window.connect("active-tab-changed", self._tab_changed)
        self._view_handlers = [self.window.connect(signal, self._queue_editor_zoom)
                               for signal in ("tab-added", "tab-removed")]
        for schema, keys in (
            ("org.gnome.gedit.preferences.editor", ("use-default-font", "editor-font")),
            ("org.gnome.desktop.interface", ("monospace-font-name",)),
        ):
            settings = Gio.Settings.new(schema)
            for key in keys:
                self._font_handlers.append((settings, settings.connect("changed::" + key, self._queue_editor_zoom)))
        self._attach_header_button()
        self._update_header_button()

    def do_deactivate(self) -> None:
        self.close_preview()
        self.window.disconnect(self._tab_handler)
        for handler in self._view_handlers:
            self.window.disconnect(handler)
        self._view_handlers.clear()
        for settings, handler in self._font_handlers:
            settings.disconnect(handler)
        self._font_handlers.clear()
        if self._editor_zoom_pending:
            GLib.source_remove(self._editor_zoom_pending)
            self._editor_zoom_pending = 0
        for view, provider in self._editor_zoom_styles.items():
            view.get_style_context().remove_provider(provider)
        self._editor_zoom_styles.clear()
        self._disconnect_header_document()
        if self._header_button is not None:
            self._header_button.destroy()
            self._header_button = None
        if self._zoom_group is not None:
            self._zoom_group.destroy()
            self._zoom_group = self._zoom_button = None
        self.window.remove_action(ACTION)

    def do_update_state(self) -> None:
        self._update_header_button()
        if self._box is not None:
            self._bind_document()

    def _attach_header_button(self) -> None:
        """Find the native Save action without depending on private widget IDs."""
        titlebar = self.window.get_titlebar()
        pending = [titlebar] if titlebar is not None else []
        while pending:
            widget = pending.pop()
            if isinstance(widget, Gtk.Actionable) and widget.get_action_name() == "win.save":
                header = widget.get_parent()
                if isinstance(header, Gtk.HeaderBar):
                    button = Gtk.ToggleButton(label="Preview")
                    button.set_action_name("win." + ACTION)
                    button.set_tooltip_text("Markdown Preview")
                    button.get_accessible().set_name("Markdown Preview")
                    button.set_no_show_all(True)
                    header.pack_end(button)
                    header.child_set_property(button, "position", header.child_get_property(widget, "position") + 1)
                    self._header_button = button
                    zoom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
                    zoom.get_accessible().set_name("Editor and preview zoom")
                    zoom.pack_start(self._icon_button("zoom-out-symbolic", "Zoom out", lambda _b: self._set_zoom(self._zoom - 0.1)), False, False, 0)
                    self._zoom_button = Gtk.Button(label="100%")
                    self._zoom_button.set_tooltip_text("Reset zoom")
                    self._zoom_button.connect("clicked", lambda _b: self._set_zoom(1.0))
                    zoom.pack_start(self._zoom_button, False, False, 0)
                    zoom.pack_start(self._icon_button("zoom-in-symbolic", "Zoom in", lambda _b: self._set_zoom(self._zoom + 0.1)), False, False, 0)
                    header.pack_end(zoom)
                    header.child_set_property(zoom, "position", header.child_get_property(button, "position") + 1)
                    self._zoom_group = zoom
                    zoom.show_all()
                    return
            if isinstance(widget, Gtk.Container):
                pending.extend(widget.get_children())
        LOGGER.warning("Could not find the Save header button; Markdown Preview remains available in the menu")

    def _disconnect_header_document(self) -> None:
        for obj, handler in self._header_handlers:
            obj.disconnect(handler)
        self._header_handlers.clear()
        self._header_document = None

    def _update_header_button(self, *_args: Any) -> None:
        if self._header_button is None:
            return
        document = self.window.get_active_document()
        if document is not self._header_document:
            self._disconnect_header_document()
            self._header_document = document
            if document is not None:
                for obj, signal in ((document, "notify::language"), (document.get_file(), "notify::location")):
                    self._header_handlers.append((obj, obj.connect(signal, self._update_header_button)))
        markdown = False
        if document is not None:
            location = document.get_file().get_location()
            if location is not None:
                markdown = Path(location.get_basename() or "").suffix.lower() in {".md", ".markdown", ".mdown", ".mkd"}
            else:
                language = document.get_language()
                markdown = language is not None and language.get_id() == "markdown"
        self._header_button.set_visible(markdown)

    def _toggle(self, _action: Any = None, _parameter: Any = None) -> None:
        if self._box is None:
            self.open_preview()
        else:
            self.close_preview()

    def _load_webkit(self) -> Any:
        """Do not load the browser engine during normal gedit startup."""
        for version in ("4.1", "4.0"):
            try:
                gi.require_version("WebKit2", version)
                from gi.repository import WebKit2
                return WebKit2
            except (ValueError, ImportError):
                continue
        raise RuntimeError(
            "WebKit bindings are missing. Install gir1.2-webkit2-4.0 on Ubuntu 22.04 "
            "or gir1.2-webkit2-4.1 on Ubuntu 24.04, then restart gedit."
        )

    def open_preview(self) -> None:
        if self._box is not None:
            return
        self._layout = self._split_side = "right"
        try:
            for filename in ("vendor.js", "preview.js", "preview.css"):
                if not (ASSETS / filename).is_file():
                    raise RuntimeError(f"Preview asset {filename} is missing. Reinstall the complete plugin.")
            self._webkit = self._load_webkit()
            self._build_widgets()
            self._attach_split()
            self._box.show_all()
            self._action.set_state(GLib.Variant.new_boolean(True))
            self._bind_document()
            self._web.load_html(preview_html(self._theme), ASSETS.as_uri() + "/")
        except Exception as error:
            LOGGER.exception("Could not open Markdown Preview")
            self.close_preview()
            dialog = Gtk.MessageDialog(
                transient_for=self.window, modal=True, message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE, text="Markdown Preview could not open",
            )
            dialog.format_secondary_text(str(error))
            dialog.connect("response", lambda widget, _response: widget.destroy())
            dialog.show()

    def _icon_button(self, name: str, label: str, callback: Any, toggle: bool = False) -> Any:
        button = Gtk.ToggleButton() if toggle else Gtk.Button()
        button.set_image(Gtk.Image.new_from_icon_name(name, Gtk.IconSize.MENU))
        button.set_tooltip_text(label)
        button.get_accessible().set_name(label)
        button.connect("toggled" if toggle else "clicked", callback)
        return button

    def _build_widgets(self) -> None:
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._box.set_size_request(280, 180)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for edge in ("top", "bottom", "start", "end"):
            getattr(toolbar, "set_margin_" + edge)(6)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        outline = self._icon_button("view-list-symbolic", "Show document outline", self._change_outline, True)
        outline.set_active(self._outline_enabled)
        controls.pack_start(outline, False, False, 0)
        sync = Gtk.ToggleButton(label="Sync scroll")
        sync.set_active(self._sync_enabled)
        sync.set_tooltip_text("Synchronize relative scroll position with the source")
        sync.connect("toggled", self._change_sync)
        controls.pack_start(sync, False, False, 0)
        toolbar.pack_start(controls, False, False, 0)
        toolbar.pack_end(self._icon_button("window-close-symbolic", "Close preview", lambda _b: self.close_preview()), False, False, 0)
        toolbar.pack_end(self._icon_button("view-refresh-symbolic", "Refresh preview", lambda _b: self._schedule_render()), False, False, 0)
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        separator.set_margin_top(4)
        separator.set_margin_bottom(4)
        toolbar.pack_end(separator, False, False, 4)
        self._full_control = self._icon_button("view-fullscreen-symbolic", "Full View", self._change_layout, True)
        self._full_control.set_active(self._layout == "full")
        toolbar.pack_end(self._full_control, False, False, 0)
        self._dark_control = self._icon_button("weather-clear-night-symbolic", "Dark Mode", self._change_theme, True)
        self._dark_control.set_active(self._theme == "dark")
        if self._theme == "light":
            self._dark_control.get_image().set_from_icon_name("weather-clear-symbolic", Gtk.IconSize.MENU)
        toolbar.pack_end(self._dark_control, False, False, 0)
        self._side_control = self._icon_button("go-previous-symbolic", "Move preview left", self._switch_side)
        toolbar.pack_end(self._side_control, False, False, 0)
        self._box.pack_start(toolbar, False, False, 0)
        manager = self._webkit.UserContentManager()
        self._manager = manager
        manager.register_script_message_handler("preview")
        manager.connect("script-message-received::preview", self._message)
        self._web = self._webkit.WebView.new_with_user_content_manager(manager)
        web_settings = self._web.get_settings()
        web_settings.set_enable_developer_extras(False)
        web_settings.set_enable_html5_database(False)
        web_settings.set_enable_html5_local_storage(False)
        self._web.set_zoom_level(self._zoom)
        self._web.connect("load-changed", self._loaded)
        self._web.connect("load-failed", self._load_failed)
        self._web.connect("decide-policy", self._decide_policy)
        self._web.connect("permission-request", lambda _view, request: request.deny() or True)
        self._web.connect("context-menu", lambda *_args: True)
        self._box.pack_start(self._web, True, True, 0)
        self._status = Gtk.Label(label="Loading preview…")
        self._status.set_xalign(0)
        self._status.set_margin_start(10)
        self._status.set_margin_end(10)
        self._status.set_margin_top(4)
        self._status.set_margin_bottom(6)
        self._status.set_line_wrap(True)
        self._box.pack_end(self._status, False, False, 0)

    def _attach_split(self) -> None:
        bottom = self.window.get_bottom_panel()
        ancestor = bottom.get_parent()
        while ancestor is not None and not isinstance(ancestor, Gtk.Paned):
            ancestor = ancestor.get_parent()
        if (ancestor is None or ancestor.get_child1() is None
                or ancestor.get_child2() is None
                or not bottom.is_ancestor(ancestor.get_child2())):
            self._fallback = True
            bottom.add_titled(self._box, "MarkdownPreview", "Markdown Preview")
            bottom.set_visible_child(self._box)
            bottom.show()
            bottom.get_parent().show()
            self._full_control.set_active(False)
            self._full_control.set_sensitive(False)
            self._side_control.set_sensitive(False)
            return
        self._outer = ancestor
        self._notebook = ancestor.get_child1()
        self._notebook_visible = self._notebook.get_visible()
        self._notebook_no_show_all = self._notebook.get_no_show_all()
        self._packing = tuple(ancestor.child_get_property(self._notebook, name) for name in ("resize", "shrink"))
        self._split = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        ancestor.remove(self._notebook)
        self._split.pack1(self._notebook, True, False)
        self._split.pack2(self._box, True, False)
        ancestor.pack1(self._split, *self._packing)
        self._split.show_all()
        self._apply_layout()

    def _apply_layout(self) -> None:
        if self._split is None:
            return
        self._cancel_layout()
        self._layout_settling = True
        self._layout_positioned = False
        if self._layout == "full":
            self._notebook.set_no_show_all(True)
            self._notebook.hide()
            self._web.grab_focus()
            self._layout_pending = GLib.timeout_add(150, self._finish_layout, self._split)
        else:
            self._notebook.set_no_show_all(self._notebook_no_show_all)
            self._notebook.set_visible(self._notebook_visible)
            self._layout_pending = GLib.idle_add(self._position_split, self._split)

    def _cancel_layout(self) -> None:
        if self._layout_pending:
            GLib.source_remove(self._layout_pending)
            self._layout_pending = 0

    def _position_split(self, split: Any) -> bool:
        if split is not self._split:
            return GLib.SOURCE_REMOVE
        self._layout_pending = 0
        width = split.get_allocated_width()
        if width <= 1:
            self._layout_pending = GLib.timeout_add(16, self._position_split, split)
            return GLib.SOURCE_REMOVE
        position = self._editor_width if self._editor_width is not None else max(180, width // 2)
        if self._split_side == "left":
            position = width - self._split_gap - position
        split.set_position(position)
        self._layout_positioned = True
        self._layout_pending = GLib.timeout_add(150, self._finish_layout, split)
        return GLib.SOURCE_REMOVE

    def _finish_layout(self, split: Any) -> bool:
        if split is not self._split:
            return GLib.SOURCE_REMOVE
        self._layout_pending = 0
        self._layout_settling = False
        return GLib.SOURCE_REMOVE

    def _capture_split(self) -> None:
        # Pre-allocation clicks must not replace content widths with stale geometry.
        if self._layout_settling and not self._layout_positioned:
            return
        if self._layout != "full" and self._notebook.get_mapped():
            if abs(self._split.get_child1().get_allocated_width() - self._split.get_position()) > 2:
                return
            self._editor_width = self._notebook.get_allocated_width()
            self._split_gap = max(0, self._split.get_allocated_width()
                                  - self._editor_width - self._box.get_allocated_width())

    def _pack_split(self, side: str) -> None:
        """Reorder the same widgets, restoring their parents if packing fails."""
        children = (self._split.get_child1(), self._split.get_child2())
        desired = (self._box, self._notebook) if side == "left" else (self._notebook, self._box)
        if children == desired:
            return
        try:
            for child in children:
                self._split.remove(child)
            self._split.pack1(desired[0], True, False)
            self._split.pack2(desired[1], True, False)
        except Exception:
            for child in (self._split.get_child1(), self._split.get_child2()):
                if child is not None:
                    self._split.remove(child)
            self._split.pack1(children[0], True, False)
            self._split.pack2(children[1], True, False)
            raise

    def _update_layout_controls(self) -> None:
        self._updating_layout = True
        try:
            full = self._layout == "full"
            self._full_control.set_active(full)
            self._full_control.get_image().set_from_icon_name(
                "view-restore-symbolic" if full else "view-fullscreen-symbolic", Gtk.IconSize.MENU)
            destination = "left" if self._split_side == "right" else "right"
            label = "Move preview " + destination
            self._side_control.set_tooltip_text(label)
            self._side_control.get_accessible().set_name(label)
            self._side_control.get_image().set_from_icon_name(
                "go-previous-symbolic" if destination == "left" else "go-next-symbolic", Gtk.IconSize.MENU)
        finally:
            self._updating_layout = False

    def _set_layout(self, layout: str) -> None:
        if self._split is None:
            return
        self._capture_split()
        self._cancel_layout()
        self._layout_settling = True
        self._layout_positioned = False
        if self._scroll_pending:
            GLib.source_remove(self._scroll_pending)
            self._scroll_pending = 0
        previous = (self._layout, self._split_side)
        try:
            if layout != "full":
                self._pack_split(layout)
                self._split_side = layout
            self._layout = layout
        except Exception:
            LOGGER.exception("Could not move Markdown Preview; restored the previous split")
            self._layout, self._split_side = previous
        self._update_layout_controls()
        self._apply_layout()

    def _switch_side(self, _button: Any) -> None:
        self._set_layout("left" if self._split_side == "right" else "right")

    def close_preview(self) -> None:
        for attr in ("_pending", "_scroll_pending", "_scroll_unlock", "_layout_pending"):
            source = getattr(self, attr, 0)
            if source:
                GLib.source_remove(source)
                setattr(self, attr, 0)
        self._disconnect_buffer()
        if self._box is not None:
            if self._split is not None:
                self._split.remove(self._notebook)
                self._outer.remove(self._split)
                self._outer.pack1(self._notebook, *self._packing)
                self._notebook.set_no_show_all(self._notebook_no_show_all)
                self._notebook.set_visible(self._notebook_visible)
                self._split.destroy()
            else:
                parent = self._box.get_parent()
                if parent is not None:
                    parent.remove(self._box)
                self._box.destroy()
        self._web = self._box = self._split = self._outer = self._notebook = self._manager = None
        self._full_control = self._dark_control = self._side_control = None
        self._layout_settling = False
        self._layout_positioned = False
        self._page_ready = False
        self._fallback = False
        self._syncing = False
        self._action.set_state(GLib.Variant.new_boolean(False))

    def _disconnect_buffer(self) -> None:
        for obj, handler in self._buffer_handlers:
            obj.disconnect(handler)
        self._buffer_handlers.clear()
        self._document = self._view = self._adjustment = None

    def _bind_document(self) -> None:
        document = self.window.get_active_document()
        if document is self._document and self.window.get_active_view() is self._view:
            return
        self._disconnect_buffer()
        self._document = document
        self._view = self.window.get_active_view()
        if document is not None:
            self._buffer_handlers.append((document, document.connect("changed", lambda *_a: self._schedule_render())))
            source_file = document.get_file()
            self._buffer_handlers.append((source_file, source_file.connect("notify::location", lambda *_a: self._schedule_render())))
        if self._view is not None:
            self._adjustment = self._view.get_vadjustment()
            self._buffer_handlers.append((self._adjustment, self._adjustment.connect("value-changed", self._source_scrolled)))
        self._schedule_render()

    def _tab_changed(self, *_args: Any) -> None:
        self._update_header_button()
        self._queue_editor_zoom()
        if self._box is not None:
            self._bind_document()

    def _queue_editor_zoom(self, *_args: Any) -> None:
        if not self._editor_zoom_pending:
            self._editor_zoom_pending = GLib.idle_add(self._refresh_editor_zoom)

    def _refresh_editor_zoom(self) -> bool:
        self._editor_zoom_pending = 0
        self._apply_editor_zoom()
        return GLib.SOURCE_REMOVE

    def _apply_editor_zoom(self) -> None:
        """Scale each view's current native font without changing preferences."""
        views = self.window.get_views()
        for view in list(self._editor_zoom_styles):
            if view not in views:
                view.get_style_context().remove_provider(self._editor_zoom_styles.pop(view))
        for view in views:
            context = view.get_style_context()
            provider = self._editor_zoom_styles.pop(view, None)
            if provider is not None:
                context.remove_provider(provider)
            if self._zoom == 1.0:
                continue
            # Remove our override first so successive clicks never compound it.
            font = context.get_font(view.get_state_flags()).copy()
            unit = "px" if font.get_size_is_absolute() else "pt"
            size = font.get_size() / Pango.SCALE * self._zoom
            provider = Gtk.CssProvider()
            provider.load_from_data(f"textview {{ font-size: {size:.6f}{unit}; }}".encode("utf-8"))
            context.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
            self._editor_zoom_styles[view] = provider

    def _schedule_render(self) -> None:
        if self._pending:
            GLib.source_remove(self._pending)
        if self._box is not None:
            self._pending = GLib.timeout_add(250, self._render)

    def _render(self) -> bool:
        self._pending = 0
        if self._web is None or not self._page_ready:
            return GLib.SOURCE_REMOVE
        source = ""
        name = "Untitled"
        base_uri = Path.home().as_uri() + "/"
        if self._document is not None:
            start, end = self._document.get_bounds()
            source = self._document.get_text(start, end, True)
            location = self._document.get_file().get_location()
            if location is not None:
                name = location.get_basename()
                parent = location.get_parent()
                if parent is not None:
                    base_uri = parent.get_uri().rstrip("/") + "/"
        options: dict[str, object] = {
            "theme": self._theme, "baseUri": base_uri, "name": name,
            "outline": self._outline_enabled, "sync": self._sync_enabled,
        }
        try:
            script = document_script(source, options)
            self._status.set_text("Rendering…")
            self._javascript(script)
        except ValueError as error:
            self._status.set_text(str(error))
        return GLib.SOURCE_REMOVE

    def _javascript(self, script: str) -> None:
        if self._web is not None and self._page_ready:
            self._web.run_javascript(script, None, self._javascript_finished, None)

    def _javascript_finished(self, web: Any, result: Any, _data: Any) -> None:
        try:
            web.run_javascript_finish(result)
        except GLib.Error as error:
            if self._web is web:
                self._status.set_text("Preview error: " + error.message)
                LOGGER.warning("Preview script failed: %s", error)

    def _loaded(self, _web: Any, event: Any) -> None:
        if _web is not self._web:
            return
        if event == self._webkit.LoadEvent.FINISHED:
            self._page_ready = True
            self._render()

    def _load_failed(self, _web: Any, _event: Any, _uri: str, error: Any) -> bool:
        if _web is self._web:
            self._status.set_text("Preview could not load: " + error.message)
        return True

    def _message(self, _manager: Any, result: Any) -> None:
        if self._box is None or _manager is not self._manager:
            return
        try:
            message = json.loads(result.get_js_value().to_string())
            kind = message.get("kind")
            if kind == "ready":
                diagrams = int(message.get("diagrams", 0))
                errors = int(message.get("errors", 0))
                self._status.set_text(f"Live · {diagrams} diagram{'s' if diagrams != 1 else ''}" + (f" · {errors} diagram error(s)" if errors else ""))
            elif kind == "scroll" and self._sync_enabled and not self._layout_settling and self._adjustment is not None:
                ratio = float(message["ratio"])
                if 0 <= ratio <= 1:
                    self._syncing = True
                    adjustment = self._adjustment
                    adjustment.set_value(adjustment.get_lower() + ratio * max(0, adjustment.get_upper() - adjustment.get_page_size() - adjustment.get_lower()))
                    if self._scroll_unlock:
                        GLib.source_remove(self._scroll_unlock)
                    self._scroll_unlock = GLib.timeout_add(200, self._unlock_scroll)
            elif kind == "copy" and isinstance(message.get("text"), str):
                from gi.repository import Gdk
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(message["text"], -1)
        except (ValueError, TypeError, KeyError, AttributeError):
            LOGGER.warning("Ignored malformed preview message")

    def _source_scrolled(self, _adjustment: Any) -> None:
        if (self._layout != "full" and not self._layout_settling and self._sync_enabled
                and not self._syncing and not self._scroll_pending):
            self._scroll_pending = GLib.timeout_add(60, self._send_scroll)

    def _send_scroll(self) -> bool:
        self._scroll_pending = 0
        if self._adjustment is not None:
            adj = self._adjustment
            span = adj.get_upper() - adj.get_page_size() - adj.get_lower()
            ratio = max(0, min(1, (adj.get_value() - adj.get_lower()) / span)) if span > 0 else 0
            self._javascript(f"window.mdPreview.scrollToRatio({ratio});")
        return GLib.SOURCE_REMOVE

    def _unlock_scroll(self) -> bool:
        self._scroll_unlock = 0
        self._syncing = False
        return GLib.SOURCE_REMOVE

    def _change_theme(self, button: Any) -> None:
        self._theme = "dark" if button.get_active() else "light"
        icon = "weather-clear-night-symbolic" if button.get_active() else "weather-clear-symbolic"
        button.get_image().set_from_icon_name(icon, Gtk.IconSize.MENU)
        self._schedule_render()

    def _change_layout(self, button: Any) -> None:
        if not self._updating_layout:
            self._set_layout("full" if button.get_active() else self._split_side)

    def _change_outline(self, button: Any) -> None:
        self._outline_enabled = button.get_active()
        self._javascript("window.mdPreview.setOutline(" + json.dumps(self._outline_enabled) + ");")

    def _change_sync(self, button: Any) -> None:
        self._sync_enabled = button.get_active()
        self._schedule_render()

    def _set_zoom(self, value: float) -> None:
        self._zoom = round(max(0.6, min(2.0, value)), 1)
        if self._zoom_button is not None:
            self._zoom_button.set_label(f"{round(self._zoom * 100)}%")
        self._apply_editor_zoom()
        if self._web is not None:
            self._web.set_zoom_level(self._zoom)

    def _decide_policy(self, _web: Any, decision: Any, kind: Any) -> bool:
        if kind not in (self._webkit.PolicyDecisionType.NAVIGATION_ACTION, self._webkit.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        navigation = decision.get_navigation_action()
        uri = navigation.get_request().get_uri()
        if not self._page_ready and uri == ASSETS.as_uri() + "/":
            return False
        decision.ignore()
        if not navigation.is_user_gesture():
            return True
        scheme = urlparse(uri).scheme.lower()
        try:
            if scheme == "file" and Path(urlparse(uri).path).suffix.lower() in {".md", ".markdown", ".mdown", ".mkd", ".txt"}:
                self.window.get_application().open([Gio.File.new_for_uri(uri)], "")
            elif scheme in {"https", "http", "mailto"}:
                Gio.AppInfo.launch_default_for_uri(uri, self.window.get_display().get_app_launch_context())
        except GLib.Error as error:
            self._status.set_text("Could not open link: " + error.message)
        return True

#!/usr/bin/env python3
"""Regression tests for Flipscan's settings information architecture."""

import os
import tempfile
import tkinter as tk
import tkinter.messagebox as messagebox
import unittest
from unittest import mock

import flipscan


def visible_texts(widget):
    """Collect text from children currently managed by a layout manager."""
    result = []
    for child in widget.winfo_children():
        # winfo_ismapped() is false for every descendant when the test root is
        # withdrawn, even though the active popup section is correctly packed.
        if child.winfo_manager():
            text = child.cget("text") if "text" in child.keys() else ""
            if text:
                result.append(str(text))
            result.extend(visible_texts(child))
    return result


def widget_with_text(widget, text):
    """Find a managed descendant with an exact text value."""
    for child in widget.winfo_children():
        if not child.winfo_manager():
            continue
        if "text" in child.keys() and child.cget("text") == text:
            return child
        found = widget_with_text(child, text)
        if found is not None:
            return found
    return None


class SettingsLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="flipscan-settings-")
        self.env = mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        self.original_language = flipscan._ZH
        flipscan._ZH = False
        self.root = tk.Tk(className="FlipscanSettingsTest")
        self.root.withdraw()
        self.app = flipscan.App.__new__(flipscan.App)
        self.app.root = self.root
        self.app.cfg = dict(flipscan.CONFIG_DEFAULTS)
        self.app.cam_idx = 999
        self.app.output_dir = flipscan.DEFAULT_DIR
        self.app._cam_picker = None
        self.app._more_dialog = None
        # Settings layout tests must never enumerate or open real camera devices.
        self.app._current_cam_path = lambda: "/dev/video-test"
        self.app._short_path = lambda path: path
        self.app._change_folder = lambda: None
        self.camera_guards = (
            mock.patch.object(
                flipscan, "list_capture_devices",
                side_effect=AssertionError("settings test enumerated cameras")),
            mock.patch.object(
                flipscan.cv2, "VideoCapture",
                side_effect=AssertionError("settings test opened a camera")),
            mock.patch.object(
                flipscan, "device_display_name", return_value="Test camera"),
        )
        for guard in self.camera_guards:
            guard.start()
        self.app._open_more_settings()
        self.root.update_idletasks()

    def tearDown(self):
        try:
            self.root.destroy()
        finally:
            for guard in reversed(self.camera_guards):
                guard.stop()
            flipscan._ZH = self.original_language
            self.env.stop()
            self.tmp.cleanup()

    def _tab(self, label):
        for child in self.app._settings_tab_bar.winfo_children():
            if child.cget("text") == label:
                return child
        self.fail(f"settings tab not found: {label}")

    def test_settings_use_a_separate_singleton_popup(self):
        dialog = self.app._more_dialog
        self.assertIsInstance(dialog, tk.Toplevel)
        self.assertIs(dialog.master, self.root)
        self.assertIs(self.app._settings_content.winfo_toplevel(), dialog)

        self.app._open_more_settings()
        self.assertIs(self.app._more_dialog, dialog)

    def test_settings_use_flipscan_specific_capture_detection_and_other_sections(self):
        self.assertEqual(
            [w.cget("text") for w in self.app._settings_tab_bar.winfo_children()],
            ["Capture & camera", "Advanced page detection", "Other"],
        )

        capture = "\n".join(visible_texts(self.app._settings_content))
        self.assertIn("Capture current frame when starting", capture)
        self.assertIn("Test camera", capture)
        self.assertIn("Remember this camera", capture)
        self.assertNotIn("Flip sensitivity", capture)
        self.assertNotIn("Update automatically", capture)

        self._tab("Advanced page detection").invoke()
        self.root.update_idletasks()
        advanced = "\n".join(visible_texts(self.app._settings_content))
        self.assertIn("Flip sensitivity", advanced)
        self.assertIn("Reset to defaults", advanced)
        self.assertNotIn("Capture current frame when starting", advanced)
        self.assertNotIn("Update automatically", advanced)

        self._tab("Other").invoke()
        self.root.update_idletasks()
        other = "\n".join(visible_texts(self.app._settings_content))
        self.assertIn("Update automatically", other)
        self.assertIn("Check for updates", other)
        self.assertNotIn("Flip sensitivity", other)
        self.assertNotIn("Capture current frame when starting", other)

    def test_settings_use_flipscan_specific_chinese_section_labels(self):
        self.app._close_more_settings(self.app._more_dialog)
        flipscan._ZH = True
        self.app._open_more_settings()
        self.root.update_idletasks()

        self.assertEqual(
            [w.cget("text") for w in self.app._settings_tab_bar.winfo_children()],
            ["拍摄与摄像头", "高级翻页检测", "其他"],
        )

    def test_advanced_reset_restores_all_tuning_defaults(self):
        self._tab("Advanced page detection").invoke()
        reset = widget_with_text(self.app._settings_content, "Reset to defaults")
        self.assertIsNotNone(reset)

        with mock.patch.object(messagebox, "showinfo") as showinfo:
            reset.invoke()

        for key in ("motion_threshold", "settle_frames", "capture_delay"):
            self.assertIsNone(self.app.cfg[key])
        showinfo.assert_called_once()


if __name__ == "__main__":
    unittest.main()

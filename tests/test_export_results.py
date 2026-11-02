"""Source contracts for the bundled exporter, without FEKO or a Lua runtime.

These protect activation order, output arguments and diagnostic sequencing;
native verification is still needed to check rendering and Lua execution.
"""
from pathlib import Path
import re
import unittest


EXPORTER = Path(__file__).resolve().parents[1] / "src/feko_mcp/examples/export_results.lua"


class ExportResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ignore line comments so commented-out operations cannot satisfy checks.
        cls.source = re.sub(r"--[^\n]*", "", EXPORTER.read_text(encoding="utf-8"))

    def test_target_view_is_shown_before_configuration_and_export(self):
        selection = re.search(r"local\s+view\s*=\s*app\.Views\[1\]", self.source)
        self.assertIsNotNone(selection, "Expected the existing target 3D view")
        view_section = self.source[selection.end():]
        first_operation = re.search(r"\bview\s*[:.]\s*(\w+)", view_section)
        self.assertIsNotNone(first_operation)
        self.assertEqual(first_operation.group(1), "Show",
                         "Show must precede all target-view configuration")
        activation = re.search(r"\bview\s*:\s*Show\s*\(\s*\)", view_section)
        self.assertIsNotNone(activation)
        first_export = re.search(r"\bexport_image\s*\(\s*view\s*,", view_section)
        self.assertIsNotNone(first_export)
        self.assertLess(activation.end(), first_export.start())

    def test_all_five_png_outputs_keep_names_dimensions_and_order(self):
        exports = re.findall(
            r'\bexport_image\s*\(\s*(graph|view)\s*,\s*base\s*\.\.\s*"([^"]+)"'
            r'\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', self.source)
        self.assertEqual(exports, [
            ("graph", "_S11", "1500", "900"),
            ("view", "_top", "1200", "1200"),
            ("view", "_bottom", "1200", "1200"),
            ("view", "_metal_overlay", "1200", "1200"),
            ("view", "_isometric", "1200", "1000"),
        ])
        # Five calls plus the function declaration; no unmatched extra exports.
        self.assertEqual(len(re.findall(r"\bexport_image\s*\(", self.source)), 6)
        self.assertEqual(len(re.findall(r"\bExportImage\s*\(", self.source)), 1)
        self.assertRegex(self.source,
                         r'window\s*:\s*ExportImage\s*\(\s*name\s*,\s*"png"'
                         r'\s*,\s*width\s*,\s*height\s*\)')

    def test_image_progress_brackets_export_and_reports_elapsed_time(self):
        helper = re.search(
            r"local\s+function\s+export_image\s*\([^)]*\)(.*?)\bend\b",
            self.source, re.S)
        self.assertIsNotNone(helper)
        remaining = helper.group(1)
        # Require start/flush before rendering and elapsed/flush only afterward.
        operations = [
            r"local\s+started\s*=\s*os\.time\s*\(\s*\)",
            r'print\s*\(\s*"EXPORT_IMAGE_START "\s*\.\.\s*name\s*\.\.\s*"\.png"\s*\)',
            r"io\.flush\s*\(\s*\)",
            r"window\s*:\s*ExportImage\s*\(",
            r'print\s*\(\s*string\.format\s*\(\s*"EXPORT_IMAGE_DONE %s\.png elapsed_seconds=%g"'
            r'\s*,\s*name\s*,\s*os\.difftime\s*\(\s*os\.time\s*\(\s*\)'
            r'\s*,\s*started\s*\)\s*\)\s*\)',
            r"io\.flush\s*\(\s*\)",
        ]
        for operation in operations:
            match = re.search(operation, remaining)
            self.assertIsNotNone(match, f"Missing or out-of-order export diagnostic: {operation}")
            remaining = remaining[match.end():]
        self.assertRegex(self.source,
                         r'app:SaveAs\(base\.\."\.pfs"\)\s*'
                         r'print\("EXPORT_COMPLETE "\.\.base\)')

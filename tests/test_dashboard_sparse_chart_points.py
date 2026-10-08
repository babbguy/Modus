# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""
Tests -- a line chart with a single value must still draw something.

Line series hide their points (radius 0), so a series with one value (the
Overview "Cost Over Time" chart on the first day of usage) rendered as an empty
chart. dashboard/modus-charts.js registers a Chart.js plugin that shows the
points of sparse line series. These tests load the real file in Node with a
minimal Chart.js stand-in and run the plugin's hook.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHARTS_JS = Path(__file__).resolve().parent.parent / "dashboard" / "modus-charts.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const registered = [];
const defaults = {
  font: {}, plugins: { legend: { labels: {} }, tooltip: {} },
  elements: { line: {}, point: {}, bar: {}, arc: {} }, scale: { grid: {}, ticks: {} },
  scales: {},
};
const Chart = { defaults, register: (p) => registered.push(p) };
const window = { Chart, addEventListener: () => {} };
const document = { documentElement: { getAttribute: () => 'dark' }, getElementById: () => null };
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'),
  { window, Chart, document, console, Number, Math, Object, Array, JSON,
    MutationObserver: class { observe() {} } });
const plugin = registered.find((p) => p.id === 'modusSparsePoints');
const cases = JSON.parse(process.argv[2]);
const out = cases.map((c) => {
  const chart = { config: { type: c.type }, data: { datasets: c.datasets } };
  for (const d of c.data_updates || [null]) {
    if (d) chart.data.datasets[0].data = d;
    plugin.beforeUpdate(chart);
  }
  return chart.data.datasets.map((ds) => ({ pointRadius: ds.pointRadius === undefined ? null : ds.pointRadius,
                                             pointBackgroundColor: ds.pointBackgroundColor || null }));
});
console.log(JSON.stringify({ registered: registered.map((p) => p.id), out }));
"""


def _run(cases: list[dict]) -> dict:
    proc = subprocess.run(
        ["node", "-e", HARNESS, str(CHARTS_JS), json.dumps(cases)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_plugin_is_registered_globally():
    assert "modusSparsePoints" in _run([])["registered"]


def test_single_value_line_series_gets_visible_points():
    res = _run([{"type": "line", "datasets": [{"data": [3.24], "pointRadius": 0, "borderColor": "#9FC131"}]}])
    assert res["out"][0][0] == {"pointRadius": 4, "pointBackgroundColor": "#9FC131"}


def test_series_with_two_or_more_values_is_unchanged():
    res = _run([{"type": "line", "datasets": [{"data": [1, 2, 3], "pointRadius": 0}]}])
    assert res["out"][0][0]["pointRadius"] == 0


def test_null_values_do_not_count_and_object_points_do():
    res = _run([
        {"type": "line", "datasets": [{"data": [None, 5, None], "pointRadius": 0}]},
        {"type": "line", "datasets": [{"data": [{"x": 1, "y": 2}, {"x": 2, "y": 3}], "pointRadius": 0}]},
    ])
    assert res["out"][0][0]["pointRadius"] == 4
    assert res["out"][1][0]["pointRadius"] == 0


def test_points_hidden_again_once_the_series_grows():
    res = _run([{"type": "line", "datasets": [{"data": [1], "pointRadius": 0}],
                 "data_updates": [[1], [1, 2]]}])
    assert res["out"][0][0]["pointRadius"] == 0


def test_bar_charts_are_not_touched():
    res = _run([{"type": "bar", "datasets": [{"data": [7]}]}])
    assert res["out"][0][0]["pointRadius"] is None

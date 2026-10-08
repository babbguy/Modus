// Copyright 2026 babbguy
// SPDX-License-Identifier: Apache-2.0
//
// Browser sweep for the release gate: signs in to the real dashboard with the
// master key, opens every hash view, and records console errors, failed API
// calls, broken text, empty or failed tiles, and a screenshot per view.
//
// Usage: node sweep.mjs <config.json>
//   config: { base, masterKey, outDir, expectations, kpis, chromium? }
// Writes <outDir>/sweep-results.json; exit code 0 unless the sweep itself crashed.

import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const expectations = JSON.parse(fs.readFileSync(cfg.expectations, 'utf8'));
const shotsDir = path.join(cfg.outDir, 'screenshots');
fs.mkdirSync(shotsDir, { recursive: true });

const BAD_TEXT = [
  ['NaN', /\bNaN\b/],
  ['undefined', /\bundefined\b/],
  ['[object Object]', /\[object Object\]/],
  ['Invalid Date', /Invalid Date/],
  ['negative relative time', /(^|[^\w.])-\d+(\.\d+)?\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s+ago\b/i],
];

const launch = { headless: true };
if (cfg.chromium) launch.executablePath = cfg.chromium;
const browser = await chromium.launch(launch);
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, timezoneId: 'UTC', locale: 'en-US' });
const page = await context.newPage();

let current = '(sign-in)';
const views = {};
const rec = (v) => (views[v] ||= { console: [], api: [], text: [], tiles: [], expect: [], kpis: [] });
const allowedApi = (v, s) => (expectations.views[v]?.allowedApiErrors || []).some((rx) => new RegExp(rx).test(s));

page.on('console', (m) => {
  if (m.type() !== 'error') return;
  rec(current).console.push(m.text().slice(0, 300));
});
page.on('pageerror', (e) => rec(current).console.push('pageerror: ' + String(e.message).slice(0, 300)));
page.on('requestfailed', (r) => {
  const u = r.url();
  if (!u.startsWith(cfg.base)) return;
  const err = r.failure()?.errorText || '';
  if (err.includes('ERR_ABORTED')) return;   // navigation away from a view cancels its in-flight fetches
  rec(current).api.push(`FAILED ${r.method()} ${u.replace(cfg.base, '')} ${err}`);
});
page.on('response', (r) => {
  const u = r.url();
  if (!u.startsWith(cfg.base + '/api/') && !u.startsWith(cfg.base + '/version')) return;
  if (r.status() < 400) return;
  const s = `${r.status()} ${r.request().method()} ${new URL(u).pathname}`;
  if (!allowedApi(current, s)) rec(current).api.push(s);
});

// ── sign in with the master key ──────────────────────────────────────────────
await page.goto(cfg.base + '/', { waitUntil: 'load' });
await page.waitForSelector('#login-token', { state: 'visible', timeout: 30000 });
await page.fill('#login-token', cfg.masterKey);
await Promise.all([
  page.waitForEvent('load', { timeout: 30000 }),
  page.locator('#login-form').evaluate((f) => f.requestSubmit()),
]);
await page.waitForFunction(() => {
  const s = document.getElementById('login-screen');
  return !s || getComputedStyle(s).display === 'none';
}, null, { timeout: 30000 });
await page.waitForTimeout(1500);

// First-run onboarding, if any, is closed the way a user would.
for (const name of [/skip/i, /close/i, /dismiss/i, /later/i]) {
  const b = page.getByRole('button', { name }).first();
  if (await b.isVisible().catch(() => false)) { await b.click().catch(() => {}); await page.waitForTimeout(300); }
}

const sidebarViews = await page.$$eval('[data-view]', (els) => [...new Set(els.map((e) => e.dataset.view).filter(Boolean))]);
const expectedViews = Object.keys(expectations.views);

async function settle() {
  await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
  // Wait until no loading skeletons remain (or give up after 15 s; leftovers are reported).
  await page.waitForFunction(() => !document.querySelector('.view-container .ds-skeleton, main .ds-skeleton, #content .ds-skeleton'),
    null, { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(800);
}

async function tileStates() {
  return page.$$eval('[data-tile-id]', (els) => els.filter((e) => e.offsetParent !== null).map((e) => {
    const body = e.querySelector('.gs-tile-body') || e;
    const empty = body.querySelector('.ds-empty');
    let state = 'ok';
    if (body.querySelector('.ds-skeleton')) state = 'loading';
    else if (empty && empty.querySelector('.fa-circle-exclamation')) state = 'error';
    else if (empty) state = 'empty';
    else if (!body.innerText.trim() && !body.querySelector('canvas,svg,img,table')) state = 'blank';
    const rows = body.querySelectorAll('tbody tr').length;
    return { id: e.getAttribute('data-tile-id'), state, rows, text: (empty ? empty.innerText : '').replace(/\s+/g, ' ').slice(0, 160) };
  }));
}

for (const v of expectedViews) {
  current = v;
  const r = rec(v);
  const exp = expectations.views[v] || {};
  const before = Date.now();
  await page.evaluate((h) => { window.location.hash = h; }, v);
  await settle();
  r.loadMs = Date.now() - before;

  const text = await page.evaluate(() => {
    const root = document.querySelector('.view-container') || document.querySelector('main') || document.body;
    return root.innerText;
  });
  for (const [name, rx] of BAD_TEXT) {
    const m = text.match(new RegExp('.{0,50}' + rx.source + '.{0,50}', rx.flags.replace('g', '')));
    if (m) r.text.push(`${name}: "${m[0].replace(/\s+/g, ' ').trim()}"`);
  }

  r.tiles = await tileStates();
  for (const t of r.tiles) {
    if (t.state === 'error' || t.state === 'loading') r.expect.push(`tile ${t.id} is ${t.state}${t.text ? ': ' + t.text : ''}`);
  }
  for (const item of exp.tilesWithData || []) {
    const [id, minRows] = Array.isArray(item) ? item : [item, 0];
    const t = r.tiles.find((x) => x.id === id);
    if (!t) r.expect.push(`tile ${id} not rendered`);
    else if (t.state !== 'ok') r.expect.push(`tile ${id} must show data but is ${t.state}${t.text ? ': ' + t.text : ''}`);
    else if (t.rows < minRows) r.expect.push(`tile ${id} shows ${t.rows} table rows, the seeded data means at least ${minRows}`);
  }
  // A chart that holds data but draws nothing (a single point on a line
  // chart with point radius 0 is invisible) looks empty to the user.
  r.charts = await page.evaluate(() => {
    const out = [];
    const C = window.Chart;
    if (!C || !C.instances) return out;
    for (const ch of Object.values(C.instances)) {
      const canvas = ch.canvas;
      if (!canvas || canvas.offsetParent === null) continue;
      const tile = canvas.closest('[data-tile-id]');
      const id = tile ? tile.getAttribute('data-tile-id') : (canvas.id || 'chart');
      ch.data.datasets.forEach((ds, i) => {
        const meta = ch.getDatasetMeta(i);
        if (meta.hidden || ds.hidden) return;
        const values = (ds.data || []).map((v) => (v && typeof v === 'object' ? v.y : v)).filter((v) => v !== null && v !== undefined && !Number.isNaN(Number(v)));
        if (!values.some((v) => Number(v) !== 0)) return;
        const type = meta.type || ch.config.type;
        if (type !== 'line') return;
        const radius = Math.max(0, ...meta.data.map((el) => (el.options && el.options.radius) || 0));
        if (values.length < 2 && radius === 0) out.push(`chart ${id} (${ds.label || 'dataset ' + i}) has data but draws nothing: ${values.length} point, point radius 0`);
      });
    }
    return out;
  });
  r.expect.push(...r.charts);
  for (const sel of exp.rowsWithData || []) {
    const n = await page.locator(sel.selector).count();
    if (n < (sel.min || 1)) r.expect.push(`${sel.selector}: ${n} rows, expected at least ${sel.min || 1}`);
  }
  for (const needle of exp.textIncludes || []) {
    const s = needle.replace(/\{(\w+)\}/g, (_, k) => cfg.vars?.[k] ?? `{${k}}`);
    if (!text.includes(s)) r.expect.push(`text "${s}" not shown`);
  }
  if (v === 'overview' && cfg.kpis) {
    const cards = await page.$$eval('.kpi-card', (els) => els.map((e) => [
      (e.querySelector('.kpi-label')?.innerText || '').trim(), (e.querySelector('.kpi-value')?.innerText || '').trim()]));
    for (const [label, want] of Object.entries(cfg.kpis)) {
      const got = cards.find(([l]) => l.toLowerCase() === label.toLowerCase());
      r.kpis.push({ label, want, got: got ? got[1] : null });
    }
  }
  await page.screenshot({ path: path.join(shotsDir, `${v}.png`), fullPage: true });
}

fs.writeFileSync(path.join(cfg.outDir, 'sweep-results.json'), JSON.stringify({ sidebarViews, expectedViews, views }, null, 1));
await browser.close();

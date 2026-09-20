#!/usr/bin/python3
# Copyright 2026 by John A Kline <john@johnkline.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

"""Drive the rendered sample report in real browsers and check that its
javascript actually works.

WHY THIS EXISTS.  tests/test_xtide.py renders the template and checks the
payload, the markup and both color palettes, but it never RUNS xtide.js or
xtide_now.js: a page whose script throws on load passes every one of those
tests.  This loads the page in Chromium and Firefox, at both OS color
settings, with the browser clock pinned to a chosen instant, and checks what
the scripts wrote against values worked out here, independently, from the
page's own XTIDE_DATA payload and table rows:

  - no console errors or uncaught exceptions
  - the Right now level is the curve's level at that instant, and the
    direction agrees with the curve
  - the next tide is the first row after that instant, the rows before it
    are dimmed, and every countdown is written
  - after the clock passes that tide, the card moves on to the one after
  - the 7 Days tab shows exactly the rows in that view, and says how many
  - pointing at the graph shows the tooltip
  - the dark palette really applies (a dark block that never matches would
    leave the page light and every other check green)
  - on a translated page, the direction word is the translation and the
    countdown is not English

and then, at 390 px and 320 px of viewport, against the phone drawing the
stylesheet swaps in there (3.3):

  - the narrow drawing is the one shown, and the wide one is not
  - every label on it clears 11 px ON THE GLASS -- the floor these pages
    hold to, and the whole reason the second drawing exists
  - no two time labels overlap, and nothing is clipped by the frame.  This
    is the live oracle for the measured widths tests/test_xtide.py pins:
    those are numbers in a file, these are the glyphs the reader gets
  - clicking a tide marker reads out THAT TIDE.  The two drawings have
    different margins and plot widths, so javascript that measured a tap on
    the narrow drawing against the wide one's geometry would land on
    another instant entirely -- and the tooltip, being perfectly legible,
    would not look wrong

Not collected by pytest: Playwright is not a test-suite requirement.  Run it
before a release.  Render the report into a scratch HTML_ROOT first (one per
language to be checked) with `weectl report run XTideReport --config ...`,
then, with a python that has Playwright:

    python tools/verify_page.py <html_root> [<html_root> ...] \\
        [--browser chromium|firefox|all] [--shots <dir>]

The phone checks run in the same page as the rest, by resizing the viewport
rather than opening a second browser: the drawing that shows is a media
query's doing, and it re-evaluates on a resize with no reload.

The browser builds live in ~/.cache/ms-playwright and are shared by every
project on the machine.  Exit status is 0 only if every check passed.
"""

import argparse
import json
import os
import re
import sys

# Pinned to an instant that is on no sample and on no event: 10:17 on the
# first day of the day view.
PIN_OFFSET = 10 * 3600 + 17 * 60


def payload_and_rows(root):
    page = open(os.path.join(root, 'index.html'), encoding='utf-8').read()
    m = re.search(r'var XTIDE_DATA = (\{.*?\});</script>', page, re.S)
    if not m:
        raise SystemExit('%s: no XTIDE_DATA payload (did tide fail?)' % root)
    rows = [int(ts) for ts in re.findall(r'<div class="xg-evrow(?: past)?" data-ts="(\d+)">', page)]
    lang = re.search(r'<html lang="([^"]*)"', page).group(1)
    return json.loads(m.group(1)), rows, lang


def page_colors(root):
    """--fc-page, light and dark, read out of the stylesheet as shipped."""
    css = open(os.path.join(root, 'xtide.css'), encoding='utf-8').read()
    light = re.search(r':root\s*\{(.*?)\n\}', css, re.S).group(1)
    dark = re.search(r'@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{(.*?)\n\}', css, re.S).group(1)

    def rgb(block):
        h = re.search(r'--fc-page:(#[0-9a-fA-F]{6})', block).group(1)
        return 'rgb(%d, %d, %d)' % tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    return {'light': rgb(light), 'dark': rgb(dark)}


def level_at(views, t):
    """The curve's level at t, from the finest view that covers it.  Worked
    out here from the payload, not read from the page, so it can disagree."""
    for name in ('day', 'week', 'month'):
        v = views[name]
        samples = v['samples']
        last = len(samples) - 1
        x = (t - v['t0']) / v['step']
        if x < 0 or x > last:
            continue
        k = min(int(x), last - 1)
        f = x - k
        return samples[k] * (1 - f) + samples[k + 1] * f
    return None


# The drawing the reader can see, and where every label on it landed, in
# SCREEN pixels: the geometry as rendered, not as computed.  fontSize on an
# SVG text node is in viewBox units, so scaling it by the frame's own
# displayed width is what turns it into pixels on the glass.
READ_DRAWING = """(view) => {
  const wrap = document.getElementById('xg-wrap-' + view);
  const svgs = [...wrap.querySelectorAll('svg.xg')];
  const shown = svgs.find(s => s.getBoundingClientRect().width > 0);
  if (!shown) { return null; }
  const r = shown.getBoundingClientRect();
  const scale = r.width / shown.viewBox.baseVal.width;
  return {
    narrow: shown.classList.contains('xg-narrow'),
    hidden: svgs.length - 1,
    width: r.width, height: r.height,
    labels: [...shown.querySelectorAll('.xg-lab')].map(e => {
      const b = e.getBoundingClientRect();
      return {
        cls: e.getAttribute('class'), text: e.textContent,
        px: parseFloat(getComputedStyle(e).fontSize) * scale,
        left: b.left - r.left, right: b.right - r.left,
        top: b.top - r.top, bottom: b.bottom - r.top,
      };
    }),
  };
}"""

# 11 px of text on the glass; below this the labels are there but not
# readable, which is what the wide drawing did on a phone before 3.3.
TYPE_FLOOR_PX = 11.0


def check_drawing(page, view, expect, narrow):
    """Every label on the drawing now showing for view: big enough to read,
    clear of its neighbors, and inside the frame."""
    drawing = page.evaluate(READ_DRAWING, view)
    where = '%s, %s' % (view, 'narrow' if narrow else 'wide')
    if drawing is None:
        expect(False, '%s: no drawing is visible' % where)
        return
    expect(drawing['narrow'] is narrow,
           '%s: the %s drawing is the one shown' % (where, 'narrow' if narrow else 'wide'))

    if not drawing['labels']:
        expect(False, '%s: the drawing carries no labels at all' % where)
        return
    smallest = min(drawing['labels'], key=lambda lab: lab['px'])
    expect(smallest['px'] >= TYPE_FLOOR_PX,
           '%s: every label clears %.0f px on the glass (smallest %r at %.1f px)'
           % (where, TYPE_FLOOR_PX, smallest['text'], smallest['px']))

    # Time labels, left to right: none may run into the next.  Event labels
    # are excluded -- two tides close together may legitimately crowd, and
    # the suite checks those against their markers instead.
    xlabs = sorted((lab for lab in drawing['labels'] if 'xg-xlab' in lab['cls']),
                   key=lambda lab: lab['left'])
    worst = None
    for a, b in zip(xlabs, xlabs[1:]):
        gap = b['left'] - a['right']
        if worst is None or gap < worst[0]:
            worst = (gap, a['text'], b['text'])
    expect(worst is None or worst[0] >= 0,
           '%s: no two of the %d time labels overlap (closest %r/%r, %.1f px apart)'
           % ((where, len(xlabs)) + (worst[1:] + (worst[0],) if worst else ('', '', 0))))

    clipped = [lab['text'] for lab in drawing['labels']
               if lab['left'] < 0 or lab['right'] > drawing['width']
               or lab['top'] < 0 or lab['bottom'] > drawing['height']]
    expect(not clipped, '%s: no label is clipped by the frame (%s)' % (where, ', '.join(clipped)))


def check_tooltip_hour(page, data, view, expect, narrow):
    """Click a tide marker AS DRAWN and check the tooltip names THAT tide.

    The expected reading is worked out from the payload's own event list and
    formatted by the page, so what is under test is the one thing in
    between: the mapping from a place on this drawing back to an instant.
    Measured against the other frame's margins and plot width it lands
    elsewhere, misses the snap, and reads out an hour nobody pointed at."""
    where = '%s, %s' % (view, 'narrow' if narrow else 'wide')
    v = data['views'][view]
    highs = [ev for ev in data['events'] if v['t0'] <= ev[0] <= v['t1'] and ev[2] == 1]
    if not highs:
        expect(False, '%s: no high tide to point at' % where)
        return
    want = highs[len(highs) // 2]          # mid-window, clear of the clamps
    marker = page.query_selector_all('#xg-wrap-%s svg%s circle.xg-hi'
                                     % (view, '.xg-narrow' if narrow else ':not(.xg-narrow)'))
    if len(marker) != len(highs):
        expect(False, '%s: %d high markers drawn for %d high tides'
                      % (where, len(marker), len(highs)))
        return
    box = marker[len(highs) // 2].bounding_box()
    page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    tip = page.evaluate("v => document.getElementById('xg-tip-' + v).textContent", view)
    opts = {'hour': 'numeric', 'minute': '2-digit', 'weekday': 'short'}
    if view != 'day':
        opts.update({'month': 'short', 'day': 'numeric'})
    if data.get('tz'):
        opts['timeZone'] = data['tz']
    if isinstance(data.get('hour12'), bool):
        opts['hour12'] = data['hour12']
    reading = page.evaluate("([ts, o]) => new Intl.DateTimeFormat(undefined, o)"
                            ".format(new Date(ts * 1000))", [want[0], opts])
    expect(reading in tip and ('%.2f' % want[1]) in tip,
           '%s: clicking the high tide drawn at %s reads it out (tooltip %r)'
           % (where, reading, tip))
    # AND THE CURSOR SITS ON THE MARKER.  The reading alone can survive a
    # wrong frame: the snap radius is scaled by the same bad number, and an
    # inflated one still finds the right event where they are far apart.
    # Where the cursor is drawn cannot survive it, because that is the
    # frame's own arithmetic put back on the frame's own canvas.
    cursor = page.query_selector('#xg-wrap-%s svg%s .xg-cursor'
                                 % (view, '.xg-narrow' if narrow else ':not(.xg-narrow)'))
    spot = cursor.bounding_box() if cursor else None
    off = (abs(spot['x'] + spot['width'] / 2 - box['x'] - box['width'] / 2)
           if spot else float('inf'))
    expect(off <= 2.0, '%s: the cursor lands on that marker (%.1f px away)' % (where, off))
    page.mouse.move(0, 0)


READ_CARD = """() => {
  const q = s => document.querySelector(s);
  return {
    level: q('.tnowbig-v').textContent,
    dirHidden: q('.tdir').hidden,
    dirClass: q('.tdir').className,
    dirWord: q('.tdir-t').textContent,
    nextTs: q('.tnext .rel').getAttribute('data-ts'),
    nextRel: q('.tnext .rel').textContent,
    nextHidden: q('.tnext').hidden,
    emptyRel: [...document.querySelectorAll('.rel')].filter(e => !e.textContent.trim()).length,
    past: [...document.querySelectorAll('.xg-evrow.past')].map(r => +r.getAttribute('data-ts')),
    bodyBg: getComputedStyle(document.body).backgroundColor,
  };
}"""


def check_page(pw, engine, scheme, root, shots, failures):
    data, rows, lang = payload_and_rows(root)
    day = data['views']['day']
    pin = day['t0'] + PIN_OFFSET
    later = [ts for ts in rows if ts > pin]
    tag = '%s %s %s' % (engine, scheme, os.path.basename(os.path.normpath(root)))

    def expect(ok, what):
        print('  %s  %s' % ('ok  ' if ok else 'FAIL', what))
        if not ok:
            failures.append('%s: %s' % (tag, what))

    print(tag)
    browser = getattr(pw, engine).launch()
    try:
        ctx = browser.new_context(viewport={'width': 1280, 'height': 900}, color_scheme=scheme)
        page = ctx.new_page()
        errors = []
        page.on('console', lambda msg: msg.type == 'error' and errors.append(msg.text))
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        # SECONDS.  Python's clock.install reads a plain number as epoch
        # seconds (JavaScript's takes milliseconds); handed milliseconds it
        # put the page some 56,000 years ahead, past every sample.
        page.clock.install(time=pin)
        page.goto('file://' + os.path.abspath(os.path.join(root, 'index.html')))
        card = page.evaluate(READ_CARD)

        want = level_at(data['views'], pin)
        try:
            shown_level = float(card['level'])
        except ValueError:
            shown_level = float('nan')   # a dash: the page found no level
        expect(abs(shown_level - want) <= 0.051,
               'Right now level %r is the curve at the pinned instant (%.3f)' % (card['level'], want))
        rising = level_at(data['views'], pin + 900) > level_at(data['views'], pin - 900)
        expect(not card['dirHidden'] and ('up' if rising else 'down') in card['dirClass'].split(),
               'direction %r agrees with the curve (%s)' % (card['dirClass'], 'rising' if rising else 'falling'))
        expect(card['dirWord'] == data['T']['Rising' if rising else 'Falling'],
               'direction word %r is the payload translation' % card['dirWord'])
        expect(later and card['nextTs'] == str(later[0]) and not card['nextHidden'],
               'next tide is the first row after the pinned instant')
        expect(card['nextRel'].strip() != '', 'the next tide has a countdown: %r' % card['nextRel'])
        expect(card['emptyRel'] == 0, 'every countdown is written (%d empty)' % card['emptyRel'])
        expect(card['past'] == [ts for ts in rows if ts <= pin],
               'exactly the rows before the pinned instant are dimmed (%d)' % len(card['past']))
        expect(card['bodyBg'] == page_colors(root)[scheme],
               'the %s palette applies: page %s' % (scheme, card['bodyBg']))
        if lang != 'en' and later:
            english = page.evaluate("([n]) => new Intl.RelativeTimeFormat('en', {numeric: 'always'})"
                                    ".format(n, 'hour')", [round((later[0] - pin) / 3600)])
            expect(card['nextRel'] != english,
                   'countdown %r is in the page language, not English (%r)' % (card['nextRel'], english))

        week = data['views']['week']
        page.click('.xg-tab[data-view="week"]')
        shown = page.evaluate("() => [...document.querySelectorAll('.xg-evrow')]"
                              ".filter(r => r.style.display !== 'none').length")
        count = page.evaluate("() => document.querySelector('#xg-count').textContent")
        in_week = len([ts for ts in rows if week['t0'] <= ts <= week['t1']])
        expect(shown == in_week and str(in_week) in count,
               '7 Days shows its %d rows (%d shown, count %r)' % (in_week, shown, count))
        page.click('.xg-tab[data-view="day"]')

        box = page.query_selector('#xg-wrap-day svg').bounding_box()
        page.mouse.move(box['x'] + box['width'] * 0.4, box['y'] + box['height'] / 2)
        tip = page.evaluate("() => getComputedStyle(document.querySelector('#xg-tip-day')).display")
        expect(tip == 'block', 'pointing at the graph shows the tooltip')
        page.mouse.move(0, 0)

        for view in ('day', 'week', 'month'):
            page.click('.xg-tab[data-view="%s"]' % view)
            check_drawing(page, view, expect, narrow=False)
            check_tooltip_hour(page, data, view, expect, narrow=False)
        page.click('.xg-tab[data-view="day"]')

        if shots:
            os.makedirs(shots, exist_ok=True)
            page.screenshot(path=os.path.join(shots, '%s.png' % tag.replace(' ', '-')), full_page=True)

        # THE PHONE, in this same page: the drawing that shows is a media
        # query's doing and re-evaluates on a resize, so no reload and no
        # second browser is needed.  320 first because it is the tight one
        # -- the least glass the graph is ever given.
        for width in (320, 390):
            page.set_viewport_size({'width': width, 'height': 844})
            for view in ('day', 'week', 'month'):
                page.click('.xg-tab[data-view="%s"]' % view)
                check_drawing(page, view, lambda ok, what: expect(ok, '%d px: %s' % (width, what)),
                              narrow=True)
                check_tooltip_hour(page, data, view,
                                   lambda ok, what: expect(ok, '%d px: %s' % (width, what)),
                                   narrow=True)
            page.click('.xg-tab[data-view="day"]')
            if shots:
                page.screenshot(path=os.path.join(shots, '%s-%dpx.png'
                                                  % (tag.replace(' ', '-'), width)),
                                full_page=True)
        page.set_viewport_size({'width': 1280, 'height': 900})

        if len(later) >= 2:
            # Run the page's own minute timer past the next tide.
            page.clock.fast_forward((later[0] - pin + 61) * 1000)
            moved = page.evaluate("() => document.querySelector('.tnext .rel').getAttribute('data-ts')")
            expect(moved == str(later[1]), 'once that tide passes, the card moves on to the next one')

        expect(not errors, 'no console errors (%s)' % '; '.join(errors))
        ctx.close()
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('roots', nargs='+', help='rendered XTideReport HTML_ROOT directories')
    parser.add_argument('--browser', choices=('chromium', 'firefox', 'all'), default='all')
    parser.add_argument('--shots', help='also save a full-page screenshot of each run here')
    args = parser.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit('Playwright is not installed for %s' % sys.executable)
    engines = ('chromium', 'firefox') if args.browser == 'all' else (args.browser,)
    failures = []
    with sync_playwright() as pw:
        for engine in engines:
            for scheme in ('light', 'dark'):
                for root in args.roots:
                    check_page(pw, engine, scheme, root, args.shots, failures)
    print()
    if failures:
        print('%d FAILED:' % len(failures))
        for f in failures:
            print('  ' + f)
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())

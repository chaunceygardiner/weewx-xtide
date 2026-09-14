/* Copyright 2026 by John A Kline.  See LICENSE.txt for your rights.

   The weewx-xtide sample report's clock work: the "Right now" card, the
   countdowns, and the tide table's dimmed past rows.

   WHY THIS RUNS IN THE BROWSER.  Anything that depends on what time it is
   now is right for one instant if it is written at report time, and nothing
   reloads the page between reports.  So each such element carries the epoch
   seconds it is about in a data-ts attribute, and this file writes it on
   load and once a minute after: the report says WHEN, this file says HOW
   LONG.

   The level is read off the same samples the graph is drawn from (the
   XTIDE_DATA payload), so the number always agrees with the picture.
   xtide.js is not touched by any of this; it still owns the tabs, the
   tooltip and the now line.

   THE HOOKS, for a skin that embeds $xtide.graph() and wants this card:
     #xt-now             the card: .tnowbig-v, .tdir (gets up/down, hidden
                         when there is no direction), .tdir-t, .tarrow path,
                         .tnext (hidden when no tide is ahead), .tnext-k,
                         .tnext-l, .tnext-t
     .xg-evrow[data-ts]  the table rows: 'past' toggled; the first row
                         still ahead has its .ev-k, .ev-l and .ev-t copied
                         into the card
     .rel[data-ts]       "in 3 hours" / "2 hours ago"
   Every part is guarded, so a page without one of them skips that part. */
(function () {
  'use strict';
  var data = window.XTIDE_DATA;
  if (!data || !data.views) { return; }
  var T = data.T || {};
  function tr(key) { return typeof T[key] === 'string' ? T[key] : key; }

  var MINUTE = 60000;
  var ARROW_UP = 'M10 3 L10 17 M4 9 L10 3 L16 9';
  var ARROW_DOWN = 'M10 17 L10 3 M4 11 L10 17 L16 11';

  function setText(el, text) {
    if (el && text !== undefined && text !== null) { el.textContent = text; }
  }

  /* ---- relative time --------------------------------------------------
     Intl.RelativeTimeFormat translates AND pluralizes in the page's own
     language ("in 3 Stunden", "vor 2 Stunden"), which is why no lang file
     carries these phrases.  An engine without it leaves them empty; the
     table's Time column still says when. */
  var rtf = null;
  try {
    rtf = new Intl.RelativeTimeFormat(document.documentElement.lang || undefined,
                                      {numeric: 'always'});
  } catch (e) {
    rtf = null;
  }

  function relative(seconds) {
    if (!rtf) { return ''; }
    var s = Math.abs(seconds);
    var n, unit;
    if (s < 5400) {
      n = Math.round(s / 60); unit = 'minute';
    } else if (s < 172800) {
      n = Math.round(s / 3600); unit = 'hour';
    } else {
      n = Math.round(s / 86400); unit = 'day';
    }
    return rtf.format(seconds < 0 ? -n : n, unit);
  }

  /* ---- the level ------------------------------------------------------
     The finest view that covers t: the 2-day view's 6-minute samples while
     they last, then the 7-day view's and the 30-day view's, so a page left
     open keeps a level until its data run out, and shows a dash after. */
  var ORDER = ['day', 'week', 'month'];

  function levelAt(t) {
    for (var i = 0; i < ORDER.length; i++) {
      var v = data.views[ORDER[i]];
      if (!v || !v.samples || v.samples.length < 2) { continue; }
      var last = v.samples.length - 1;
      var x = (t - v.t0) / v.step;
      if (x < 0 || x > last) { continue; }
      var k = Math.min(Math.floor(x), last - 1);
      var f = x - k;
      return v.samples[k] * (1 - f) + v.samples[k + 1] * f;
    }
    return null;
  }

  /* ---- the clock pass ------------------------------------------------ */

  function tickPast(now) {
    var rows = document.querySelectorAll('.xg-evrow[data-ts]');
    for (var i = 0; i < rows.length; i++) {
      var ts = parseInt(rows[i].getAttribute('data-ts'), 10);
      if (!isNaN(ts)) { rows[i].classList.toggle('past', ts <= now); }
    }
  }

  function copyCell(row, from, box, to) {
    var src = row.querySelector(from);
    var dst = box.querySelector(to);
    if (src && dst) { dst.textContent = src.textContent; }
  }

  function tickNow(now) {
    var box = document.getElementById('xt-now');
    if (!box) { return; }

    var lvl = levelAt(now);
    setText(box.querySelector('.tnowbig-v'), lvl === null ? '—' : lvl.toFixed(1));

    /* Direction over half an hour centered on now, from the same samples. */
    var dir = box.querySelector('.tdir');
    if (dir) {
      var before = levelAt(now - 900);
      var after = levelAt(now + 900);
      if (lvl === null || before === null || after === null) {
        dir.hidden = true;
      } else {
        var rising = after > before;
        dir.hidden = false;
        dir.classList.toggle('up', rising);
        dir.classList.toggle('down', !rising);
        setText(dir.querySelector('.tdir-t'), tr(rising ? 'Rising' : 'Falling'));
        var arrow = dir.querySelector('.tarrow path');
        if (arrow) { arrow.setAttribute('d', rising ? ARROW_UP : ARROW_DOWN); }
      }
    }

    /* WHICH tide is next moves on the clock, not on the report cycle, so it
       is re-picked here from the table's own rows rather than left as the
       one written at report time -- otherwise a tide that has just turned
       goes on being announced as next, with a countdown reading "3 minutes
       ago".  xtide.js hides the rows outside the chosen tab but leaves them
       in the document, so the first row still ahead is always here.  The
       countdown itself is a .rel element, which tickRelative writes; that
       is why this runs before it. */
    var rows = document.querySelectorAll('.xg-evrow[data-ts]');
    var next = null;
    for (var i = 0; i < rows.length; i++) {
      var rowTs = parseInt(rows[i].getAttribute('data-ts'), 10);
      if (!isNaN(rowTs) && rowTs > now) { next = rows[i]; break; }
    }
    var card = box.querySelector('.tnext');
    if (card) { card.hidden = next === null; }
    if (next) {
      copyCell(next, '.ev-k', box, '.tnext-k');
      copyCell(next, '.ev-l', box, '.tnext-l');
      copyCell(next, '.ev-t', box, '.tnext-t');
      var rel = box.querySelector('.rel[data-ts]');
      if (rel) { rel.setAttribute('data-ts', next.getAttribute('data-ts')); }
    }
  }

  function tickRelative(now) {
    var els = document.querySelectorAll('.rel[data-ts]');
    for (var i = 0; i < els.length; i++) {
      var ts = parseInt(els[i].getAttribute('data-ts'), 10);
      if (!isNaN(ts)) { setText(els[i], relative(ts - now)); }
    }
  }

  function tick() {
    var now = Math.floor(Date.now() / 1000);
    tickPast(now);
    tickNow(now);
    tickRelative(now);
  }

  tick();
  setInterval(tick, MINUTE);
}());

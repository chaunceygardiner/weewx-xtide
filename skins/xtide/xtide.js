/* Copyright 2026 by John A Kline.  See LICENSE.txt for your rights.
   Tabs, click/hover tooltip, "now" line, and event-list filtering for the
   weewx-xtide sample report.  Data arrives via the XTIDE_DATA global that
   the template embeds; the SVGs themselves are generated server-side.

   TWO DRAWINGS PER VIEW.  Since 3.3 the builder draws every view into two
   frames -- a wide one for a desktop and a narrow one laid out for a phone
   -- and a page may emit both inside a view's wrapper and show whichever
   fits, by a media query it owns.  The frames differ in viewBox, margins
   and value scale, so NOTHING here may hold a single geometry: every
   measurement is taken against the frame of the svg it is measuring, read
   off that element.  Given the wide frame's numbers, a tap on the narrow
   drawing maps to the wrong instant and the tooltip reads out an hour the
   reader is not pointing at.  A page that emits only one drawing is the
   same code path with one svg in the wrapper. */
(function () {
  'use strict';
  var data = window.XTIDE_DATA;
  if (!data) { return; }
  var L = data.layouts;
  /* A payload from before 3.3 carries 'layout', singular, and no frames at
     all, so there is no geometry here to measure anything against.  Leave
     the page as the server drew it rather than throw: the SVGs are already
     complete and readable, and every line below this one would fail on the
     first lookup -- including updateNow(), which runs at load, so the whole
     script would die and take the tabs and the event-list filtering with
     it.  This happens when the skin is NEWER than the extension, which is a
     misconfiguration, but a misconfigured page should degrade, not break. */
  if (!L) { return; }
  /* Server-translated strings (payload T); a missing entry falls back to
     the English key, so an old payload still renders. */
  var T = data.T || {};
  function tr(key) { return typeof T[key] === 'string' ? T[key] : key; }
  var views = ['day', 'week', 'month'];
  var active = 'day';

  function wrapEl(view) { return document.getElementById('xg-wrap-' + view); }
  function svgsIn(view) { return wrapEl(view).querySelectorAll('svg.xg'); }
  function tipEl(view) { return document.getElementById('xg-tip-' + view); }

  /* Which frame an svg was drawn into.  The builder marks the narrow one
     with xg-narrow; anything else is the wide frame, which is what a page
     from before 3.3 emits. */
  function frameOf(svg) { return svg.classList.contains('xg-narrow') ? 'narrow' : 'wide'; }
  function layoutOf(svg) { return L[frameOf(svg)]; }
  function scaleOf(view, svg) { return data.views[view].scales[frameOf(svg)]; }

  /* The drawing the reader can actually see.  Asked at every interaction
     rather than cached: the page switches between the two on width alone,
     so a rotation or a resized window changes the answer with no reload. */
  function shownSvg(view) {
    var list = svgsIn(view);
    for (var i = 0; i < list.length; i++) {
      if (list[i].getBoundingClientRect().width > 0) { return list[i]; }
    }
    return list.length ? list[0] : null;
  }

  function xOf(svg, v, t) {
    var l = layoutOf(svg);
    return l.ml + (t - v.t0) * l.pw / (v.t1 - v.t0);
  }

  function yOf(svg, sc, val) {
    var l = layoutOf(svg);
    return l.mt + (sc.vhi - val) * l.ph / (sc.vhi - sc.vlo);
  }

  function fmtTime(ts, view) {
    var opts = { hour: 'numeric', minute: '2-digit', weekday: 'short' };
    if (view !== 'day') { opts.month = 'short'; opts.day = 'numeric'; }
    if (data.tz) { opts.timeZone = data.tz; }
    /* The page's clock, when the skin stated one via graph(clock=...).
       Without it Intl follows the VISITOR's locale, so a tooltip could read
       24-hour over a 12-hour table.  An older payload has no hour12 and
       keeps that locale-driven behavior. */
    if (typeof data.hour12 === 'boolean') { opts.hour12 = data.hour12; }
    return new Intl.DateTimeFormat(undefined, opts).format(new Date(ts * 1000));
  }

  /* One cursor per DRAWING, kept on the element itself: two drawings of the
     same view are two svgs, and a cursor cached per view would be appended
     to one and moved for taps on the other. */
  function cursorEl(svg) {
    var c = svg.querySelector('.xg-cursor');
    if (!c) {
      c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('class', 'xg-cursor');
      c.setAttribute('r', String(layoutOf(svg).cur));
      c.setAttribute('cx', '-10');
      c.setAttribute('cy', '-10');
      svg.appendChild(c);
    }
    return c;
  }

  function hideTip(view) {
    tipEl(view).style.display = 'none';
    var list = svgsIn(view);
    for (var i = 0; i < list.length; i++) {
      var c = list[i].querySelector('.xg-cursor');
      if (c) { c.setAttribute('cx', '-10'); c.setAttribute('cy', '-10'); }
    }
  }

  function showAt(view, clientX) {
    var svg = shownSvg(view);
    if (!svg) { return; }
    var v = data.views[view];
    var sc = scaleOf(view, svg);
    var l = layoutOf(svg);
    var rect = svg.getBoundingClientRect();
    /* A drawing with no width would make every measurement below Infinity
       or NaN, and a NaN never fails the bounds test that follows. */
    if (!rect.width) { hideTip(view); return; }
    var scale = rect.width / l.w;
    var sx = (clientX - rect.left) / scale;
    if (sx < l.ml || sx > l.ml + l.pw) { hideTip(view); return; }
    var t = v.t0 + (sx - l.ml) * (v.t1 - v.t0) / l.pw;

    /* Snap to a tidal event within ~10 CSS PIXELS of the pointer -- a reach
       converted through this drawing's scale, so a thumb gets the same
       target on the narrow drawing as a mouse does on the wide one. */
    var snap = (10 / scale) * (v.t1 - v.t0) / l.pw;
    var hit = null;
    for (var i = 0; i < data.events.length; i++) {
      var ev = data.events[i];
      if (ev[0] >= v.t0 && ev[0] <= v.t1 && Math.abs(ev[0] - t) <= snap) {
        if (hit === null || Math.abs(ev[0] - t) < Math.abs(hit[0] - t)) { hit = ev; }
      }
    }
    var ts, val, title;
    if (hit) {
      ts = hit[0];
      val = hit[1];
      title = tr(hit[2] === 1 ? 'High Tide' : 'Low Tide');
    } else {
      var idx = Math.round((t - v.t0) / v.step);
      if (idx < 0) { idx = 0; }
      if (idx >= v.samples.length) { idx = v.samples.length - 1; }
      ts = v.t0 + idx * v.step;
      val = v.samples[idx];
      title = '';
    }

    var c = cursorEl(svg);
    c.setAttribute('cx', xOf(svg, v, ts).toFixed(1));
    c.setAttribute('cy', yOf(svg, sc, val).toFixed(1));

    var tip = tipEl(view);
    var html = '';
    if (title) { html += '<b>' + title + '</b><br>'; }
    html += val.toFixed(2) + ' ' + data.unit + '<br>' + fmtTime(ts, view);
    tip.innerHTML = html;
    tip.style.display = 'block';
    /* The tooltip is positioned inside the WRAPPER, which need not be where
       the svg starts: a page that shows one drawing of two wraps each in a
       box of its own.  So offset by where this svg actually sits. */
    var wrapRect = wrapEl(view).getBoundingClientRect();
    var px = rect.left - wrapRect.left + xOf(svg, v, ts) * scale;
    var py = rect.top - wrapRect.top + yOf(svg, sc, val) * scale;
    var left = px + 14;
    if (left + tip.offsetWidth > wrapEl(view).clientWidth) { left = px - tip.offsetWidth - 14; }
    if (left < 0) { left = 0; }
    var top = py - tip.offsetHeight - 10;
    if (top < 0) { top = py + 14; }
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  /* Every drawing of every view, not just the visible one: the page can
     switch drawings on a resize without reloading, and the line has to be
     in the right place on the one that appears. */
  function updateNow() {
    var now = Date.now() / 1000;
    for (var i = 0; i < views.length; i++) {
      var v = data.views[views[i]];
      var list = svgsIn(views[i]);
      for (var j = 0; j < list.length; j++) {
        var line = list[j].querySelector('.xg-nowline');
        if (!line) { continue; }
        var px = (now >= v.t0 && now <= v.t1) ? xOf(list[j], v, now).toFixed(1) : '-10';
        line.setAttribute('x1', px);
        line.setAttribute('x2', px);
      }
    }
  }

  function filterList() {
    var v = data.views[active];
    var rows = document.querySelectorAll('.xg-evrow');
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var ts = parseInt(rows[i].getAttribute('data-ts'), 10);
      var show = ts >= v.t0 && ts <= v.t1;
      rows[i].style.display = show ? '' : 'none';
      if (show) { shown++; }
    }
    var count = document.getElementById('xg-count');
    if (count) { count.textContent = tr('{n} tidal events.').replace('{n}', shown); }
  }

  function selectView(view) {
    active = view;
    for (var i = 0; i < views.length; i++) {
      wrapEl(views[i]).classList.toggle('xg-hidden', views[i] !== view);
      hideTip(views[i]);
    }
    var tabs = document.querySelectorAll('.xg-tab');
    for (var j = 0; j < tabs.length; j++) {
      tabs[j].classList.toggle('xg-active', tabs[j].getAttribute('data-view') === view);
    }
    filterList();
  }

  var tabs = document.querySelectorAll('.xg-tab');
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].addEventListener('click', function () {
      selectView(this.getAttribute('data-view'));
    });
  }
  views.forEach(function (view) {
    var wrap = wrapEl(view);
    wrap.addEventListener('mousemove', function (e) { showAt(view, e.clientX); });
    wrap.addEventListener('click', function (e) { showAt(view, e.clientX); });
    wrap.addEventListener('mouseleave', function () { hideTip(view); });
  });

  updateNow();
  setInterval(updateNow, 60000);
  filterList();
})();

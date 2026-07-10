/* Copyright 2026 by John A Kline.  See LICENSE.txt for your rights.
   Tabs, click/hover tooltip, "now" line, and event-list filtering for the
   weewx-xtide sample report.  Data arrives via the XTIDE_DATA global that
   the template embeds; the SVGs themselves are generated server-side. */
(function () {
  'use strict';
  var data = window.XTIDE_DATA;
  if (!data) { return; }
  var L = data.layout;
  var views = ['day', 'week', 'month'];
  var active = 'day';
  var cursors = {};

  function wrapEl(view) { return document.getElementById('xg-wrap-' + view); }
  function svgEl(view) { return wrapEl(view).querySelector('svg.xg'); }
  function tipEl(view) { return document.getElementById('xg-tip-' + view); }

  function xOf(v, t) { return L.ml + (t - v.t0) * L.pw / (v.t1 - v.t0); }
  function yOf(v, val) { return L.mt + (v.vhi - val) * L.ph / (v.vhi - v.vlo); }

  function fmtTime(ts, view) {
    var opts = { hour: 'numeric', minute: '2-digit', weekday: 'short' };
    if (view !== 'day') { opts.month = 'short'; opts.day = 'numeric'; }
    if (data.tz) { opts.timeZone = data.tz; }
    return new Intl.DateTimeFormat(undefined, opts).format(new Date(ts * 1000));
  }

  function cursorEl(view) {
    if (!cursors[view]) {
      var c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('class', 'xg-cursor');
      c.setAttribute('r', '5');
      c.setAttribute('cx', '-10');
      c.setAttribute('cy', '-10');
      svgEl(view).appendChild(c);
      cursors[view] = c;
    }
    return cursors[view];
  }

  function hideTip(view) {
    tipEl(view).style.display = 'none';
    if (cursors[view]) {
      cursors[view].setAttribute('cx', '-10');
      cursors[view].setAttribute('cy', '-10');
    }
  }

  function showAt(view, clientX) {
    var v = data.views[view];
    var svg = svgEl(view);
    var rect = svg.getBoundingClientRect();
    var scale = rect.width / L.w;
    var sx = (clientX - rect.left) / scale;
    if (sx < L.ml || sx > L.ml + L.pw) { hideTip(view); return; }
    var t = v.t0 + (sx - L.ml) * (v.t1 - v.t0) / L.pw;

    /* Snap to a tidal event when the pointer is within ~10px of one. */
    var snap = 10 * (v.t1 - v.t0) / L.pw;
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
      title = hit[2] === 1 ? 'High Tide' : 'Low Tide';
    } else {
      var idx = Math.round((t - v.t0) / v.step);
      if (idx < 0) { idx = 0; }
      if (idx >= v.samples.length) { idx = v.samples.length - 1; }
      ts = v.t0 + idx * v.step;
      val = v.samples[idx];
      title = '';
    }

    var c = cursorEl(view);
    c.setAttribute('cx', xOf(v, ts).toFixed(1));
    c.setAttribute('cy', yOf(v, val).toFixed(1));

    var tip = tipEl(view);
    var html = '';
    if (title) { html += '<b>' + title + '</b><br>'; }
    html += val.toFixed(2) + ' ' + data.unit + '<br>' + fmtTime(ts, view);
    tip.innerHTML = html;
    tip.style.display = 'block';
    var px = xOf(v, ts) * scale;
    var py = yOf(v, val) * scale;
    var left = px + 14;
    if (left + tip.offsetWidth > wrapEl(view).clientWidth) { left = px - tip.offsetWidth - 14; }
    var top = py - tip.offsetHeight - 10;
    if (top < 0) { top = py + 14; }
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  function updateNow() {
    var now = Date.now() / 1000;
    for (var i = 0; i < views.length; i++) {
      var v = data.views[views[i]];
      var line = svgEl(views[i]).querySelector('.xg-nowline');
      if (!line) { continue; }
      var px = (now >= v.t0 && now <= v.t1) ? xOf(v, now).toFixed(1) : '-10';
      line.setAttribute('x1', px);
      line.setAttribute('x2', px);
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
    if (count) { count.textContent = shown + ' tidal events.'; }
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

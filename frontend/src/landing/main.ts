// @ts-nocheck — self-contained vanilla canvas/reveal script for the marketing
// landing page; not part of the typed app surface.
// Forge landing page entry. Fonts + styles are bundled by Vite (same-origin),
// so the page is served CSP-clean by the app's StaticFiles mount.
import './landing.css'
import '@fontsource/space-grotesk/500.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-mono/500.css'

(function () {
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- Hero lattice: a bracket domain triangulated into a truss whose members
  // heat cool->ember by how hard they carry the load. This IS topology optimization,
  // shown live rather than screenshotted. ----
  function ramp(t) {
    // 0..1 -> stress ramp: void -> mid -> ember -> filament
    var stops = [[58,70,88],[140,111,132],[232,106,60],[255,184,107]];
    t = Math.max(0, Math.min(1, t));
    var x = t * (stops.length - 1), i = Math.floor(x), f = x - i;
    var a = stops[i], b = stops[Math.min(i + 1, stops.length - 1)];
    return "rgb(" + Math.round(a[0]+(b[0]-a[0])*f) + "," + Math.round(a[1]+(b[1]-a[1])*f) + "," + Math.round(a[2]+(b[2]-a[2])*f) + ")";
  }

  function buildTruss(w, h) {
    // Load applied at bottom-right; fixed along the left edge. Members near the
    // straight support->load path carry the most -> hottest.
    var cols = 7, rows = 7, nodes = [], pad = 0.12;
    for (var r = 0; r < rows; r++) for (var c = 0; c < cols; c++) {
      nodes.push({ x: (pad + (c/(cols-1))*(1-2*pad)) * w, y: (pad + (r/(rows-1))*(1-2*pad)) * h, r: r, c: c });
    }
    var load = { x: (1-pad)*w, y: (1-pad)*h }, sup = { x: pad*w, y: pad*h };
    function distToPath(px, py) {
      var dx = load.x - sup.x, dy = load.y - sup.y, L2 = dx*dx + dy*dy;
      var t = ((px - sup.x)*dx + (py - sup.y)*dy) / L2; t = Math.max(0, Math.min(1, t));
      var qx = sup.x + t*dx, qy = sup.y + t*dy;
      return Math.hypot(px - qx, py - qy);
    }
    var members = [], maxd = Math.hypot(w, h);
    for (var i = 0; i < nodes.length; i++) for (var j = i+1; j < nodes.length; j++) {
      var a = nodes[i], b = nodes[j];
      var dr = Math.abs(a.r-b.r), dc = Math.abs(a.c-b.c);
      if (dr <= 1 && dc <= 1 && (dr+dc) > 0) {
        var mx = (a.x+b.x)/2, my = (a.y+b.y)/2;
        var stress = 1 - Math.min(1, distToPath(mx, my) / (maxd*0.34));
        stress = Math.pow(stress, 1.5);
        members.push({ a: a, b: b, s: stress, order: (a.c+b.c+a.r+b.r) });
      }
    }
    members.sort(function (m1, m2) { return m1.order - m2.order; });
    return { nodes: nodes, members: members, load: load, sup: sup };
  }

  function runLattice(canvas) {
    var ctx = canvas.getContext("2d");
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    function size() {
      var rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      return { w: rect.width, h: rect.height };
    }
    var dim = size(), T = buildTruss(dim.w, dim.h);
    var start = null, DUR = 2100;

    function frame(ts) {
      if (start === null) start = ts;
      var p = reduce ? 1 : Math.min(1, (ts - start) / DUR);
      ctx.clearRect(0, 0, dim.w, dim.h);
      // draw cool members first, then energized ones on top
      for (var pass = 0; pass < 2; pass++) {
        for (var k = 0; k < T.members.length; k++) {
          var m = T.members[k];
          var appear = m.order / 14;             // stagger by position
          var e = Math.max(0, Math.min(1, (p*1.6) - appear*0.6));
          var lit = m.s * e;
          var hot = lit > 0.28;
          if ((pass === 0) === hot) continue;
          ctx.beginPath();
          ctx.moveTo(m.a.x, m.a.y); ctx.lineTo(m.b.x, m.b.y);
          ctx.strokeStyle = hot ? ramp(0.3 + m.s*0.7) : "rgba(58,70,88,0.5)";
          ctx.lineWidth = hot ? (0.7 + lit*3.1) : 0.8;
          ctx.globalAlpha = hot ? (0.5 + lit*0.5) : 0.5;
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
      // nodes
      for (var n = 0; n < T.nodes.length; n++) {
        ctx.beginPath(); ctx.arc(T.nodes[n].x, T.nodes[n].y, 1.6, 0, 6.29);
        ctx.fillStyle = "rgba(120,132,150,0.6)"; ctx.fill();
      }
      // support (fixed) + load arrow
      ctx.fillStyle = ramp(1);
      ctx.beginPath(); ctx.arc(T.load.x, T.load.y, 4.5, 0, 6.29); ctx.fill();
      ctx.strokeStyle = "rgba(120,132,150,0.7)"; ctx.lineWidth = 1.4;
      var sx = T.sup.x, sy = T.sup.y;
      ctx.beginPath(); ctx.moveTo(sx-7, sy-9); ctx.lineTo(sx-7, sy+9); ctx.stroke();
      for (var t2 = -9; t2 < 9; t2 += 4) { ctx.beginPath(); ctx.moveTo(sx-7, sy+t2+4); ctx.lineTo(sx-12, sy+t2); ctx.stroke(); }

      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
    var ro = new ResizeObserver(function () { dim = size(); T = buildTruss(dim.w, dim.h); start = null; requestAnimationFrame(frame); });
    ro.observe(canvas);
  }

  var hero = document.getElementById("lattice");
  if (hero) runLattice(hero);
  var mini = document.getElementById("mini-lattice");
  if (mini) runLattice(mini);

  // ---- scroll reveal ----
  if (!reduce && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
    }, { threshold: 0.15 });
    document.querySelectorAll(".rise").forEach(function (el) { io.observe(el); });
  } else {
    document.querySelectorAll(".rise").forEach(function (el) { el.classList.add("in"); });
  }
})();

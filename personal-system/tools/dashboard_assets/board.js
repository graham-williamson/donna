/* Daruma Board ambience + interactions. Progressive enhancement — the board
   works without this file; this adds theme switching, ambient particles,
   koi, the achieve celebration, and the incense focus timer. */
(function () {
  'use strict';
  var reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var doc = document.documentElement;

  /* ---------- theme switching ---------- */
  function applyTheme(t) {
    doc.dataset.theme = t;
    try {
      localStorage.setItem('daruma-theme', t);
      if (t !== 'twilight') localStorage.setItem('daruma-light-pref', t);
    } catch (e) {}
    markButtons();
    restartAmbience();
  }
  function markButtons() {
    document.querySelectorAll('[data-theme-btn]').forEach(function (b) {
      b.classList.toggle('active', b.dataset.themeBtn === doc.dataset.theme);
    });
  }
  document.querySelectorAll('[data-theme-btn]').forEach(function (b) {
    b.addEventListener('click', function () { applyTheme(b.dataset.themeBtn); });
  });
  markButtons();

  /* ---------- temple bell (WebAudio — no audio assets) ---------- */
  function bell(times) {
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      for (var i = 0; i < (times || 1); i++) {
        var t0 = ctx.currentTime + 0.05 + i * 1.4;
        [523.25, 1046.5, 1567.98].forEach(function (f, j) {
          var o = ctx.createOscillator(), g = ctx.createGain();
          o.type = 'sine'; o.frequency.value = f;
          g.gain.setValueAtTime(0.16 / (j + 1), t0);
          g.gain.exponentialRampToValueAtTime(0.0001, t0 + 1.3);
          o.connect(g); g.connect(ctx.destination);
          o.start(t0); o.stop(t0 + 1.3);
        });
      }
    } catch (e) {}
  }

  /* ---------- ambient particle layer ---------- */
  var canvas = document.getElementById('particles');
  var ctx2d = canvas ? canvas.getContext('2d') : null;
  var parts = [], raf = 0;
  function resize() {
    if (!canvas) return;
    canvas.width = innerWidth; canvas.height = innerHeight;
  }
  addEventListener('resize', resize);

  function rnd(a, b) { return a + Math.random() * (b - a); }

  function seasonNow() {
    var m = new Date().getMonth() + 1;
    return (m === 12 || m <= 2) ? 'winter' : m <= 5 ? 'spring' : m <= 8 ? 'summer' : 'autumn';
  }

  function mixFor(theme) {
    var s = seasonNow(), m = [];
    function add(kind, n) { while (n-- > 0) m.push(kind); }
    if (theme === 'washi') {
      add('mist', 3); add('mote', s === 'winter' ? 6 : 14);
      if (s === 'winter') add('snow', 16);
      if (s === 'summer') add('firefly', 5);
      if (s === 'autumn') add('momiji', 6);
    } else if (theme === 'sakura') {
      add('petal', s === 'autumn' ? 8 : 16);
      if (s === 'autumn') add('momiji', 10);
      if (s === 'winter') add('snow', 18);
      if (s === 'summer') add('firefly', 5);
    } else {
      add('star', 42); add('firefly', 8);
      if (s === 'winter') add('snow', 14);
    }
    if (innerWidth < 700) m = m.filter(function (_, i) { return i % 2 === 0; });
    return m;
  }

  function makeParticle(kind) {
    var p = { kind: kind, x: rnd(0, innerWidth), y: rnd(0, innerHeight), ph: rnd(0, 6.28) };
    if (kind === 'mote')    { p.r = rnd(1, 2.4); p.vx = rnd(-.08, .08); p.vy = rnd(-.05, -.16); p.a = rnd(.06, .14); }
    if (kind === 'mist')    { p.r = rnd(160, 320); p.vx = rnd(.03, .1); p.vy = 0; p.a = rnd(.025, .05); }
    if (kind === 'petal')   { p.r = rnd(3, 5.5); p.vx = rnd(.1, .4); p.vy = rnd(.3, .7); p.a = rnd(.5, .85); p.hue = Math.random() < .5 ? '#f4c6cf' : '#eaa6b4'; }
    if (kind === 'momiji')  { p.r = rnd(3.5, 6); p.vx = rnd(.1, .4); p.vy = rnd(.3, .65); p.a = rnd(.5, .8); p.hue = Math.random() < .5 ? '#c8552f' : '#d9763a'; }
    if (kind === 'snow')    { p.r = rnd(1, 2.6); p.vx = rnd(-.06, .06); p.vy = rnd(.18, .45); p.a = rnd(.3, .6); }
    if (kind === 'star')    { p.y = rnd(0, innerHeight * .65); p.r = rnd(.6, 1.6); p.vx = 0; p.vy = 0; p.a = rnd(.25, .7); p.tw = rnd(.4, 1.4); }
    if (kind === 'firefly') { p.r = rnd(1.4, 2.4); p.vx = rnd(-.25, .25); p.vy = rnd(-.18, .18); p.a = rnd(.4, .8); p.tw = rnd(.6, 1.6); }
    return p;
  }

  function step(p, t) {
    p.x += p.vx; p.y += p.vy;
    if (p.kind === 'petal' || p.kind === 'momiji') p.x += Math.sin(t / 900 + p.ph) * .3;
    if (p.kind === 'firefly') {
      p.vx += rnd(-.02, .02); p.vy += rnd(-.02, .02);
      p.vx = Math.max(-.3, Math.min(.3, p.vx)); p.vy = Math.max(-.3, Math.min(.3, p.vy));
    }
    if (p.x > innerWidth + 40) p.x = -30;
    if (p.x < -40) p.x = innerWidth + 30;
    if (p.y > innerHeight + 20) { p.y = -15; p.x = rnd(0, innerWidth); }
    if (p.y < -360) p.y = innerHeight + 10;
  }

  function draw(p, t) {
    var c = ctx2d;
    if (p.kind === 'mote') {
      c.globalAlpha = p.a; c.fillStyle = '#8c7850';
      c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill();
    } else if (p.kind === 'mist') {
      var g = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
      g.addColorStop(0, 'rgba(200,185,150,' + p.a + ')');
      g.addColorStop(1, 'rgba(200,185,150,0)');
      c.globalAlpha = 1; c.fillStyle = g;
      c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill();
    } else if (p.kind === 'petal' || p.kind === 'momiji') {
      c.save(); c.translate(p.x, p.y); c.rotate(t / 1300 + p.ph);
      c.globalAlpha = p.a; c.fillStyle = p.hue;
      c.beginPath(); c.ellipse(0, 0, p.r, p.r * .62, 0, 0, 7); c.fill();
      c.restore();
    } else if (p.kind === 'snow') {
      c.globalAlpha = p.a; c.fillStyle = '#fff';
      c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill();
    } else if (p.kind === 'star') {
      c.globalAlpha = p.a * (0.55 + 0.45 * Math.sin(t / 1000 * p.tw + p.ph));
      c.fillStyle = '#f2e6c0';
      c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill();
    } else if (p.kind === 'firefly') {
      var a = p.a * (0.4 + 0.6 * Math.sin(t / 700 * p.tw + p.ph));
      var g2 = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 5);
      g2.addColorStop(0, 'rgba(220,235,140,' + a + ')');
      g2.addColorStop(1, 'rgba(220,235,140,0)');
      c.globalAlpha = 1; c.fillStyle = g2;
      c.beginPath(); c.arc(p.x, p.y, p.r * 5, 0, 7); c.fill();
    }
  }

  function loop(t) {
    ctx2d.clearRect(0, 0, canvas.width, canvas.height);
    for (var i = 0; i < parts.length; i++) { step(parts[i], t); draw(parts[i], t); }
    raf = requestAnimationFrame(loop);
  }

  function restartAmbience() {
    if (!canvas || !ctx2d || reduced) return;
    cancelAnimationFrame(raf);
    resize();
    parts = mixFor(doc.dataset.theme).map(makeParticle);
    raf = requestAnimationFrame(loop);
    ensureKoi();
  }

  /* ---------- koi (sakura theme — CSS offset-path does the swimming) ---------- */
  var KOI =
    '<svg viewBox="0 0 120 60">' +
    '<path d="M14 30 C30 8 78 8 100 26 C106 30 106 30 100 34 C78 52 30 52 14 30 Z" fill="#f6f3ee"/>' +
    '<path d="M14 30 C8 18 2 16 4 28 C2 44 8 42 14 30 Z" fill="#f0958a"/>' +
    '<circle cx="88" cy="27" r="2.4" fill="#222"/>' +
    '<path d="M44 16 C54 14 64 18 70 24 C62 28 50 26 44 16 Z" fill="#e2654f"/>' +
    '<path d="M36 42 C44 40 54 42 60 38 C52 34 40 35 36 42 Z" fill="#e2654f" opacity=".85"/>' +
    '</svg>';
  function ensureKoi() {
    var layer = document.querySelector('.koi-layer');
    if (!layer) return;
    if (reduced || doc.dataset.theme !== 'sakura') { layer.innerHTML = ''; return; }
    if (!layer.children.length) {
      layer.innerHTML = '<div class="koi koi-a">' + KOI + '</div>' +
                        '<div class="koi koi-b">' + KOI + '</div>';
    }
  }

  /* ---------- achieve celebration ---------- */
  var celebrateId = document.body.dataset.celebrate;
  function celebrate(card) {
    bell(2);
    if (reduced || !card) return;
    var k = document.createElement('div');
    k.className = 'tassei'; k.textContent = '達成';
    card.appendChild(k);
    setTimeout(function () { k.remove(); }, 2600);
    var colours = ['#f4c6cf', '#eaa6b4', '#d4af37', '#f2e6c0', '#e2654f'];
    for (var i = 0; i < 26; i++) {
      var s = document.createElement('span');
      s.className = 'burst-p';
      s.style.background = colours[i % colours.length];
      s.style.left = '90px'; s.style.top = '60px';
      card.appendChild(s);
      var ang = rnd(0, 6.28), dist = rnd(60, 170);
      s.animate([
        { transform: 'translate(0,0) rotate(0deg)', opacity: 1 },
        { transform: 'translate(' + Math.cos(ang) * dist + 'px,' +
          (Math.sin(ang) * dist - 40) + 'px) rotate(' + rnd(-220, 220) + 'deg)', opacity: 0 }
      ], { duration: rnd(900, 1600), easing: 'cubic-bezier(.2,.6,.4,1)', fill: 'forwards' });
      setTimeout(function (el) { el.remove(); }.bind(null, s), 1700);
    }
  }
  if (celebrateId) {
    celebrate(document.querySelector('[data-goal-id="' + celebrateId + '"]'));
    history.replaceState(null, '', '/');
  }

  /* ---------- incense focus timer ---------- */
  var incense = document.getElementById('incense');
  if (incense) {
    var stick = incense.querySelector('.incense-stick');
    var leftEl = incense.querySelector('.incense-left');
    var stopBtn = incense.querySelector('[data-incense-stop]');
    var startBtns = incense.querySelectorAll('[data-incense]');
    var tick = null, endAt = 0, total = 0;
    function fmt(s) { return Math.floor(s / 60) + ':' + ('0' + Math.floor(s % 60)).slice(-2); }
    function update() {
      var s = (endAt - Date.now()) / 1000;
      if (s <= 0) { finish(); return; }
      leftEl.textContent = fmt(s);
      stick.style.setProperty('--burnt', (100 * (1 - s / total)) + '%');
    }
    function start(mins) {
      total = mins * 60; endAt = Date.now() + total * 1000;
      window.__incense = true;
      startBtns.forEach(function (b) { b.hidden = true; });
      stopBtn.hidden = false; leftEl.hidden = false;
      incense.classList.add('lit');
      tick = setInterval(update, 1000); update();
    }
    function reset() {
      clearInterval(tick); window.__incense = false;
      incense.classList.remove('lit');
      startBtns.forEach(function (b) { b.hidden = false; });
      stopBtn.hidden = true;
      stick.style.setProperty('--burnt', '0%');
    }
    function finish() { reset(); bell(2); leftEl.hidden = false; leftEl.textContent = '— done'; }
    startBtns.forEach(function (b) {
      b.addEventListener('click', function () { start(parseInt(b.dataset.incense, 10)); });
    });
    stopBtn.addEventListener('click', function () { reset(); leftEl.hidden = true; });
  }

  /* ---------- gentle auto-refresh (replaces the old meta refresh) ---------- */
  setInterval(function () {
    if (!window.__incense && !document.hidden && !document.querySelector('details[open]')) {
      location.reload();
    }
  }, 120000);

  restartAmbience();
})();

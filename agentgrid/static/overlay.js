/* AgentGrid Web Review overlay.
 *
 * Injected onto an arbitrary page (the user's own dev site) via a bookmarklet
 * or a console snippet served from AgentGrid. Lets the user highlight text or
 * click near anything to pin a comment, collect a batch, then "release" the
 * batch to an AgentGrid agent to go fix.
 *
 * Design goals:
 *   - Zero style bleed in or out: everything lives in a Shadow DOM.
 *   - Survives scroll/resize: pins are anchored to content, repositioned live.
 *   - Drafts persist in localStorage keyed by URL, so a reload never loses work.
 *   - Uses AgentGrid's palette and follows the OS light/dark theme.
 *
 * The loader sets window.__AG_REVIEW__ = {origin, token} OR encodes them on the
 * script src (?ag=<origin>&token=<tok>); we discover both here.
 */
(function () {
  "use strict";

  // Guard against double-injection: toggle instead of stacking overlays.
  if (window.__agReviewMounted) {
    window.__agReviewToggle && window.__agReviewToggle();
    return;
  }
  window.__agReviewMounted = true;

  // ---- Discover AgentGrid origin + token ---------------------------------
  function discoverConfig() {
    var cfg = window.__AG_REVIEW__ || {};
    if (cfg.origin && cfg.token) return cfg;
    var src = "";
    try {
      src = (document.currentScript && document.currentScript.src) || "";
    } catch (e) {}
    if (!src) {
      // Last resort: find our own <script> by filename.
      var scripts = document.getElementsByTagName("script");
      for (var i = 0; i < scripts.length; i++) {
        if ((scripts[i].src || "").indexOf("overlay.js") !== -1) {
          src = scripts[i].src;
          break;
        }
      }
    }
    try {
      var u = new URL(src, location.href);
      cfg.token = cfg.token || u.searchParams.get("t") || u.searchParams.get("token") || "";
      cfg.origin = cfg.origin || u.searchParams.get("ag") || u.origin;
    } catch (e) {}
    return cfg;
  }
  var CFG = discoverConfig();
  var AG_ORIGIN = (CFG.origin || "").replace(/\/$/, "");
  var AG_TOKEN = CFG.token || "";

  // ---- Small helpers -----------------------------------------------------
  var STORE_KEY = "ag_review::" + location.origin + location.pathname;
  var uid = function () {
    return "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  };
  var clamp = function (v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  };

  // Build a reasonably stable CSS selector path for an element.
  function cssPath(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "#" + CSS.escape(el.id);
    var parts = [];
    var node = el;
    var depth = 0;
    while (node && node.nodeType === 1 && node !== document.body && depth < 6) {
      var sel = node.nodeName.toLowerCase();
      if (node.id) {
        parts.unshift("#" + CSS.escape(node.id));
        break;
      }
      var parent = node.parentNode;
      if (parent) {
        var sib = parent.children;
        var same = [];
        for (var i = 0; i < sib.length; i++) {
          if (sib[i].nodeName === node.nodeName) same.push(sib[i]);
        }
        if (same.length > 1) {
          sel += ":nth-of-type(" + (same.indexOf(node) + 1) + ")";
        }
      }
      parts.unshift(sel);
      node = node.parentNode;
      depth++;
    }
    return parts.join(" > ");
  }

  function resolveEl(selector) {
    if (!selector) return null;
    try {
      return document.querySelector(selector);
    } catch (e) {
      return null;
    }
  }

  // ---- State + persistence ----------------------------------------------
  var comments = []; // {id, kind:'text'|'point'|'image', anchor, quote, note, done}
  var armed = false;
  var trayOpen = false;
  var sessions = [];
  var targetSession = ""; // sessionId or "" for "new agent"

  function load() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (raw) comments = JSON.parse(raw) || [];
    } catch (e) {
      comments = [];
    }
  }
  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(comments));
    } catch (e) {}
  }

  // ---- Shadow DOM scaffold ----------------------------------------------
  var host = document.createElement("div");
  host.id = "ag-review-host";
  host.style.cssText =
    "all:initial;position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
  (document.body || document.documentElement).appendChild(host);
  var root = host.attachShadow({ mode: "open" });

  var STYLE = [
    ":host{ all:initial; }",
    "*{ box-sizing:border-box; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }",
    ":root, .scope{",
    " --bg:#fbfbfa; --card:#ffffff; --card-hover:#f4f4f2;",
    " --text:#18181b; --text-2:#6b6b73; --text-3:#a3a3ab;",
    " --line:#e7e7e4; --line-2:#d6d6d2; --accent:#b4690e;",
    " --shadow:0 6px 24px rgba(0,0,0,.14),0 1px 3px rgba(0,0,0,.08);",
    " --ease:cubic-bezier(.32,.72,0,1);",
    "}",
    "@media (prefers-color-scheme:dark){ .scope{",
    " --bg:#131315; --card:#1a1a1d; --card-hover:#212126;",
    " --text:#ececee; --text-2:#9797a0; --text-3:#63636b;",
    " --line:#252529; --line-2:#33333a; --accent:#d9a441;",
    " --shadow:0 8px 30px rgba(0,0,0,.5);",
    "}}",
    // launcher pill
    ".pill{ pointer-events:auto; position:fixed; right:18px; bottom:18px;",
    " display:flex; align-items:center; gap:8px; height:40px; padding:0 14px;",
    " background:var(--card); color:var(--text); border:1px solid var(--line-2);",
    " border-radius:22px; box-shadow:var(--shadow); cursor:pointer; font-size:13px;",
    " font-weight:560; transition:transform .18s var(--ease),background .15s; }",
    ".pill:hover{ background:var(--card-hover); transform:translateY(-1px); }",
    ".pill .dot{ width:9px; height:9px; border-radius:50%; background:var(--text-3); transition:background .15s; }",
    ".pill.armed .dot{ background:var(--accent); box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 22%,transparent); }",
    ".pill .count{ min-width:19px; height:19px; padding:0 5px; border-radius:10px;",
    " background:var(--accent); color:#fff; font-size:11px; font-weight:680;",
    " display:none; align-items:center; justify-content:center; }",
    ".pill .count.show{ display:inline-flex; }",
    "@media (prefers-color-scheme:dark){ .pill .count{ color:#131315; } }",
    // pins
    ".pin{ pointer-events:auto; position:fixed; width:24px; height:24px; margin:-12px 0 0 -12px;",
    " border-radius:50% 50% 50% 2px; background:var(--accent); color:#fff; font-size:12px;",
    " font-weight:680; display:flex; align-items:center; justify-content:center;",
    " box-shadow:0 2px 6px rgba(0,0,0,.3); cursor:pointer; transition:transform .12s var(--ease); }",
    ".pin:hover,.pin.glow{ transform:scale(1.18); z-index:2; }",
    ".pin.done{ background:var(--text-3); }",
    "@media (prefers-color-scheme:dark){ .pin{ color:#131315; } }",
    // highlight band for text anchors
    ".band{ pointer-events:none; position:fixed; background:color-mix(in srgb,var(--accent) 26%,transparent);",
    " border-radius:2px; transition:opacity .12s; }",
    ".band.glow{ background:color-mix(in srgb,var(--accent) 42%,transparent); }",
    // selection chip
    ".chip{ pointer-events:auto; position:fixed; display:flex; align-items:center; gap:6px;",
    " height:30px; padding:0 11px; background:var(--accent); color:#fff; border:none;",
    " border-radius:16px; font-size:12px; font-weight:600; cursor:pointer;",
    " box-shadow:0 3px 10px rgba(0,0,0,.28); transform:translate(-50%,-100%); }",
    "@media (prefers-color-scheme:dark){ .chip{ color:#131315; } }",
    // popover
    ".pop{ pointer-events:auto; position:fixed; width:290px; background:var(--card);",
    " border:1px solid var(--line-2); border-radius:12px; box-shadow:var(--shadow);",
    " padding:12px; }",
    ".pop .q{ font-size:11.5px; color:var(--text-2); background:var(--card-hover);",
    " border-left:2px solid var(--accent); padding:6px 8px; border-radius:4px;",
    " margin-bottom:8px; max-height:64px; overflow:auto; white-space:pre-wrap; }",
    ".pop textarea{ width:100%; min-height:64px; resize:vertical; border:1px solid var(--line-2);",
    " border-radius:8px; padding:8px; font-size:13px; color:var(--text); background:var(--bg);",
    " outline:none; font-family:inherit; }",
    ".pop textarea:focus{ border-color:var(--accent); }",
    ".pop .row{ display:flex; justify-content:space-between; align-items:center; margin-top:9px; }",
    ".pop .hint{ font-size:10.5px; color:var(--text-3); }",
    ".btn{ height:30px; padding:0 13px; border-radius:8px; font-size:12.5px; font-weight:600;",
    " cursor:pointer; border:1px solid var(--line-2); background:var(--card); color:var(--text); }",
    ".btn:hover{ background:var(--card-hover); }",
    ".btn.primary{ background:var(--accent); color:#fff; border-color:var(--accent); }",
    ".btn.ghost{ border:none; background:transparent; color:var(--text-2); }",
    "@media (prefers-color-scheme:dark){ .btn.primary{ color:#131315; } }",
    // tray
    ".tray{ pointer-events:auto; position:fixed; top:0; right:0; height:100%; width:340px;",
    " background:var(--bg); border-left:1px solid var(--line); box-shadow:var(--shadow);",
    " display:flex; flex-direction:column; transform:translateX(100%);",
    " transition:transform .28s var(--ease); }",
    ".tray.open{ transform:translateX(0); }",
    ".tray header{ display:flex; align-items:center; justify-content:space-between;",
    " padding:14px 16px; border-bottom:1px solid var(--line); }",
    ".tray header .t{ font-size:14px; font-weight:640; color:var(--text); }",
    ".tray .list{ flex:1; overflow:auto; padding:8px 12px; }",
    ".tray .empty{ color:var(--text-3); font-size:12.5px; text-align:center; padding:40px 20px; line-height:1.6; }",
    ".item{ display:flex; gap:9px; padding:10px; border-radius:10px; cursor:pointer; }",
    ".item:hover{ background:var(--card-hover); }",
    ".item .n{ flex:none; width:20px; height:20px; border-radius:50%; background:var(--accent); color:#fff;",
    " font-size:11px; font-weight:680; display:flex; align-items:center; justify-content:center; margin-top:1px; }",
    ".item.done .n{ background:var(--text-3); }",
    "@media (prefers-color-scheme:dark){ .item .n{ color:#131315; } }",
    ".item .body{ flex:1; min-width:0; }",
    ".item .qq{ font-size:11px; color:var(--text-3); white-space:nowrap; overflow:hidden;",
    " text-overflow:ellipsis; margin-bottom:2px; }",
    ".item .nn{ font-size:12.5px; color:var(--text); line-height:1.4; word-break:break-word; }",
    ".item .x{ flex:none; color:var(--text-3); font-size:15px; line-height:1; opacity:0; padding:2px 4px; border-radius:6px; }",
    ".item:hover .x{ opacity:1; }",
    ".item .x:hover{ background:var(--line-2); color:var(--text); }",
    ".tray footer{ border-top:1px solid var(--line); padding:12px 14px; display:flex; flex-direction:column; gap:9px; }",
    ".tray select{ width:100%; height:32px; border:1px solid var(--line-2); border-radius:8px;",
    " background:var(--bg); color:var(--text); font-size:12.5px; padding:0 8px; font-family:inherit; }",
    ".tray footer .actions{ display:flex; gap:8px; }",
    ".tray footer .actions .btn{ flex:1; height:34px; }",
    ".release{ flex:2 !important; }",
    // toast
    ".toast{ pointer-events:none; position:fixed; left:50%; bottom:70px; transform:translateX(-50%);",
    " background:var(--text); color:var(--bg); font-size:12.5px; font-weight:560; padding:9px 15px;",
    " border-radius:9px; box-shadow:var(--shadow); opacity:0; transition:opacity .2s,transform .2s; }",
    ".toast.show{ opacity:1; transform:translateX(-50%) translateY(-4px); }",
    // armed body cursor is set on the real document, not here
  ].join("\n");

  var wrap = document.createElement("div");
  wrap.className = "scope";
  var styleEl = document.createElement("style");
  styleEl.textContent = STYLE;
  root.appendChild(styleEl);
  root.appendChild(wrap);

  // ---- Layers ------------------------------------------------------------
  var pinLayer = document.createElement("div");
  var bandLayer = document.createElement("div");
  wrap.appendChild(bandLayer);
  wrap.appendChild(pinLayer);

  var pill = document.createElement("div");
  pill.className = "pill";
  pill.innerHTML =
    '<span class="dot"></span><span class="lbl">Review</span><span class="count"></span>';
  wrap.appendChild(pill);

  var tray = document.createElement("div");
  tray.className = "tray";
  wrap.appendChild(tray);

  var toast = document.createElement("div");
  toast.className = "toast";
  wrap.appendChild(toast);

  var chip = null; // transient selection chip
  var pop = null; // transient popover

  var toastTimer;
  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toast.classList.remove("show");
    }, 2200);
  }

  // ---- Anchoring + positioning ------------------------------------------
  // For a text selection we store the client rects (relative to a container
  // element) so we can redraw the highlight band and place the pin.
  function anchorFromSelection(sel) {
    var range = sel.getRangeAt(0);
    var container =
      range.commonAncestorContainer.nodeType === 1
        ? range.commonAncestorContainer
        : range.commonAncestorContainer.parentElement;
    var selector = cssPath(container);
    var cRect = container.getBoundingClientRect();
    var rects = [];
    var list = range.getClientRects();
    for (var i = 0; i < list.length; i++) {
      var r = list[i];
      if (r.width < 1 && r.height < 1) continue;
      rects.push({
        // fractions relative to container box, so they survive layout shifts
        x: (r.left - cRect.left) / (cRect.width || 1),
        y: (r.top - cRect.top) / (cRect.height || 1),
        w: r.width / (cRect.width || 1),
        h: r.height / (cRect.height || 1),
      });
    }
    return {
      kind: "text",
      selector: selector,
      rects: rects,
      quote: sel.toString().trim().slice(0, 400),
    };
  }

  function anchorFromPoint(clientX, clientY, targetEl) {
    var el = targetEl;
    var isImg = el && el.tagName === "IMG";
    // Prefer anchoring to a meaningful element (image, or nearest sized box).
    var rect = el ? el.getBoundingClientRect() : null;
    if (!rect || rect.width === 0 || rect.height === 0) {
      el = document.body;
      rect = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
    }
    return {
      kind: isImg ? "image" : "point",
      selector: cssPath(el),
      fracX: clamp((clientX - rect.left) / (rect.width || 1), 0, 1),
      fracY: clamp((clientY - rect.top) / (rect.height || 1), 0, 1),
      // absolute page fallback if the element can't be found later
      pageX: clientX + window.scrollX,
      pageY: clientY + window.scrollY,
      label: isImg ? (el.getAttribute("alt") || el.getAttribute("src") || "image") : "",
    };
  }

  // Given an anchor, return {x,y} client coords for the pin, and (for text)
  // an array of band rects in client coords. Returns null if off-screen far.
  function positionFor(anchor) {
    if (anchor.kind === "text") {
      var el = resolveEl(anchor.selector);
      if (!el) return null;
      var cr = el.getBoundingClientRect();
      var bands = anchor.rects.map(function (f) {
        return {
          left: cr.left + f.x * cr.width,
          top: cr.top + f.y * cr.height,
          width: f.w * cr.width,
          height: f.h * cr.height,
        };
      });
      var last = bands[bands.length - 1] || bands[0];
      return {
        pinX: last ? last.left + last.width : cr.right,
        pinY: last ? last.top : cr.top,
        bands: bands,
      };
    }
    // point / image
    var pe = resolveEl(anchor.selector);
    if (pe) {
      var r = pe.getBoundingClientRect();
      if (r.width || r.height) {
        return { pinX: r.left + anchor.fracX * r.width, pinY: r.top + anchor.fracY * r.height, bands: [] };
      }
    }
    return { pinX: anchor.pageX - window.scrollX, pinY: anchor.pageY - window.scrollY, bands: [] };
  }

  // ---- Render pins + bands ----------------------------------------------
  var glowId = null;
  function render() {
    // pins
    pinLayer.innerHTML = "";
    bandLayer.innerHTML = "";
    comments.forEach(function (c, idx) {
      var pos = positionFor(c.anchor);
      if (!pos) return;
      // bands
      (pos.bands || []).forEach(function (b) {
        var band = document.createElement("div");
        band.className = "band" + (glowId === c.id ? " glow" : "");
        band.style.left = b.left + "px";
        band.style.top = b.top + "px";
        band.style.width = b.width + "px";
        band.style.height = b.height + "px";
        bandLayer.appendChild(band);
      });
      // pin
      var pin = document.createElement("div");
      pin.className = "pin" + (c.done ? " done" : "") + (glowId === c.id ? " glow" : "");
      pin.textContent = idx + 1;
      pin.style.left = clamp(pos.pinX, 6, window.innerWidth - 6) + "px";
      pin.style.top = clamp(pos.pinY, 6, window.innerHeight - 6) + "px";
      pin.title = c.note || "";
      pin.addEventListener("click", function (e) {
        e.stopPropagation();
        openTray();
        var node = tray.querySelector('[data-id="' + c.id + '"]');
        if (node) node.scrollIntoView({ block: "center", behavior: "smooth" });
        setGlow(c.id);
      });
      pinLayer.appendChild(pin);
    });
    // count
    var n = comments.filter(function (c) { return !c.done; }).length;
    var cnt = pill.querySelector(".count");
    cnt.textContent = n;
    cnt.classList.toggle("show", n > 0);
  }

  // Redraw pins now, and rebuild the tray only if it is open. Kept separate
  // from render() so scroll/resize (60fps) never thrash the tray's DOM.
  function refresh() {
    render();
    if (trayOpen) renderTray();
  }

  function setGlow(id) {
    glowId = id;
    render();
    setTimeout(function () {
      if (glowId === id) {
        glowId = null;
        render();
      }
    }, 1400);
  }

  // schedule renders on scroll/resize without thrashing
  var rafPending = false;
  function scheduleRender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(function () {
      rafPending = false;
      render();
    });
  }
  window.addEventListener("scroll", scheduleRender, true);
  window.addEventListener("resize", scheduleRender);

  // ---- Popover for creating/editing a comment ----------------------------
  function closePop() {
    if (pop) {
      pop.remove();
      pop = null;
    }
  }
  function closeChip() {
    if (chip) {
      chip.remove();
      chip = null;
    }
  }

  function openPopover(anchor, existing, atX, atY) {
    closePop();
    closeChip();
    pop = document.createElement("div");
    pop.className = "pop";
    var quote =
      (existing && existing.anchor.quote) ||
      anchor.quote ||
      (anchor.label ? "■ " + anchor.label : "");
    pop.innerHTML =
      (quote ? '<div class="q">' + esc(quote) + "</div>" : "") +
      '<textarea placeholder="What should change here?"></textarea>' +
      '<div class="row"><span class="hint">↵ save · ⇧↵ newline · esc cancel</span>' +
      '<span><button class="btn ghost cancel">Cancel</button> <button class="btn primary save">Save</button></span></div>';
    wrap.appendChild(pop);
    var ta = pop.querySelector("textarea");
    if (existing) ta.value = existing.note || "";
    // position: near the anchor, clamped to viewport
    var px = clamp(atX, 8, window.innerWidth - 298);
    var py = clamp(atY, 8, window.innerHeight - 180);
    pop.style.left = px + "px";
    pop.style.top = py + "px";
    ta.focus();

    function commit() {
      var val = ta.value.trim();
      if (!val) {
        closePop();
        return;
      }
      if (existing) {
        existing.note = val;
      } else {
        comments.push({ id: uid(), kind: anchor.kind, anchor: anchor, note: val, done: false });
      }
      save();
      closePop();
      refresh();
      showToast(existing ? "Comment updated" : "Comment added · " + comments.filter(function(c){return !c.done;}).length + " pending");
    }
    pop.querySelector(".save").addEventListener("click", commit);
    pop.querySelector(".cancel").addEventListener("click", closePop);
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        commit();
      } else if (e.key === "Escape") {
        e.preventDefault();
        closePop();
      }
    });
  }

  // ---- Capture: selection chip + click pin -------------------------------
  document.addEventListener("mouseup", function (e) {
    if (!armed) return;
    if (isOurs(e.target)) return;
    setTimeout(function () {
      var sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim().length > 0) {
        showChip(sel);
      }
    }, 0);
  });

  function showChip(sel) {
    closeChip();
    var range = sel.getRangeAt(0);
    var rect = range.getBoundingClientRect();
    chip = document.createElement("button");
    chip.className = "chip";
    chip.innerHTML = "💬 Comment";
    chip.style.left = clamp(rect.left + rect.width / 2, 40, window.innerWidth - 40) + "px";
    chip.style.top = clamp(rect.top - 6, 34, window.innerHeight - 6) + "px";
    var anchor = anchorFromSelection(sel);
    chip.addEventListener("mousedown", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      openPopover(anchor, null, rect.left, rect.bottom + 8);
    });
    wrap.appendChild(chip);
  }

  document.addEventListener(
    "click",
    function (e) {
      if (!armed) return;
      if (isOurs(e.target)) return;
      // if there is an active text selection, let the chip handle it
      var sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim().length > 0) return;
      // suppress the page's own click while armed
      e.preventDefault();
      e.stopPropagation();
      var anchor = anchorFromPoint(e.clientX, e.clientY, e.target);
      openPopover(anchor, null, e.clientX + 10, e.clientY + 10);
    },
    true
  );

  // Is this event target inside our shadow host?
  function isOurs(t) {
    return t === host || (t && t.getRootNode && t.getRootNode() === root);
  }

  // ---- Tray --------------------------------------------------------------
  function openTray() {
    trayOpen = true;
    tray.classList.add("open");
    renderTray();
  }
  function closeTray() {
    trayOpen = false;
    tray.classList.remove("open");
  }

  function renderTray() {
    var pending = comments.length;
    var items = comments
      .map(function (c, idx) {
        var q =
          c.anchor.quote ||
          (c.anchor.label ? "■ " + c.anchor.label : "• pinned point");
        return (
          '<div class="item' + (c.done ? " done" : "") + '" data-id="' + c.id + '">' +
          '<div class="n">' + (idx + 1) + "</div>" +
          '<div class="body"><div class="qq">' + esc(q) + "</div>" +
          '<div class="nn">' + esc(c.note) + "</div></div>" +
          '<div class="x" title="Delete">×</div></div>'
        );
      })
      .join("");
    var opts =
      '<option value="">💾 Save for later (no agent)</option>' +
      sessions
        .map(function (s) {
          var label = (s.name || s.project || s.id).slice(0, 40);
          return (
            '<option value="' + esc(s.id) + '"' +
            (s.id === targetSession ? " selected" : "") +
            ">" + esc(label) + "</option>"
          );
        })
        .join("");
    tray.innerHTML =
      '<header><span class="t">Review · ' + pending + "</span>" +
      '<button class="btn ghost close">Done</button></header>' +
      '<div class="list">' +
      (items || '<div class="empty">No comments yet.<br>Highlight text or click near anything on the page to leave one.</div>') +
      "</div>" +
      '<footer>' +
      '<select class="target">' + opts + "</select>" +
      '<div class="actions">' +
      '<button class="btn copy">Copy</button>' +
      '<button class="btn primary release">Release ' + pending + " to agent</button>" +
      "</div></footer>";

    tray.querySelector(".close").addEventListener("click", closeTray);
    tray.querySelector(".target").addEventListener("change", function (e) {
      targetSession = e.target.value;
    });
    tray.querySelector(".copy").addEventListener("click", function () {
      var md = compileMarkdown();
      navigator.clipboard && navigator.clipboard.writeText(md);
      showToast("Copied review as markdown");
    });
    tray.querySelector(".release").addEventListener("click", release);
    Array.prototype.forEach.call(tray.querySelectorAll(".item"), function (node) {
      var id = node.getAttribute("data-id");
      node.addEventListener("mouseenter", function () {
        glowId = id;
        render();
      });
      node.addEventListener("mouseleave", function () {
        if (glowId === id) {
          glowId = null;
          render();
        }
      });
      node.addEventListener("click", function (e) {
        if (e.target.classList.contains("x")) {
          comments = comments.filter(function (c) { return c.id !== id; });
          save();
          refresh();
          return;
        }
        var c = comments.filter(function (x) { return x.id === id; })[0];
        if (!c) return;
        openPopover(c.anchor, c, window.innerWidth / 2 - 145, 120);
      });
    });
  }

  // ---- Compile + release -------------------------------------------------
  function compileMarkdown() {
    var lines = [];
    lines.push("# Web review — " + document.title);
    lines.push("Page: " + location.href);
    lines.push("");
    comments.forEach(function (c, i) {
      lines.push((i + 1) + ". **" + locationLabel(c) + "**");
      if (c.anchor.quote) lines.push("   > " + c.anchor.quote.replace(/\n/g, "\n   > "));
      lines.push("   " + c.note);
      lines.push("");
    });
    return lines.join("\n");
  }
  function locationLabel(c) {
    if (c.anchor.kind === "text") return "Selected text";
    if (c.anchor.kind === "image") return "Image: " + (c.anchor.label || "");
    return "Element " + (c.anchor.selector || "");
  }

  function release() {
    if (!comments.length) {
      showToast("Nothing to release");
      return;
    }
    if (!AG_ORIGIN || !AG_TOKEN) {
      showToast("AgentGrid link missing — re-open from the app");
      return;
    }
    var payload = {
      url: location.href,
      title: document.title,
      target: targetSession || null,
      prompt: compileMarkdown(),
      comments: comments.map(function (c) {
        return { kind: c.anchor.kind, selector: c.anchor.selector, quote: c.anchor.quote || "", label: c.anchor.label || "", note: c.note };
      }),
    };
    var relBtn = tray.querySelector(".release");
    if (relBtn) { relBtn.textContent = "Releasing…"; relBtn.disabled = true; }
    fetch(AG_ORIGIN + "/api/review/release?t=" + encodeURIComponent(AG_TOKEN), {
      method: "POST",
      // text/plain avoids a CORS preflight; the server json-parses regardless
      headers: { "Content-Type": "text/plain" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (res) {
        if (res && res.ok) {
          comments = [];
          save();
          render();
          closeTray();
          showToast(
            res.dispatched === "pending"
              ? "Saved — pick it up in AgentGrid"
              : "Released to agent ✔"
          );
        } else {
          showToast((res && res.error) || "Release failed");
          if (relBtn) { relBtn.disabled = false; renderTray(); }
        }
      })
      .catch(function () {
        showToast("Could not reach AgentGrid");
        if (relBtn) { relBtn.disabled = false; renderTray(); }
      });
  }

  // ---- Sessions (for the target dropdown) --------------------------------
  function loadSessions() {
    if (!AG_ORIGIN || !AG_TOKEN) return;
    fetch(AG_ORIGIN + "/api/sessions?t=" + encodeURIComponent(AG_TOKEN))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var arr = Array.isArray(data) ? data : data.sessions || [];
        sessions = arr.map(function (s) {
          return { id: s.id || s.sessionId, name: s.name || s.title, project: s.project || s.cwd || s.projectPath };
        });
        // Default to a running agent so Release dispatches instead of silently
        // parking. Only "no agent running" falls back to save-for-later.
        if (!targetSession && sessions.length) targetSession = sessions[0].id;
        renderTray();
      })
      .catch(function () {});
  }

  // ---- Arm / disarm + toggle --------------------------------------------
  function setArmed(v) {
    armed = v;
    pill.classList.toggle("armed", armed);
    pill.querySelector(".lbl").textContent = armed ? "Reviewing" : "Review";
    document.documentElement.style.cursor = armed ? "crosshair" : "";
    if (!armed) {
      closeChip();
    }
  }

  // Pill body toggles comment mode; the count badge opens/closes the tray.
  pill.addEventListener("click", function () {
    setArmed(!armed);
  });
  pill.querySelector(".count").addEventListener("click", function (e) {
    e.stopPropagation();
    if (trayOpen) closeTray();
    else openTray();
  });

  // The extension owns the global Cmd/Ctrl+K when it injected us; only bind it
  // here for bookmarklet users, so the two don't both fire and cancel out.
  var extOwnsHotkey = !!(window.__AG_REVIEW__ && window.__AG_REVIEW__.ext);

  // Esc disarms / closes transient UI
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (pop) { closePop(); return; }
      if (chip) { closeChip(); return; }
      if (armed) setArmed(false);
    }
    var typing = /^(input|textarea|select)$/i.test((e.target.tagName || "")) ||
      e.target.isContentEditable || isOurs(e.target);
    // Cmd/Ctrl+K toggles the overlay (bookmarklet mode only).
    if (!extOwnsHotkey && (e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      setArmed(!armed);
      return;
    }
    // "c" toggles arm when not typing
    if (e.key === "c" && !typing && !pop) {
      setArmed(!armed);
    }
  });

  // External toggle (re-run bookmarklet)
  window.__agReviewToggle = function () {
    setArmed(!armed);
    if (armed) showToast("Review on — highlight or click to comment");
  };

  // ---- Boot --------------------------------------------------------------
  load();
  loadSessions();
  render();
  setArmed(true);
  showToast("Review on — highlight text or click near anything");
})();

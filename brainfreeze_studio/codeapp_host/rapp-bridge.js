/* rapp-bridge.js — the first script of a rapplication UI inside a Power Apps code app.
 *
 * A rapplication UI talks to its brainstem over HTTP (/chat, /api/binder/agent, /health, or a local brainstem URL).
 * Code apps allow no network access, so those calls go to the host page (window.parent) instead, which answers them
 * with the rapplication's flows and its Copilot Studio agent. Every other request is left alone. It also runs the
 * inline event handlers the build moved out of the page (see rapp-handlers.js), because code apps allow no inline
 * script.
 */
(function () {
  "use strict";
  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  var pending = {};
  var next = 1;
  var BRAINSTEM_PATH = /^\/(chat|api\/|health|agents|binder\/)/;
  var LOCAL_BRAINSTEM = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?\//i;

  function isBrainstem(url) {
    return (url.origin === location.origin && BRAINSTEM_PATH.test(url.pathname)) || LOCAL_BRAINSTEM.test(url.href);
  }

  window.addEventListener("message", function (ev) {
    var d = ev.data;
    if (!d || d.type !== "rapp:bridge:result" || !pending[d.id]) return;
    var done = pending[d.id];
    delete pending[d.id];
    done(d);
  });

  function viaHost(url, init) {
    var body = init && init.body;
    try { body = typeof body === "string" ? JSON.parse(body) : body; } catch (e) { /* not JSON: pass the text */ }
    var id = "b" + (next++);
    return new Promise(function (resolve) {
      pending[id] = function (d) {
        resolve(new Response(JSON.stringify(d.body), { status: d.status || 200,
                                                       headers: { "Content-Type": "application/json" } }));
      };
      window.parent.postMessage({ type: "rapp:bridge", id: id, path: url.pathname,
                                  method: ((init && init.method) || "GET").toUpperCase(), body: body }, "*");
    });
  }

  if (realFetch) {
    window.fetch = function (input, init) {
      var href = typeof input === "string" ? input : input && input.url;
      var url;
      try { url = new URL(href, location.href); } catch (e) { return realFetch(input, init); }
      if (!isBrainstem(url)) return realFetch(input, init);
      if (typeof input !== "string" && !init) init = { method: input.method };
      return viaHost(url, init);
    };
  }

  // Handlers moved out of the markup (rapp-handlers.js registers them by id): attach them as the page builds, and
  // again for markup the page adds later.
  var registry = window.__rappHandlers = window.__rappHandlers || {};
  function wire(root) {
    var nodes = root.querySelectorAll ? root.querySelectorAll("[data-rapp-h]") : [];
    for (var i = 0; i < nodes.length; i++) attach(nodes[i]);
    if (root.getAttribute && root.getAttribute("data-rapp-h")) attach(root);
  }
  function attach(el) {
    if (el.__rappWired) return;
    var ids = el.getAttribute("data-rapp-h").split(" ");
    for (var i = 0; i < ids.length; i++) {
      var h = registry[ids[i]];
      if (h) el.addEventListener(h.event, h.fn);
    }
    el.__rappWired = true;
  }
  window.__rappWire = function () { wire(document); };

  // Markup the page builds at run time can't carry inline handlers under the code app policy either. The common
  // kind is a call with literal arguments, such as onclick="editProject('p1')" or
  // onclick="event.stopPropagation(); showTab('x')"; those run here, without eval. Anything else stays blocked.
  function literal(src, pos) {
    var ch = src[pos];
    if (ch === "'" || ch === '"') {
      var out = "", i = pos + 1;
      while (i < src.length && src[i] !== ch) {
        if (src[i] === "\\" && i + 1 < src.length) { out += src[i + 1]; i += 2; } else { out += src[i++]; }
      }
      return [out, i + 1];
    }
    var m = /^(-?\d+(?:\.\d+)?|true|false|null|undefined|this|event)/.exec(src.slice(pos));
    if (!m) return null;
    var v = m[1];
    var value = v === "true" ? true : v === "false" ? false : v === "null" ? null : v === "undefined" ? undefined
      : v === "this" ? { ref: "this" } : v === "event" ? { ref: "event" } : Number(v);
    return [value, pos + v.length];
  }
  function parseCall(src) {
    var steps = [], pos = 0, m;
    src = src.trim();
    while (pos < src.length) {
      if (steps.length && src[pos] === ".") pos++;
      m = /^[A-Za-z_$][\w$]*/.exec(src.slice(pos));
      if (!m) return null;
      var step = { name: m[0] };
      pos += m[0].length;
      if (src[pos] === "(") {
        pos++;
        step.args = [];
        while (true) {
          while (src[pos] === " ") pos++;
          if (src[pos] === ")") { pos++; break; }
          var lit = literal(src, pos);
          if (!lit) return null;
          step.args.push(lit[0]);
          pos = lit[1];
          while (src[pos] === " ") pos++;
          if (src[pos] === ",") { pos++; continue; }
          if (src[pos] === ")") { pos++; break; }
          return null;
        }
      }
      steps.push(step);
      if (pos < src.length && src[pos] !== ".") return null;
    }
    return steps.length ? steps : null;
  }
  function statements(code) {
    var out = [], cur = "", q = null;
    for (var i = 0; i < code.length; i++) {
      var c = code[i];
      if (q) { cur += c; if (c === "\\" && i + 1 < code.length) { cur += code[++i]; } else if (c === q) q = null; }
      else if (c === "'" || c === '"') { q = c; cur += c; }
      else if (c === ";") { if (cur.trim()) out.push(cur); cur = ""; }
      else cur += c;
    }
    if (cur.trim()) out.push(cur);
    return out;
  }
  function compileInline(code) {
    var parsed = statements(code).map(parseCall);
    if (!parsed.length || parsed.some(function (p) { return !p; })) return null;
    return function (event) {
      var self = this;
      parsed.forEach(function (steps) {
        var target, value;
        steps.forEach(function (step, n) {
          if (n === 0) {
            value = step.name === "event" ? event : step.name === "this" ? self : window[step.name];
            target = window;
          } else {
            target = value;
            value = value == null ? undefined : value[step.name];
          }
          if (step.args) {
            var args = step.args.map(function (a) { return a && a.ref === "this" ? self : a && a.ref === "event" ? event : a; });
            if (typeof value !== "function") throw new Error("inline handler: " + step.name + " is not a function");
            value = value.apply(target, args);
          }
        });
      });
    };
  }
  function adopt(el) {
    var attrs = el.attributes ? Array.prototype.slice.call(el.attributes) : [];
    attrs.forEach(function (a) {
      if (!/^on[a-z]+$/.test(a.name)) return;
      var fn = compileInline(a.value);
      el.removeAttribute(a.name);
      if (fn) el.addEventListener(a.name.slice(2), fn);
      else if (window.console) console.warn("rapp-bridge: this inline handler can't run in a code app:", a.value);
    });
  }
  function adoptTree(root) {
    if (root.nodeType !== 1) return;
    adopt(root);
    var all = root.getElementsByTagName ? root.getElementsByTagName("*") : [];
    for (var i = 0; i < all.length; i++) adopt(all[i]);
  }
  window.__rappInline = compileInline;

  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var added = records[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          if (added[j].nodeType === 1) { adoptTree(added[j]); wire(added[j]); }
        }
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
})();

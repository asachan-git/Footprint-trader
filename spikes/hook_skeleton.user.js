// ==UserScript==
// @name         FootprintBiot Spike Hook
// @namespace    footprintbiot
// @version      0.1
// @description  Phase 0b spike — taps GoCharting WebSocket + fetch traffic, dumps frames to console and forwards likely-footprint frames to local Flask spike server.
// @match        https://gocharting.com/*
// @match        https://*.gocharting.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  const SPIKE_URL = 'https://localhost:5001/spike_ingest';
  const seen = new Set();

  function looksLikeFootprint(obj) {
    if (!obj || typeof obj !== 'object') return false;
    const blob = JSON.stringify(obj);
    return /bid[_]?vol|ask[_]?vol|footprint|ladder|orderflow/i.test(blob);
  }

  function forward(source, raw) {
    let data;
    try { data = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return; }
    if (!looksLikeFootprint(data)) return;
    const key = source + ':' + JSON.stringify(data).slice(0, 200);
    if (seen.has(key)) return;
    seen.add(key);
    console.log('[fb-spike]', source, data);
    fetch(SPIKE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format: 'userscript_v1', source, payload: data }),
    }).catch(() => {});
  }

  // ---- WebSocket hook ----
  const OrigWS = window.WebSocket;
  window.WebSocket = function (url, proto) {
    const ws = new OrigWS(url, proto);
    ws.addEventListener('message', (ev) => forward('ws:' + url, ev.data));
    return ws;
  };
  window.WebSocket.prototype = OrigWS.prototype;

  // ---- fetch hook ----
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    try {
      const clone = res.clone();
      const txt = await clone.text();
      forward('fetch:' + (args[0]?.url || args[0]), txt);
    } catch {}
    return res;
  };

  // ---- XHR hook ----
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) {
    this._fb_url = u;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', () => forward('xhr:' + this._fb_url, this.responseText));
    return origSend.apply(this, arguments);
  };

  console.log('[fb-spike] hooks installed');
})();

// ==UserScript==
// @name         FootprintBiot Bridge
// @namespace    footprintbiot
// @version      0.1
// @description  Production WS/fetch hook — forwards GoCharting footprint frames to Flask /ingest.
// @match        https://*.gocharting.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  // ---- config ----
  const FLASK_URL = 'http://localhost:5000/ingest';
  const SYMBOL = 'NQ';    // TODO: read from URL or page state
  const TF = '1m';        // TODO: detect from active chart
  // URL substrings whose responses are candidates. Set narrowly after Phase 0b
  // identifies the real footprint endpoint(s). Empty list = filter disabled.
  const URL_HINTS = [];   // e.g. ['/footprint', '/orderflow']

  function urlMatches(url) {
    if (!URL_HINTS.length) return true;       // disabled until set
    return URL_HINTS.some((h) => String(url).includes(h));
  }

  // TODO: fill in after Phase 0b spike identifies the exact footprint frame schema.
  function isFootprintFrame(obj) {
    if (!obj || typeof obj !== 'object') return false;
    return false;   // STUB — replace with real predicate
  }

  // TODO: extract { close_ts, bid_ladder, ask_ladder, ohlc } from the raw frame.
  function extractBar(raw) {
    return null;    // STUB
  }

  // ---- core ----
  async function sha1(s) {
    const buf = new TextEncoder().encode(s);
    const hash = await crypto.subtle.digest('SHA-1', buf);
    return Array.from(new Uint8Array(hash))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  }

  function isReplayMode() {
    // TODO: detect GoCharting replay mode (URL fragment, DOM, or page state).
    return false;
  }

  async function forward(raw) {
    let obj;
    try { obj = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return; }
    if (!isFootprintFrame(obj)) return;
    const bar = extractBar(obj);
    if (!bar) return;

    const bar_id = await sha1(`${SYMBOL}|${TF}|${bar.close_ts}`);
    const payload = {
      format: 'userscript_v1',
      source: isReplayMode() ? 'replay' : 'live',
      bar_id,
      symbol: SYMBOL,
      tf: TF,
      close_ts: bar.close_ts,
      raw_frame: obj,
    };
    fetch(FLASK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).catch((e) => console.warn('[fb] forward failed', e));
  }

  // ---- hooks ----
  const OrigWS = window.WebSocket;
  window.WebSocket = function (url, proto) {
    const ws = new OrigWS(url, proto);
    ws.addEventListener('message', (ev) => forward(ev.data));
    return ws;
  };
  window.WebSocket.prototype = OrigWS.prototype;

  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    const url = args[0]?.url ?? args[0];
    if (urlMatches(url)) {
      try { forward(await res.clone().text()); } catch {}
    }
    return res;
  };

  console.log('[fb] bridge installed');
})();

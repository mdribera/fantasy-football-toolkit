// Fallback draft mirror: watches ESPN's own pick-history panel in Mark's
// draft-room tab and POSTs each new sale to the console's local listener.
// Never touches the websocket, the token, or cookies -- reads only what's
// already rendered on screen. See docs/draft-ws-plan.md Workstream 4.
//
// The selectors below (pick-history/draft-history, row/li) are a starting
// guess, not confirmed against the real draft room DOM -- Rehearsal 1
// pastes this into DevTools during a real practice draft and fixes them
// against what's actually there before this is trusted for anything.
(function () {
  const PORT = 8765;
  const seen = new Set();

  function findHistoryPanel() {
    return document.querySelector('[class*="pick-history"], [class*="draft-history"]');
  }

  function parseRow(row) {
    const text = row.textContent.trim();
    const priceMatch = text.match(/\$(\d+)/);
    if (!priceMatch) return null;
    return { raw: text, price: parseInt(priceMatch[1], 10) };
  }

  function onMutations() {
    const panel = findHistoryPanel();
    if (!panel) return;
    for (const row of panel.querySelectorAll('[class*="row"], li')) {
      const key = row.textContent.trim();
      if (seen.has(key)) continue;
      const parsed = parseRow(row);
      if (!parsed) continue;
      seen.add(key);
      fetch(`http://127.0.0.1:${PORT}/pick`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(parsed),
      }).catch((err) => console.warn('mirror POST failed', err));
    }
  }

  const panel = findHistoryPanel();
  if (!panel) {
    console.warn('mirror: pick-history panel not found -- selector needs updating for this draft room build');
    return;
  }
  new MutationObserver(onMutations).observe(panel, { childList: true, subtree: true });
  console.log('mirror: watching pick history, POSTing to 127.0.0.1:' + PORT);
})();

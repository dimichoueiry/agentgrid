/* Page <-> extension bridge for screenshots.
 *
 * The overlay runs in the page's main world and cannot capture the screen, so
 * it posts a window message; this content script (isolated world) relays it to
 * the background service worker and posts the resulting image back. Correlated
 * by reqId so several captures never cross wires.
 */
(function () {
  "use strict";
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (e.source !== window || !d || d.source !== "ag-review" || d.type !== "capture") return;
    chrome.runtime.sendMessage({ type: "ag-capture" }, function (res) {
      window.postMessage({
        source: "ag-review-ext",
        type: "capture-result",
        reqId: d.reqId,
        dataUrl: (res && res.dataUrl) || null,
      }, "*");
    });
  });
})();

/* Service worker: turns the hotkey (or toolbar click) into "toggle the review
 * overlay on the current tab". It reads the AgentGrid origin + token that
 * capture.js stashed, then injects the overlay into the page's main world.
 */

function toggleOnActiveTab() {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    var tab = tabs[0];
    if (!tab || !tab.id) return;
    chrome.storage.local.get(["agOrigin", "agToken"], function (cfg) {
      if (!cfg.agOrigin || !cfg.agToken) {
        // Not linked yet: show a one-line banner telling the user what to do.
        chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: "MAIN",
          func: showLinkHint,
        }).catch(function () {});
        return;
      }
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: injectOverlay,
        args: [{ origin: cfg.agOrigin, token: cfg.agToken }],
      }).catch(function () {});
    });
  });
}

// Runs in the page's main world. Sets the config the overlay reads, then loads
// overlay.js from AgentGrid. Re-invoking toggles the overlay instead of stacking.
function injectOverlay(cfg) {
  if (window.__agReviewMounted) {
    if (window.__agReviewToggle) window.__agReviewToggle();
    return;
  }
  window.__AG_REVIEW__ = { origin: cfg.origin, token: cfg.token, ext: true };
  var s = document.createElement("script");
  s.src = cfg.origin + "/overlay.js?ag=" + encodeURIComponent(cfg.origin) +
    "&t=" + encodeURIComponent(cfg.token) + "&_=" + Date.now();
  (document.body || document.documentElement).appendChild(s);
}

// Runs in the page's main world when no AgentGrid link is stored yet.
function showLinkHint() {
  var id = "ag-review-link-hint";
  if (document.getElementById(id)) return;
  var d = document.createElement("div");
  d.id = id;
  d.textContent = "Open the AgentGrid board in a tab once, then press the hotkey again.";
  d.style.cssText =
    "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483647;" +
    "background:#18181b;color:#fff;font:14px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
    "padding:11px 16px;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.35);max-width:90vw;";
  document.documentElement.appendChild(d);
  setTimeout(function () { d.remove(); }, 4200);
}

// The overlay (page main world) asks for a screenshot through bridge.js, which
// relays here; only the extension can capture the visible tab.
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (!msg || msg.type !== "ag-capture") return;
  var winId = sender.tab ? sender.tab.windowId : undefined;
  chrome.tabs.captureVisibleTab(winId, { format: "png" }, function (dataUrl) {
    if (chrome.runtime.lastError) {
      sendResponse({ dataUrl: null, error: String(chrome.runtime.lastError.message || "") });
    } else {
      sendResponse({ dataUrl: dataUrl });
    }
  });
  return true; // keep the channel open for the async capture
});

chrome.commands.onCommand.addListener(function (command) {
  if (command === "toggle-overlay") toggleOnActiveTab();
});
chrome.action.onClicked.addListener(function () {
  toggleOnActiveTab();
});

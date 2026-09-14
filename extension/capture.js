/* Content script on loopback origins.
 *
 * When you open the AgentGrid board (its URL carries ?t=<token>), grab the
 * origin + token and stash them, so the hotkey can inject the overlay onto any
 * other localhost tab without you pasting anything. It only trusts a token it
 * can confirm belongs to a real AgentGrid server, so a dev site that happens to
 * have a ?t= param is never mistaken for AgentGrid.
 */
(function () {
  "use strict";
  try {
    var token = new URLSearchParams(location.search).get("t");
    if (!token) return;
    var origin = location.origin;
    fetch(origin + "/api/models?t=" + encodeURIComponent(token))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.models) {
          chrome.storage.local.set({ agOrigin: origin, agToken: token, agSeenAt: Date.now() });
        }
      })
      .catch(function () {});
  } catch (e) {}
})();

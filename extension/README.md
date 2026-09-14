# AgentGrid Web Review — Chrome extension

Press a hotkey on any localhost page to open the review overlay: highlight text
or click near anything to leave a comment, then release the batch to an
AgentGrid agent. No bookmarklet, no copying URLs.

## Install (once)

1. Open `chrome://extensions` in Chrome.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and choose this `extension/` folder.
4. (Optional) At `chrome://extensions/shortcuts` confirm the toggle is
   **Cmd+K** (mac) / **Ctrl+K** (Windows/Linux), or rebind it.

## Use

1. Start AgentGrid (`ag --web`) and open its board tab once. The extension
   silently learns the server's address and token from that tab — you never
   paste anything.
2. Go to the localhost page you're building.
3. Press **Cmd+K** (or **Ctrl+K**). The **Review** pill appears and arms.
   Press it again to toggle off.
4. Highlight text or click near anything (images included) to comment. Open the
   tray via the pill's number badge, pick the agent working on this project, and
   hit **Release**.

## Notes

- The token rotates each time AgentGrid restarts. Just reopen the board tab
  once after a restart; the extension re-learns it automatically.
- The overlay loads `overlay.js` from AgentGrid, so a dev site with a strict
  Content-Security-Policy blocking external scripts can stop it. Plain localhost
  dev servers are unaffected.
- The extension only touches `localhost` and `127.0.0.1`, and only reads a token
  it can confirm belongs to a running AgentGrid server.

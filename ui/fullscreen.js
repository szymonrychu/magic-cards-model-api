// The fullscreen button, first in `#game-toolbar`.
//
// A phone in landscape loses a strip of its height to the browser's own bars.
// Where the Fullscreen API exists (`document.fullscreenEnabled`: Chrome and
// Firefox on Android, Samsung Internet, iPadOS, desktop browsers) this button
// takes the whole page fullscreen with the navigation UI hidden, then asks for
// landscape - `screen.orientation.lock` only works while fullscreen, and only
// on Chrome-based Android browsers, so its refusal is expected and ignored.
// iPhone Safari has no element fullscreen at all: there the button stays
// hidden and Add to Home Screen (`manifest.webmanifest`) is the way to get
// the room back.
//
// Every request can be refused (no user activation, a frame without the
// permission, an engine that throws), and none of that is worth a console
// error: each promise's rejection is caught and dropped.
//
// A reload always leaves fullscreen, and fullscreen can only be requested from
// inside a user gesture. So the choice is remembered (`mcm.fullscreen` in
// localStorage), and after a reload the next click anywhere re-requests it -
// once, from a passive capture-phase listener that never stops or prevents
// that click, so the card, button or menu it was aimed at still gets it. A
// long press's swallowed click (`preview.js`, stopped at `window` capture)
// never reaches this listener, so it neither triggers nor disarms it.
// Leaving fullscreen - by this button, or by the system back gesture or
// Escape - forgets the choice; the exit a page unload causes does not.

const PREF_KEY = "mcm.fullscreen";

function readPref() {
  try {
    return globalThis.localStorage.getItem(PREF_KEY) === "1";
  } catch {
    return false;
  }
}

function writePref(on) {
  try {
    if (on) globalThis.localStorage.setItem(PREF_KEY, "1");
    else globalThis.localStorage.removeItem(PREF_KEY);
  } catch {
    // Storage blocked (private mode, a sandboxed frame): nothing to remember.
  }
}

/** Calls `fn`, swallowing a synchronous throw and a rejected promise alike.
 * Resolves `true` when `fn` neither threw nor rejected. */
function quietly(fn) {
  try {
    return Promise.resolve(fn()).then(
      () => true,
      () => false,
    );
  } catch {
    return Promise.resolve(false);
  }
}

export function mountFullscreen(doc) {
  const toolbar = doc.querySelector("#game-toolbar");
  if (!toolbar) return;

  const button = doc.createElement("button");
  button.setAttribute("id", "fullscreen-toggle");
  button.setAttribute("type", "button");
  button.className = "ghost fullscreen-toggle";
  const glyph = doc.createElement("span");
  glyph.className = "fullscreen-glyph";
  glyph.setAttribute("aria-hidden", "true");
  button.appendChild(glyph);
  toolbar.insertBefore(button, toolbar.firstChild);

  const enabled = doc.fullscreenEnabled === true;
  button.hidden = !enabled;

  const sync = () => {
    const on = Boolean(doc.fullscreenElement);
    const label = on ? "Exit fullscreen" : "Enter fullscreen";
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
  };

  const enter = () => {
    const root = doc.documentElement;
    quietly(() => root.requestFullscreen({ navigationUI: "hide" })).then((ok) => {
      if (!ok) {
        writePref(false);
        sync();
        return;
      }
      const orientation = globalThis.screen && globalThis.screen.orientation;
      if (orientation && typeof orientation.lock === "function") {
        quietly(() => orientation.lock("landscape"));
      }
    });
  };

  const RESUME_OPTIONS = { capture: true, passive: true };
  let armed = false;
  const disarm = () => {
    if (!armed) return;
    armed = false;
    doc.removeEventListener("click", resume, RESUME_OPTIONS);
  };
  // The click that re-enters after a reload. The button's own click is left
  // to the button, which would otherwise request a second time.
  function resume(event) {
    disarm();
    if (button.contains(event.target) || doc.fullscreenElement) return;
    enter();
  }
  const arm = () => {
    if (armed || !enabled || !readPref() || doc.fullscreenElement) return;
    armed = true;
    doc.addEventListener("click", resume, RESUME_OPTIONS);
  };

  button.addEventListener("click", () => {
    disarm();
    if (doc.fullscreenElement) {
      writePref(false);
      quietly(() => doc.exitFullscreen());
    } else {
      writePref(true);
      enter();
    }
  });

  let unloading = false;
  doc.addEventListener("fullscreenchange", () => {
    if (!doc.fullscreenElement && !unloading) writePref(false);
    sync();
  });
  const win = globalThis.window;
  if (win && typeof win.addEventListener === "function") {
    win.addEventListener("pagehide", () => {
      unloading = true;
    });
    // Back from the back-forward cache: the page is live again, out of
    // fullscreen, and the next click may re-enter.
    win.addEventListener("pageshow", () => {
      unloading = false;
      sync();
      arm();
    });
  }

  sync();
  arm();
}

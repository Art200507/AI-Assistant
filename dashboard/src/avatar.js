import { createHologram } from "./hologram.js";

// Routing tier -> hologram color, reusing the prototype's own spectrum
// names/values (jarvis-interface's color swatches) so "what tier is
// active" has a real, distinct visual identity instead of one flat color:
// cyan for the fast tier (its default), violet for cloud/Claude reasoning
// (set apart as the "heavier" tier), amber for local Claude (warm = this
// machine, not the server), white for plain grounded lookups.
export const ROUTE_COLOR = {
  simple: "#79edff",
  reasoning: "#bc9aff",
  local: "#ff9c42",
  lookup: "#e8f8ff",
};

// Voice state -> hologram mode. Orbital is the calm default; neural (a
// connected particle field) reads as "actively processing", which is
// exactly what thinking/speaking are — lattice (rigid structural
// geometry) never fit any real JARVIS state on its own, which is why it's
// only reachable manually (see setManualMode) rather than auto-driven.
const STATE_MODE = {
  idle: "orbital",
  listening: "orbital",
  thinking: "neural",
  speaking: "neural",
};

// Baseline energy per voice state, same idea as the old avatar.js's
// per-state wobble amplitude. Speaking adds the real mic/TTS amplitude
// on top (see setState) instead of using a flat number.
const STATE_ENERGY = {
  idle: 0.35,
  listening: 0.55,
  thinking: 0.75,
  speaking: 0.6,
};

export function createAvatar(container) {
  const engine = createHologram(container, {});
  engine.state.auto = true;
  engine.state.motion = true;

  // Drag-to-rotate — engine.drag()/cursor()/clearCursor() were already in
  // hologram.js (ported along with everything else, just never wired to
  // real pointer events since the Dashboard started as a passive status
  // display). Ported from the prototype's own app.js pointer handlers,
  // same math, just attached to this container instead of its #scene div.
  let dragging = false;
  let lastX = 0;
  let lastY = 0;
  const onPointerDown = (e) => {
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
    container.setPointerCapture(e.pointerId);
    container.style.cursor = "grabbing";
  };
  const onPointerMove = (e) => {
    if (!dragging) return;
    engine.drag(e.clientX - lastX, e.clientY - lastY);
    lastX = e.clientX;
    lastY = e.clientY;
  };
  const onPointerUp = () => {
    dragging = false;
    container.style.cursor = "grab";
  };
  const onPointerLeave = () => {
    if (!dragging) engine.clearCursor();
  };
  container.style.cursor = "grab";
  container.addEventListener("pointerdown", onPointerDown);
  container.addEventListener("pointermove", onPointerMove);
  container.addEventListener("pointerup", onPointerUp);
  container.addEventListener("pointercancel", onPointerUp);
  container.addEventListener("pointerleave", onPointerLeave);

  let currentState = "idle";
  let targetEnergy = STATE_ENERGY.idle;
  let currentRoute = null;
  let rafId = null;
  // Manual mode/color picks (the new CORE CONTROL panel) pin that one
  // dimension so the automatic voice-state/routing drivers stop touching
  // it, without disabling the other dimension's automatic behavior —
  // picking a color manually shouldn't also freeze the mode, and vice
  // versa. clearOverrides() (the AUTO button) releases both at once.
  let modeOverride = null;
  let colorOverride = null;
  let energyOverride = null;

  function setState(state, amplitude = 0) {
    if (state !== currentState) {
      currentState = state;
      engine.state.listening = state === "listening";
      if (!modeOverride) engine.setMode(STATE_MODE[state] || "orbital");
      if (state === "thinking" || state === "speaking") engine.burst(0.5);
    }
    if (energyOverride === null) {
      const base = STATE_ENERGY[state] ?? STATE_ENERGY.idle;
      targetEnergy = state === "speaking" ? base + amplitude * 0.6 : base;
    }
  }

  // Tier actually changed -> a scan sweep is a real, literal "routing
  // decision just happened" signal, not a decorative effect; reusing the
  // prototype's own scan() rather than inventing a second one. Still
  // scans on a tier change even under a color override, since the scan
  // itself carries real information independent of what color is showing.
  function setRouting(tier) {
    if (!tier || tier === currentRoute) return;
    currentRoute = tier;
    if (!colorOverride) {
      const hex = ROUTE_COLOR[tier];
      if (hex) engine.setColor(hex);
    }
    engine.scan();
  }

  function setManualMode(mode) {
    modeOverride = mode;
    engine.setMode(mode);
  }

  function setManualColor(hex) {
    colorOverride = hex;
    engine.setColor(hex);
  }

  // Expansion has no automatic driver to fight (nothing in setState/
  // setRouting ever touches it), so unlike mode/color it needs no
  // override flag — the slider can just call engine.expand() directly.
  function setManualExpansion(v) {
    engine.expand(v);
  }

  function setManualEnergy(v) {
    energyOverride = v;
    engine.state.energy = v;
  }

  function clearOverrides() {
    modeOverride = null;
    colorOverride = null;
    energyOverride = null;
    engine.setMode(STATE_MODE[currentState] || "orbital");
    if (currentRoute && ROUTE_COLOR[currentRoute]) engine.setColor(ROUTE_COLOR[currentRoute]);
    engine.expand(0.12);
  }

  function tick() {
    if (energyOverride === null) {
      engine.state.energy += (targetEnergy - engine.state.energy) * 0.12;
    }
    rafId = requestAnimationFrame(tick);
  }
  tick();

  function resize() {
    // createHologram observes `container` with its own ResizeObserver —
    // nothing to do here. Kept as a no-op so main.js's existing call
    // shape (if it ever resizes explicitly) doesn't break.
  }

  function dispose() {
    if (rafId) cancelAnimationFrame(rafId);
    container.removeEventListener("pointerdown", onPointerDown);
    container.removeEventListener("pointermove", onPointerMove);
    container.removeEventListener("pointerup", onPointerUp);
    container.removeEventListener("pointercancel", onPointerUp);
    container.removeEventListener("pointerleave", onPointerLeave);
    engine.dispose();
  }

  return {
    setState,
    setRouting,
    setManualMode,
    setManualColor,
    setManualExpansion,
    setManualEnergy,
    clearOverrides,
    resize,
    dispose,
  };
}

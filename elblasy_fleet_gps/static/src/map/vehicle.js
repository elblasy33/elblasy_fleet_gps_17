/** @odoo-module **/
// Vehicle state, colours and map markers (ported from the Tracar dashboard: vstate / vehicle / MapView).
import { _t } from "@web/core/l10n/translation";

const FRESH_MS = 5 * 60000; // a repeated old fix keeps the speed it had back then: that is not "moving now"
const MOVING_KMH = 2;

/** Font Awesome 4 icon names (Odoo 17). */
export const CATEGORY_ICON = {
    default: "location-arrow", car: "car", pickup: "car", van: "bus",
    truck: "truck", container: "truck", bus: "bus", trolleybus: "bus",
    motorcycle: "motorcycle", scooter: "motorcycle", bicycle: "motorcycle",
    tractor: "wrench", crane: "wrench", offroad: "wrench", camper: "bus",
    boat: "ship", ship: "ship", plane: "plane", helicopter: "plane",
    train: "train", tram: "train", person: "user", animal: "paw",
};

export const STATES = {
    moving: _t("Moving"),
    over: _t("Over speed"),
    idle: _t("Idling"),
    stopped: _t("Stopped"),
    offline: _t("Offline"),
    nofix: _t("No position"),
};

export const hasPosition = (d) => !(d.lat === 0 && d.lon === 0) && Number.isFinite(d.lat) && Number.isFinite(d.lon) && !!d.fix_time;

const utc = (s) => (s ? Date.parse(String(s).replace(" ", "T") + "Z") : NaN);
export const utcMs = utc;

/** {vs: visual state, color}. Colours come from the settings (cfg.colors); a colour set on the device wins in "device" mode. */
export function vehicleState(d, cfg, colorMode = "state", now = Date.now()) {
    let vs;
    if (d.status !== "online") {
        vs = "offline";
    } else if (!hasPosition(d)) {
        vs = "nofix";
    } else {
        const fresh = now - utc(d.fix_time) < FRESH_MS;
        if (fresh && d.speed > MOVING_KMH) {
            vs = d.speed > cfg.speed_limit ? "over" : "moving";
        } else {
            vs = d.ignition ? "idle" : "stopped";
        }
    }
    const palette = cfg.colors;
    const color = (colorMode === "device" && d.color) || palette[vs] || palette.offline;
    return { vs, color };
}

export function timeAgo(value, now = Date.now()) {
    const t = utc(value);
    if (!Number.isFinite(t)) {
        return "—";
    }
    const s = Math.max(0, (now - t) / 1000);
    if (s < 60) {
        return _t("just now");
    }
    const [n, unit] = s < 3600 ? [Math.floor(s / 60), _t("min")] : s < 86400 ? [Math.floor(s / 3600), _t("h")] : [Math.floor(s / 86400), _t("d")];
    return `${n} ${unit}`;
}

// Top-down container truck: rotates with the heading instead of an upright icon in a badge
const TOP_VIEW = {
    container: {
        w: 26, h: 66,
        svg: (c) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 26 66" width="26" height="66">
      <g fill="#05080f">
        <rect x="0.5" y="6" width="3" height="7" rx="1"/><rect x="22.5" y="6" width="3" height="7" rx="1"/>
        <rect x="0.5" y="26" width="3" height="6" rx="1"/><rect x="22.5" y="26" width="3" height="6" rx="1"/>
        <rect x="0.5" y="52" width="3" height="6" rx="1"/><rect x="22.5" y="52" width="3" height="6" rx="1"/>
        <rect x="0.5" y="59" width="3" height="6" rx="1"/><rect x="22.5" y="59" width="3" height="6" rx="1"/>
        <rect x="10" y="14" width="6" height="12"/>
      </g>
      <rect x="3" y="24" width="20" height="41" rx="1.6" fill="${c}" stroke="#fff" stroke-width="1.3"/>
      ${[7, 10.5, 14, 17.5, 21].map((x) => `<line x1="${x - 1}" y1="26.5" x2="${x - 1}" y2="62.5" stroke="rgba(0,0,0,.3)" stroke-width="1.1"/>`).join("")}
      <rect x="3" y="24" width="20" height="3" fill="rgba(0,0,0,.28)"/><rect x="3" y="62" width="20" height="3" fill="rgba(0,0,0,.28)"/>
      <rect x="4.5" y="1.5" width="17" height="19" rx="4.5" fill="${c}" stroke="#fff" stroke-width="1.3"/>
      <path d="M6.5 5.5q6.5-2 13 0l.6 5.2h-14.2z" fill="#0b1220" opacity=".9"/>
      <rect x="6" y="13" width="14" height="5" rx="1.2" fill="rgba(0,0,0,.28)"/>
      <rect x="1.4" y="3.5" width="2.4" height="3.6" rx=".8" fill="#e2e8f0"/><rect x="22.2" y="3.5" width="2.4" height="3.6" rx=".8" fill="#e2e8f0"/>
    </svg>`,
    },
};

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/** Marker content; the heading (course) is applied separately so a rotation never rebuilds the DOM. */
export function markerHtml(d, { vs, color }, { selected, label, showLabel }) {
    const top = TOP_VIEW[d.category];
    const cls = `o_gps_veh ${vs}${selected ? " sel" : ""}${top ? " top" : ""}`;
    const lbl = showLabel ? `<div class="o_gps_lbl">${esc(label)}</div>` : "";
    if (top) {
        return `<div class="${cls}" style="--c:${color}">${selected ? '<div class="o_gps_ring"></div>' : ""}
          <div class="o_gps_heading" style="transform:rotate(${d.course || 0}deg)"><div class="o_gps_shape" style="width:${top.w}px;height:${top.h}px;margin:${-top.h / 2}px 0 0 ${-top.w / 2}px">${top.svg(color)}</div></div>${lbl}</div>`;
    }
    const icon = CATEGORY_ICON[d.category] || CATEGORY_ICON.default;
    return `<div class="${cls}" style="--c:${color}">
      <div class="o_gps_heading" style="transform:rotate(${d.course || 0}deg)"><i></i></div>
      <div class="o_gps_badge"><i class="fa fa-${icon}"></i></div>${lbl}</div>`;
}

// How long a marker glides to a new fix: about the last update interval, so it arrives when the next fix does.
const MIN_MS = 1000, MAX_MS = 30000, PAUSED_MS = 90000, MAX_SPEED = 60;
export function glideMs(sinceLastMoveMs, distanceM) {
    if (sinceLastMoveMs >= PAUSED_MS) {
        return MIN_MS;
    }
    const ms = Math.min(MAX_MS, Math.max(MIN_MS, sinceLastMoveMs));
    return distanceM / (ms / 1000) > MAX_SPEED ? MIN_MS : ms;
}

export function distanceM(a, b) {
    const r = Math.PI / 180;
    const x = Math.sin(((b[1] - a[1]) * r) / 2) ** 2 + Math.cos(a[1] * r) * Math.cos(b[1] * r) * Math.sin(((b[0] - a[0]) * r) / 2) ** 2;
    return 6371000 * 2 * Math.asin(Math.sqrt(x));
}

export const lerpAngle = (a, b, t) => a + ((((b - a) % 360) + 540) % 360 - 180) * t;
export const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

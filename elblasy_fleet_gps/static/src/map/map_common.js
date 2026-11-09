/** @odoo-module **/
import { session } from "@web/session";

const RTL_PLUGIN = "/elblasy_fleet_gps/static/lib/maplibre/mapbox-gl-rtl-text.js";
export const mapLang = () => (((session.user_context && session.user_context.lang) || document.documentElement.lang || "en").toLowerCase().startsWith("ar") ? "ar" : "en");

/** Without this plugin MapLibre draws Arabic letters one by one, unjoined and in the wrong order. Loaded once, only when needed. */
export function enableArabicShaping() {
    const gl = globalThis.maplibregl;
    if (gl && gl.getRTLTextPluginStatus?.() === "unavailable") {
        gl.setRTLTextPlugin(new URL(RTL_PLUGIN, location.origin).href, true).catch(() => {});
    }
}

export const hasWebGL = () => {
    try {
        const c = document.createElement("canvas");
        return !!(c.getContext("webgl2") || c.getContext("webgl"));
    } catch {
        return false;
    }
};



/**
 * Run `fn` (which adds sources / layers) as soon as the style JSON is there. `map.isStyleLoaded()` is no good for this: it
 * is false while any map tile is still downloading, so a layer drawn at that moment would silently never appear.
 */
export function withStyle(map, fn) {
    try {
        fn();
    } catch (e) {
        if (/not done loading/i.test(e?.message || "")) {
            map.once("styledata", () => withStyle(map, fn));
        } else {
            throw e;
        }
    }
}

/**
 * MapLibre opens the attribution box in full on every size; it then covers the legend and a quarter of a phone screen.
 * Keep it as the small (i) button: the credits stay one tap away, as the licences require.
 */
export function collapseAttribution(map) {
    const fold = () => {
        const el = map.getContainer().querySelector(".maplibregl-ctrl-attrib");
        el?.classList.remove("maplibregl-compact-show");
        el?.removeAttribute("open");
    };
    map.once("load", () => setTimeout(fold, 0));
    map.once("idle", fold);
}

/**
 * Vehicle icons follow the ground size when zoomed in and stop shrinking at a readable minimum (the top-view truck is drawn
 * 66 px long for a ~16 m vehicle). `percent` is the user-chosen size (setting, 140 by default) on top of that.
 */
export function applyIconScale(map, root, percent = 140) {
    if (!map || !root) {
        return;
    }
    const k = Math.min(2.5, Math.max(0.5, percent / 100));
    const mpp = (156543.03 * Math.cos((map.getCenter().lat * Math.PI) / 180)) / Math.pow(2, map.getZoom()); // metres per pixel
    const real = 16 / mpp / 66;
    root.style.setProperty("--gps-s", (Math.min(1.6, Math.max(0.4, real)) * k).toFixed(3));
    root.style.setProperty("--gps-sb", (Math.min(1.2, Math.max(0.65, real)) * k).toFixed(3));
}

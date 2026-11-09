/** @odoo-module **/
// Map styles. Every source works without an API key: OpenFreeMap (vector, OpenStreetMap data), Esri imagery, OpenTopoMap.
import { _t } from "@web/core/l10n/translation";

const OFM = "https://tiles.openfreemap.org/styles";
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
const OFM_ATTR = '© <a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> © <a href="https://www.openmaptiles.org/" target="_blank" rel="noreferrer">OpenMapTiles</a> Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>';

export const STYLE_LIST = [
    { key: "sat", label: _t("Satellite"), icon: "globe", maxZoom: 19 },
    { key: "light", label: _t("Light"), icon: "map-o", maxZoom: 20 },
    { key: "dark", label: _t("Dark"), icon: "moon-o", maxZoom: 20 },
    { key: "topo", label: _t("Terrain"), icon: "picture-o", maxZoom: 17 },
];

const raster = (tiles, attribution, maxzoom) => ({
    version: 8,
    sources: { r: { type: "raster", tiles, tileSize: 256, maxzoom, attribution } },
    layers: [{ id: "r", type: "raster", source: "r" }],
});

/** Liberty with its land/water fills replaced by satellite imagery; only roads and labels stay on top. */
async function hybridStyle() {
    const style = await (await fetch(`${OFM}/liberty`)).json();
    const roadLine = (id) => /^(road|bridge|tunnel)_/.test(id) && !/casing|rail|hatching|path|arrow|shield/.test(id);
    const layers = style.layers
        .filter((l) => (l.type === "line" && roadLine(l.id)) || (l.type === "symbol" && !l.id.startsWith("poi")))
        .map((l) =>
            l.type === "symbol"
                ? { ...l, paint: { ...l.paint, "text-color": "#ffffff", "text-halo-color": "rgba(0,0,0,.85)", "text-halo-width": 1.6 } }
                : { ...l, paint: { ...l.paint, "line-opacity": 0.55 } }
        );
    return {
        ...style,
        sources: {
            ...style.sources,
            esri: { type: "raster", tileSize: 256, maxzoom: 18, attribution: `Imagery © Esri, Maxar, Earthstar Geographics · ${OFM_ATTR}`, tiles: [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`] },
        },
        layers: [{ id: "satellite", type: "raster", source: "esri" }, ...layers],
    };
}

/**
 * OpenFreeMap labels carry two names (Latin over the local script). Show one, in the user's language: Arabic when the
 * interface is Arabic, English otherwise, falling back to the local name where a place has no translation.
 */
export function localizeLabels(style, lang) {
    const field = lang === "ar"
        ? ["coalesce", ["get", "name:ar"], ["get", "name"]]
        : ["coalesce", ["get", "name_en"], ["get", "name:latin"], ["get", "name"]];
    for (const l of style.layers || []) {
        if (l.type === "symbol" && l.layout?.["text-field"] && /name/.test(JSON.stringify(l.layout["text-field"]))) {
            l.layout["text-field"] = field;
        }
    }
    return style;
}

/** A style value for map.setStyle(): a URL, or an object. Falls back to plain raster tiles when the vector style is unreachable. */
export async function resolveStyle(key, lang = "en") {
    try {
        if (key === "sat") {
            return localizeLabels(await hybridStyle(), lang);
        }
        if (key === "topo") {
            return raster(["a", "b", "c"].map((s) => `https://${s}.tile.opentopomap.org/{z}/{x}/{y}.png`), "© OpenTopoMap (CC-BY-SA)", 17);
        }
        const url = `${OFM}/${key === "dark" ? "dark" : "liberty"}`;
        const r = await fetch(url);
        if (!r.ok) {
            throw new Error(r.status);
        }
        return localizeLabels(await r.json(), lang);
    } catch {
        const t = { sat: "World_Imagery", dark: "Canvas/World_Dark_Gray_Base", light: "World_Street_Map" }[key] || "World_Street_Map";
        return raster([`${ESRI}/${t}/MapServer/tile/{z}/{y}/{x}`], "© Esri", 18);
    }
}

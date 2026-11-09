/** @odoo-module **/
// Traccar stores geofence areas as WKT with "lat lon" order: CIRCLE (lat lon, radius), POLYGON ((lat lon, ...)), LINESTRING (lat lon, ...)
const R = 6371008.8;

function circle(lat, lon, radius, steps = 64) {
    const ring = [];
    const dLat = (radius / R) * (180 / Math.PI);
    const dLon = dLat / Math.cos((lat * Math.PI) / 180);
    for (let i = 0; i <= steps; i++) {
        const a = (i / steps) * 2 * Math.PI;
        ring.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
    }
    return ring;
}

const pairs = (text) =>
    text.replace(/[()]/g, "").split(",").map((s) => s.trim().split(/\s+/).map(Number)).filter((p) => p.length >= 2 && p.every(Number.isFinite)).map(([lat, lon]) => [lon, lat]);

/** A GeoJSON geometry for a Traccar area string, or null when it cannot be read. */
export function parseArea(area) {
    const m = String(area || "").match(/^\s*(\w+)\s*\(([\s\S]*)\)\s*$/);
    if (!m) {
        return null;
    }
    const type = m[1].toUpperCase();
    if (type === "CIRCLE") {
        const [c, r] = m[2].split(",");
        const [lat, lon] = c.trim().split(/\s+/).map(Number);
        const radius = Number(r);
        return [lat, lon, radius].every(Number.isFinite) ? { type: "Polygon", coordinates: [circle(lat, lon, radius)] } : null;
    }
    const pts = pairs(m[2]);
    if (type === "POLYGON" && pts.length >= 3) {
        return { type: "Polygon", coordinates: [[...pts, pts[0]]] };
    }
    return type === "LINESTRING" && pts.length >= 2 ? { type: "LineString", coordinates: pts } : null;
}

export function fencesToGeoJSON(list) {
    return {
        type: "FeatureCollection",
        features: list.map((g) => ({ type: "Feature", properties: { name: g.name }, geometry: parseArea(g.area) })).filter((f) => f.geometry),
    };
}

/** @odoo-module **/
import { Component, onMounted, onWillDestroy, onWillStart, useRef, useState } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { fencesToGeoJSON } from "./geofence";
import { STYLE_LIST, resolveStyle } from "./map_styles";
import { applyIconScale, collapseAttribution, enableArabicShaping, hasWebGL, mapLang, withStyle } from "./map_common";
import { STATES, distanceM, ease, glideMs, hasPosition, lerpAngle, markerHtml, timeAgo, utcMs, vehicleState } from "./vehicle";

const BUS_TYPE = "elblasy_fleet_gps/live";
const BUS_CHANNEL = "elblasy_fleet_gps";
const FULL_REFRESH_MS = 60000; // statuses can change without a position: refresh everything once a minute
const POLL_MS = 10000; // no push from Traccar: the open map asks Odoo to pull, at most this often
const TRAIL_POINTS = 40;
const FILTERS = ["all", "moving", "stopped", "offline"];

const store = {
    get(key, fallback) {
        try {
            return JSON.parse(localStorage.getItem(`fleet_gps.${key}`)) ?? fallback;
        } catch {
            return fallback;
        }
    },
    set(key, value) {
        try {
            localStorage.setItem(`fleet_gps.${key}`, JSON.stringify(value));
        } catch {
            /* private mode: the preference just is not remembered */
        }
    },
};

export class FleetGpsMap extends Component {
    static template = "elblasy_fleet_gps.Map";
    static props = ["*"];

    setup() {
        // Owl 2 calls template handlers with the render context as `this`: bind every method to the component
        const proto = Object.getPrototypeOf(this);
        for (const name of Object.getOwnPropertyNames(proto)) {
            const fn = Object.getOwnPropertyDescriptor(proto, name).value;
            if (name !== "constructor" && name !== "setup" && typeof fn === "function") {
                this[name] = fn.bind(this);
            }
        }
        this.mapRef = useRef("mapRef");
        this.rootRef = useRef("rootRef");
        this.orm = useService("orm");
        this.action = useService("action");
        this.bus = this.env.services.bus_service;
        this.notification = useService("notification");
        this.state = useState({
            loading: true,
            error: "",
            noGl: false,
            devices: {}, // id -> live record
            cfg: { speed_limit: 80, icon_size: 140, colors: { moving: "#23e76a", idle: "#f59e0b", stopped: "#27aef1", over: "#f20202", offline: "#727374" } },
            search: "",
            filter: "all",
            selected: 0,
            follow: store.get("follow", true),
            style: store.get("style", "sat"),
            styleMenu: false,
            labels: store.get("labels", true),
            trails: store.get("trails", false),
            fences: store.get("fences", true),
            extra: null, // since when / why quiet / address of the selected vehicle
            details: false,
            listOpen: false, // phones: the list is a sheet over the map
            tick: 0, // re-renders the "x min ago" texts
        });
        this.markers = new Map(); // device id -> { marker, el, from, to, t0, dur, cur, course, lastMove, key }
        this.trails = new Map(); // device id -> [[lon, lat], ...]
        this.cursor = null;
        this.fences = { type: "FeatureCollection", features: [] };
        this.raf = 0;
        this.refreshing = false;
        this.dirty = false;

        // a hidden tab costs the server nothing: updates wait until the user looks at the map again
        const refreshOnBus = () => (document.hidden ? (this.stale = true) : this.refresh());
        const onBus = ({ detail }) => detail.some((n) => n.type === BUS_TYPE) && refreshOnBus();
        const onVisible = () => {
            if (!document.hidden && this.stale) {
                this.stale = false;
                this.refresh(true);
            }
        };
        document.addEventListener("visibilitychange", onVisible);
        onWillStart(async () => {
            await Promise.all([loadBundle("elblasy_fleet_gps.map_lib"), this.refresh(true), this.loadFences()]);
            this.state.loading = false;
        });
        onMounted(() => {
            this.initMap();
            this.bus.addChannel(BUS_CHANNEL);
            this.bus.addEventListener("notification", onBus);
        });
        // safety nets: a full refresh (statuses, bus down) and the relative times
        this._full = setInterval(() => (document.hidden ? (this.stale = true) : this.refresh(true)), FULL_REFRESH_MS);
        this._tick = setInterval(() => this.state.tick++, 30000);
        this._poll = setInterval(() => this.pollIfNoPush(), POLL_MS);
        this._extra = setInterval(() => !document.hidden && this.loadExtra(), 30000);
        onWillDestroy(() => {
            this.bus.removeEventListener("notification", onBus);
            this.bus.deleteChannel(BUS_CHANNEL);
            document.removeEventListener("visibilitychange", onVisible);
            clearInterval(this._full);
            clearInterval(this._tick);
            clearInterval(this._poll);
            clearInterval(this._extra);
            cancelAnimationFrame(this.raf);
            this.resizeObserver?.disconnect();
            this.map?.remove();
            this.map = null;
        });
    }

    // ----------------------------------------------------------------------------------------------- data
    async refresh(full = false) {
        if (this.refreshing) {
            this.dirty = true; // answer again once the running call is back, so the last change is never missed
            return;
        }
        this.refreshing = true;
        try {
            const res = await this.orm.call("fleet.gps.device", "get_live_data", [], { since: full ? null : this.cursor });
            this.cursor = res.now;
            this.state.cfg = res.config;
            this.applyScale(); // the size setting may have changed
            const devices = res.full ? {} : { ...this.state.devices };
            for (const d of res.devices) {
                devices[d.id] = d;
            }
            this.state.devices = devices;
            this.state.error = "";
            this.syncMarkers(res.devices, res.full);
        } catch (e) {
            this.state.error = e?.data?.message || _t("Could not load the vehicles.");
        } finally {
            this.refreshing = false;
            if (this.dirty) {
                this.dirty = false;
                this.refresh();
            }
        }
    }

    async loadFences() {
        try {
            this.fences = fencesToGeoJSON(await this.orm.call("fleet.gps.geofence", "get_geofences", []));
        } catch {
            /* the map works without its fences */
        }
    }

    /** Traccar cannot reach this Odoo (no forward.url, or a local test): pull from it while someone is looking. */
    async pollIfNoPush() {
        if (document.hidden || this.state.cfg.push_active || this.polling) {
            return;
        }
        this.polling = true;
        try {
            if (await this.orm.call("fleet.gps.server", "poll_now", [])) {
                await this.refresh();
            }
        } catch {
            /* a failed pull is retried at the next tick */
        } finally {
            this.polling = false;
        }
    }

    // ----------------------------------------------------------------------------------------------- map
    async initMap() {
        const root = this.mapRef.el;
        if (!root || !globalThis.maplibregl) {
            return;
        }
        if (!hasWebGL()) {
            this.state.noGl = true;
            return;
        }
        enableArabicShaping();
        const style = await resolveStyle(this.state.style, mapLang());
        if (!this.mapRef.el) {
            return; // left the screen while the style was loading
        }
        this.map = new globalThis.maplibregl.Map({
            container: root, style, center: [31.2, 30.05], zoom: 5, attributionControl: { compact: true },
            maxZoom: STYLE_LIST.find((s) => s.key === this.state.style)?.maxZoom ?? 19,
        });
        this.map.addControl(new globalThis.maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
        collapseAttribution(this.map);
        // a tile or font that cannot be fetched (offline, blocked by a firewall) is not an error worth a red console: the map stays usable
        this.map.on("error", (e) => console.debug("map:", e?.error?.message || e));
        this.map.on("zoom", () => this.applyScale());
        this.map.on("dragstart", () => {
            if (this.state.follow && this.state.selected) {
                this.state.follow = false; // the user took the map: stop pulling it back
            }
        });
        this.map.on("style.load", () => {
            this.drawTrails();
            this.drawFences();
        });
        this.map.once("load", () => {
            this.applyScale();
            this.drawFences();
            this.syncMarkers(Object.values(this.state.devices), true);
            this.fitAll();
        });
        this.resizeObserver = new ResizeObserver(() => this.map?.resize());
        this.resizeObserver.observe(root);
    }

    applyScale() {
        applyIconScale(this.map, this.rootRef.el, this.state.cfg.icon_size);
    }

    markerKey(d, info) {
        return `${d.category}|${info.vs}|${info.color}|${this.state.selected === d.id}|${this.state.labels}|${d.plate || d.name}`;
    }

    syncMarkers(changed, full) {
        if (!this.map) {
            return;
        }
        const now = Date.now();
        const seen = new Set();
        for (const d of changed) {
            seen.add(d.id);
            const entry = this.markers.get(d.id);
            if (!hasPosition(d)) {
                if (entry) {
                    entry.marker.remove();
                    this.markers.delete(d.id);
                }
                continue;
            }
            const info = vehicleState(d, this.state.cfg, "state", now);
            const lngLat = [d.lon, d.lat];
            if (!entry) {
                const el = document.createElement("div");
                el.className = "o_gps_marker";
                el.addEventListener("click", (ev) => {
                    ev.stopPropagation();
                    this.select(d.id);
                });
                el.innerHTML = markerHtml(d, info, this.markerOpts(d));
                const marker = new globalThis.maplibregl.Marker({ element: el, anchor: "center" }).setLngLat(lngLat).addTo(this.map);
                this.markers.set(d.id, { marker, el, cur: lngLat, to: lngLat, from: lngLat, t0: 0, dur: 0, course: d.course, key: this.markerKey(d, info), lastMove: utcMs(d.fix_time) });
            } else {
                this.updateMarker(entry, d, info, lngLat);
            }
            this.pushTrail(d, lngLat);
        }
        if (full) {
            for (const [id, entry] of this.markers) {
                if (!seen.has(id) && !this.state.devices[id]) {
                    entry.marker.remove();
                    this.markers.delete(id);
                }
            }
        }
        this.drawTrails();
        if (this.state.follow && this.state.selected && seen.has(this.state.selected)) {
            const e = this.markers.get(this.state.selected);
            if (e) {
                this.map.easeTo({ center: e.to, duration: 800 });
            }
        }
        if (!this.raf) {
            this.raf = requestAnimationFrame((t) => this.frame(t));
        }
    }

    markerOpts(d) {
        return { selected: this.state.selected === d.id, label: d.plate || d.name, showLabel: this.state.labels };
    }

    updateMarker(entry, d, info, lngLat) {
        const key = this.markerKey(d, info);
        const oldCourse = entry.course;
        if (key !== entry.key) {
            entry.el.innerHTML = markerHtml(d, info, this.markerOpts(d));
            entry.key = key;
        }
        const heading = entry.el.querySelector(".o_gps_heading");
        entry.course = d.course || 0;
        if (heading && entry.course !== oldCourse) {
            heading.style.transform = `rotate(${entry.course}deg)`;
        }
        const [lon, lat] = entry.to;
        if (lon !== lngLat[0] || lat !== lngLat[1]) {
            const fix = utcMs(d.fix_time);
            const dist = distanceM(entry.cur, lngLat);
            entry.from = entry.cur;
            entry.to = lngLat;
            entry.t0 = performance.now();
            entry.dur = glideMs(Number.isFinite(fix) ? fix - entry.lastMove : 0, dist);
            entry.lastMove = Number.isFinite(fix) ? fix : entry.lastMove;
        }
    }

    frame(t) {
        this.raf = 0;
        let active = false;
        for (const e of this.markers.values()) {
            if (!e.dur) {
                continue;
            }
            const k = Math.min(1, (t - e.t0) / e.dur);
            const p = ease(k);
            e.cur = [e.from[0] + (e.to[0] - e.from[0]) * p, e.from[1] + (e.to[1] - e.from[1]) * p];
            e.marker.setLngLat(e.cur);
            if (k < 1) {
                active = true;
            } else {
                e.dur = 0;
            }
        }
        if (active && this.map) {
            this.raf = requestAnimationFrame((x) => this.frame(x));
        }
    }

    // trails: the last positions of each vehicle seen while the screen is open
    pushTrail(d, lngLat) {
        const list = this.trails.get(d.id) || [];
        const last = list.at(-1);
        if (!last || last[0] !== lngLat[0] || last[1] !== lngLat[1]) {
            list.push(lngLat);
            if (list.length > TRAIL_POINTS) {
                list.shift();
            }
            this.trails.set(d.id, list);
        }
    }

    drawTrails() {
        const m = this.map;
        if (!m) {
            return;
        }
        const data = {
            type: "FeatureCollection",
            features: this.state.trails
                ? [...this.trails.entries()].filter(([, l]) => l.length > 1).map(([id, l]) => ({
                    type: "Feature", properties: { c: vehicleState(this.state.devices[id] || {}, this.state.cfg).color }, geometry: { type: "LineString", coordinates: l },
                }))
                : [],
        };
        withStyle(m, () => {
            const src = m.getSource("gps-trails");
            if (src) {
                src.setData(data);
            } else {
                m.addSource("gps-trails", { type: "geojson", data });
                m.addLayer({ id: "gps-trails", type: "line", source: "gps-trails", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "c"], "line-width": 3, "line-opacity": 0.8 } });
            }
        });
    }

    drawFences() {
        const m = this.map;
        if (!m) {
            return;
        }
        const data = this.state.fences ? this.fences : { type: "FeatureCollection", features: [] };
        withStyle(m, () => {
            const src = m.getSource("gps-fences");
            if (src) {
                src.setData(data);
                return;
            }
            m.addSource("gps-fences", { type: "geojson", data });
            m.addLayer({ id: "gps-fences-fill", type: "fill", source: "gps-fences", filter: ["==", "$type", "Polygon"], paint: { "fill-color": "#8b5cf6", "fill-opacity": 0.14 } });
            m.addLayer({ id: "gps-fences-line", type: "line", source: "gps-fences", paint: { "line-color": "#8b5cf6", "line-width": 2 } });
        });
    }

    toggleFences() {
        this.state.fences = !this.state.fences;
        store.set("fences", this.state.fences);
        this.drawFences();
    }

    rebuildMarkers() {
        for (const [id, e] of this.markers) {
            const d = this.state.devices[id];
            if (d) {
                const info = vehicleState(d, this.state.cfg, "state");
                e.el.innerHTML = markerHtml(d, info, this.markerOpts(d));
                e.key = this.markerKey(d, info);
            }
        }
    }

    fitAll() {
        const pts = [...this.markers.values()].map((e) => e.to);
        if (!this.map || !pts.length) {
            return;
        }
        const b = new globalThis.maplibregl.LngLatBounds(pts[0], pts[0]);
        pts.forEach((p) => b.extend(p));
        this.map.fitBounds(b, { padding: 70, maxZoom: 16, duration: 600 });
    }

    // ----------------------------------------------------------------------------------------------- UI actions
    select(id) {
        const sameAgain = this.state.selected === id;
        this.state.selected = sameAgain ? 0 : id;
        this.state.extra = null;
        this.state.listOpen = false;
        if (!sameAgain) {
            this.loadExtra();
        }
        this.rebuildMarkers();
        if (!sameAgain) {
            const e = this.markers.get(id);
            if (e && this.map) {
                this.state.follow = true;
                this.map.flyTo({ center: e.to, zoom: Math.max(this.map.getZoom(), 15), duration: 900 });
            }
        }
    }

    closeCard() {
        this.state.selected = 0;
        this.state.extra = null;
        this.rebuildMarkers();
    }

    async loadExtra() {
        const id = this.state.selected;
        if (!id) {
            return;
        }
        try {
            const extra = await this.orm.call("fleet.gps.device", "get_card_extra", [id]);
            if (this.state.selected === id) {
                this.state.extra = extra;
            }
        } catch {
            /* the card works without these details */
        }
    }

    /** "2h 10m" since a past instant (ms). */
    since(ms) {
        void this.state.tick;
        if (!ms) {
            return "";
        }
        const total = Math.max(0, Math.floor((Date.now() - ms) / 60000));
        const d = Math.floor(total / 1440), h = Math.floor((total % 1440) / 60), m = total % 60;
        // an LTR isolate keeps "2 h 10 min" in one piece inside a right-to-left sentence
        const text = d ? `${d} ${_t("d")} ${h} ${_t("h")}` : h ? `${h} ${_t("h")} ${m} ${_t("min")}` : `${m} ${_t("min")}`;
        return `\u2066${text}\u2069`;
    }

    get silenceText() {
        const s = this.state.extra?.silence;
        return s === "power" ? _t("Power cut or empty battery") : s === "network" ? _t("Power was fine: likely a SIM or coverage problem") : s === "unknown" ? _t("Cause unknown") : "";
    }

    toggleDetails() {
        this.state.details = !this.state.details;
    }

    async copy(text) {
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            const ta = Object.assign(document.createElement("textarea"), { value: text, readOnly: true });
            ta.style.cssText = "position:fixed;opacity:0";
            document.body.append(ta);
            ta.select();
            document.execCommand("copy");
            ta.remove();
        }
        this.notification.add(_t("Copied"), { type: "success" });
    }

    toggleFollow() {
        this.state.follow = !this.state.follow;
        store.set("follow", this.state.follow);
        const e = this.markers.get(this.state.selected);
        if (this.state.follow && e) {
            this.map?.easeTo({ center: e.to, duration: 500 });
        }
    }

    toggleLabels() {
        this.state.labels = !this.state.labels;
        store.set("labels", this.state.labels);
        this.rebuildMarkers();
    }

    toggleTrails() {
        this.state.trails = !this.state.trails;
        store.set("trails", this.state.trails);
        this.drawTrails();
    }

    async setStyle(key) {
        this.state.styleMenu = false;
        if (key === this.state.style || !this.map) {
            return;
        }
        this.state.style = key;
        store.set("style", key);
        const def = STYLE_LIST.find((s) => s.key === key);
        this.map.setMaxZoom(def?.maxZoom ?? 19);
        this.map.setStyle(await resolveStyle(key, mapLang()));
    }

    toggleFullscreen() {
        const root = this.rootRef.el;
        if (document.fullscreenElement) {
            document.exitFullscreen();
        } else {
            root?.requestFullscreen?.();
        }
    }

    onSearch(ev) {
        this.state.search = ev.target.value;
    }

    openVehicle(d) {
        if (d.vehicle_id) {
            this.action.doAction({ type: "ir.actions.act_window", res_model: "fleet.vehicle", res_id: d.vehicle_id, views: [[false, "form"]] });
        } else {
            this.action.doAction({ type: "ir.actions.act_window", res_model: "fleet.gps.device", res_id: d.id, views: [[false, "form"]] });
        }
    }

    async openPlayback(d) {
        this.action.doAction(await this.orm.call("fleet.gps.device", "action_playback", [[d.id]]));
    }

    openMapsLink(d) {
        window.open(`https://www.google.com/maps/dir/?api=1&destination=${d.lat},${d.lon}`, "_blank", "noopener");
    }

    // ----------------------------------------------------------------------------------------------- view data
    info(d) {
        return vehicleState(d, this.state.cfg, "state");
    }

    stateLabel(d) {
        return STATES[this.info(d).vs];
    }

    ago(value) {
        void this.state.tick;
        return timeAgo(value);
    }

    get filters() {
        return FILTERS.map((key) => ({ key, label: { all: _t("All"), moving: _t("Moving"), stopped: _t("Stopped"), offline: _t("Offline") }[key], count: this.counts[key] }));
    }

    get counts() {
        const c = { all: 0, moving: 0, stopped: 0, offline: 0 };
        for (const d of Object.values(this.state.devices)) {
            const vs = this.info(d).vs;
            c.all++;
            c[vs === "moving" || vs === "over" ? "moving" : vs === "offline" ? "offline" : "stopped"]++;
        }
        return c;
    }

    get rows() {
        const q = this.state.search.trim().toLowerCase();
        const f = this.state.filter;
        const rows = [];
        for (const d of Object.values(this.state.devices)) {
            const info = this.info(d);
            const group = info.vs === "moving" || info.vs === "over" ? "moving" : info.vs === "offline" ? "offline" : "stopped";
            if (f !== "all" && f !== group) {
                continue;
            }
            if (q && ![d.name, d.plate, d.driver, d.vehicle].some((v) => (v || "").toLowerCase().includes(q))) {
                continue;
            }
            rows.push({ d, ...info, group });
        }
        const rank = { moving: 0, stopped: 1, offline: 2 };
        return rows.sort((a, b) => rank[a.group] - rank[b.group] || (a.d.plate || a.d.name).localeCompare(b.d.plate || b.d.name));
    }

    get selectedRow() {
        const d = this.state.devices[this.state.selected];
        return d ? { d, ...this.info(d) } : null;
    }

    get legend() {
        return ["moving", "over", "idle", "stopped", "offline"].map((vs) => ({ vs, label: STATES[vs], color: this.state.cfg.colors[vs] }));
    }

    get styles() {
        return STYLE_LIST;
    }

    ignitionOn = _t("ON");
    ignitionOff = _t("OFF");

    fmtSpeed(v) {
        return Math.round(v || 0);
    }
}

registry.category("actions").add("elblasy_fleet_gps_map", FleetGpsMap);

/** @odoo-module **/
import { Component, onMounted, onWillDestroy, onWillStart, useRef, useState } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { formatFloat } from "@web/views/fields/formatters";
import { localization } from "@web/core/l10n/localization";
import { useService } from "@web/core/utils/hooks";
import { resolveStyle } from "../map/map_styles";
import { applyIconScale, collapseAttribution, enableArabicShaping, hasWebGL, mapLang, withStyle } from "../map/map_common";
import { distanceM, lerpAngle, markerHtml, vehicleState } from "../map/vehicle";

const { DateTime } = luxon;
const SPEEDS = [10, 30, 60, 120, 300, 600]; // simulated seconds per real second
const GAP_JUMP_S = 300;                     // no data for this long: the playback jumps over the silence
const CHART_POINTS = 1200;

// One hue, light to dark = faster. Over the limit is a status, so it is red, and a vehicle standing still is neutral grey.
const BAND_COLORS = ["#9aa3b2", "#8dbbf0", "#4f95e6", "#2a78d6", "#1b4f99"];
const OVER_COLOR = "#e34948";
const STEP = 1000;
const store = {
    get(key, fallback) {
        try {
            return JSON.parse(localStorage.getItem(`fleet_gps.pb.${key}`)) ?? fallback;
        } catch {
            return fallback;
        }
    },
    set(key, value) {
        try {
            localStorage.setItem(`fleet_gps.pb.${key}`, JSON.stringify(value));
        } catch {
            /* private mode */
        }
    },
};

const bandOf = (kmh, limit) => (kmh > limit ? 5 : kmh < 3 ? 0 : kmh < 30 ? 1 : kmh < 60 ? 2 : kmh < 90 ? 3 : 4);
const colorOfBand = (b) => (b === 5 ? OVER_COLOR : BAND_COLORS[b]);

export class FleetGpsPlayback extends Component {
    static template = "elblasy_fleet_gps.Playback";
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
        this.chartRef = useRef("chartRef");
        this.rootRef = useRef("rootRef");
        this.orm = useService("orm");
        this.action = useService("action");
        const params = this.props.action?.params || this.props.action?.context || {};
        this.state = useState({
            devices: [],
            deviceId: params.device_id || 0,
            day: DateTime.local().toISODate(),
            data: null,
            loading: true,
            error: "",
            noGl: false,
            playing: false,
            mult: 60,
            t: 0,        // the cursor, ms
            range: [0, 0],
            tripIdx: -1, // -1 = the whole window
            follow: true,
            mini: store.get("mini", window.innerWidth < 768),       // the control panel folded to one slim row
            showChart: store.get("chart", true),
        });
        this.pts = [];
        this.cum = [];
        this.idx = 0;
        this.raf = 0;
        this.last = 0;
        this.lastPainted = -1;
        this.chart = null;
        this.params = params;
        this.speeds = SPEEDS;
        onWillStart(async () => {
            await loadBundle("elblasy_fleet_gps.map_lib");
            await loadBundle("web.chartjs_lib");
            this.state.devices = await this.orm.searchRead("fleet.gps.device", [["disabled", "=", false]], ["name"], { order: "name" });
            if (!this.state.deviceId && this.state.devices.length) {
                this.state.deviceId = this.state.devices[0].id;
            }
            await this.load(true);
        });
        onMounted(() => {
            this.initMap();
            this.drawChart();
        });
        onWillDestroy(() => {
            cancelAnimationFrame(this.raf);
            this.chart?.destroy();
            this.resize?.disconnect();
            this.map?.remove();
            this.map = null;
        });
    }

    // ---------------------------------------------------------------------------------------------- data
    async load(first = false) {
        if (!this.state.deviceId) {
            this.state.loading = false;
            return;
        }
        this.state.loading = true;
        this.pause();
        try {
            const p = this.params;
            const byWindow = first && p.start && p.end;
            const data = await this.orm.call("fleet.gps.position", "get_route", [this.state.deviceId], byWindow ? { start: p.start, end: p.end } : { day: this.state.day });
            this.state.error = "";
            this.state.data = data;
            this.scaleIcons();
            this.pts = data.pts;
            this.buildCumulative();
            this.state.tripIdx = -1;
            this.setRange(this.pts.length ? [this.pts[0][0], this.pts.at(-1)[0]] : [0, 0]);
            this.seek(this.state.range[0]);
        } catch (e) {
            this.state.error = e?.data?.message || _t("Could not load the route.");
            this.state.data = null;
            this.pts = [];
        } finally {
            this.state.loading = false;
        }
        if (this.map) {
            this.drawRoute();
            this.drawStops();
            this.drawChart();
            this.fitRoute();
        }
    }

    buildCumulative() {
        let total = 0;
        this.cum = this.pts.map((p, i) => {
            if (i) {
                const a = this.pts[i - 1], d = distanceM([a[2], a[1]], [p[2], p[1]]), dt = (p[0] - a[0]) / 1000;
                if (dt > 0 && d / dt < 61 && d < 5000) {
                    total += d; // a jump faster than 220 km/h is a GPS glitch, not driving
                }
            }
            return total;
        });
    }

    setRange(range) {
        this.state.range = range;
    }

    // ---------------------------------------------------------------------------------------------- controls
    async onDevice(ev) {
        this.params = {};
        this.state.deviceId = parseInt(ev.target.value) || 0;
        await this.load();
    }

    async onDay(ev) {
        this.params = {};
        this.state.day = ev.target.value || this.state.day;
        await this.load();
    }

    shiftDay(n) {
        this.state.day = DateTime.fromISO(this.state.day).plus({ days: n }).toISODate();
        this.params = {};
        return this.load();
    }

    onTrip(ev) {
        const i = parseInt(ev.target.value);
        this.state.tripIdx = i;
        const trips = this.state.data?.trips || [];
        if (i < 0 || !trips[i]) {
            this.setRange(this.pts.length ? [this.pts[0][0], this.pts.at(-1)[0]] : [0, 0]);
        } else {
            this.setRange([this.utc(trips[i].start), this.utc(trips[i].end)]);
        }
        this.seek(this.state.range[0]);
        this.drawRoute();
        this.drawChart();
        this.fitRoute();
    }

    toggle() {
        this.state.playing ? this.pause() : this.play();
    }

    play() {
        if (!this.pts.length) {
            return;
        }
        if (this.state.t >= this.state.range[1] - 500) {
            this.seek(this.state.range[0]);
        }
        this.state.playing = true;
        this.last = 0;
        if (this.state.follow && this.map && this.cur && this.map.getZoom() < 13) {
            this.map.jumpTo({ center: [this.cur.lon, this.cur.lat], zoom: 14 }); // following a vehicle from space is no use
        }
        this.raf = requestAnimationFrame((t) => this.frame(t));
    }

    pause() {
        this.state.playing = false;
        cancelAnimationFrame(this.raf);
    }

    setSpeed(mult) {
        this.state.mult = mult;
    }

    onSpeed(ev) {
        this.state.mult = parseInt(ev.target.value) || 60;
    }

    toggleMini() {
        this.state.mini = !this.state.mini;
        store.set("mini", this.state.mini);
    }

    async toggleChart() {
        this.state.showChart = !this.state.showChart;
        store.set("chart", this.state.showChart);
        if (this.state.showChart) {
            await Promise.resolve(); // the canvas is back in the DOM after this render
            setTimeout(() => this.drawChart(), 0);
        } else {
            this.chart?.destroy();
            this.chart = null;
        }
    }

    toggleFullscreen() {
        if (document.fullscreenElement) {
            document.exitFullscreen();
        } else {
            this.rootRef.el?.requestFullscreen?.();
        }
    }

    toggleFollow() {
        this.state.follow = !this.state.follow;
    }

    onSlider(ev) {
        this.seek(parseInt(ev.target.value));
    }

    jump(seconds) {
        this.seek(Math.min(this.state.range[1], Math.max(this.state.range[0], this.state.t + seconds * 1000)));
    }

    // ---------------------------------------------------------------------------------------------- playback engine
    frame(now) {
        if (!this.state.playing) {
            return;
        }
        const dt = this.last ? Math.min(now - this.last, 100) : 16; // a background tab must not make a giant leap
        this.last = now;
        let t = this.state.t + dt * this.state.mult;
        const next = this.pts[this.idx + 1];
        if (next && next[0] - t > GAP_JUMP_S * 1000 && next[0] - this.pts[this.idx][0] > GAP_JUMP_S * 1000) {
            t = next[0] - 1000; // silence between two fixes: skip it
        }
        if (t >= this.state.range[1]) {
            this.seek(this.state.range[1]);
            this.pause();
            return;
        }
        this.seek(t);
        this.raf = requestAnimationFrame((x) => this.frame(x));
    }

    indexAt(t) {
        let lo = 0, hi = this.pts.length - 1;
        while (lo < hi) {
            const mid = (lo + hi + 1) >> 1;
            this.pts[mid][0] <= t ? (lo = mid) : (hi = mid - 1);
        }
        return lo;
    }

    /** The vehicle at time t: between two fixes it is interpolated, unless they are far apart in time (then it waits). */
    seek(t) {
        this.state.t = t;
        if (!this.pts.length) {
            return;
        }
        const i = this.indexAt(t);
        this.idx = i;
        const a = this.pts[i], b = this.pts[i + 1];
        let lon = a[2], lat = a[1], course = a[4];
        if (b && b[0] > a[0] && b[0] - a[0] <= 120000 && t > a[0]) {
            const k = (t - a[0]) / (b[0] - a[0]);
            lon += (b[2] - a[2]) * k;
            lat += (b[1] - a[1]) * k;
            course = lerpAngle(a[4], b[4], k);
        }
        this.cur = { lon, lat, course, kmh: a[3], ign: a[5], km: this.cum[i] / 1000 };
        if (this.map && this.marker) {
            this.marker.setLngLat([lon, lat]);
            const heading = this.marker.getElement().querySelector(".o_gps_heading");
            if (heading) {
                heading.style.transform = `rotate(${course}deg)`;
            }
            if (this.state.follow && this.state.playing) {
                this.map.easeTo({ center: [lon, lat], duration: 120, easing: (x) => x });
            }
            if (i !== this.lastPainted) {
                this.lastPainted = i;
                this.paintProgress();
            }
        }
        const now = performance.now();
        if (this.chart && (now - (this.lastDraw || 0) > 90 || !this.state.playing)) {
            this.lastDraw = now;
            this.chart.draw(); // only the cursor moved: no data recalculation, and at most ~11 redraws a second
        }
    }

    paintProgress() {
        const m = this.map;
        if (m?.getLayer("pb-line")) {
            m.setPaintProperty("pb-line", "line-opacity", ["case", ["<=", ["get", "to"], this.idx], 1, 0.55]);
        }
    }

    // ---------------------------------------------------------------------------------------------- map
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
        const style = await resolveStyle(this.state.data ? "sat" : "light", mapLang());
        if (!this.mapRef.el) {
            return;
        }
        this.map = new globalThis.maplibregl.Map({ container: root, style, center: [31.2, 30.05], zoom: 6, attributionControl: { compact: true } });
        this.map.addControl(new globalThis.maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
        collapseAttribution(this.map);
        this.map.on("error", (e) => console.debug("map:", e?.error?.message || e));
        this.map.on("zoom", () => this.scaleIcons());
        this.map.once("load", () => this.scaleIcons());
        this.map.once("load", () => {
            this.drawRoute();
            this.drawStops();
            this.makeMarker();
            this.fitRoute();
        });
        this.resize = new ResizeObserver(() => this.map?.resize());
        this.resize.observe(root);
    }

    scaleIcons() {
        applyIconScale(this.map, this.rootRef.el, this.state.data?.icon_size);
    }

    makeMarker() {
        const d = this.state.data?.device;
        if (!d || !this.pts.length) {
            return;
        }
        this.marker?.remove();
        const el = document.createElement("div");
        el.className = "o_gps_marker";
        const info = { vs: "moving", color: "#23e76a" };
        el.innerHTML = markerHtml({ category: d.category, course: 0 }, info, { selected: false, label: d.plate || d.name, showLabel: true });
        this.marker = new globalThis.maplibregl.Marker({ element: el, anchor: "center" }).setLngLat([this.pts[0][2], this.pts[0][1]]).addTo(this.map);
        this.seek(this.state.t);
    }

    segments() {
        const limit = this.state.data?.limit || 80;
        const feats = [];
        let run = null;
        for (let i = 0; i < this.pts.length - 1; i++) {
            const a = this.pts[i], b = this.pts[i + 1];
            if (b[0] - a[0] > 600000) { // more than ten minutes between fixes: not one continuous drive
                run = null;
                continue;
            }
            const band = bandOf(a[3], limit);
            if (!run || run.band !== band) {
                run = { band, coords: [[a[2], a[1]]], from: i, to: i };
                feats.push(run);
            }
            run.coords.push([b[2], b[1]]);
            run.to = i + 1;
        }
        return {
            type: "FeatureCollection",
            features: feats.map((r) => ({ type: "Feature", properties: { c: colorOfBand(r.band), from: r.from, to: r.to }, geometry: { type: "LineString", coordinates: r.coords } })),
        };
    }

    drawRoute() {
        const m = this.map;
        if (!m) {
            return;
        }
        const data = this.segments();
        withStyle(m, () => {
            if (m.getSource("pb-route")) {
                m.getSource("pb-route").setData(data);
            } else {
                m.addSource("pb-route", { type: "geojson", data });
                m.addLayer({ id: "pb-casing", type: "line", source: "pb-route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#ffffff", "line-width": 8, "line-opacity": 0.85 } });
                m.addLayer({ id: "pb-line", type: "line", source: "pb-route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "c"], "line-width": 5, "line-opacity": 0.55 } });
            }
            this.lastPainted = -1;
        });
        this.makeMarker();
    }

    drawStops() {
        this.stopMarkers?.forEach((x) => x.remove());
        this.stopMarkers = [];
        if (!this.map) {
            return;
        }
        for (const s of this.state.data?.stops || []) {
            const el = document.createElement("div");
            el.className = `o_gps_stopmark ${s.ign ? "idle" : ""}`;
            el.title = `${s.place || ""} · ${s.minutes} ${_t("min")}`;
            el.innerHTML = `<span>${s.minutes >= 60 ? Math.floor(s.minutes / 60) + _t("h") : s.minutes + _t("min")}</span>`;
            this.stopMarkers.push(new globalThis.maplibregl.Marker({ element: el }).setLngLat([s.lon, s.lat]).addTo(this.map));
        }
    }

    fitRoute() {
        if (!this.map || !this.pts.length) {
            return;
        }
        const [from, to] = this.state.range;
        const inRange = this.pts.filter((p) => p[0] >= from && p[0] <= to);
        const list = inRange.length ? inRange : this.pts;
        const b = new globalThis.maplibregl.LngLatBounds([list[0][2], list[0][1]], [list[0][2], list[0][1]]);
        list.forEach((p) => b.extend([p[2], p[1]]));
        this.map.fitBounds(b, { padding: 60, maxZoom: 17, duration: 0 }); // instant: an animation would be cut short by the first play frame
    }

    // ---------------------------------------------------------------------------------------------- chart
    drawChart() {
        const el = this.chartRef.el;
        if (!el || !globalThis.Chart) {
            return;
        }
        this.chart?.destroy();
        this.chart = null;
        if (!this.pts.length) {
            return;
        }
        const css = getComputedStyle(this.rootRef.el);
        const v = (n) => css.getPropertyValue(n).trim();
        const step = Math.max(1, Math.ceil(this.pts.length / CHART_POINTS));
        const rows = this.pts.filter((_p, i) => i % step === 0 || i === this.pts.length - 1);
        const limit = this.state.data.limit;
        const [from, to] = this.state.range[1] > this.state.range[0] ? this.state.range : [this.pts[0][0], this.pts.at(-1)[0]];
        const text = v("--gps-muted"), grid = v("--gps-grid"), main = v("--gps-series");
        // time runs along a real time axis (not the point index): the cursor and the slider then always agree, however unevenly
        // the tracker reported
        const cursor = {
            id: "cursor",
            afterDatasetsDraw: (chart) => {
                const x = chart.scales.x.getPixelForValue(this.state.t);
                const { top, bottom, left, right } = chart.chartArea;
                if (Number.isFinite(x) && x >= left && x <= right) {
                    const ctx = chart.ctx;
                    ctx.save();
                    ctx.strokeStyle = v("--gps-text");
                    ctx.lineWidth = 1.5;
                    ctx.beginPath();
                    ctx.moveTo(x, top);
                    ctx.lineTo(x, bottom);
                    ctx.stroke();
                    ctx.restore();
                }
            },
        };
        const hourStep = [300, 600, 900, 1800, 3600, 7200, 10800, 21600].find((s) => (to - from) / 1000 / s <= 9) || 21600;
        this.chart = new globalThis.Chart(el, {
            type: "line",
            data: {
                datasets: [
                    { label: _t("km/h"), data: rows.map((p) => ({ x: p[0], y: p[3] })), borderColor: main, backgroundColor: main + "26", fill: true, borderWidth: 1.5, pointRadius: 0, tension: 0.15 },
                    { label: _t("Limit"), data: [{ x: from, y: limit }, { x: to, y: limit }], borderColor: OVER_COLOR, borderDash: [5, 4], borderWidth: 1.2, pointRadius: 0, fill: false },
                ],
            },
            options: {
                parsing: false,
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: "nearest", axis: "x", intersect: false },
                plugins: {
                    legend: { display: false },
                    tooltip: { rtl: localization.direction === "rtl", callbacks: { title: (items) => DateTime.fromMillis(items[0].parsed.x).toFormat("HH:mm:ss") } },
                },
                scales: {
                    x: {
                        type: "linear", min: from, max: to, grid: { display: false }, border: { color: grid },
                        afterBuildTicks: (axis) => {
                            const stepMs = hourStep * 1000, ticks = [];
                            // ticks on round local times (every 10 min, 1 h ...), not on arbitrary milliseconds
                            for (let t = DateTime.fromMillis(from).startOf("hour").toMillis(); t <= to; t += stepMs) {
                                if (t >= from) {
                                    ticks.push({ value: t });
                                }
                            }
                            axis.ticks = ticks;
                        },
                        ticks: { color: text, maxRotation: 0, callback: (value) => DateTime.fromMillis(value).toFormat("HH:mm") },
                    },
                    y: { beginAtZero: true, grid: { color: grid }, ticks: { color: text, maxTicksLimit: 5 }, border: { display: false } },
                },
                onClick: (e, _els, chart) => {
                    const t = chart.scales.x.getValueForPixel(e.x);
                    if (Number.isFinite(t)) {
                        this.seek(Math.min(this.state.range[1], Math.max(this.state.range[0], t)));
                    }
                },
            },
            plugins: [cursor],
        });
    }

    // ---------------------------------------------------------------------------------------------- view
    utc(s) {
        return Date.parse(String(s).replace(" ", "T") + "Z");
    }

    fmtTime(ms) {
        return ms ? DateTime.fromMillis(ms).toFormat("HH:mm:ss") : "—";
    }

    fmtTrip(t) {
        return `${DateTime.fromMillis(this.utc(t.start)).toFormat("HH:mm")} → ${DateTime.fromMillis(this.utc(t.end)).toFormat("HH:mm")}  ·  ${formatFloat(t.km, { digits: [16, 1] })} ${_t("km")}`;
    }

    get cursorInfo() {
        const c = this.cur;
        return c ? { km: formatFloat(c.km, { digits: [16, 1] }), kmh: Math.round(c.kmh), ign: c.ign } : { km: "0", kmh: 0, ign: 0 };
    }

    get legend() {
        const lim = this.state.data?.limit || 80;
        return [
            { c: BAND_COLORS[0], l: "< 3" }, { c: BAND_COLORS[1], l: "3–30" }, { c: BAND_COLORS[2], l: "30–60" },
            { c: BAND_COLORS[3], l: "60–90" }, { c: BAND_COLORS[4], l: "90+" }, { c: OVER_COLOR, l: `> ${lim}` },
        ];
    }

    get totalKm() {
        return this.cum.length ? formatFloat(this.cum.at(-1) / 1000, { digits: [16, 1] }) : "0";
    }

    openDevice() {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "fleet.gps.device", res_id: this.state.deviceId, views: [[false, "form"]] });
    }
}

registry.category("actions").add("elblasy_fleet_gps_playback", FleetGpsPlayback);

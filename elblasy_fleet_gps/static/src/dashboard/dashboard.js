/** @odoo-module **/
import { Component, onMounted, onWillDestroy, onWillStart, useRef, useState } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { formatFloat } from "@web/views/fields/formatters";
import { formatInteger } from "@web/views/fields/formatters";
import { useService } from "@web/core/utils/hooks";
import { localization } from "@web/core/l10n/localization";

const { DateTime } = luxon;
const REFRESH_MS = 60000;
const BANDS = ["0-3", "3-30", "30-60", "60-90", "90-120", "120+"];

const RANGES = {
    today: () => [DateTime.local(), DateTime.local()],
    "7d": () => [DateTime.local().minus({ days: 6 }), DateTime.local()],
    "30d": () => [DateTime.local().minus({ days: 29 }), DateTime.local()],
    month: () => [DateTime.local().startOf("month"), DateTime.local()],
};

export class FleetGpsDashboard extends Component {
    static template = "elblasy_fleet_gps.Dashboard";
    static props = ["*"];

    setup() {
        this.rootRef = useRef("rootRef");
        this.dailyRef = useRef("dailyRef");
        this.statusRef = useRef("statusRef");
        this.vehicleRef = useRef("vehicleRef");
        this.speedRef = useRef("speedRef");
        this.utilRef = useRef("utilRef");
        this.hourlyRef = useRef("hourlyRef");
        this.scoreRef = useRef("scoreRef");
        this.eventsRef = useRef("eventsRef");
        this.orm = useService("orm");
        this.action = useService("action");
        this.charts = {};
        this.state = useState({ range: "7d", deviceId: 0, data: null, loading: true, error: "" });
        this.ranges = [
            { key: "today", label: _t("Today") },
            { key: "7d", label: _t("7 days") },
            { key: "30d", label: _t("30 days") },
            { key: "month", label: _t("This month") },
        ];
        onWillStart(async () => {
            await Promise.all([loadBundle("web.chartjs_lib"), this.load()]);
        });
        onMounted(() => this.draw());
        this._timer = setInterval(() => !document.hidden && this.reload(), REFRESH_MS);
        onWillDestroy(() => {
            clearInterval(this._timer);
            Object.values(this.charts).forEach((c) => c.destroy());
        });
    }

    // ------------------------------------------------------------------------------------------------ data
    async load() {
        const [from, to] = RANGES[this.state.range]();
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("fleet.gps.dashboard", "get_data", [from.toISODate(), to.toISODate()], {
                device_ids: this.state.deviceId ? [this.state.deviceId] : null,
            });
            this.state.error = "";
        } catch (e) {
            this.state.error = e?.data?.message || _t("Could not load the dashboard.");
        } finally {
            this.state.loading = false;
        }
    }

    async reload() {
        await this.load();
        this.draw();
    }

    setRange(key) {
        this.state.range = key;
        return this.reload();
    }

    onDevice(ev) {
        this.state.deviceId = parseInt(ev.target.value) || 0;
        return this.reload();
    }

    // ------------------------------------------------------------------------------------------------ heatmap
    get hourLabels() {
        return Array.from({ length: 24 }, (_v, i) => (i % 3 === 0 ? String(i) : ""));
    }

    /** One hue, light to dark: the share of the busiest cell. No hue change, so no false boundaries. */
    heatStyle(value) {
        const max = this.state.data?.heatmap.max || 0;
        const share = max ? Math.round((value / max) * 100) : 0;
        return value > 0 ? `background: color-mix(in srgb, var(--gps-series) ${Math.max(share, 12)}%, transparent)` : "";
    }

    heatLabel(label) {
        return this.state.data.heatmap.mode === "day" ? DateTime.fromISO(label).toFormat("dd LLL") : label;
    }

    heatTitle(label, hour, value) {
        return `${this.heatLabel(label)}  ${String(hour).padStart(2, "0")}:00 — ${this.hours(value)}`;
    }

    // ------------------------------------------------------------------------------------------------ formatting
    num(v, digits = 0) {
        return digits ? formatFloat(v, { digits: [16, digits] }) : formatInteger(Math.round(v));
    }

    hours(v) {
        const total = Math.round(v * 60);
        // an LTR isolate keeps "270 h 38 min" in one piece inside a right-to-left page
        return `\u2066${Math.floor(total / 60)} ${_t("h")} ${total % 60} ${_t("min")}\u2069`;
    }

    get kpis() {
        const k = this.state.data?.kpi;
        if (!k) {
            return [];
        }
        const d = this.state.data;
        return [
            { key: "vehicles", label: _t("Vehicles"), value: this.num(k.vehicles), sub: _t("%(n)s online", { n: k.online }) },
            { key: "moving", label: _t("Moving now"), value: this.num(k.moving_now) },
            { key: "distance", label: _t("Distance"), value: this.num(k.distance_km), unit: _t("km") },
            { key: "trips", label: _t("Trips"), value: this.num(k.trips) },
            { key: "max", label: _t("Max speed"), value: this.num(k.max_speed), unit: _t("km/h") },
            { key: "avg", label: _t("Avg speed"), value: this.num(k.avg_speed), unit: _t("km/h") },
            { key: "driving", label: _t("Driving time"), value: this.hours(k.moving_hours) },
            { key: "idle", label: _t("Idling"), value: this.hours(k.idle_hours), sub: _t("%(p)s% of engine time", { p: this.num(k.idle_pct) }) },
            { key: "over", label: _t("Overspeed events"), value: this.num(k.over_count), sub: _t("over %(n)s km/h", { n: d.limit }) },
            { key: "harsh", label: _t("Harsh events"), value: this.num(k.harsh_brake + k.harsh_accel), sub: _t("%(b)s braking, %(a)s accel.", { b: k.harsh_brake, a: k.harsh_accel }) },
            { key: "score", label: _t("Driving score"), value: this.num(k.score), unit: "/100" },
            { key: "events", label: _t("Events"), value: this.num(k.events) },
        ];
    }

    openVehicle(id) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "fleet.gps.device", res_id: id, views: [[false, "form"]] });
    }

    // ------------------------------------------------------------------------------------------------ charts
    /** Colours come from the page itself (CSS variables), so light and dark mode need no second palette in JS. */
    palette() {
        const css = getComputedStyle(this.rootRef.el);
        const v = (name) => css.getPropertyValue(name).trim();
        const states = this.state.data?.colors || {};
        return {
            text: v("--gps-text"), muted: v("--gps-muted"), grid: v("--gps-grid"), surface: v("--gps-panel"), main: v("--gps-series"),
            // the vehicle states keep the colours chosen in the settings, so they match the live map
            moving: states.moving || v("--gps-moving"), idle: states.idle || v("--gps-idle"),
            stopped: states.stopped || v("--gps-stopped"), offline: states.offline || v("--gps-offline"),
        };
    }

    mount(key, ref, config) {
        this.charts[key]?.destroy();
        const el = ref.el;
        if (el) {
            this.charts[key] = new globalThis.Chart(el, config);
        }
    }

    draw() {
        const d = this.state.data;
        if (!d || !globalThis.Chart || !this.rootRef.el) {
            return;
        }
        const c = this.palette();
        const rtl = localization.direction === "rtl";
        const base = {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { display: false },
                tooltip: { rtl, textDirection: rtl ? "rtl" : "ltr", padding: 10, cornerRadius: 6 },
            },
        };
        const axis = (extra = {}) => ({
            grid: { color: c.grid, drawTicks: false },
            border: { display: false },
            ticks: { color: c.muted, padding: 6, maxTicksLimit: 8 },
            ...extra,
        });
        const noGrid = { grid: { display: false }, border: { color: c.grid }, ticks: { color: c.muted, maxRotation: 0, autoSkip: true } };
        const bar = (data, extra = {}) => ({ data, backgroundColor: c.main, borderRadius: 4, borderSkipped: false, maxBarThickness: 22, ...extra });
        const kmShort = _t("km");
        const hShort = _t("h");

        this.mount("daily", this.dailyRef, {
            type: "line",
            data: {
                labels: d.daily.map((x) => DateTime.fromISO(x.date).toFormat("dd LLL")),
                datasets: [{ label: kmShort, data: d.daily.map((x) => x.km), borderColor: c.main, backgroundColor: c.main + "26", fill: true, tension: 0.3, borderWidth: 2, pointRadius: d.daily.length > 31 ? 0 : 3, pointBackgroundColor: c.main, pointBorderColor: c.surface, pointBorderWidth: 2, pointHoverRadius: 5 }],
            },
            options: { ...base, interaction: { mode: "index", intersect: false }, scales: { x: noGrid, y: axis({ beginAtZero: true }) } },
        });

        const live = d.live;
        this.mount("status", this.statusRef, {
            type: "doughnut",
            data: {
                labels: [_t("Moving"), _t("Stopped"), _t("Offline")],
                datasets: [{ data: [live.moving, live.stopped, live.offline], backgroundColor: [c.moving, c.stopped, c.offline], borderColor: c.surface, borderWidth: 2, hoverOffset: 4 }],
            },
            options: { ...base, cutout: "68%", plugins: { ...base.plugins, legend: { display: true, position: "bottom", rtl, labels: { color: c.text, usePointStyle: true, boxWidth: 8, padding: 14 } } } },
        });

        const horizontal = (key, ref, labels, values, label, extra = {}) =>
            this.mount(key, ref, {
                type: "bar",
                data: { labels, datasets: [bar(values, { label, ...extra })] },
                options: { ...base, indexAxis: "y", scales: { x: axis({ beginAtZero: true, ...(extra.xScale || {}) }), y: { ...noGrid, ticks: { color: c.text } } } },
            });
        horizontal("vehicle", this.vehicleRef, d.by_device.map((x) => x.name), d.by_device.map((x) => x.km), kmShort);

        this.mount("speed", this.speedRef, {
            type: "bar",
            data: { labels: BANDS, datasets: [bar(d.speed_hist, { label: hShort, maxBarThickness: 36 })] },
            options: { ...base, scales: { x: noGrid, y: axis({ beginAtZero: true }) } },
        });

        const stack = (name, color) => ({ label: name, backgroundColor: color, borderColor: c.surface, borderWidth: 2, borderRadius: 4, borderSkipped: false, maxBarThickness: 22 });
        this.mount("util", this.utilRef, {
            type: "bar",
            data: {
                labels: d.utilization.map((x) => x.name),
                datasets: [
                    { ...stack(_t("Driving"), c.moving), data: d.utilization.map((x) => x.moving) },
                    { ...stack(_t("Idling"), c.idle), data: d.utilization.map((x) => x.idle) },
                    { ...stack(_t("Stopped"), c.stopped), data: d.utilization.map((x) => x.stopped) },
                ],
            },
            options: {
                ...base, indexAxis: "y",
                plugins: { ...base.plugins, legend: { display: true, position: "bottom", rtl, labels: { color: c.text, usePointStyle: true, boxWidth: 8, padding: 14 } } },
                scales: { x: axis({ stacked: true, beginAtZero: true }), y: { ...noGrid, stacked: true, ticks: { color: c.text } } },
            },
        });

        this.mount("hourly", this.hourlyRef, {
            type: "bar",
            data: { labels: d.hourly.map((_v, i) => String(i)), datasets: [bar(d.hourly, { label: hShort, maxBarThickness: 18 })] },
            options: { ...base, scales: { x: noGrid, y: axis({ beginAtZero: true }) } },
        });

        horizontal("score", this.scoreRef, d.scores.map((x) => x.name), d.scores.map((x) => x.score), _t("Score"), { xScale: { max: 100 } });
        horizontal("events", this.eventsRef, d.event_types.slice(0, 8).map((x) => x.label), d.event_types.slice(0, 8).map((x) => x.count), _t("Events"));
    }
}

registry.category("actions").add("elblasy_fleet_gps_dashboard", FleetGpsDashboard);

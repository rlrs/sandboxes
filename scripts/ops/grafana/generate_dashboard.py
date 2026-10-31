"""Write dashboards/sandboxes.json, the one Grafana dashboard.

The first screen (19 grid rows, fits 1080p) answers: is the fleet healthy, what
is it doing, and what needs a hand. Everything else sits in collapsed rows.

    uv run python scripts/ops/grafana/generate_dashboard.py
"""

import json
from pathlib import Path

METRICS = {"type": "prometheus", "uid": "ucloud-metrics"}
GATEWAY = {"type": "yesoreyeram-infinity-datasource", "uid": "ucloud-gateway"}
TRACES = {"type": "tempo", "uid": "ucloud-traces"}
OPS_HEADERS = [["X-UCloud-Ops", "1"], ["Content-Type", "application/json"]]
CREATE = 'operation="gateway.sandbox_create"'
OPS = "ucloud_platform_operation"

RED, ORANGE, GREEN, BLUE, PURPLE = "red", "orange", "green", "blue", "purple"


class Layout:
    def __init__(self):
        self.panels, self.y, self.next_id, self.row = [], 0, 0, None

    def add(self, panel, w, h, x):
        self.next_id += 1
        panel["id"] = self.next_id
        panel["gridPos"] = {"h": h, "w": w, "x": x, "y": self.y}
        (self.row["panels"] if self.row else self.panels).append(panel)

    def down(self, h):
        self.y += h

    def collapsed_row(self, title):
        # Collapsed rows sit on consecutive lines; each row's panels start just below it.
        y = self.row["gridPos"]["y"] + 1 if self.row else self.y
        self.next_id += 1
        self.row = {"type": "row", "title": title, "id": self.next_id, "collapsed": True,
                    "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}
        self.panels.append(self.row)
        self.y = y + 1


def q(expr, legend="", ref="A", instant=False, fmt=None):
    target = {"datasource": METRICS, "expr": expr, "legendFormat": legend, "refId": ref,
              "range": not instant, "instant": instant}
    if fmt:
        target["format"] = fmt
    return target


def steps(*pairs):
    """Threshold steps: base colour, then (value, colour) pairs."""
    base, *rest = pairs
    return {"mode": "absolute", "steps": [{"color": base, "value": None},
                                          *({"color": c, "value": v} for v, c in rest)]}


def stat(title, targets, unit="short", thresholds=None, no_value="0", description="", overrides=()):
    named = len(targets) > 1
    return {
        "type": "stat", "title": title, "description": description, "datasource": METRICS, "targets": targets,
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "graphMode": "none",
                    "colorMode": "value" if thresholds else "none", "justifyMode": "center",
                    "textMode": "value_and_name" if named else "value", "wideLayout": False,
                    "orientation": "vertical" if named else "auto",
                    "text": {"valueSize": 26, "titleSize": 11}},
        "fieldConfig": {"defaults": {"unit": unit, "noValue": no_value, "decimals": None,
                                     "thresholds": thresholds or steps("text"),
                                     "color": {"mode": "thresholds"}}, "overrides": list(overrides)},
    }


def series(title, targets, unit="short", stack=False, overrides=(), description="", minimum=None, maximum=None,
           legend="list", bars=False):
    custom = {"lineWidth": 1, "fillOpacity": 20 if stack else 0, "showPoints": "never", "spanNulls": True,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    if bars:
        custom.update(drawStyle="bars", fillOpacity=80)
    defaults = {"unit": unit, "custom": custom}
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    return {"type": "timeseries", "title": title, "description": description, "datasource": METRICS,
            "targets": targets,
            "options": {"legend": {"displayMode": legend, "placement": "bottom", "calcs": []},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
            "fieldConfig": {"defaults": defaults, "overrides": list(overrides)}}


def by_name(name, **props):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": key, "value": value} for key, value in props.items()]}


def line(name, colour, dash=False, axis_right=False, stack=True):
    props = {"color": {"mode": "fixed", "fixedColor": colour}}
    if dash:
        props["custom.lineStyle"] = {"fill": "dash", "dash": [6, 4]}
    if axis_right:
        props["custom.axisPlacement"] = "right"
    if not stack:
        props["custom.stacking"] = {"mode": "none", "group": "A"}
        props["custom.fillOpacity"] = 0
    return by_name(name, **props)


def action(title, url, body="", confirmation=""):
    fetch = {"method": "POST", "url": url, "headers": OPS_HEADERS, "queryParams": []}
    if body:
        fetch["body"] = body
    return {"type": "fetch", "title": title, "confirmation": confirmation, "fetch": fetch}


def gauge_cell(name, unit="percent"):
    return by_name(name, unit=unit, min=0, max=100, **{
        "custom.cellOptions": {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"},
        "thresholds": steps(GREEN, (80, ORANGE), (95, RED)), "color": {"mode": "thresholds"}})


def gateway_table(title, url, root, columns, actions, description, transformations=()):
    return {
        "type": "table", "title": title, "description": description, "datasource": GATEWAY,
        "targets": [{"datasource": GATEWAY, "refId": "A", "type": "json", "source": "url", "parser": "backend",
                     "format": "table", "url": url, "url_options": {"method": "GET"}, "root_selector": root,
                     "columns": [{"selector": s, "text": t, "type": k} for s, t, k in columns]}],
        "transformations": list(transformations),
        "options": {"showHeader": True, "cellHeight": "sm"},
        "fieldConfig": {"defaults": {"noValue": "None", "actions": actions}, "overrides": []},
    }


def build():
    L = Layout()

    # Headline numbers.
    L.add(stat("Workers", [
        q('ucloud_fleet_autoscaler_nodes{state="ready"}', "ready", "A", True),
        q('ucloud_fleet_autoscaler_nodes{state="provisioning"}', "booting", "B", True),
        q('ucloud_fleet_autoscaler_policy{limit="max_nodes"}', "max", "C", True),
    ], description="Sandbox workers ready, booting, and the autoscaler's limit"), 4, 3, 0)
    L.add(stat("Sandboxes", [q('ucloud_fleet_sandbox_routes{kind="running"}', instant=True)]), 3, 3, 4)
    L.add(stat("Waiting for capacity", [
        q('ucloud_fleet_pending_creates{kind="capacity"}', "creates", "A", True),
        q("ucloud_fleet_pending_create_oldest_seconds", "oldest", "B", True),
    ], thresholds=steps("text", (1, ORANGE)), overrides=[
        by_name("oldest", unit="s", noValue="–", thresholds=steps("text", (60, ORANGE), (300, RED)))]), 4, 3, 7)
    L.add(stat("Create p95", [q(
        f"histogram_quantile(0.95, sum by (le) (rate({OPS}_duration_seconds_bucket{{{CREATE}}}[5m])))",
        instant=True)], unit="s", thresholds=steps("text", (30, ORANGE), (120, RED)), no_value="–",
        description="Gateway create latency over 5 minutes, including any wait for capacity"), 3, 3, 11)
    L.add(stat("Failed (1h)", [q(
        f'sum(increase({OPS}_count_total{{{CREATE},status="error"}}[1h])) or vector(0)', instant=True)],
        thresholds=steps("text", (1, ORANGE), (20, RED))), 3, 3, 14)
    L.add(stat("Builds", [
        q("ucloud_fleet_image_builds_active", "running", "A", True),
        q("ucloud_fleet_image_builds_pending", "queued", "B", True),
    ]), 4, 3, 17)
    L.add(stat("Registry disk", [q("ucloud_fleet_registry_disk_used_percent", instant=True)], unit="percent",
               thresholds=steps(GREEN, (80, ORANGE), (90, RED)), no_value="–",
               ), 3, 3, 21)
    L.down(3)

    # What the fleet is doing.
    L.add(series("Sandboxes", [
        q("sum by (state) (ucloud_fleet_sandboxes)", "{{state}}"),
        q('ucloud_fleet_pending_creates{kind="capacity"}', "waiting", "B"),
    ], stack=True, overrides=[line("waiting", RED, dash=True, stack=False)],
        description="Sandboxes by state (stacked) and creates waiting for capacity"), 6, 7, 0)
    L.add(series("Workers", [
        q('ucloud_fleet_autoscaler_nodes{state="ready"}', "ready"),
        q('ucloud_fleet_autoscaler_nodes{state="provisioning"}', "booting", "B"),
        q('ucloud_fleet_autoscaler_nodes{state="unreachable"}', "unreachable", "C"),
        q('ucloud_fleet_nodes{state="builder"}', "builders", "D"),
        q('ucloud_fleet_autoscaler_policy{limit="max_nodes"}', "max", "E"),
    ], stack=True, overrides=[line("ready", GREEN), line("booting", BLUE), line("unreachable", RED),
                              line("builders", PURPLE, stack=False), line("max", "text", dash=True, stack=False)],
        minimum=0), 6, 7, 6)
    pool = 'ucloud_fleet_pool_resources{{pool="sandbox",resource="{r}",kind="{k}"}}'
    L.add(series("Sandbox worker load", [
        q(f'100 * sum({pool.format(r="vcpu", k="used")}) / sum({pool.format(r="vcpu", k="total")})',
          "vCPU admitted"),
        q(f'100 * sum({pool.format(r="memory_mb", k="used")}) / sum({pool.format(r="memory_mb", k="total")})',
          "memory admitted", "B"),
        q('avg(ucloud_fleet_node_cpu_percent{role="sandbox"})', "CPU used", "C"),
        q('avg(ucloud_fleet_node_memory_percent{role="sandbox"})', "memory used", "D"),
    ], unit="percent", minimum=0, maximum=100,
        overrides=[line("CPU used", ORANGE, dash=True), line("memory used", BLUE, dash=True),
                   line("vCPU admitted", ORANGE), line("memory admitted", BLUE)],
        description="Admitted: reserved by placed sandboxes. Used: measured on the workers. Averages over "
                    "sandbox workers."), 6, 7, 12)
    L.add(series("Create latency", [
        q(f"histogram_quantile(0.5, sum by (le) (rate({OPS}_duration_seconds_bucket{{{CREATE}}}[$__rate_interval])))",
          "p50"),
        q(f"histogram_quantile(0.95, sum by (le) (rate({OPS}_duration_seconds_bucket{{{CREATE}}}[$__rate_interval])))",
          "p95", "B"),
        q(f'60 * sum(rate({OPS}_count_total{{{CREATE},status="error"}}[$__rate_interval]))', "errors/min", "C"),
    ], unit="s", overrides=[
        by_name("errors/min", unit="short", min=0, **{
            "custom.axisPlacement": "right", "custom.drawStyle": "bars", "custom.fillOpacity": 60,
            "color": {"mode": "fixed", "fixedColor": RED}}),
    ], description="Gateway create latency, including any wait for capacity"), 6, 7, 18)
    L.down(7)

    # What needs a hand.
    worker = "max by (job_id, role) ({})"
    state = (
        "max by (job_id, role) ("
        "(5 * (1 - ucloud_fleet_node_fresh)) > 0"
        " or (4 * ucloud_fleet_node_operator_drained) > 0"
        " or (3 * ucloud_fleet_node_soft_drained) > 0"
        " or (2 * ucloud_fleet_node_draining) > 0"
        ' or (1 - ucloud_fleet_node_admission_open{role="sandbox"}) > 0'
        " or 0 * ucloud_fleet_node_fresh)"
    )
    def resource(kind, name):
        return worker.format(f'ucloud_fleet_node_resources{{kind="{kind}",resource="{name}"}}')

    columns = [
        ("S", state, "State"),
        ("A", worker.format("ucloud_fleet_node_active_sandboxes"), "Sandboxes"),
        ("V", f'100 * {resource("used", "vcpu")} / {resource("total", "vcpu")}', "vCPU adm."),
        ("M", f'100 * {resource("used", "memory_mb")} / {resource("total", "memory_mb")}', "Mem adm."),
        ("C", worker.format("ucloud_fleet_node_cpu_percent"), "CPU"),
        ("D", resource("free", "disk_mb") + " * 1048576", "Disk free"),
    ]
    job = '${__data.fields["job_id"]}'
    names = ["job_id", "role", *(name for _, _, name in columns)]
    workers = {
        "type": "table", "title": "Workers", "datasource": METRICS,
        "description": "Click a row for actions. Drain: no new work, parked sandboxes move away, stopped once "
                       "empty. Drain and evict: also deletes its sandboxes. Undrain: back into service.",
        "targets": [q(expr, "", ref, True, "table") for ref, expr, _ in columns],
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "indexByName": {name: i for i, name in enumerate(names)},
                "renameByName": {"job_id": "Worker", "role": "Role",
                                 **{f"Value #{ref}": name for ref, _, name in columns}}}},
        ],
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Sandboxes", "desc": True}]},
        "fieldConfig": {"defaults": {"noValue": "No workers", "custom": {"align": "auto"}, "decimals": 0,
                                     "actions": [
            action("Drain", f"/ops/v1/nodes/{job}/drain", "{}",
                   f"Drain worker {job}? It takes no new work and is stopped once empty."),
            action("Drain and evict", f"/ops/v1/nodes/{job}/drain", '{"evict": true}',
                   f"Drain worker {job} and DELETE every sandbox on it?"),
            action("Undrain", f"/ops/v1/nodes/{job}/undrain", "", f"Put worker {job} back into service?"),
        ]}, "overrides": [
            by_name("State", mappings=[{"type": "value", "options": {
                "0": {"text": "ready", "color": GREEN, "index": 0},
                "1": {"text": "closed", "color": ORANGE, "index": 1},
                "2": {"text": "draining", "color": ORANGE, "index": 2},
                "3": {"text": "emptying", "color": BLUE, "index": 3},
                "4": {"text": "drained", "color": PURPLE, "index": 4},
                "5": {"text": "stale", "color": RED, "index": 5}}}],
                **{"custom.cellOptions": {"type": "color-text"}, "custom.align": "left"}),
            gauge_cell("vCPU adm."), gauge_cell("Mem adm."), gauge_cell("CPU"),
            by_name("Disk free", unit="bytes", decimals=None),
            *(by_name(name, **{"custom.width": width}) for name, width in (
                ("Worker", 95), ("Role", 70), ("State", 80), ("Sandboxes", 100), ("vCPU adm.", 105),
                ("Mem adm.", 105), ("CPU", 105), ("Disk free", 85))),
        ]},
    }
    L.add(workers, 14, 9, 0)

    sandbox = '${__data.fields["Sandbox"]}'
    L.add(gateway_table("Queued creates", "/v1/creates", "creates", [
        ("sandbox_id", "Sandbox", "string"), ("state", "State", "string"),
        ("attempts", "Tries", "number"), ("age_seconds", "Age", "number"),
    ], [
        action("Cancel", "/ops/v1/creates:cancel", f'{{"sandbox_ids": ["{sandbox}"]}}',
               f"Cancel the create of {sandbox}?"),
        action("Cancel all queued creates", "/ops/v1/creates:cancel", '{"all": true}',
               "Cancel EVERY queued create?"),
    ], "Creates waiting for placement, live from the gateway. Click a row to cancel; the waiting client "
       "gets 410."), 5, 9, 14)
    L.panels[-1]["fieldConfig"]["overrides"] = [by_name("Age", unit="s")]

    build_id = '${__data.fields["Build"]}'
    L.add(gateway_table("Image builds", "/v1/images/builds", "builds", [
        ("build_id", "Build", "string"), ("tag", "Image", "string"), ("status", "Status", "string"),
        ("created_at", "Created", "timestamp"), ("error", "Error", "string"),
    ], [
        action("Cancel build", f"/ops/v1/images/builds/{build_id}:cancel", "{}",
               f"Cancel build {build_id}?"),
    ], "Running and failed builds, live from the gateway. Click a row to cancel a running one.", [
        {"id": "filterByValue", "options": {"type": "exclude", "match": "any", "filters": [
            {"fieldName": "Status", "config": {"id": "equal", "options": {"value": "succeeded"}}}]}},
        {"id": "sortBy", "options": {"sort": [{"field": "Created", "desc": True}]}},
    ]), 5, 9, 19)
    L.panels[-1]["fieldConfig"]["overrides"] = [
        by_name("Created", unit="dateTimeFromNow"), by_name("Build", **{"custom.hidden": True}),
        by_name("Status", **{"custom.width": 80})]
    L.down(9)

    # Detail, collapsed.
    L.collapsed_row("Per worker")
    L.add(series("Sandboxes", [q("ucloud_fleet_node_active_sandboxes", "{{job_id}}")]), 6, 7, 0)
    L.add(series("CPU used", [q("ucloud_fleet_node_cpu_percent", "{{job_id}}")], unit="percent",
                 minimum=0, maximum=100), 6, 7, 6)
    L.add(series("Pressure (PSI full, avg10)", [
        q("ucloud_fleet_node_memory_psi_full_avg10", "mem {{job_id}}"),
        q("ucloud_fleet_node_io_psi_full_avg10", "io {{job_id}}", "B"),
    ], minimum=0), 6, 7, 12)
    L.add(series("Disk free", [
        q('ucloud_fleet_node_resources{kind="free",resource="disk_mb"} * 1048576', "{{job_id}}")],
        unit="bytes", minimum=0), 6, 7, 18)
    L.down(7)

    L.collapsed_row("Scaling and programs")
    L.add(series("Waiting demand (vCPU)", [
        q('ucloud_fleet_pending_resources{resource="vcpu"}', "waiting ({{kind}})"),
        q('ucloud_fleet_autoscaler_resources{resource="vcpu",kind="deficit"}', "autoscaler deficit", "B"),
    ], minimum=0), 6, 7, 0)
    L.add(series("Autoscaler decisions", [q("ucloud_fleet_autoscaler_actions", "{{role}} {{kind}}")],
                 bars=True, description="Nodes each decision creates or stops"), 6, 7, 6)
    L.add(series("Programs by state", [q("ucloud_fleet_program_states", "{{state}}")], stack=True), 6, 7, 12)
    L.add(series("Model wait", [
        q("ucloud_fleet_program_model_wait_p95_ms / 1000", "p95"),
        q("ucloud_fleet_program_response_to_wake_p95_ms / 1000", "response to wake p95", "B"),
        q("ucloud_fleet_program_oldest_model_wait_seconds", "oldest wait", "C"),
    ], unit="s", minimum=0, description="Programs parked on the model relay"), 6, 7, 18)
    L.down(7)

    L.collapsed_row("Requests, traces and storage")
    L.add(series("Slowest operations (p95)", [q(
        f"topk(8, histogram_quantile(0.95, sum by (le, operation) "
        f"(rate({OPS}_duration_seconds_bucket[$__rate_interval]))))", "{{operation}}")],
        unit="s"), 8, 8, 0)
    L.add({"type": "table", "title": "Errors in range", "datasource": METRICS,
           "targets": [q(f'sum by (operation) (increase({OPS}_count_total{{status="error"}}[$__range]))'
                         " > 0", "", "A", True, "table")],
           "transformations": [{"id": "organize", "options": {
               "excludeByName": {"Time": True},
               "renameByName": {"Value": "Errors", "operation": "Operation"}}}],
           "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Errors", "desc": True}]},
           "fieldConfig": {"defaults": {"noValue": "None", "decimals": 0}, "overrides": []}}, 6, 8, 8)
    L.add({"type": "traces", "title": "Traces over 100 ms", "datasource": TRACES,
           "targets": [{"datasource": TRACES, "limit": 50, "query": "{ duration > 100ms }", "queryType": "traceql",
                        "refId": "A", "tableType": "traces"}]}, 10, 8, 14)
    L.down(8)
    L.add(series("Registry disk", [q("ucloud_fleet_registry_disk_bytes", "{{kind}}")], unit="bytes",
                 minimum=0), 8, 6, 0)
    L.add(series("Postgres p95", [q(
        "histogram_quantile(0.95, sum by (le, service_name) "
        "(rate(ucloud_platform_postgres_duration_seconds_bucket[$__rate_interval])))", "{{service_name}}")],
        unit="s"), 8, 6, 8)
    L.add(series("Telemetry drops", [
        q('sum(ucloud_fleet_telemetry_spans{kind="dropped"})', "dropped spans"),
        q("sum(ucloud_fleet_telemetry_failed_exports)", "failed exports", "B"),
    ], minimum=0), 8, 6, 16)

    return {
        "uid": "ucloud-fleet", "title": "Sandboxes", "tags": ["ucloud"], "timezone": "utc",
        "schemaVersion": 41, "version": 1, "editable": False, "refresh": "30s", "graphTooltip": 1,
        "time": {"from": "now-3h", "to": "now"}, "links": [], "annotations": {"list": []},
        "templating": {"list": []}, "panels": L.panels,
    }


if __name__ == "__main__":
    out = Path(__file__).parent / "dashboards" / "sandboxes.json"
    out.write_text(json.dumps(build(), indent=1) + "\n")
    print(out)

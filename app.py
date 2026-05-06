from __future__ import annotations

import datetime as dt
import ipaddress
import os
import re
import sqlite3
import math
from html import escape
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import hashlib

from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for, Response


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NETMAP_DB", str(APP_DIR / "netmap.sqlite3")))


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    @app.get("/")
    def index():
        return redirect(url_for("hosts"))

    @app.get("/hosts")
    def hosts():
        q = (request.args.get("q") or "").strip()
        port = (request.args.get("port") or "").strip()
        service = (request.args.get("service") or "").strip()
        subnet = (request.args.get("subnet") or "").strip()
        hide_done = (request.args.get("hide_done") or "").strip() in {"1", "true", "on", "yes"}

        rows = query_hosts(
            q=q or None,
            port=int(port) if port.isdigit() else None,
            service=service or None,
            subnet=subnet or None,
            hide_done=hide_done,
        )
        subnets = list_subnets()
        services = list_services()
        ctx = {
            "rows": rows,
            "q": q,
            "port": port,
            "service": service,
            "subnet": subnet,
            "hide_done": hide_done,
            "subnets": subnets,
            "services": services,
        }

        if request.headers.get("HX-Request") == "true":
            return render_template("partials/hosts_table.html", **ctx)
        return render_template("hosts.html", **ctx)

    @app.get("/hosts/<int:host_id>")
    def host_detail(host_id: int):
        host = get_host(host_id)
        if host is None:
            abort(404)
        ports = list_ports_for_host(host_id)
        return render_template("host_detail.html", host=host, ports=ports)

    @app.post("/hosts/<int:host_id>/notes")
    def host_notes(host_id: int):
        notes = request.form.get("notes", "")
        update_host_notes(host_id, notes)
        host = get_host(host_id)
        return render_template("partials/host_notes.html", host=host)

    @app.post("/hosts/<int:host_id>/toggle_inspected")
    def host_toggle_inspected(host_id: int):
        inspected = toggle_host_inspected(host_id)
        host = get_host(host_id)
        if request.headers.get("HX-Request") == "true":
            return render_template("partials/host_inspected_badge.html", host=host)
        return jsonify({"inspected": inspected})

    @app.get("/graph")
    def graph():
        # full-page graph UI; filters refresh the graph data
        q = (request.args.get("q") or "").strip()
        port = (request.args.get("port") or "").strip()
        service = (request.args.get("service") or "").strip()
        subnet = (request.args.get("subnet") or "").strip()
        hide_done = (request.args.get("hide_done") or "").strip() in {"1", "true", "on", "yes"}
        ctx = {"q": q, "port": port, "service": service, "subnet": subnet, "hide_done": hide_done}
        return render_template("graph.html", **ctx)

    @app.get("/graph/data")
    def graph_data():
        q = (request.args.get("q") or "").strip()
        port = (request.args.get("port") or "").strip()
        service = (request.args.get("service") or "").strip()
        subnet = (request.args.get("subnet") or "").strip()
        hide_done = (request.args.get("hide_done") or "").strip() in {"1", "true", "on", "yes"}
        cluster = (request.args.get("cluster") or "subnet").strip().lower()
        if cluster not in {"subnet", "similarity"}:
            cluster = "subnet"

        rows = query_hosts(
            q=q or None,
            port=int(port) if port.isdigit() else None,
            service=service or None,
            subnet=subnet or None,
            hide_done=hide_done,
        )

        host_ids = [int(r["id"]) for r in rows]
        ports_by_host = get_open_ports_for_hosts(host_ids)

        nodes: list[dict] = []
        edges: list[dict] = []
        groups: dict[str, int] = {}

        # cluster hubs (visible + draggable)
        anchor_node_id: dict[str, str] = {}
        cluster_members: dict[str, list[int]] = {}
        for r in rows:
            if cluster == "similarity":
                key = similarity_key(ports_by_host.get(int(r["id"]), []))
            else:
                key = r["subnet"] or "unknown"
            groups.setdefault(key, len(groups) + 1)
            cluster_members.setdefault(key, []).append(int(r["id"]))
        group_items = list(groups.items())
        n_groups = max(1, len(group_items))
        # Scatter hubs across a grid so clusters start separated (instead of a tight circle).
        cols = max(1, int(math.ceil(math.sqrt(n_groups))))
        rows_n = int(math.ceil(n_groups / cols))
        hub_spacing_x = 640
        hub_spacing_y = 520
        hub_pos: dict[str, tuple[int, int]] = {}
        for idx, (subnet_key, gi) in enumerate(group_items):
            sid = f"cluster:{subnet_key}"
            anchor_node_id[subnet_key] = sid
            col = idx % cols
            row = idx // cols
            ax = int((col - (cols - 1) / 2.0) * hub_spacing_x)
            ay = int((row - (rows_n - 1) / 2.0) * hub_spacing_y)
            hub_pos[subnet_key] = (ax, ay)
            label = subnet_key if cluster == "subnet" else subnet_key.replace("+", " + ")
            title = f"Cluster: {subnet_key}"
            if cluster == "subnet":
                title = f"Subnet cluster: {subnet_key}"
            elif cluster == "similarity":
                title = f"Similarity cluster: {subnet_key}"
            nodes.append(
                {
                    "id": sid,
                    "label": label,
                    "title": title,
                    "group": gi,
                    "kind": "cluster",
                    "role": "cluster",
                    "shape": "box",
                    "size": 18,
                    "hidden": False,
                    "physics": False,
                    "fixed": False,
                    "x": ax,
                    "y": ay,
                    "cluster_key": subnet_key,
                }
            )

        for r in rows:
            cluster_key = (
                similarity_key(ports_by_host.get(int(r["id"]), []))
                if cluster == "similarity"
                else (r["subnet"] or "unknown")
            )
            open_ports = ports_by_host.get(int(r["id"]), [])
            role = classify_role(ip=r["ip"], hostname=r["hostname"], open_ports=open_ports)
            node_shape = shape_for_role(role)
            tip_html = build_host_tooltip_html(
                ip=r["ip"],
                hostname=r["hostname"],
                subnet=(r["subnet"] or "unknown"),
                inspected=bool(r["inspected"]),
                open_ports=open_ports,
                notes=(r["notes"] or ""),
            )

            # Seed initial positions near the cluster hub so clusters don't start entangled.
            # Nodes are NOT fixed; physics will still run and settle them.
            hx, hy = hub_pos.get(cluster_key, (0, 0))
            h = hashlib.sha1(f"{cluster_key}|{r['ip']}".encode("utf-8")).digest()
            a = int.from_bytes(h[:2], "big") / 65535.0 * (2.0 * math.pi)
            rr = 130 * (0.55 + (h[2] / 255.0) * 0.65)  # ~72..156
            x = hx + int(math.cos(a) * rr)
            y = hy + int(math.sin(a) * rr)
            nodes.append(
                {
                    "id": r["id"],
                    "label": r["ip"],
                    "title": f"{r['ip']}",  # plain fallback (custom tooltip uses `tip_html`)
                    "tip_html": tip_html,
                    "group": groups[cluster_key],
                    "subnet": (r["subnet"] or "unknown"),
                    "inspected": bool(r["inspected"]),
                    "kind": "host",
                    "role": role,
                    "shape": node_shape,
                    "physics": True,
                    "cluster_key": cluster_key,
                    "x": x,
                    "y": y,
                }
            )
            edges.append(
                {
                    "from": anchor_node_id[cluster_key],
                    "to": r["id"],
                }
            )

        return jsonify(
            {
                "nodes": nodes,
                "edges": edges,
                "groups": list(groups.keys()),
                "host_count": len(rows),
                "clusters": [
                    {"key": key, "hub_id": anchor_node_id[key], "member_ids": mids}
                    for key, mids in cluster_members.items()
                ],
            }
        )

    @app.get("/export/urls")
    def export_urls_page():
        q = (request.args.get("q") or "").strip()
        subnet = (request.args.get("subnet") or "").strip()
        hide_done = (request.args.get("hide_done") or "").strip() in {"1", "true", "on", "yes"}
        return render_template("export_urls.html", q=q, subnet=subnet, hide_done=hide_done)

    @app.get("/export/urls.txt")
    def export_urls_txt():
        q = (request.args.get("q") or "").strip()
        subnet = (request.args.get("subnet") or "").strip()
        hide_done = (request.args.get("hide_done") or "").strip() in {"1", "true", "on", "yes"}

        rows = query_hosts(q=q or None, port=None, service=None, subnet=subnet or None, hide_done=hide_done)
        host_ids = [int(r["id"]) for r in rows]
        open_ports = get_open_ports_for_hosts(host_ids)

        urls: list[str] = []
        for r in rows:
            ip = r["ip"]
            for p in open_ports.get(int(r["id"]), []):
                scheme = scheme_for_service(p.service)
                if scheme is None:
                    continue
                urls.append(f"{scheme}://{ip}:{p.port}")

        # stable, de-duped output
        uniq = sorted(set(urls))
        body = "\n".join(uniq) + ("\n" if uniq else "")
        return Response(body, mimetype="text/plain; charset=utf-8")

    @app.get("/import")
    def import_page():
        return render_template("import.html", default_path=str(APP_DIR / "full-scan-deep"))

    @app.post("/import")
    def import_post():
        path = (request.form.get("path") or "").strip()
        if not path:
            abort(400)
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            abort(400, "file not found")
        imported = import_nmap_onormal(p)
        return render_template("import_done.html", path=str(p), imported=imported)

    @app.get("/assistant")
    def assistant():
        return render_template("assistant.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.before_request
    def _ensure_db():
        init_db()

    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def init_db() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS host (
          id INTEGER PRIMARY KEY,
          ip TEXT NOT NULL UNIQUE,
          hostname TEXT,
          subnet TEXT,
          inspected INTEGER NOT NULL DEFAULT 0,
          notes TEXT NOT NULL DEFAULT '',
          last_seen_utc TEXT
        );

        CREATE TABLE IF NOT EXISTS port (
          id INTEGER PRIMARY KEY,
          host_id INTEGER NOT NULL REFERENCES host(id) ON DELETE CASCADE,
          port INTEGER NOT NULL,
          transport TEXT NOT NULL,
          state TEXT NOT NULL,
          service TEXT,
          product TEXT,
          version TEXT,
          extra TEXT,
          UNIQUE(host_id, port, transport)
        );

        CREATE INDEX IF NOT EXISTS idx_port_service ON port(service);
        CREATE INDEX IF NOT EXISTS idx_port_port ON port(port);
        CREATE INDEX IF NOT EXISTS idx_host_subnet ON host(subnet);
        """
    )
    # lightweight "migration" for older DBs
    try:
        conn.execute("ALTER TABLE host ADD COLUMN raw TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        # already exists
        pass
    conn.commit()


def close_db(_: object | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@dataclass(frozen=True)
class ParsedPort:
    port: int
    transport: str
    state: str
    service: str | None
    product: str | None
    version: str | None
    extra: str | None


HOST_LINE_RE = re.compile(r"^Nmap scan report for (?P<target>.+)$")
PORT_LINE_RE = re.compile(
    r"^(?P<port>\d+)\/(?P<transport>\w+)\s+(?P<state>\w+)\s+(?P<service>\S+)(?:\s+(?P<rest>.*))?$"
)
IP_IN_PARENS_RE = re.compile(r"\((?P<ip>(?:\d{1,3}\.){3}\d{1,3})\)")
BARE_IP_RE = re.compile(r"^(?P<ip>(?:\d{1,3}\.){3}\d{1,3})$")


def import_nmap_onormal(path: Path) -> dict:
    """
    Import nmap normal output (-oN) into sqlite.
    Idempotent-ish: hosts keyed by IP, ports upserted by (host,port,transport).
    """
    text = path.read_text(errors="replace").splitlines()
    now = dt.datetime.now(dt.UTC).isoformat()

    current_ip: str | None = None
    current_hostname: str | None = None
    current_ports: list[ParsedPort] = []
    current_raw: list[str] = []
    imported_hosts = 0
    imported_ports = 0

    def flush_current():
        nonlocal imported_hosts, imported_ports, current_ip, current_hostname, current_ports, current_raw
        if not current_ip:
            return

        host_id = upsert_host(current_ip, current_hostname, now)
        update_host_raw(host_id, "\n".join(current_raw).strip() + "\n" if current_raw else "")
        imported_hosts += 1
        for p in current_ports:
            upsert_port(host_id, p)
            imported_ports += 1

        current_ip = None
        current_hostname = None
        current_ports = []
        current_raw = []

    for line in text:
        m = HOST_LINE_RE.match(line)
        if m:
            flush_current()
            target = m.group("target").strip()
            ip = None
            hostname = None
            mip = IP_IN_PARENS_RE.search(target)
            if mip:
                ip = mip.group("ip")
                hostname = target.split("(")[0].strip()
            else:
                mb = BARE_IP_RE.match(target)
                if mb:
                    ip = mb.group("ip")
                else:
                    # sometimes hostnames resolve without ip in parens; skip until we see an IP
                    ip = None
                    hostname = target

            current_ip = ip
            current_hostname = hostname if hostname and hostname != ip else None
            current_raw = [line]
            continue

        if not current_ip:
            continue

        current_raw.append(line)

        mp = PORT_LINE_RE.match(line)
        if mp:
            port = int(mp.group("port"))
            transport = mp.group("transport")
            state = mp.group("state")
            service = mp.group("service")
            rest = (mp.group("rest") or "").strip()
            product, version, extra = split_version_blob(rest)
            current_ports.append(
                ParsedPort(
                    port=port,
                    transport=transport,
                    state=state,
                    service=service if service not in {"unknown", "?"} else None,
                    product=product,
                    version=version,
                    extra=extra,
                )
            )

    flush_current()

    return {"hosts": imported_hosts, "ports": imported_ports}


def split_version_blob(rest: str) -> tuple[str | None, str | None, str | None]:
    if not rest:
        return None, None, None
    # Heuristic: product up to first version-looking token, remainder extra.
    tokens = rest.split()
    if not tokens:
        return None, None, None
    if len(tokens) == 1:
        return tokens[0], None, None

    # If there's a token that looks like a dotted version, treat it as version start.
    ver_idx = None
    for i, t in enumerate(tokens):
        if re.search(r"\d+\.\d+", t):
            ver_idx = i
            break
    if ver_idx is None:
        return rest, None, None
    product = " ".join(tokens[:ver_idx]).strip() or None
    version = tokens[ver_idx]
    extra = " ".join(tokens[ver_idx + 1 :]).strip() or None
    return product, version, extra


def ip_to_subnet(ip: str, prefix: int = 24) -> str:
    try:
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        return str(net)
    except Exception:
        return "unknown"


def upsert_host(ip: str, hostname: str | None, last_seen_utc: str) -> int:
    conn = get_db()
    subnet = ip_to_subnet(ip, 24)
    conn.execute(
        """
        INSERT INTO host (ip, hostname, subnet, last_seen_utc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ip) DO UPDATE SET
          hostname = COALESCE(excluded.hostname, host.hostname),
          subnet = excluded.subnet,
          last_seen_utc = excluded.last_seen_utc
        """,
        (ip, hostname, subnet, last_seen_utc),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM host WHERE ip = ?", (ip,)).fetchone()
    assert row is not None
    return int(row["id"])


def upsert_port(host_id: int, p: ParsedPort) -> None:
    conn = get_db()
    conn.execute(
        """
        INSERT INTO port (host_id, port, transport, state, service, product, version, extra)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(host_id, port, transport) DO UPDATE SET
          state=excluded.state,
          service=COALESCE(excluded.service, port.service),
          product=COALESCE(excluded.product, port.product),
          version=COALESCE(excluded.version, port.version),
          extra=COALESCE(excluded.extra, port.extra)
        """,
        (
            host_id,
            p.port,
            p.transport,
            p.state,
            p.service,
            p.product,
            p.version,
            p.extra,
        ),
    )
    conn.commit()


def query_hosts(
    *,
    q: str | None,
    port: int | None,
    service: str | None,
    subnet: str | None,
    hide_done: bool,
) -> list[sqlite3.Row]:
    conn = get_db()
    where = []
    params: list[object] = []

    if hide_done:
        where.append("h.inspected = 0")
    if subnet:
        where.append("h.subnet = ?")
        params.append(subnet)
    if q:
        like = f"%{q}%"
        where.append(
            "("
            "h.ip LIKE ? OR COALESCE(h.hostname,'') LIKE ? OR COALESCE(h.notes,'') LIKE ? "
            "OR EXISTS ("
            "  SELECT 1 FROM port p "
            "  WHERE p.host_id = h.id AND ("
            "    CAST(p.port AS TEXT) LIKE ? "
            "    OR COALESCE(p.transport,'') LIKE ? "
            "    OR COALESCE(p.state,'') LIKE ? "
            "    OR COALESCE(p.service,'') LIKE ? "
            "    OR COALESCE(p.product,'') LIKE ? "
            "    OR COALESCE(p.version,'') LIKE ? "
            "    OR COALESCE(p.extra,'') LIKE ?"
            "  )"
            ")"
            ")"
        )
        params.extend([like, like, like, like, like, like, like, like, like, like])
    if port is not None:
        where.append("EXISTS (SELECT 1 FROM port p WHERE p.host_id = h.id AND p.port = ? AND p.state = 'open')")
        params.append(port)
    if service:
        where.append(
            "EXISTS (SELECT 1 FROM port p WHERE p.host_id = h.id AND p.state='open' AND COALESCE(p.service,'') LIKE ?)"
        )
        params.append(f"%{service}%")

    sql = """
      SELECT
        h.id, h.ip, h.hostname, h.subnet, h.inspected, h.notes, h.last_seen_utc,
        (SELECT COUNT(*) FROM port p WHERE p.host_id=h.id AND p.state='open') AS open_ports
      FROM host h
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY h.subnet, h.ip"

    return list(conn.execute(sql, params).fetchall())


def list_ports_for_host(host_id: int) -> list[sqlite3.Row]:
    conn = get_db()
    return list(
        conn.execute(
            "SELECT port, transport, state, service, product, version, extra FROM port WHERE host_id=? ORDER BY port",
            (host_id,),
        ).fetchall()
    )


def get_open_ports_for_hosts(host_ids: list[int]) -> dict[int, list[ParsedPort]]:
    if not host_ids:
        return {}
    conn = get_db()
    qmarks = ",".join(["?"] * len(host_ids))
    rows = conn.execute(
        f"""
        SELECT host_id, port, transport, state, service, product, version, extra
        FROM port
        WHERE state='open' AND host_id IN ({qmarks})
        ORDER BY host_id, port
        """,
        host_ids,
    ).fetchall()
    out: dict[int, list[ParsedPort]] = {}
    for r in rows:
        out.setdefault(int(r["host_id"]), []).append(
            ParsedPort(
                port=int(r["port"]),
                transport=str(r["transport"]),
                state=str(r["state"]),
                service=str(r["service"]) if r["service"] else None,
                product=str(r["product"]) if r["product"] else None,
                version=str(r["version"]) if r["version"] else None,
                extra=str(r["extra"]) if r["extra"] else None,
            )
        )
    return out


def scheme_for_service(service: str | None) -> str | None:
    if not service:
        return None
    s = service.strip().lower()
    # nmap often uses: http, https, ssl/http, http-proxy, https-alt
    if "https" in s:
        return "https"
    if "ssl" in s and "http" in s:
        return "https"
    if "http" in s:
        return "http"
    return None


def build_host_tooltip_html(
    *,
    ip: str,
    hostname: str | None,
    subnet: str | None,
    inspected: bool,
    open_ports: list[ParsedPort],
    notes: str,
) -> str:
    ip_e = escape(ip)
    hn_e = escape(hostname) if hostname else ""
    subnet_e = escape(subnet) if subnet else "unknown"
    status = "Inspected" if inspected else "Todo"

    port_items = []
    for p in open_ports:
        svc = escape((p.service or "?").strip())
        prod = escape(p.product) if p.product else ""
        ver = escape(p.version) if p.version else ""
        meta = " ".join([x for x in [prod, ver] if x]).strip()
        meta_html = f"<span class='nmTip__portMeta'>{meta}</span>" if meta else ""
        port_items.append(
            "<div class='nmTip__portRow'>"
            f"<span class='nmTip__port mono'>{p.port}</span>"
            f"<span class='nmTip__svc mono'>{svc}</span>"
            f"{meta_html}"
            "</div>"
        )

    notes_html = ""
    n = (notes or "").strip()
    if n:
        preview = re.sub(r"\s+", " ", n)
        if len(preview) > 110:
            preview = preview[:110] + "…"
        notes_html = f"<div class='nmTip__notes'>{escape(preview)}</div>"

    head = f"<div class='nmTip__head'><span class='mono'>{ip_e}</span>"
    if hn_e:
        head += f"<span class='nmTip__hn'>{hn_e}</span>"
    head += "</div>"

    meta = (
        f"<div class='nmTip__meta'>"
        f"<span class='nmTip__pill mono'>{subnet_e}</span>"
        f"<span class='nmTip__pill'>{escape(status)}</span>"
        f"</div>"
    )

    if port_items:
        ports_html = f"<div class='nmTip__ports'>{''.join(port_items)}</div>"
    else:
        ports_html = "<div class='nmTip__empty muted'>No open ports imported</div>"

    return f"<div class='nmTip'>{head}{meta}{ports_html}{notes_html}</div>"


def classify_role(*, ip: str, hostname: str | None, open_ports: list[ParsedPort]) -> str:
    """
    Heuristic role classification based on open services/ports.
    Returns one of: domain_controller, database, webserver, workstation, ftp, router, server
    """
    ports = {p.port for p in open_ports}
    services = {(p.service or "").lower() for p in open_ports}
    hn = (hostname or "").lower()

    has = lambda s: any(s in x for x in services)
    any_port = lambda ps: any(p in ports for p in ps)

    # Domain Controller-ish: kerberos + ldap + smb (common on AD DCs)
    if (88 in ports and any_port([389, 636, 3268, 3269]) and 445 in ports) or ("domain" in hn and "controller" in hn):
        return "domain_controller"

    # Database servers
    if any_port([1433, 1521, 27017, 3306, 5432, 6379, 9200]) or has("mysql") or has("ms-sql") or has("postgres") or has("mongodb") or has("redis"):
        return "database"

    # FTP
    if 21 in ports or has("ftp"):
        return "ftp"

    # Web
    if any("http" in s for s in services) or any("https" in s for s in services) or has("ssl/http"):
        return "webserver"

    # Workstations (Windows-y)
    if any_port([135, 139, 445, 3389]) and not any_port([88, 389, 636, 3268, 3269]):
        return "workstation"

    # Routers / network gear (very heuristic)
    if any_port([161, 23]) or "router" in hn or "gateway" in hn or hn.endswith("-gw"):
        return "router"

    return "server"


def shape_for_role(role: str) -> str:
    # vis-network built-in shapes: box, ellipse, circle, database, diamond, dot, star, triangle, hexagon, ...
    return {
        "domain_controller": "star",
        # vis-network's "database" shape is visually oversized and tends to
        # place labels awkwardly; use a consistent shape instead.
        "database": "triangleDown",
        "webserver": "dot",
        "workstation": "box",
        "ftp": "triangle",
        "router": "hexagon",
        "server": "diamond",
    }.get(role, "diamond")


def similarity_key(open_ports: list[ParsedPort]) -> str:
    """
    Coarse fingerprint for "similarity" clustering.
    Groups hosts by presence of common service families + a few key ports.
    """
    services = {(p.service or "").lower() for p in open_ports}
    ports = {p.port for p in open_ports}

    def has_service(substr: str) -> bool:
        return any(substr in s for s in services)

    flags: list[str] = []
    if any("http" in s for s in services) or any("https" in s for s in services) or has_service("ssl/http"):
        flags.append("web")
    if 445 in ports or has_service("smb") or has_service("microsoft-ds") or has_service("netbios"):
        flags.append("smb")
    if 22 in ports or has_service("ssh"):
        flags.append("ssh")
    if 21 in ports or has_service("ftp"):
        flags.append("ftp")
    if 3389 in ports or has_service("ms-wbt-server") or has_service("rdp"):
        flags.append("rdp")
    if 1433 in ports or has_service("ms-sql"):
        flags.append("mssql")
    if 3306 in ports or has_service("mysql"):
        flags.append("mysql")
    if 5432 in ports or has_service("postgres"):
        flags.append("pg")
    if 88 in ports or has_service("kerberos"):
        flags.append("kerb")
    if 389 in ports or 636 in ports or has_service("ldap"):
        flags.append("ldap")
    if 53 in ports or has_service("domain"):
        flags.append("dns")
    if 161 in ports or has_service("snmp"):
        flags.append("snmp")

    if not flags:
        # fall back to a tiny signature so things still cluster somewhat
        # (top 2 services + top 2 ports)
        sv = sorted([s for s in services if s])[:2]
        pp = sorted(list(ports))[:2]
        return "misc:" + ",".join(sv + [str(p) for p in pp]) if (sv or pp) else "misc"

    return "+".join(sorted(set(flags)))

def get_host(host_id: int) -> sqlite3.Row | None:
    conn = get_db()
    return conn.execute("SELECT * FROM host WHERE id=?", (host_id,)).fetchone()


def update_host_notes(host_id: int, notes: str) -> None:
    conn = get_db()
    conn.execute("UPDATE host SET notes=? WHERE id=?", (notes, host_id))
    conn.commit()


def update_host_raw(host_id: int, raw: str) -> None:
    conn = get_db()
    conn.execute("UPDATE host SET raw=? WHERE id=?", (raw, host_id))
    conn.commit()


def toggle_host_inspected(host_id: int) -> int:
    conn = get_db()
    row = conn.execute("SELECT inspected FROM host WHERE id=?", (host_id,)).fetchone()
    if row is None:
        abort(404)
    new_val = 0 if int(row["inspected"]) else 1
    conn.execute("UPDATE host SET inspected=? WHERE id=?", (new_val, host_id))
    conn.commit()
    return new_val


def list_subnets() -> list[str]:
    conn = get_db()
    rows = conn.execute(
        "SELECT subnet, COUNT(*) as c FROM host WHERE subnet IS NOT NULL GROUP BY subnet ORDER BY subnet"
    ).fetchall()
    return [r["subnet"] for r in rows if r["subnet"]]


def list_services() -> list[str]:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT service, COUNT(*) as c
        FROM port
        WHERE service IS NOT NULL AND state='open'
        GROUP BY service
        ORDER BY c DESC, service ASC
        LIMIT 200
        """
    ).fetchall()
    return [r["service"] for r in rows if r["service"]]


app = create_app()
app.teardown_appcontext(close_db)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", "5000")))

(function () {
  function createNetwork(el) {
    const data = { nodes: new vis.DataSet([]), edges: new vis.DataSet([]) };
    const options = {
      autoResize: true,
        layout: { improvedLayout: false, randomSeed: 2 },
      physics: {
          enabled: true,
          stabilization: { enabled: true, iterations: 420, updateInterval: 25, fit: true },
          solver: "barnesHut",
          barnesHut: {
            gravitationalConstant: -2600,
            centralGravity: 0.22,
            springLength: 150,
            springConstant: 0.045,
            damping: 0.65,
            avoidOverlap: 0.35
          },
          maxVelocity: 14,
          minVelocity: 0.6,
          timestep: 0.35
      },
      nodes: {
        shape: "dot",
        size: 12,
        font: { color: "#e7ecff", face: "ui-sans-serif, system-ui", size: 12 },
        borderWidth: 1,
        color: {
          border: "rgba(125,211,252,.55)",
          background: "rgba(11,16,32,.55)",
          highlight: { border: "rgba(125,211,252,.85)", background: "rgba(11,16,32,.70)" }
        }
      },
      groups: {},
      edges: {
        color: "rgba(255,255,255,.12)",
        width: 1,
        smooth: { enabled: false }
      },
      interaction: { hover: true, tooltipDelay: 120, multiselect: false }
    };
    return new vis.Network(el, data, options);
  }

  function colorForIndex(i) {
    const palette = [
      ["rgba(125,211,252,.60)", "rgba(11,16,32,.55)"],
      ["rgba(167,139,250,.60)", "rgba(11,16,32,.55)"],
      ["rgba(52,211,153,.60)", "rgba(11,16,32,.55)"],
      ["rgba(251,191,36,.60)", "rgba(11,16,32,.55)"],
      ["rgba(251,113,133,.60)", "rgba(11,16,32,.55)"],
      ["rgba(96,165,250,.60)", "rgba(11,16,32,.55)"],
      ["rgba(34,197,94,.60)", "rgba(11,16,32,.55)"],
      ["rgba(244,114,182,.60)", "rgba(11,16,32,.55)"]
    ];
    return palette[(i - 1) % palette.length];
  }

  const state = {
    network: null,
    el: null,
    onClick: null,
    tooltipEl: null,
    tipById: {},
    clusterByHubId: {},
    draggingHub: null,
    dragStartPos: null
  };

  async function load(url) {
    const res = await fetch(url, { headers: { "Accept": "application/json" } });
    if (!res.ok) throw new Error("graph data fetch failed");
    return await res.json();
  }

  function applyGraph(data) {
    if (!state.network) return;
    const groups = {};
    (data.groups || []).forEach(function (name, idx) {
      const gi = idx + 1;
      const colors = colorForIndex(gi);
      groups[gi] = {
        color: {
          border: colors[0],
          background: colors[1],
          highlight: { border: colors[0], background: colors[1] }
        }
      };
    });
    state.network.setOptions({ groups: groups });

    const countEl = document.getElementById("graph-host-count");
    if (countEl && typeof data.host_count !== "undefined") {
      countEl.textContent = data.host_count + " host(s)";
    }

    const nodes = (data.nodes || []).map(function (n) {
      const base = {
        id: n.id,
        label: n.label,
        title: "", // disable built-in tooltip (we use custom hover tooltip)
        group: n.group
      };
      if (n.tip_html) state.tipById[n.id] = n.tip_html;
      if (n.kind === "cluster") {
        // visible hub node
        base.shape = "box";
        base.margin = 10;
        base.font = { color: "#e7ecff", face: "ui-sans-serif, system-ui", size: 12, bold: true };
        base.color = { border: "rgba(255,255,255,.20)", background: "rgba(16,26,51,.70)" };
      }
      if (n.hidden) base.hidden = true;
      if (typeof n.physics !== "undefined") base.physics = n.physics;
      if (typeof n.fixed !== "undefined") base.fixed = n.fixed;
      if (typeof n.x !== "undefined") base.x = n.x;
      if (typeof n.y !== "undefined") base.y = n.y;

      if (n.shape) {
        base.shape = n.shape;
      }
      if (n.size) base.size = n.size;

      if (n.role) {
        base.label = n.label;
      }
      // Keep host labels below icons consistently across shapes.
      if (n.kind === "host") {
        base.font = Object.assign({}, base.font || {}, { vadjust: 22, align: "center", multi: false });
      }
      if (n.role === "domain_controller") {
        base.size = 10;
        base.scaling = { min: 10, max: 10 };
      }

      if (n.inspected && n.kind !== "subnet") {
        base.shape = "dot";
        base.size = 10;
        base.color = { border: "rgba(52,211,153,.70)", background: "rgba(11,16,32,.55)" };
      }

      // Keep role icon sizes consistent.
      if (n.kind === "host" && n.role && n.role !== "webserver") {
        base.size = 10;
        base.scaling = { min: 10, max: 10 };
      }
      return base;
    });
    state.network.setData({
      nodes: new vis.DataSet(nodes),
      edges: new vis.DataSet(data.edges || [])
    });

    // hub -> member ids mapping for "drag to move cluster"
    state.clusterByHubId = {};
    (data.clusters || []).forEach(function (c) {
      if (!c || !c.hub_id) return;
      state.clusterByHubId[c.hub_id] = c.member_ids || [];
    });
  }

  async function refresh(url) {
    if (!state.el || !state.network) return;
    try {
      const data = await load(url);
      applyGraph(data);
    } catch (e) {
      // no-op; avoid breaking the rest of the UI
      console.warn(e);
    }
  }

  function ensureTooltipEl() {
    if (state.tooltipEl) return state.tooltipEl;
    const el = document.createElement("div");
    el.className = "nmHoverTip";
    el.style.display = "none";
    document.body.appendChild(el);
    state.tooltipEl = el;
    return el;
  }

  function showTip(nodeId, x, y) {
    const html = state.tipById[nodeId] || "<div class='nmTip'><div class='nmTip__head'><span class='mono'>Host</span></div></div>";
    const el = ensureTooltipEl();
    el.innerHTML = html;
    el.style.display = "block";
    positionTip(x, y);
  }

  function positionTip(x, y) {
    const el = state.tooltipEl;
    if (!el || el.style.display === "none") return;
    const pad = 14;
    const w = el.offsetWidth || 360;
    const h = el.offsetHeight || 220;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left = x + pad;
    let top = y + pad;
    if (left + w + 10 > vw) left = Math.max(10, x - w - pad);
    if (top + h + 10 > vh) top = Math.max(10, y - h - pad);
    el.style.left = left + "px";
    el.style.top = top + "px";
  }

  function hideTip() {
    if (!state.tooltipEl) return;
    state.tooltipEl.style.display = "none";
  }

  function mount(opts) {
    state.el = opts.el;
    state.onClick = opts.onClick || null;
    if (!state.el) return;
    state.network = createNetwork(state.el);
    state.tipById = {};
    state.clusterByHubId = {};
    state.draggingHub = null;
    state.dragStartPos = null;

    // Keep it "less jelly": after initial stabilization, keep physics enabled
    // but heavily damped + low velocity so nodes barely drift.
    state.network.once("stabilizationIterationsDone", function () {
      state.network.setOptions({
        physics: {
          enabled: true,
          stabilization: { enabled: false },
          maxVelocity: 4,
          minVelocity: 0.2,
          timestep: 0.25,
          barnesHut: { damping: 0.82, springConstant: 0.06, avoidOverlap: 0.45 }
        }
      });
    });

    state.network.on("click", function (params) {
      if (!state.onClick) return;
      const id = params && params.nodes && params.nodes[0];
      if (!id) return;
      state.onClick({ id: id });
    });

    // Dragging a hub moves the whole cluster.
    state.network.on("dragStart", function (params) {
      const id = params && params.nodes && params.nodes[0];
      if (!id) return;
      if (!state.clusterByHubId[id]) return;
      state.draggingHub = id;
      const pos = state.network.getPositions([id])[id];
      state.dragStartPos = pos ? { x: pos.x, y: pos.y } : null;
    });
    state.network.on("dragEnd", function () {
      if (!state.draggingHub || !state.dragStartPos) {
        state.draggingHub = null;
        state.dragStartPos = null;
        return;
      }
      const hubId = state.draggingHub;
      const endPos = state.network.getPositions([hubId])[hubId];
      if (!endPos) return;
      const dx = endPos.x - state.dragStartPos.x;
      const dy = endPos.y - state.dragStartPos.y;
      const members = state.clusterByHubId[hubId] || [];
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) {
        state.draggingHub = null;
        state.dragStartPos = null;
        return;
      }
      const posMap = state.network.getPositions(members);
      members.forEach(function (mid) {
        const p = posMap[mid];
        if (!p) return;
        state.network.moveNode(mid, p.x + dx, p.y + dy);
      });
      state.draggingHub = null;
      state.dragStartPos = null;
    });

    // Custom hover tooltip using sanitized HTML from backend.
    state.network.on("hoverNode", function (params) {
      const ev = params && params.event && params.event.srcEvent;
      if (!ev) return;
      showTip(params.node, ev.clientX, ev.clientY);
    });
    state.network.on("blurNode", function () {
      hideTip();
    });
    state.network.on("mousemove", function (params) {
      const ev = params && params.event && params.event.srcEvent;
      if (!ev) return;
      positionTip(ev.clientX, ev.clientY);
    });

    const url = opts.url || (opts.initialQueryString ? (opts.url + "?" + opts.initialQueryString) : opts.url);
    if (url) refresh(url);
  }

  function setSpacing(value) {
    if (!state.network) return;
    const v = isFinite(value) ? Math.max(0, Math.min(100, value)) : 50;
    // Map slider to physics parameters (tighter -> shorter springs + less repulsion)
    const t = v / 100.0;
    const springLength = Math.round(80 + t * 220); // 80..300
    const grav = Math.round(-1200 - t * 3400); // -1200..-4600
    const avoid = 0.15 + t * 0.55; // 0.15..0.70
    state.network.setOptions({
      physics: {
        enabled: true,
        solver: "barnesHut",
        barnesHut: {
          gravitationalConstant: grav,
          springLength: springLength,
          avoidOverlap: avoid
        }
      }
    });
  }

  window.NetMapGraph = { mount: mount, refresh: refresh, setSpacing: setSpacing };
})();


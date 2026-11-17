(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行" || state === "裁撤中" || state === "进行中"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁"
          ? "ok"
          : state === "已失败" || state === "双影"
            ? "bad"
            : state === "已作废"
              ? "muted"
              : "";
    return `<span class="tag ${cls}">${state}</span>`;
  }

  function imageTag(p) {
    // 页面、详情、对账同判：库内态是单态就按库内态显示，双影直接标红并写明原因。
    const display = p.display_state || p.storage_state;
    const title = p.image && p.image.reasons && p.image.reasons.length
      ? ` title="${p.image.reasons.join("；")}"`
      : "";
    return `<span${title}>${tag(display)}</span>`;
  }

  function imageNote(p) {
    if (!p.image) return "—";
    if (p.image.dual) return `<span class="bad-text">双影：${p.image.reasons.join("；")}</span>`;
    if (p.image.stale_shadow) return `<span class="bad-text">残留归档影子标记</span>`;
    if (p.image.broken_archive) return `<span class="bad-text">已归档但缺归档地址</span>`;
    return `<span class="muted-text">单态</span>`;
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>版本</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td>${p.domain_name}</td>
              <td class="mono">${p.domain_code}</td>
              <td>${p.keep_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${p.version}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    const dualCount = data.items.filter((p) => p.image && p.image.dual).length;
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">分区检索
          <input id="part-filter" placeholder="主题域编码 / 业务日" />
        </label>
        <span class="pill ${dualCount ? "bad" : "ok"}">双影 ${dualCount}</span>
      </div>
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中；映像单态与双影按同口径判定。点「详情」看回灌链路与快照。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>映像判定</th><th>归档地址</th><th>操作</th></tr></thead>
          <tbody id="part-body">
            ${data.items
              .map(
                (p) => `<tr data-row="${p.id}" data-key="${p.domain_code} ${p.biz_date}">
              <td class="mono">${p.domain_code}</td>
              <td class="mono">${p.biz_date}</td>
              <td>${p.row_count}</td>
              <td>${imageTag(p)}</td>
              <td>${imageNote(p)}</td>
              <td class="mono">${p.archive_uri || "—"}</td>
              <td><button type="button" class="secondary" data-detail="${p.id}">详情</button></td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>
      <section class="panel" id="detail-panel" hidden>
        <h2>分区详情</h2>
        <div id="detail-body"></div>
      </section>`;
    const filter = document.getElementById("part-filter");
    filter.oninput = () => {
      const q = filter.value.trim().toLowerCase();
      document.querySelectorAll("#part-body tr[data-row]").forEach((tr) => {
        tr.hidden = !!q && !tr.dataset.key.toLowerCase().includes(q);
      });
    };
    main.querySelectorAll("[data-detail]").forEach((btn) => {
      btn.onclick = () => showPartitionDetail(Number(btn.dataset.detail));
    });
  }

  async function showPartitionDetail(partitionId) {
    const data = await api(`/api/partitions/${partitionId}`);
    const p = data.item;
    const panel = document.getElementById("detail-panel");
    const body = document.getElementById("detail-body");
    panel.hidden = false;
    const runs = p.reinject_runs || [];
    body.innerHTML = `
      <div class="detail-head">
        <div>
          <p class="mono">${p.domain_code} · ${p.biz_date} · #${p.id}</p>
          <p>存储态 ${imageTag(p)} · 行数 ${p.row_count} · 归档地址
            <span class="mono">${p.archive_uri || "—"}</span></p>
          <p>映像判定：${imageNote(p)}（影子标记：${p.shadow_state || "无"}）</p>
        </div>
        <div class="detail-actions">
          ${
            p.storage_state === "已归档" || p.image.dual || p.image.stale_shadow
              ? `<button type="button" id="btn-reinject" class="secondary">归档回灌</button>`
              : ""
          }
        </div>
      </div>
      <h3>回灌链路（同库事务 · 快照 → 对照临时副本 → 升主轨）</h3>
      <p class="sub">路径固定「对照临时副本」。任一环节失败整笔退回；快照字段记录回灌前实物。</p>
      ${
        runs.length
          ? `<table>
              <thead><tr><th>编号</th><th>模式</th><th>结果</th><th>快照态</th><th>快照归档地址</th><th>行数</th><th>说明</th><th>时间</th></tr></thead>
              <tbody>${runs
                .map(
                  (r) => `<tr>
                    <td>${r.id}</td>
                    <td>${r.mode}</td>
                    <td>${tag(r.ok ? "已完成" : "已失败")}</td>
                    <td>${r.snapshot_state}</td>
                    <td class="mono">${r.snapshot_archive_uri || "—"}</td>
                    <td>${r.snapshot_row_count}</td>
                    <td>${r.detail}</td>
                    <td class="mono">${r.created_at}</td>
                  </tr>`
                )
                .join("")}</tbody>
            </table>`
          : `<p class="empty">尚无回灌记录</p>`
      }
      <p class="err" id="reinject-err" hidden></p>`;
    const btn = document.getElementById("btn-reinject");
    if (btn) {
      btn.onclick = async () => {
        const err = document.getElementById("reinject-err");
        err.hidden = true;
        try {
          await api(`/api/partitions/${p.id}/reinject`, {
            method: "POST",
            body: JSON.stringify({ mode: "对照临时副本" }),
          });
        } catch (e) {
          err.hidden = false;
          err.textContent = e.message;
          if (/双影|影子/.test(e.message)) {
            // 双影专拦：显式确认后才归一，避免一键把双态抹成看似在线。
            if (confirm(`${e.message}\n\n确认归一并回灌为在线单态？`)) {
              try {
                await api(`/api/partitions/${p.id}/reinject`, {
                  method: "POST",
                  body: JSON.stringify({
                    mode: "对照临时副本",
                    allow_dual_repair: true,
                  }),
                });
              } catch (e2) {
                err.textContent = e2.message;
                return;
              }
            } else {
              return;
            }
          } else {
            return;
          }
        }
        await renderPartitions();
        await showPartitionDetail(p.id);
      };
    }
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    const duals = await api("/api/partitions/images/duals");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel ${duals.count ? "panel-alert" : ""}">
        <h2>扫描卡片 · 双影专拦 ${duals.count ? `<span class="pill bad">${duals.count}</span>` : `<span class="pill ok">0</span>`}</h2>
        <p class="sub">在线却仍带归档地址或影子标记的分区不在扫描中开新裁决，避免回灌双影叠成重复入库。</p>
        ${
          duals.count
            ? `<table>
                <thead><tr><th>分区</th><th>主题域</th><th>业务日</th><th>库内态</th><th>拦截原因</th></tr></thead>
                <tbody>${duals.items
                  .map(
                    (p) => `<tr>
                      <td>#${p.id}</td>
                      <td class="mono">${p.domain_code}</td>
                      <td class="mono">${p.biz_date}</td>
                      <td>${imageTag(p)}</td>
                      <td><span class="bad-text">${p.image.reasons.join("；")}</span></td>
                    </tr>`
                  )
                  .join("")}</tbody>
              </table>`
            : `<p class="empty">无双影分区，扫描口径与库内单态一致</p>`
        }
      </section>
      <section class="panel">
        <h2>扫描任务</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.as_of_date}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${j.message}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="5" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      await api("/api/scan-jobs", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderScan();
    };
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">待执行可点「执行」。执行结果写回分区存储态与执行流水。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>状态</th><th>说明</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.domain_code}</td>
              <td class="mono">${j.biz_date}</td>
              <td>v${j.policy_version} / ${j.keep_days}天</td>
              <td>${j.decided_action}</td>
              <td>${tag(j.status)}</td>
              <td>${j.fail_reason ? `<span class="bad-text">${j.fail_reason}</span>` : "—"}</td>
              <td>${
                j.status === "待执行" || j.status === "已失败"
                  ? `<button type="button" data-exec="${j.id}">执行</button>`
                  : "—"
              }</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
        } catch (e) {
          alert(e.message);
        }
        await renderJudgments();
      };
    });
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    const queue = await api("/api/reconcile-queue");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
        <span class="pill ${queue.counts.dual ? "bad" : "ok"}">双影 ${queue.counts.dual}</span>
        <span class="pill ${queue.counts.mismatch ? "bad" : "ok"}">错位 ${queue.counts.mismatch}</span>
      </div>
      <section class="panel ${queue.counts.dual ? "panel-alert" : ""}">
        <h2>对账队列 · 双影专拦</h2>
        <p class="sub">双影按映像判定单列，不混入在线数，也不被普通错位稀释。</p>
        ${
          queue.duals.length
            ? `<table>
                <thead><tr><th>分区</th><th>主题域</th><th>业务日</th><th>库内态</th><th>归档地址</th><th>原因</th><th>操作</th></tr></thead>
                <tbody>${queue.duals
                  .map(
                    (d) => `<tr>
                      <td>#${d.partition_id}</td>
                      <td class="mono">${d.domain_code}</td>
                      <td class="mono">${d.biz_date}</td>
                      <td>${tag("双影")}</td>
                      <td class="mono">${d.archive_uri || "—"}</td>
                      <td><span class="bad-text">${d.reasons.join("；")}</span></td>
                      <td><button type="button" class="secondary" data-goto="${d.partition_id}">去分区详情</button></td>
                    </tr>`
                  )
                  .join("")}</tbody>
              </table>`
            : `<p class="empty">无双影分区</p>`
        }
      </section>
      <section class="panel">
        <h2>对账队列 · 错位 ${queue.counts.mismatch}</h2>
        ${
          queue.mismatches.length
            ? `<table>
                <thead><tr><th>裁决</th><th>分区</th><th>主题域</th><th>业务日</th><th>动作</th><th>库内态</th></tr></thead>
                <tbody>${queue.mismatches
                  .map(
                    (m) => `<tr>
                      <td>#${m.judgment_id}</td>
                      <td>#${m.partition_id}</td>
                      <td class="mono">${m.domain_code}</td>
                      <td class="mono">${m.biz_date}</td>
                      <td>${m.decided_action}</td>
                      <td>${tag(m.storage_state)}</td>
                    </tr>`
                  )
                  .join("")}</tbody>
              </table>`
            : `<p class="empty">无错位</p>`
        }
        ${
          queue.stale_archive_traces.length
            ? `<p class="sub bad-text">另有 ${queue.stale_archive_traces.length} 条归档痕迹未收尾（残留影子标记 / 已归档缺地址），建议走一次回灌归一。</p>`
            : ""
        }
      </section>
      <section class="panel">
        <h2>对账报告</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>错位</th><th>双影</th><th>摘要</th></tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${r.as_of_date}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.mismatch_count}</td>
              <td>${r.dual_count}</td>
              <td>${r.summary}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="9" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-goto]").forEach((btn) => {
      btn.onclick = async () => {
        await show("partitions");
        await showPartitionDetail(Number(btn.dataset.goto));
      };
    });
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      await api("/api/reconcile-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderReconcile();
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();

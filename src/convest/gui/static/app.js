"use strict";
const $ = (id) => document.getElementById(id),
  clone = (x) => JSON.parse(JSON.stringify(x));
const esc = (x) =>
  String(x ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let token = "",
  draft = null,
  snapshot = null,
  selected = null,
  dirty = false,
  polling = false,
  browser = null,
  toastTimer;
const names = {
  stopped: "已停止",
  starting: "正在启动",
  running: "运行中",
  stopping: "停止中 · 等待当前作业收尾",
  error: "需要处理",
  pending: "排队中",
  retry: "等待重试",
  upload_retry: "上传待重试",
  processing: "处理中",
  converted: "本机完成",
  completed: "校验完成",
  uploaded: "已发布",
  local_ready: "本机就绪",
  excluded: "质量检查未通过",
  failed: "处理失败",
  conflict: "文件冲突",
  waiting_stable: "等待录制稳定",
  waiting_space: "等待磁盘空间",
  active: "累计中",
  sealed: "已封存",
  uploading: "上传中",
  waiting_batch: "等待新批次",
  waiting_seal: "等待 24h 封存",
  blocked: "受阻",
  transferring: "传输中",
  remote_prepare: "准备远端目录",
  remote_sha256: "远端 SHA256 校验",
  deleting_verified_original: "释放已备份原包",
  normal: "生成 normal",
  speedup: "生成 speedup",
  checking_source: "核验批次来源",
  final_verification: "成品完整校验",
  local_ready: "成品已就绪",
  original_final_hash: "原包完整校验",
  queued_for_upload: "已加入上传队列",
  upload_manifest: "生成上传清单",
};
const label = (x) => names[x] || x || "等待开始";
function bytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  let i = Math.min(3, Math.floor(Math.log(n) / Math.log(1024)));
  return (
    (n / 1024 ** i).toFixed(i > 1 ? 2 : 0) + " " + ["B", "KiB", "MiB", "GiB"][i]
  );
}
function duration(n) {
  if (n == null || !Number.isFinite(n)) return "—";
  n = Math.max(0, Math.round(n));
  if (n < 60) return n + " 秒";
  if (n < 3600) return Math.ceil(n / 60) + " 分钟";
  return (
    Math.floor(n / 3600) + " 小时 " + Math.floor((n % 3600) / 60) + " 分钟"
  );
}
function toast(text, error = false) {
  $("toast").textContent = text;
  $("toast").className = "visible" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").className = ""), 6500);
}
async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers:
      body === undefined
        ? {}
        : { "Content-Type": "application/json", "X-Workflow-Token": token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const v = await r.json();
  if (!r.ok) throw Error(v.error || "请求未完成");
  return v;
}
function current() {
  return draft?.tasks.find((t) => t.id === selected);
}
function state() {
  return (
    snapshot?.tasks.find((t) => t.id === selected) || {
      state: "stopped",
      jobs: [],
      uploads: [],
      batches: [],
      training: [],
    }
  );
}
function mark() {
  dirty = true;
  $("dirty").classList.remove("hidden");
  renderPreviews();
}
function renderEditor() {
  const t = current();
  $("demo-notice").classList.toggle(
    "hidden",
    !t?.source.includes("/outputs/gui/demo-input/"),
  );
  document.querySelectorAll("[data-task]").forEach((el) => {
    el.disabled = !t;
    if (!t) {
      if (el.type !== "checkbox") el.value = "";
      return;
    }
    if (el.type === "checkbox") el.checked = t[el.dataset.task];
    else el.value = t[el.dataset.task];
  });
  renderPreviews();
}
function renderPreviews() {
  const t = current();
  const previews = [
    ["raw-preview", "raw_root", "episode…"],
    ["data-preview", "lerobot_root", "24h 批次"],
    ["normal-preview", "normal_root", "批次版本"],
    ["train-preview", "speedup_root", "批次版本"],
  ];
  previews.forEach(([id, field, child]) => {
    $(id).innerHTML = t
      ? "<strong>" +
        esc(t[field] || "请选择存储目录") +
        "</strong><br>├ A / " +
        child +
        "<br>└ B / " +
        child
      : "添加任务后配置存储路径";
  });
}
function renderTasks() {
  if (!draft) return;
  $("task-list").innerHTML = draft.tasks
    .map((t) => {
      const s = snapshot?.tasks.find((s) => s.id === t.id) || {
        state: "stopped",
      };
      return (
        '<div class="task-item ' +
        (selected === t.id ? "selected" : "") +
        '" data-select="' +
        t.id +
        '" role="button" tabindex="0"><span class="radio"></span><div><div class="task-name">' +
        esc(t.name) +
        '</div><div class="task-sub">' +
        esc(t.project) +
        " / " +
        esc(t.robot) +
        " / " +
        esc(t.scene) +
        '</div></div><span class="task-status ' +
        esc(s.state) +
        '">' +
        esc(t.enabled ? label(s.state) : "未启用") +
        "</span></div>"
      );
    })
    .join("");
  document.querySelectorAll("[data-select]").forEach((el) => {
    const select = () => {
      selected = el.dataset.select;
      renderEditor();
      renderTasks();
      renderProgress();
      refreshLogs();
    };
    el.onclick = select;
    el.onkeydown = (e) => {
      if (e.key === "Enter") select();
    };
  });
}
function transferCard(title, p, violet = false, count = "") {
  p = p || {};
  const total = p.bytes_total || 0,
    done = p.bytes_done || 0;
  const percent = total ? Math.min(100, (done / total) * 100) : 0;
  return (
    '<div><div class="transfer-title">' +
    title +
    "<span>" +
    esc(p.phase ? label(p.phase) : "等待任务") +
    '</span></div><div class="track"><div class="fill ' +
    (violet ? "violet" : "") +
    '" style="width:' +
    percent +
    '%"></div></div><div class="transfer-detail"><span>' +
    esc(total ? bytes(done) + " / " + bytes(total) : count || "暂无上传") +
    "</span><span>" +
    esc(p.eta_seconds != null ? "剩余 " + duration(p.eta_seconds) : "") +
    "</span></div>" +
    (p.speed_bps && p.phase === "transferring"
      ? '<div class="hint">' + bytes(p.speed_bps) + "/s · 当前包</div>"
      : "") +
    (p.error ? '<p class="hint error">' + esc(p.error) + "</p>" : "") +
    "</div>"
  );
}
function renderProgress() {
  const t = current(),
    s = state(),
    jobs = s.jobs || [],
    uploads = s.uploads || [],
    training = s.training || [],
    batches = s.batches || [];
  $("progress-title").textContent = t ? t.name + " · 任务进度" : "任务进度";
  $("run-state").textContent = label(s.state);
  $("run-state").className =
    "pill " +
    (s.state === "stopped"
      ? "stopped"
      : s.state === "error"
        ? "error"
        : s.state === "stopping"
          ? "stopping"
          : "");
  const pending = jobs.filter(
      (j) => !["converted", "excluded"].includes(j.state),
    ).length,
    done = jobs.filter((j) => j.state === "converted").length,
    backed = uploads.filter((u) => u.raw_done).length,
    trained = training.filter((j) => j.state === "uploaded").length;
  $("metrics").innerHTML = [
    ["待处理录制", pending],
    ["本机已完成", done],
    ["202 已备份原包", backed],
    ["训练已发布批次", trained],
  ]
    .map(
      ([k, v]) =>
        '<div class="metric"><div class="label">' +
        k +
        '</div><div class="value">' +
        v +
        "</div></div>",
    )
    .join("");
  const processing = s.processing || {},
    tp = s.training_processing || {},
    videos = s.videos || {};
  let phase =
    s.state === "stopped"
      ? s.root
        ? "已停止。队列与成品已保留，点击开始可继续。"
        : "尚未开始。保存设置，然后点击开始工作流。"
      : s.state === "stopping"
        ? "已停止接收新作业，正在等待当前清洗、转换或上传安全收尾。"
        : "正在监听录制目录，完整且稳定的录制会自动进入队列。";
  if (s.state === "running" && processing.phase)
    phase =
      (processing.episode ? processing.episode + " · " : "") +
      label(processing.phase);
  if (
    s.state === "running" &&
    tp.phase &&
    !["local_ready", "uploaded"].includes(tp.phase)
  )
    phase +=
      "　 /　" +
      label(tp.phase) +
      (videos.task === tp.task && tp.phase === "speedup"
        ? " · 视频 " + videos.done + "/" + videos.total
        : "");
  if (
    s.state === "running" &&
    jobs.length &&
    jobs.every((j) => ["converted", "excluded"].includes(j.state)) &&
    uploads.every((u) => u.state === "completed")
  ) {
    if (batches.some((b) => b.state === "active"))
      phase = "原包已完成备份，等待 24h 批次封存；继续监听新录制。";
    else if (training.length && training.every((j) => j.state === "uploaded"))
      phase = "已发现的录制与两版成品均已校验上传，继续监听新录制。";
  }
  if (s.error) phase = s.error;
  $("current-phase").textContent = phase;
  $("transfer-grid").innerHTML =
    transferCard(
      "原始 ROSbag → 202",
      s.raw_upload,
      false,
      backed + " 包备份完成",
    ) +
    transferCard(
      "24h LeRobot → 202",
      s.batch_upload,
      false,
      uploads.filter((u) => u.dataset_done).length + " 批完成",
    ) +
    transferCard("normal → 28", s.normal_upload, true, trained + " 批完成") +
    transferCard("speedup → 28", s.speedup_upload, true, trained + " 批完成");
  const errors = [
    s.error,
    s.training_processing?.error,
    s.training_upload?.error,
    ...jobs.map((j) => j.error),
    ...uploads.map((j) => j.error),
    ...training.map((j) => j.error),
    ...Object.values(s.capture_status?.route_errors || {}),
    ...Object.values(s.discovery?.routes || {}).map((r) => r.error),
  ].filter(Boolean);
  $("workflow-error").classList.toggle("hidden", !errors.length);
  $("workflow-error").textContent = [...new Set(errors)].slice(0, 5).join("\n");
  const uploadMap = new Map(uploads.map((u) => [u.route + "/" + u.episode, u]));
  let html = jobs
    .slice(-150)
    .map((j) => {
      const u = uploadMap.get(j.route + "/" + j.episode);
      return (
        "<tr><td>" +
        esc(j.episode) +
        "</td><td>" +
        esc(j.route) +
        "</td><td>" +
        esc(label(j.state)) +
        "</td><td>" +
        esc(u ? label(u.state) : "—") +
        '</td><td class="' +
        (j.error || u?.error ? "error" : "") +
        '">' +
        esc(j.error || u?.error || "—") +
        "</td></tr>"
      );
    })
    .join("");
  html += batches
    .map(
      (b) =>
        "<tr><td>24h · " +
        esc(b.name || b.batch_id) +
        "</td><td>" +
        esc(b.route) +
        "</td><td>" +
        esc(label(b.state)) +
        "</td><td>" +
        esc(b.state === "uploaded" ? "完整校验通过" : "等待封存 / 上传") +
        "</td><td>" +
        esc(b.frames || 0) +
        " 帧 · " +
        esc(JSON.parse(b.sources || "[]").length) +
        " 条轨迹</td></tr>",
    )
    .join("");
  html += training
    .map(
      (j) =>
        "<tr><td>训练 · " +
        esc(j.name || j.id.slice(0, 12)) +
        "</td><td>" +
        esc(j.route || "—") +
        "</td><td>" +
        esc(
          ["local_ready", "uploading", "uploaded", "upload_retry"].includes(
            j.state,
          )
            ? "两版已就绪"
            : label(j.state),
        ) +
        "</td><td>28 · " +
        esc(label(j.state)) +
        '</td><td class="' +
        (j.error ? "error" : "") +
        '">' +
        esc(j.error || j.batch_id || "—") +
        "</td></tr>",
    )
    .join("");
  $("records").innerHTML =
    html ||
    '<tr><td class="empty" colspan="5">还没有录制进入队列。可以先选择测试输入目录，再开始工作流。</td></tr>';
  const deadlines = batches
    .filter((b) => b.state === "active" && b.deadline_ns)
    .map((b) => b.deadline_ns / 1e9 - Date.now() / 1000);
  $("batch-clock").textContent = deadlines.length
    ? "下一批封存剩余 " + duration(Math.min(...deadlines))
    : "24 小时滚动封存，封存后自动衔接两版处理";
  $("runtime-path").textContent = s.root
    ? "运行记录：" +
      s.root +
      (s.versions?.length > 1
        ? " · 保留 " + s.versions.length + " 个配置版本"
        : "")
    : "";
  $("last-updated").textContent = snapshot
    ? "最近更新 " +
      new Date(snapshot.updated_ns / 1e6).toLocaleTimeString("zh-CN")
    : "";
  $("retry-one").disabled =
    !t || ["running", "starting", "stopping"].includes(s.state);
  $("start-one").disabled =
    !t || !t.enabled || ["running", "starting", "stopping"].includes(s.state);
  $("stop-one").disabled =
    !t || !["running", "starting", "stopping"].includes(s.state);
  $("stop").disabled = !snapshot?.tasks.some((t) =>
    ["running", "starting", "stopping"].includes(t.state),
  );
}
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    snapshot = await api("/api/status");
    $("server-state").textContent = "本机已连接";
    $("server-state").className = "pill";
    $("disk").textContent = "可用空间 " + bytes(snapshot.disk.free);
    renderTasks();
    renderProgress();
    if (document.querySelector(".logs").open) await refreshLogs();
  } catch (e) {
    $("server-state").textContent = "本机服务未连接";
    $("server-state").className = "pill error";
  } finally {
    polling = false;
  }
}
async function refreshLogs() {
  if (!selected) return;
  try {
    $("log-output").textContent = (
      await api("/api/logs?task=" + encodeURIComponent(selected))
    ).text;
  } catch (e) {
    $("log-output").textContent = e.message;
  }
}
async function action(path, id) {
  if (path === "start" && dirty) {
    toast("请先保存设置，再开始工作流。", true);
    return;
  }
  try {
    snapshot = await api("/api/" + path, id ? { task: id } : {});
    renderTasks();
    renderProgress();
    toast(
      path === "start"
        ? "已启动工作流，正在检查录制目录。"
        : "已请求停止，新作业将暂停，当前作业会安全收尾。",
    );
  } catch (e) {
    toast(e.message, true);
  }
}
async function check(server) {
  const prefix = server === "data_server" ? "data" : "training",
    chip = server === "data_server" ? "data-chip" : "train-chip";
  $(prefix + "-result").textContent = "连接中…";
  $(chip).textContent = "连接中…";
  $(chip).className = "connection-chip";
  try {
    const v = await api("/api/check", { server: draft[server] });
    $(prefix + "-result").textContent = v.rsync
      ? "SSH / rsync 已就绪"
      : "SSH 已连接，但远端缺少 rsync";
    $(chip).className = "connection-chip " + (v.rsync ? "ok" : "bad");
    $(chip).textContent = v.rsync ? "已连接" : "缺少 rsync";
    $(chip).title = $(prefix + "-result").textContent;
  } catch (e) {
    $(prefix + "-result").textContent = e.message;
    $(chip).className = "connection-chip bad";
    $(chip).textContent = "未连接";
    $(chip).title = e.message;
  }
}
async function browse(field) {
  const t = current();
  if (!t) return;
  const server =
    field === "source"
      ? null
      : field === "raw_root" || field === "lerobot_root"
        ? draft.data_server
        : draft.training_server;
  browser = { field, server, path: t[field], parent: "/" };
  $("browser-dialog").showModal();
  $("browser-host").textContent = server
    ? server.user + "@" + server.host + ":" + server.port
    : "本机目录";
  $("mkdir-area").classList.toggle("hidden", !server);
  $("folder-name").value = "";
  await browseTo(browser.path);
}
async function browseTo(path) {
  if (!browser) return;
  const active = browser;
  const requestId = (active.request = (active.request || 0) + 1);
  $("browser-path").value = path;
  $("browser-error").textContent = "";
  $("browser-entries").innerHTML = '<div class="empty">正在读取目录…</div>';
  $("choose-folder").disabled = true;
  try {
    const v = await api("/api/browse", { server: browser.server, path });
    if (browser !== active || active.request !== requestId) return;
    browser.path = v.path;
    browser.parent = v.parent;
    $("browser-path").value = v.path;
    $("browser-entries").innerHTML =
      v.entries
        .map(
          (e, i) =>
            '<div class="directory" data-directory="' +
            i +
            '" role="button" tabindex="0"><span class="folder-icon">▰</span>' +
            esc(e.name) +
            (e.symlink ? '<span class="symlink">链接</span>' : "") +
            '<span class="directory-arrow">›</span></div>',
        )
        .join("") || '<div class="empty">这个目录是空的</div>';
    document.querySelectorAll("[data-directory]").forEach((el) => {
      el.onclick = () => browseTo(v.entries[Number(el.dataset.directory)].path);
      el.onkeydown = (e) => {
        if (e.key === "Enter") el.click();
      };
    });
    $("choose-folder").disabled = false;
  } catch (e) {
    if (browser !== active || active.request !== requestId) return;
    $("browser-path").value = path;
    $("browser-error").textContent = e.message;
    $("browser-entries").innerHTML =
      '<div class="empty">请修改上方路径，或返回上一级目录</div>';
  }
}
function alignDestinationRows() {
  const sections = [...document.querySelectorAll(".destination-first")];
  const [left, right] = sections;
  if (!left || !right) return;
  const sameExpansion =
    left.querySelector("details").open === right.querySelector("details").open;
  const height =
    window.matchMedia("(min-width: 781px)").matches && sameExpansion
      ? Math.max(
          ...sections.map(
            (section) =>
              section.firstElementChild.getBoundingClientRect().height,
          ),
        )
      : 0;
  sections.forEach((section) => {
    section.style.minHeight = height ? height + "px" : "";
  });
}
function setupDestinationLayout() {
  const observer = new ResizeObserver(alignDestinationRows);
  document
    .querySelectorAll(".destination-content")
    .forEach((el) => observer.observe(el));
  document
    .querySelectorAll(".destination-preview")
    .forEach((el) => el.addEventListener("toggle", alignDestinationRows));
  window.addEventListener("resize", alignDestinationRows);
  alignDestinationRows();
}
async function init() {
  try {
    const b = await api("/api/bootstrap");
    token = b.token;
    snapshot = b.snapshot;
    draft = clone(snapshot.settings);
    selected = draft.tasks[0]?.id;
    document.querySelectorAll("[data-server]").forEach((el) => {
      el.value = draft[el.dataset.server][el.dataset.key];
      el.oninput = () => {
        draft[el.dataset.server][el.dataset.key] =
          el.type === "number" ? Number(el.value) : el.value;
        const chip =
          el.dataset.server === "data_server" ? "data-chip" : "train-chip";
        $(chip).className = "connection-chip";
        $(chip).textContent = "待测试";
        $(chip).title = "连接设置已修改，请重新测试";
        mark();
      };
    });
    document.querySelectorAll("[data-task]").forEach(
      (el) =>
        (el.oninput = () => {
          if (!current()) return;
          current()[el.dataset.task] =
            el.type === "checkbox" ? el.checked : el.value;
          mark();
          renderTasks();
        }),
    );
    document
      .querySelectorAll("[data-browse]")
      .forEach((el) => (el.onclick = () => browse(el.dataset.browse)));
    document
      .querySelectorAll("[data-check]")
      .forEach((el) => (el.onclick = () => check(el.dataset.check)));
    $("save").onclick = async () => {
      try {
        const v = await api("/api/save", draft);
        draft = clone(v.settings);
        dirty = false;
        $("dirty").classList.add("hidden");
        toast("设置已保存。路径变化会使用独立队列，历史结果会保留。");
        await refresh();
      } catch (e) {
        toast(e.message, true);
      }
    };
    $("retry-one").onclick = async () => {
      try {
        snapshot = await api("/api/retry", { task: selected });
        renderProgress();
        toast(
          "失败项已准备重试。质量排除和文件冲突保持原状，点击开始即可继续。",
        );
      } catch (e) {
        toast(e.message, true);
      }
    };
    $("start").onclick = () => action("start");
    $("stop").onclick = () => action("stop");
    $("start-one").onclick = () => action("start", selected);
    $("stop-one").onclick = () => action("stop", selected);
    $("add-task").onclick = async () => {
      try {
        const t = await api("/api/new-task", { index: draft.tasks.length + 1 });
        draft.tasks.push(t);
        selected = t.id;
        mark();
        renderEditor();
        renderTasks();
        renderProgress();
      } catch (e) {
        toast(e.message, true);
      }
    };
    $("remove-task").onclick = () => {
      if (["running", "starting", "stopping"].includes(state().state)) {
        toast("请先停止当前任务。", true);
        return;
      }
      draft.tasks = draft.tasks.filter((t) => t.id !== selected);
      selected = draft.tasks[0]?.id;
      mark();
      renderEditor();
      renderTasks();
      renderProgress();
    };
    $("generate-paths").onclick = () => {
      const t = current();
      if (!t) return;
      const base = "/data/data-demo/real/" + t.project + "/data/";
      t.raw_root = base + "rosbag/" + t.robot + "/" + t.scene;
      t.lerobot_root = base + "lerobot/" + t.robot + "/" + t.scene;
      const train =
        "/mnt/data/train_platform/datasets/" + t.project + "_" + t.robot + "/";
      t.normal_root = train + "normal/" + t.scene;
      t.speedup_root = train + "speedup/" + t.scene;
      mark();
      renderEditor();
      toast("已填入默认路径，仍可浏览修改。保存前不会写入服务器。");
    };
    $("close-browser").onclick = () => $("browser-dialog").close();
    $("browser-go").onclick = () => browseTo($("browser-path").value);
    $("browser-path").onkeydown = (e) => {
      if (e.key === "Enter") browseTo(e.target.value);
    };
    $("browser-up").onclick = () => browseTo(browser.parent);
    $("choose-folder").onclick = () => {
      current()[browser.field] = browser.path;
      mark();
      renderEditor();
      $("browser-dialog").close();
    };
    $("mkdir").onclick = async () => {
      try {
        const v = await api("/api/mkdir", {
          server: browser.server,
          parent: browser.path,
          name: $("folder-name").value,
          with_ab: $("with-ab").checked,
        });
        await browseTo(v.path);
        $("folder-name").value = "";
        toast(
          "新目录已创建" + ($("with-ab").checked ? "，已包含 A / B。" : "。"),
        );
      } catch (e) {
        $("browser-error").textContent = e.message;
      }
    };
    document.querySelector(".logs").ontoggle = refreshLogs;
    setupDestinationLayout();
    renderEditor();
    renderTasks();
    renderProgress();
    await refresh();
    check("data_server");
    check("training_server");
    setInterval(refresh, 2000);
  } catch (e) {
    toast("界面初始化失败：" + e.message, true);
  }
}
init();

/* app.js — Connect Customer 队列监控前端
 *
 * 左侧(40%) 树形目录: 区域 → Connect Customer 实例 → 已监控队列(带红色告警计数)
 * 右侧(60%) 监控明细:
 *   - 区域: 该区域全部实例 + 是否监控 / 开始 / 结束 / 时区 / 监控状态, 可添加/取消监控并配置
 *   - 实例: 监控时间(周一~周日 + 开始/结束 + 时区 + 排除日期)与队列(是否监控 / 频率 / 阈值)
 *   - 队列: 当天(实例时区)的所有记录, lastUpdate 由新到旧, 分页, 导出 CSV, 无变化记录高亮
 * 配置通过 PUT /config 持久化为 S3 上的 JSON, 下次打开直接加载。
 * 角色(GET /me): admin 可修改配置、开始/结束轮询、添加/移除区域; viewer 只读(界面禁用编辑, 后端同样拒绝)。
 * 区域: 左侧目录的区域列表保存在配置 JSON 的 regionList 中, 只能添加提供 Connect Customer 服务的区域。
 * API 由 auth.js 提供的 window.Api 调用。
 */
(function () {
  "use strict";

  var WEEKDAYS = [[1, "周一"], [2, "周二"], [3, "周三"], [4, "周四"], [5, "周五"], [6, "周六"], [7, "周日"]];
  var WD_SHORT = { 1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日" };
  var BROWSER_TZ = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";
  var TZ_LIST = (function () {
    var list = [];
    try { list = Intl.supportedValuesOf("timeZone"); } catch (e) { list = []; }
    if (!list.length) list = ["Asia/Shanghai", "Asia/Singapore", "Asia/Tokyo", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles"];
    ["UTC", BROWSER_TZ].forEach(function (z) { if (list.indexOf(z) < 0) list.unshift(z); });
    return list;
  })();
  var LS_REFRESH = "cm_refresh_minutes";
  var LS_SPLIT = "cm_split_left_pct";        // 按登录邮箱区分: cm_split_left_pct:<email>
  var SPLIT_DEFAULT = 40, SPLIT_MIN_LEFT = 240, SPLIT_MIN_RIGHT = 420;
  var DEFAULT_FREQ = 1, DEFAULT_THRESHOLD = 15;

  // 只读用户仍可使用的操作(查看/刷新/翻页/导出), 其余可编辑控件全部禁用
  var VIEW_ACTS = ["edit-inst", "edit-inst-btn", "close-editor", "reload-instances", "reload-queues",
    "reload-records", "csv", "page", "page-size", "page-jump", "qfilter"];

  var S = {
    email: "",
    role: "viewer",
    readOnly: true,
    config: null,          // 服务器上已保存的配置
    version: null,         // 配置 updatedAt, 保存时作为乐观锁
    regions: [],
    instances: {},         // region -> [{instanceId, instanceArn, instanceAlias, status}]
    instLoading: {},
    instError: {},
    queues: {},            // instanceId -> [{queueId, queueArn, queueName}]
    queuesLoading: {},
    queuesError: {},
    expanded: {},          // "r:<region>" / "i:<instanceId>" -> true
    sel: null,             // {type: region|instance|queue, region, instanceId, queueId}
    draft: null,           // {region, instances: {...}} 编辑中的区域配置
    editIid: null,         // 区域视图下正在配置的实例
    qFilter: "",
    alerts: {},            // "<instanceId>#<queueId>" -> {stalledCount, ...}
    scheduler: null,
    records: null,
    recordsError: null,
    page: 1,
    pageSize: 20,
    refreshMinutes: 1,
    timer: null,
    paused: false,
  };

  // -------------------------------------------------------------------------
  // 工具
  // -------------------------------------------------------------------------
  function $(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function byName(a, b) { return String(a.queueName || "").localeCompare(String(b.queueName || "")); }
  function values(o) { return Object.keys(o || {}).map(function (k) { return o[k]; }); }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function toMin(hhmm) { var p = String(hhmm || "0:0").split(":"); return parseInt(p[0], 10) * 60 + parseInt(p[1], 10); }
  function shortId(id) { return id ? String(id).slice(0, 8) + "…" : ""; }

  function toast(msg, kind, ms) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = msg;
    el.addEventListener("click", function () { el.remove(); });
    $("toast").appendChild(el);
    setTimeout(function () { el.remove(); }, ms || (kind === "err" ? 8000 : 3500));
  }
  var busyCount = 0;
  function busy(on) {
    busyCount = Math.max(0, busyCount + (on ? 1 : -1));
    $("busy").classList.toggle("show", busyCount > 0);
  }

  /** 指定时区下的本地日期/时间/星期(ISO 1=周一) */
  function tzParts(tz, d) {
    var fmt;
    try {
      fmt = new Intl.DateTimeFormat("en-CA", {
        timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", weekday: "short",
      });
    } catch (e) { return tzParts("UTC", d); }
    var p = {};
    fmt.formatToParts(d || new Date()).forEach(function (x) { p[x.type] = x.value; });
    var wd = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 }[p.weekday] || 1;
    return { date: p.year + "-" + p.month + "-" + p.day, time: p.hour + ":" + p.minute, seconds: p.second, weekday: wd };
  }
  function fmtInTz(iso, tz) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    var p = tzParts(tz || "UTC", d);
    return p.date + " " + p.time + ":" + p.seconds;
  }
  function prevDate(dateStr) {
    var d = new Date(dateStr + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - 1);
    return d.toISOString().slice(0, 10);
  }
  function utcKeyToLocal(utcDate, hhmm) {
    if (!utcDate || !hhmm) return "";
    var d = new Date(utcDate + "T" + hhmm + ":00Z");
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  // -------------------------------------------------------------------------
  // 配置访问
  // -------------------------------------------------------------------------
  function cfgInstances(region) { return ((S.config.regions || {})[region] || {}).instances || {}; }
  function cfgInst(region, iid) { return cfgInstances(region)[iid]; }
  function monitoredQueues(inst) {
    if (!inst || !inst.monitored) return [];
    return values(inst.queues).filter(function (q) { return q.monitored; }).sort(byName);
  }
  function liveInst(region, iid) {
    return (S.instances[region] || []).filter(function (i) { return i.instanceId === iid; })[0];
  }
  /** 区域下实例: 实时列表 + 配置中有但列表里没有的(已删除/无权限) */
  function mergedInstances(region, instMap) {
    var map = {};
    (S.instances[region] || []).forEach(function (i) { map[i.instanceId] = { live: i, cfg: null }; });
    values(instMap || cfgInstances(region)).forEach(function (c) {
      if (map[c.instanceId]) map[c.instanceId].cfg = c;
      else map[c.instanceId] = { live: null, cfg: c, missing: !!S.instances[region] };
    });
    return values(map).map(function (x) {
      x.instanceId = (x.live || x.cfg).instanceId;
      x.alias = (x.live && x.live.instanceAlias) || (x.cfg && x.cfg.instanceAlias) || x.instanceId;
      return x;
    }).sort(function (a, b) { return a.alias.toLowerCase().localeCompare(b.alias.toLowerCase()); });
  }
  function alertCount(iid, qid) {
    var a = S.alerts[iid + "#" + qid];
    return a ? Number(a.stalledCount) || 0 : 0;
  }
  function instAlertSum(inst) {
    return monitoredQueues(inst).reduce(function (s, q) { return s + alertCount(inst.instanceId, q.queueId); }, 0);
  }
  function regionAlertSum(region) {
    return values(cfgInstances(region)).reduce(function (s, i) { return s + instAlertSum(i); }, 0);
  }

  /** 实例当前是否处于监控时间窗(按实例时区) */
  function windowState(inst) {
    var p = tzParts(inst.timezone || "UTC", new Date());
    var mins = toMin(p.time), s = toMin(inst.startTime), e = toMin(inst.endTime);
    var overnight = e <= s;
    var wds = inst.weekdays || [], ex = inst.excludedDates || [];
    function dayOk(date, wd) { return wds.indexOf(wd) >= 0 && ex.indexOf(date) < 0; }
    var yWd = p.weekday === 1 ? 7 : p.weekday - 1, yDate = prevDate(p.date);
    if (!overnight && dayOk(p.date, p.weekday) && mins >= s && mins <= e) return { active: true };
    if (overnight && ((dayOk(p.date, p.weekday) && mins >= s) || (dayOk(yDate, yWd) && mins <= e))) return { active: true };
    if (ex.indexOf(p.date) >= 0) return { active: false, reason: "今日排除" };
    if (wds.indexOf(p.weekday) < 0) return { active: false, reason: "今日不监控" };
    return { active: false, reason: mins < s ? "等待开始" : "今日已结束" };
  }
  function instanceStatus(inst) {
    if (!inst || !inst.monitored) return { cls: "off", text: "未监控" };
    if (!monitoredQueues(inst).length) return { cls: "warn", text: "未选择队列" };
    var w = windowState(inst);
    if (S.scheduler && !S.scheduler.enabled) return { cls: "off", text: "轮询已停止" };
    if (w.active) return { cls: "on", text: "监控中" };
    return { cls: "info", text: w.reason };
  }
  function statusPill(inst) {
    var st = instanceStatus(inst);
    return '<span class="pill ' + st.cls + '">' + esc(st.text) + "</span>";
  }
  function weekdaysText(wds) {
    wds = wds || [];
    if (wds.length === 7) return "每天";
    if (wds.join(",") === "1,2,3,4,5") return "工作日";
    if (wds.join(",") === "6,7") return "周末";
    if (!wds.length) return "—";
    return "周" + wds.map(function (d) { return WD_SHORT[d]; }).join("、");
  }

  // -------------------------------------------------------------------------
  // 草稿(编辑中的区域配置)
  // -------------------------------------------------------------------------
  function enterDraft(region) {
    S.draft = { region: region, instances: clone(cfgInstances(region)) };
  }
  function isDirty() {
    if (!S.draft || S.readOnly) return false;
    return JSON.stringify(S.draft.instances) !== JSON.stringify(cfgInstances(S.draft.region));
  }
  function defaultInstance(region, iid) {
    var live = liveInst(region, iid) || { instanceId: iid, instanceArn: "", instanceAlias: iid };
    return {
      instanceId: iid, instanceArn: live.instanceArn, instanceAlias: live.instanceAlias,
      monitored: false, timezone: BROWSER_TZ, startTime: "09:00", endTime: "18:00",
      weekdays: [1, 2, 3, 4, 5], excludedDates: [], queues: {},
    };
  }
  function draftInst(iid, create) {
    var d = S.draft.instances[iid];
    if (!d && create) d = S.draft.instances[iid] = defaultInstance(S.draft.region, iid);
    return d;
  }
  function draftQueue(inst, qid, create) {
    var q = inst.queues[qid];
    if (!q && create) {
      var live = (S.queues[inst.instanceId] || []).filter(function (x) { return x.queueId === qid; })[0] || {};
      q = inst.queues[qid] = {
        queueId: qid, queueArn: live.queueArn || "", queueName: live.queueName || qid,
        monitored: false, frequency: DEFAULT_FREQ, threshold: DEFAULT_THRESHOLD,
      };
    }
    return q;
  }
  function confirmLeave() {
    if (!isDirty()) return true;
    return window.confirm("当前有未保存的配置修改，确定放弃吗？");
  }

  function validateDraft() {
    var errs = [];
    values(S.draft.instances).forEach(function (inst) {
      var label = "[" + (inst.instanceAlias || inst.instanceId) + "] ";
      if (!/^\d{2}:\d{2}$/.test(inst.startTime || "")) errs.push(label + "开始时间无效");
      if (!/^\d{2}:\d{2}$/.test(inst.endTime || "")) errs.push(label + "结束时间无效");
      if (inst.monitored && !(inst.weekdays || []).length) errs.push(label + "至少选择一天(周一~周日)");
      if (!inst.instanceArn) errs.push(label + "缺少实例 ARN，请刷新实例列表后重试");
      values(inst.queues).forEach(function (q) {
        var f = q.frequency, t = q.threshold;
        var ql = label + "队列 " + q.queueName + ": ";
        if (!Number.isInteger(f) || f < 1) errs.push(ql + "监控频率必须是 ≥1 的整数(分钟)");
        else if (!Number.isInteger(t) || t < 1) errs.push(ql + "阈值必须是 ≥1 的整数(分钟)");
        else if (q.monitored && t < f) errs.push(ql + "阈值不能小于监控频率");
      });
    });
    return errs;
  }

  // -------------------------------------------------------------------------
  // 数据加载
  // -------------------------------------------------------------------------
  function loadInstances(region, force) {
    if (S.instLoading[region] || (S.instances[region] && !force)) return Promise.resolve();
    S.instLoading[region] = true;
    renderTree();
    return Api.get("/instances", { region: region }).then(function (r) {
      S.instances[region] = r.instances || [];
      delete S.instError[region];
    }).catch(function (e) {
      S.instError[region] = e.message || String(e);
    }).then(function () {
      S.instLoading[region] = false;
      renderTree();
      if (S.sel && S.sel.region === region && S.sel.type === "region") renderDetail();
    });
  }
  function loadQueues(region, iid, force) {
    if (S.queuesLoading[iid] || (S.queues[iid] && !force)) return Promise.resolve();
    S.queuesLoading[iid] = true;
    return Api.get("/queues", { region: region, instanceId: iid }).then(function (r) {
      S.queues[iid] = r.queues || [];
      delete S.queuesError[iid];
    }).catch(function (e) {
      S.queuesError[iid] = e.message || String(e);
    }).then(function () {
      S.queuesLoading[iid] = false;
      if (S.sel && (S.sel.instanceId === iid || S.editIid === iid)) renderDetail(true);
    });
  }
  function refreshAlerts() {
    return Api.get("/alerts").then(function (r) { S.alerts = r.alerts || {}; })
      .catch(function (e) { toast("刷新告警失败：" + e.message, "err"); });
  }
  function refreshScheduler() {
    return Api.get("/scheduler").then(function (r) { S.scheduler = r; renderScheduler(); })
      .catch(function (e) {
        S.scheduler = null;
        $("schedState").className = "pill err";
        $("schedState").textContent = "轮询状态读取失败";
        $("schedInfo").textContent = e.message;
      });
  }
  function loadRecords(keepPage) {
    var sel = S.sel;
    if (!sel || sel.type !== "queue") return Promise.resolve();
    return Api.get("/records", { region: sel.region, instanceId: sel.instanceId, queueId: sel.queueId }).then(function (r) {
      if (S.sel !== sel) return;
      S.records = r;
      S.recordsError = null;
      if (!keepPage) S.page = 1;
      renderDetail(true);
    }).catch(function (e) {
      if (S.sel !== sel) return;
      S.recordsError = e.message;
      renderDetail(true);
    });
  }
  function reloadConfig() {
    return Api.get("/config").then(function (r) {
      S.config = r.config;
      S.version = r.version;
    });
  }

  function refreshAll() {
    if (S.paused) return Promise.resolve();
    return Promise.all([refreshAlerts(), refreshScheduler()]).then(function () {
      renderTree();
      updateStatusPills();
      $("lastRefresh").textContent = "上次刷新 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
      if (S.sel && S.sel.type === "queue") return loadRecords(true);
    });
  }
  function startTimer() {
    if (S.timer) clearInterval(S.timer);
    S.timer = setInterval(refreshAll, S.refreshMinutes * 60000);
  }

  // -------------------------------------------------------------------------
  // 菜单栏: 轮询开关 + 刷新频率
  // -------------------------------------------------------------------------
  function renderScheduler() {
    var s = S.scheduler;
    if (!s) return;
    var el = $("schedState");
    el.className = "pill " + (s.enabled ? "on" : "off");
    el.textContent = s.enabled ? "● 轮询运行中" : "○ 轮询已停止";
    $("btnStart").disabled = !!s.enabled || S.readOnly;
    $("btnStop").disabled = !s.enabled || S.readOnly;
    if (S.readOnly) {
      $("btnStart").title = $("btnStop").title = "只读用户不能开始/结束轮询";
    }
    var t = s.today || {};
    var info = "";
    if (!t.slots) info = "今日(UTC " + (t.utcDate || "") + ")无轮询计划";
    else {
      info = "今日轮询字典(UTC " + t.utcDate + "): " + t.earliest + " – " + t.latest + " UTC · 共 " + t.polls + " 次队列采集";
      if (s.enabled && t.nextPoll) info += " · 下次 " + utcKeyToLocal(t.utcDate, t.nextPoll) + "(本地)";
    }
    $("schedInfo").textContent = info;
    $("schedInfo").title = t.slots ? "UTC " + t.earliest + " – " + t.latest + "，规则: " + (s.ruleName || "") : (s.ruleName || "");
  }
  function setScheduler(action) {
    if (S.readOnly) return;
    if (action === "stop" && !window.confirm("确定停止轮询吗？停止后 Lambda 不再采集任何队列。")) return;
    busy(true);
    Api.post("/scheduler", { action: action }).then(function (r) {
      S.scheduler = r;
      renderScheduler();
      renderTree();
      updateStatusPills();
      toast(action === "start" ? "已开始轮询" : "已停止轮询", "ok");
    }).catch(function (e) { toast("操作失败：" + e.message, "err"); })
      .then(function () { busy(false); });
  }
  function onRefreshInput() {
    var el = $("refreshInput");
    var v = Number(el.value);
    if (!Number.isInteger(v) || v < 1) {
      el.classList.add("invalid");
      toast("刷新频率必须是 ≥1 的整数(分钟)", "warn");
      return;
    }
    el.classList.remove("invalid");
    S.refreshMinutes = v;
    localStorage.setItem(LS_REFRESH, String(v));
    startTimer();
    toast("刷新频率已设置为每 " + v + " 分钟", "ok");
  }

  // -------------------------------------------------------------------------
  // 左侧树
  // -------------------------------------------------------------------------
  function nodeHtml(o) {
    var caretCls = o.caret == null ? "blank" : (o.caret ? "open" : "");
    return '<div class="node lvl' + o.lvl + " " + (o.cls || "") + '" data-act="' + o.act + '" ' + o.attrs + ">" +
      '<span class="caret ' + caretCls + '" data-act="toggle" data-node="' + esc(o.id) + '">▶</span>' +
      '<span class="icon">' + o.icon + "</span>" +
      '<span class="label" title="' + esc(o.title || o.label) + '">' + esc(o.label) + "</span>" +
      (o.sub ? '<span class="sub">' + esc(o.sub) + "</span>" : "") +
      '<span class="grow"></span>' + (o.right || "") + "</div>";
  }
  function isSel(type, region, iid, qid) {
    var s = S.sel;
    return s && s.type === type && s.region === region && (!iid || s.instanceId === iid) && (!qid || s.queueId === qid);
  }
  function renderTree() {
    if (!S.config) return;
    var html = [];
    S.regions.forEach(function (region) {
      var rid = "r:" + region, rOpen = !!S.expanded[rid];
      var rSum = regionAlertSum(region);
      var list = mergedInstances(region);
      var cnt = S.instances[region] ? list.length + " 个实例" : (S.instLoading[region] ? "加载中…" : "");
      html.push(nodeHtml({
        lvl: 0, id: rid, act: "sel-region", attrs: 'data-region="' + esc(region) + '"',
        caret: rOpen, icon: "🌐", label: region, sub: cnt,
        cls: isSel("region", region) ? "selected" : "",
        right: rSum ? '<span class="badge-sum" title="该区域今日异常记录总数">' + rSum + "</span>" : "",
      }));
      if (!rOpen) return;
      if (S.instError[region]) html.push('<div class="tree-hint lvl1 err">加载失败：' + esc(S.instError[region]) + "</div>");
      else if (!S.instances[region] && S.instLoading[region]) html.push('<div class="tree-hint lvl1">加载中…</div>');
      else if (!list.length) html.push('<div class="tree-hint lvl1">该区域没有 Connect Customer 实例</div>');
      list.forEach(function (x) {
        var inst = x.cfg, iid = x.instanceId, iOpen = !!S.expanded["i:" + iid];
        var qs = monitoredQueues(inst);
        var sum = instAlertSum(inst);
        var st = instanceStatus(inst);
        var dot = '<span class="dot ' + (st.cls === "on" ? "on" : (inst && inst.monitored ? "wait" : "")) + '" title="' + esc(st.text) + '"></span>';
        html.push(nodeHtml({
          lvl: 1, id: "i:" + iid, act: "sel-instance",
          attrs: 'data-region="' + esc(region) + '" data-iid="' + esc(iid) + '"',
          caret: qs.length ? iOpen : null, icon: dot, label: x.alias, title: x.alias + " (" + iid + ")",
          sub: x.missing ? "(实例列表中未找到)" : (qs.length ? qs.length + " 个队列" : (inst && inst.monitored ? "未选择队列"
            : (pendingQueueCount(inst) ? "未监控(已选 " + pendingQueueCount(inst) + " 个队列，需开启实例监控)" : "未监控"))),
          cls: isSel("instance", region, iid) ? "selected" : "",
          right: sum ? '<span class="badge-sum" title="该实例今日异常记录总数">' + sum + "</span>" : "",
        }));
        if (!iOpen || !qs.length) return;
        qs.forEach(function (q) {
          var n = alertCount(iid, q.queueId);
          html.push(nodeHtml({
            lvl: 2, id: "q:" + iid + ":" + q.queueId, act: "sel-queue",
            attrs: 'data-region="' + esc(region) + '" data-iid="' + esc(iid) + '" data-qid="' + esc(q.queueId) + '"',
            caret: null, icon: "📋", label: q.queueName, sub: "每" + q.frequency + "分 · 阈值" + q.threshold + "分",
            cls: (isSel("queue", region, iid, q.queueId) ? "selected " : "") + (n ? "alert" : ""),
            right: n ? '<span class="badge-red" title="今日 CONTACTS_HANDLED 在阈值时间内无变化的记录数">' + n + "</span>" : "",
          }));
        });
      });
    });
    if (!S.regions.length) html.push('<div class="tree-hint">部署配置中没有区域(regions)</div>');
    var tree = $("tree"), top = tree.scrollTop;
    tree.innerHTML = html.join("");
    tree.scrollTop = top;
  }

  function onTreeClick(ev) {
    var caret = ev.target.closest(".caret");
    var node = ev.target.closest(".node");
    if (!node) return;
    var d = node.dataset;
    if (caret && !caret.classList.contains("blank")) {
      var id = caret.dataset.node;
      S.expanded[id] = !S.expanded[id];
      if (S.expanded[id] && id.indexOf("r:") === 0) loadInstances(id.slice(2));
      renderTree();
      return;
    }
    if (d.act === "sel-region") selectRegion(d.region);
    else if (d.act === "sel-instance") selectInstance(d.region, d.iid);
    else if (d.act === "sel-queue") selectQueue(d.region, d.iid, d.qid);
  }

  function expandAll() {
    S.regions.forEach(function (r) { S.expanded["r:" + r] = true; loadInstances(r); });
    S.regions.forEach(function (r) {
      values(cfgInstances(r)).forEach(function (i) { if (monitoredQueues(i).length) S.expanded["i:" + i.instanceId] = true; });
    });
    renderTree();
  }
  function collapseAll() {
    S.expanded = {};
    renderTree();
  }

  // -------------------------------------------------------------------------
  // 选择
  // -------------------------------------------------------------------------
  function selectRegion(region) {
    if (!isSel("region", region) && !confirmLeave()) return;
    if (!isSel("region", region)) {
      S.sel = { type: "region", region: region };
      enterDraft(region);
      S.editIid = null;
    }
    S.expanded["r:" + region] = true;
    loadInstances(region);
    renderTree();
    renderDetail();
  }
  function selectInstance(region, iid) {
    if (isSel("instance", region, iid)) {
      if (monitoredQueues(cfgInst(region, iid)).length) { S.expanded["i:" + iid] = !S.expanded["i:" + iid]; renderTree(); }
      return;
    }
    if (!confirmLeave()) return;
    S.sel = { type: "instance", region: region, instanceId: iid };
    enterDraft(region);
    S.qFilter = "";
    if (monitoredQueues(cfgInst(region, iid)).length) S.expanded["i:" + iid] = true;
    loadQueues(region, iid);
    renderTree();
    renderDetail();
  }
  function selectQueue(region, iid, qid) {
    if (!isSel("queue", region, iid, qid) && !confirmLeave()) return;
    S.sel = { type: "queue", region: region, instanceId: iid, queueId: qid };
    S.draft = null;
    S.records = null;
    S.recordsError = null;
    S.page = 1;
    renderTree();
    renderDetail();
    loadRecords(false);
  }

  // -------------------------------------------------------------------------
  // 右侧明细
  // -------------------------------------------------------------------------
  function crumb(parts) {
    return '<span class="crumb">' + parts.map(function (p, i) {
      return i === parts.length - 1 ? "<b>" + esc(p) + "</b>" : esc(p);
    }).join(" › ") + "</span>";
  }

  function renderDetail(keepScroll) {
    var body = $("detailBody");
    var scroll = body.scrollTop;
    var qwrap = body.querySelector(".queue-wrap");
    var qscroll = qwrap ? qwrap.scrollTop : 0;
    var focusId = document.activeElement && document.activeElement.id;
    var s = S.sel;
    if (!s) return;
    if (s.type === "region") {
      $("detailHead").innerHTML = crumb(["监控明细", s.region]);
      body.innerHTML = regionViewHtml(s.region);
    } else if (s.type === "instance") {
      var inst = draftInst(s.instanceId, false) || defaultInstance(s.region, s.instanceId);
      $("detailHead").innerHTML = crumb(["监控明细", s.region, inst.instanceAlias || s.instanceId]);
      body.innerHTML = editorHtml(s.region, s.instanceId, true);
    } else if (s.type === "queue") {
      var ci = cfgInst(s.region, s.instanceId) || {};
      var q = (ci.queues || {})[s.queueId] || {};
      $("detailHead").innerHTML = crumb(["监控明细", s.region, ci.instanceAlias || s.instanceId, q.queueName || s.queueId]);
      body.innerHTML = queueViewHtml(s, ci, q);
    }
    if (keepScroll) {
      body.scrollTop = scroll;
      var nq = body.querySelector(".queue-wrap");
      if (nq) nq.scrollTop = qscroll;
    } else body.scrollTop = 0;
    if (focusId && $(focusId)) $(focusId).focus();
    applyQueueFilter();
    applyReadOnly(body);
  }

  function applyReadOnly(root) {
    if (!S.readOnly) return;
    root.querySelectorAll("input, select, button").forEach(function (el) {
      if (VIEW_ACTS.indexOf(el.dataset.act) < 0) {
        el.disabled = true;
        el.title = "只读用户不能修改";
      }
    });
    root.querySelectorAll("label.wd").forEach(function (el) { el.style.cursor = "default"; });
  }

  /** 定时刷新时只更新状态徽标, 不重绘编辑表单 */
  function updateStatusPills() {
    document.querySelectorAll("[data-status-iid]").forEach(function (el) {
      var inst = S.draft && S.draft.instances[el.dataset.statusIid];
      el.innerHTML = statusPill(inst);
    });
    document.querySelectorAll("[data-alert-q]").forEach(function (el) {
      var p = el.dataset.alertQ.split("#");
      var n = alertCount(p[0], p[1]);
      el.innerHTML = n ? '<span class="badge-red">' + n + "</span>" : '<span class="muted">0</span>';
    });
  }

  // ---- 区域视图 ----
  function regionViewHtml(region) {
    var list = mergedInstances(region, S.draft.instances);
    var monitored = list.filter(function (x) { return x.cfg && x.cfg.monitored; }).length;
    var h = [];
    h.push('<div class="card"><div class="card-title">🌐 ' + esc(region) +
      ' <span class="pill">' + list.length + ' 个实例</span><span class="pill on">已监控 ' + monitored + "</span>" +
      '<span class="spacer"></span><button class="btn sm" data-act="reload-instances">↻ 刷新实例列表</button>' +
      (S.readOnly ? "" : '<button class="btn sm danger" data-act="remove-region" title="从左侧目录移除该区域">移除区域</button>') +
      "</div>");
    if (S.instError[region]) h.push('<div class="row" style="color:var(--red)">实例列表加载失败：' + esc(S.instError[region]) + "</div>");
    if (!S.instances[region] && S.instLoading[region]) h.push('<div class="row muted">加载中…</div>');
    h.push('<div class="table-wrap"><table class="grid"><thead><tr>' +
      '<th class="c">监控</th><th>实例别名</th><th>实例 ID</th><th>开始时间</th><th>结束时间</th><th>时区</th>' +
      "<th>监控日</th><th>排除日期</th><th>监控状态</th><th>操作</th></tr></thead><tbody>");
    if (!list.length && S.instances[region]) h.push('<tr><td colspan="10" class="muted">该区域没有实例</td></tr>');
    list.forEach(function (x) {
      var c = x.cfg, iid = x.instanceId, on = !!(c && c.monitored);
      var cls = (S.editIid === iid ? "active " : "") + (on ? "" : "dim");
      h.push('<tr class="' + cls + '" data-act="edit-inst" data-iid="' + esc(iid) + '">' +
        '<td class="c"><input type="checkbox" data-act="inst-toggle" data-iid="' + esc(iid) + '"' + (on ? " checked" : "") + "></td>" +
        '<td><span class="link-cell">' + esc(x.alias) + "</span>" + (x.missing ? ' <span class="pill warn">未找到</span>' : "") + "</td>" +
        '<td class="mono" title="' + esc(iid) + '">' + esc(shortId(iid)) + "</td>" +
        "<td>" + (c ? esc(c.startTime) : "—") + "</td><td>" + (c ? esc(c.endTime) + (toMin(c.endTime) <= toMin(c.startTime) ? ' <span class="hint">(次日)</span>' : "") : "—") + "</td>" +
        "<td>" + (c ? esc(c.timezone) : "—") + "</td>" +
        "<td>" + (c ? esc(weekdaysText(c.weekdays)) : "—") + "</td>" +
        '<td title="' + esc(c ? (c.excludedDates || []).join(", ") : "") + '">' + (c && c.excludedDates.length ? c.excludedDates.length + " 天" : "—") + "</td>" +
        '<td data-status-iid="' + esc(iid) + '">' + statusPill(c) + "</td>" +
        '<td><button class="btn sm' + (on ? " danger" : "") + '" data-act="inst-toggle-btn" data-iid="' + esc(iid) + '">' + (on ? "取消监控" : "添加监控") + "</button> " +
        '<button class="btn sm" data-act="edit-inst-btn" data-iid="' + esc(iid) + '">配置</button></td></tr>');
    });
    h.push("</tbody></table></div>");
    h.push(saveRowHtml());
    h.push("</div>");
    if (S.editIid) h.push(editorHtml(region, S.editIid, false));
    else h.push('<div class="empty" style="margin-top:30px">点击实例行或「配置」按钮，设置监控时间与监控队列</div>');
    return h.join("");
  }

  function saveRowHtml() {
    if (S.readOnly) {
      return '<div class="row" style="margin-top:12px;margin-bottom:0"><span class="pill info">只读</span>' +
        '<span class="hint">只读用户可以查看配置和监控记录，不能修改；如需修改请联系管理员</span></div>';
    }
    var dirty = isDirty();
    return '<div class="row" style="margin-top:12px;margin-bottom:0">' +
      '<button class="btn primary" data-act="save"' + (dirty ? "" : " disabled") + ">💾 保存配置</button>" +
      '<button class="btn" data-act="discard">放弃修改并重新加载</button>' +
      '<span class="dirty">' + (dirty ? "● 有未保存的修改" : "") + "</span>" +
      '<span class="hint">保存后同步更新左侧目录，并持久化为 JSON 文件</span></div>';
  }

  // ---- 实例编辑器(区域视图与实例视图共用) ----
  function editorHtml(region, iid, standalone) {
    var inst = draftInst(iid, false) || defaultInstance(region, iid);
    var live = S.queues[iid] || [];
    var rows = {};
    live.forEach(function (q) { rows[q.queueId] = { live: q, cfg: inst.queues[q.queueId] }; });
    values(inst.queues).forEach(function (q) { if (!rows[q.queueId]) rows[q.queueId] = { live: null, cfg: q }; });
    var list = values(rows).map(function (r) {
      r.queueId = (r.live || r.cfg).queueId;
      r.queueName = (r.live && r.live.queueName) || (r.cfg && r.cfg.queueName) || r.queueId;
      return r;
    }).sort(byName);
    var monCount = list.filter(function (r) { return r.cfg && r.cfg.monitored; }).length;
    var h = [];
    h.push('<div class="card" id="instEditor">');
    h.push('<div class="card-title">⚙ ' + esc(inst.instanceAlias || iid) + ' <span class="mono muted" style="font-size:11px;font-weight:400">' + esc(iid) + "</span>" +
      '<label class="row" style="margin:0;font-weight:400;font-size:12px"><input type="checkbox" data-act="inst-toggle" data-iid="' + esc(iid) + '"' + (inst.monitored ? " checked" : "") + "> 监控该实例</label>" +
      '<span data-status-iid="' + esc(iid) + '">' + statusPill(draftInst(iid, false)) + "</span>" +
      '<span class="spacer"></span>' + (standalone ? "" : '<button class="btn sm" data-act="close-editor">收起</button>') + "</div>");

    // 时间
    var tzOpts = TZ_LIST.map(function (z) { return '<option value="' + esc(z) + '"' + (z === inst.timezone ? " selected" : "") + ">" + esc(z) + "</option>"; }).join("");
    h.push('<div class="row">' +
      '<label class="f">开始时间 <input type="time" id="fStart" data-act="field" data-f="startTime" data-iid="' + esc(iid) + '" value="' + esc(inst.startTime) + '"></label>' +
      '<label class="f">结束时间 <input type="time" id="fEnd" data-act="field" data-f="endTime" data-iid="' + esc(iid) + '" value="' + esc(inst.endTime) + '"></label>' +
      '<label class="f">时区 <select id="fTz" data-act="field" data-f="timezone" data-iid="' + esc(iid) + '">' + tzOpts + "</select></label>" +
      '<span class="hint">结束时间 ≤ 开始时间表示跨午夜到次日；时间、星期和排除日期均按该时区计算</span></div>');
    // 星期
    h.push('<div class="row"><span class="f muted" style="font-size:12px">监控日</span>' +
      WEEKDAYS.map(function (w) {
        var on = (inst.weekdays || []).indexOf(w[0]) >= 0;
        return '<label class="wd' + (on ? " on" : "") + '"><input type="checkbox" data-act="wd" data-iid="' + esc(iid) + '" data-wd="' + w[0] + '"' + (on ? " checked" : "") + ">" + w[1] + "</label>";
      }).join("") +
      '<button class="btn link" data-act="wd-preset" data-v="work" data-iid="' + esc(iid) + '">工作日</button>' +
      '<button class="btn link" data-act="wd-preset" data-v="all" data-iid="' + esc(iid) + '">每天</button>' +
      '<button class="btn link" data-act="wd-preset" data-v="none" data-iid="' + esc(iid) + '">清空</button></div>');
    // 排除日期
    var today = tzParts(inst.timezone || "UTC").date;
    h.push('<div class="row"><span class="f muted" style="font-size:12px">排除日期</span>' +
      '<input type="date" id="exDateInput" min="' + today + '">' +
      '<button class="btn sm" data-act="ex-add" data-iid="' + esc(iid) + '">+ 添加</button>' +
      '<div class="chips">' + ((inst.excludedDates || []).length ? inst.excludedDates.map(function (d) {
        return '<span class="chip' + (d < today ? " past" : "") + '">' + esc(d) + '<button data-act="ex-del" data-iid="' + esc(iid) + '" data-d="' + esc(d) + '" title="移除">×</button></span>';
      }).join("") : '<span class="hint">无</span>') + "</div></div>");

    // 队列
    h.push('<div class="section-title">队列 <span class="pill on">已监控 ' + monCount + " / 共 " + list.length + "</span>" +
      '<span class="spacer"></span><input type="text" id="qFilter" placeholder="筛选队列名称…" value="' + esc(S.qFilter) + '" data-act="qfilter">' +
      '<button class="btn sm" data-act="reload-queues" data-iid="' + esc(iid) + '">↻ 刷新队列列表</button></div>');
    if (!inst.monitored) {
      var pend = pendingQueueCount(inst);
      h.push('<div class="hint" style="margin-bottom:8px' + (pend ? ';color:var(--amber)' : '') + '">' +
        (pend ? "该实例未开启监控，已勾选的 " + pend + " 个队列不会显示在左侧目录，也不会被轮询。请勾选上方「监控该实例」并保存。"
              : "该实例未监控：勾选队列时会自动开启实例监控。") + "</div>");
    }
    h.push('<div class="row"><span class="hint">批量设置已勾选的队列：</span>' +
      '<label class="f">频率 <input type="number" id="bulkFreq" min="1" step="1" value="' + DEFAULT_FREQ + '"> 分钟</label>' +
      '<label class="f">阈值 <input type="number" id="bulkThr" min="1" step="1" value="' + DEFAULT_THRESHOLD + '"> 分钟</label>' +
      '<button class="btn sm" data-act="bulk-apply" data-iid="' + esc(iid) + '">应用</button></div>');
    if (S.queuesError[iid]) h.push('<div class="row" style="color:var(--red)">队列列表加载失败：' + esc(S.queuesError[iid]) + "</div>");
    var allOn = list.length && monCount === list.length;
    h.push('<div class="table-wrap queue-wrap" style="max-height:460px"><table class="grid"><thead><tr>' +
      '<th class="c"><input type="checkbox" data-act="q-all" data-iid="' + esc(iid) + '"' + (allOn ? " checked" : "") + ' title="全选/全不选(仅作用于筛选结果)"></th>' +
      "<th>队列名称</th><th>队列 ID</th><th>监控频率(分钟)</th><th>阈值(分钟)</th><th>今日异常</th></tr></thead><tbody>");
    if (!list.length) {
      h.push('<tr><td colspan="6" class="muted">' + (S.queuesLoading[iid] || !S.queues[iid] ? "队列加载中…" : "该实例没有标准队列") + "</td></tr>");
    }
    list.forEach(function (r) {
      var q = r.cfg || { frequency: DEFAULT_FREQ, threshold: DEFAULT_THRESHOLD, monitored: false };
      var bad = q.monitored && q.threshold < q.frequency;
      h.push('<tr class="qrow' + (q.monitored ? "" : " dim") + '" data-name="' + esc(String(r.queueName).toLowerCase()) + '">' +
        '<td class="c"><input type="checkbox" data-act="q-mon" data-iid="' + esc(iid) + '" data-qid="' + esc(r.queueId) + '"' + (q.monitored ? " checked" : "") + "></td>" +
        "<td>" + esc(r.queueName) + (r.live ? "" : ' <span class="pill warn" title="实例中已找不到该队列">未找到</span>') + "</td>" +
        '<td class="mono" title="' + esc(r.queueId) + '">' + esc(shortId(r.queueId)) + "</td>" +
        '<td><input type="number" min="1" step="1" data-act="q-num" data-k="frequency" data-iid="' + esc(iid) + '" data-qid="' + esc(r.queueId) + '" value="' + esc(q.frequency) + '"></td>' +
        '<td><input type="number" min="1" step="1" data-act="q-num" data-k="threshold" data-iid="' + esc(iid) + '" data-qid="' + esc(r.queueId) + '" value="' + esc(q.threshold) + '"' + (bad ? ' class="invalid"' : "") + "></td>" +
        '<td data-alert-q="' + esc(iid + "#" + r.queueId) + '">' + (alertCount(iid, r.queueId) ? '<span class="badge-red">' + alertCount(iid, r.queueId) + "</span>" : '<span class="muted">0</span>') + "</td></tr>");
    });
    h.push("</tbody></table></div>");
    if (standalone) h.push(saveRowHtml());
    h.push("</div>");
    return h.join("");
  }

  function applyQueueFilter() {
    var f = (S.qFilter || "").trim().toLowerCase();
    document.querySelectorAll("#detailBody tr.qrow").forEach(function (tr) {
      tr.style.display = !f || tr.dataset.name.indexOf(f) >= 0 ? "" : "none";
    });
  }
  function refreshSaveRow() {
    var dirty = isDirty();
    document.querySelectorAll('#detailBody [data-act="save"]').forEach(function (b) { b.disabled = !dirty; });
    document.querySelectorAll("#detailBody .dirty").forEach(function (el) { el.textContent = dirty ? "● 有未保存的修改" : ""; });
  }

  // ---- 队列视图 ----
  function fmtNum(v) { return v == null || v === "" ? '<span class="muted">—</span>' : esc(v); }
  function queueViewHtml(s, inst, q) {
    var tz = (S.records && S.records.timezone) || inst.timezone || "UTC";
    var r = S.records;
    var items = (r && r.items) || [];
    var total = items.length;
    var pages = Math.max(1, Math.ceil(total / S.pageSize));
    if (S.page > pages) S.page = pages;
    var start = (S.page - 1) * S.pageSize;
    var pageItems = items.slice(start, start + S.pageSize);
    var stalled = r ? r.stalledCount : 0;
    var h = [];
    h.push('<div class="card"><div class="card-title">📋 ' + esc(q.queueName || s.queueId) +
      '<span class="pill">频率 ' + esc(q.frequency || "?") + " 分钟</span>" +
      '<span class="pill warn">阈值 ' + esc(q.threshold || "?") + " 分钟</span>" +
      (q.monitored && inst.monitored ? "" : '<span class="pill off">当前未监控</span>') +
      '<span class="spacer"></span><button class="btn sm" data-act="reload-records">↻ 刷新</button>' +
      '<button class="btn sm primary" data-act="csv"' + (total ? "" : " disabled") + ">⬇ 导出 CSV</button></div>");
    h.push('<div class="kv-grid">' +
      kv("区域", s.region) + kv("实例", inst.instanceAlias || s.instanceId) +
      kv("实例 ARN", inst.instanceArn, true) + kv("队列 ARN", q.queueArn, true) +
      kv("时区", tz) + kv("日期(实例时区)", r ? r.date : tzParts(tz).date) +
      kv("当天记录数", r ? total : "…") +
      '<div><div class="k">异常记录数</div><div class="v' + (stalled ? " red" : "") + '">' + (r ? stalled : "…") + "</div></div>" +
      "</div></div>");
    h.push('<div class="row legend"><span><i class="sw" style="background:rgba(248,81,73,.45)"></i>高亮：当前记录与 ' +
      esc(q.threshold || "阈值") + " 分钟前的记录相比，CONTACTS_HANDLED 差为 0</span>" +
      "<span>CONTACTS_HANDLED 为实例时区当天 00:00 起的累计值</span></div>");
    if (S.recordsError) h.push('<div class="row" style="color:var(--red)">加载记录失败：' + esc(S.recordsError) + "</div>");
    h.push('<div class="table-wrap"><table class="grid"><thead><tr>' +
      '<th class="num">#</th><th>更新时间(lastUpdate)</th><th class="num">CONTACTS_HANDLED</th>' +
      '<th>对比记录时间</th><th class="num">对比值</th><th class="num">差值</th>' +
      '<th class="num">排队中</th><th class="num">最长等待(秒)</th><th class="num">在线坐席</th><th class="num">空闲坐席</th><th class="num">处理中坐席</th><th>状态</th>' +
      "</tr></thead><tbody>");
    if (!r && !S.recordsError) h.push('<tr><td colspan="12" class="muted">加载中…</td></tr>');
    else if (!total) h.push('<tr><td colspan="12" class="muted">当天还没有记录</td></tr>');
    pageItems.forEach(function (it, i) {
      var status;
      if (it.error && it.CONTACTS_HANDLED == null) status = '<span class="pill warn" title="' + esc(it.error) + '">采集失败</span>';
      else if (it.stalled) status = '<span class="pill err">无变化</span>';
      else if (it.compareLastUpdate == null) status = '<span class="pill off" title="没有找到阈值分钟前的记录">无对比</span>';
      else status = '<span class="pill on">正常</span>';
      if (it.error && it.CONTACTS_HANDLED != null) status += ' <span class="pill warn" title="' + esc(it.error) + '">部分失败</span>';
      h.push('<tr class="' + (it.stalled ? "stalled" : "") + '">' +
        '<td class="num muted">' + (start + i + 1) + "</td>" +
        '<td class="mono" title="UTC ' + esc(it.lastUpdate) + '">' + esc(it.lastUpdateLocal || fmtInTz(it.lastUpdate, tz)) + "</td>" +
        '<td class="num"><b>' + fmtNum(it.CONTACTS_HANDLED) + "</b></td>" +
        '<td class="mono">' + (it.compareLastUpdate ? esc(fmtInTz(it.compareLastUpdate, tz)) : '<span class="muted">—</span>') + "</td>" +
        '<td class="num">' + fmtNum(it.compareContactsHandled) + "</td>" +
        '<td class="num">' + fmtNum(it.delta) + "</td>" +
        '<td class="num">' + fmtNum(it.CONTACTS_IN_QUEUE) + "</td>" +
        '<td class="num">' + fmtNum(it.OLDEST_CONTACT_AGE) + "</td>" +
        '<td class="num">' + fmtNum(it.AGENTS_ONLINE) + "</td>" +
        '<td class="num">' + fmtNum(it.AGENTS_AVAILABLE) + "</td>" +
        '<td class="num">' + fmtNum(it.AGENTS_ON_CONTACT) + "</td>" +
        "<td>" + status + "</td></tr>");
    });
    h.push("</tbody></table></div>");
    h.push('<div class="pagination"><span>共 ' + total + " 条</span>" +
      '<label>每页 <select data-act="page-size">' + [20, 50, 100, 200].map(function (n) {
        return '<option value="' + n + '"' + (n === S.pageSize ? " selected" : "") + ">" + n + "</option>";
      }).join("") + "</select></label>" +
      '<button class="btn sm" data-act="page" data-p="1"' + (S.page <= 1 ? " disabled" : "") + ">首页</button>" +
      '<button class="btn sm" data-act="page" data-p="' + (S.page - 1) + '"' + (S.page <= 1 ? " disabled" : "") + ">上一页</button>" +
      "<span>第 " + S.page + " / " + pages + " 页</span>" +
      '<button class="btn sm" data-act="page" data-p="' + (S.page + 1) + '"' + (S.page >= pages ? " disabled" : "") + ">下一页</button>" +
      '<button class="btn sm" data-act="page" data-p="' + pages + '"' + (S.page >= pages ? " disabled" : "") + ">末页</button>" +
      '<label>跳至 <input type="number" min="1" max="' + pages + '" id="pageJump" data-act="page-jump"> 页</label></div>');
    return h.join("");
  }
  function kv(k, v, mono) {
    return '<div><div class="k">' + esc(k) + '</div><div class="v' + (mono ? " mono" : "") + '" style="' + (mono ? "font-size:11px" : "") + '">' + (v ? esc(v) : '<span class="muted">—</span>') + "</div></div>";
  }

  // ---- CSV ----
  var CSV_COLS = ["region", "connectArn", "connectAlias", "queueName", "queueArn", "queueId", "lastUpdate", "lastUpdateLocal",
    "timezone", "CONTACTS_HANDLED", "compareLastUpdate", "compareContactsHandled", "delta", "stalled", "threshold", "frequency",
    "CONTACTS_IN_QUEUE", "OLDEST_CONTACT_AGE", "AGENTS_ONLINE", "AGENTS_AVAILABLE", "AGENTS_ON_CONTACT", "error"];
  function csvCell(v) {
    if (v == null) return "";
    var s = String(v);
    if (typeof v === "string" && /^[=+\-@\t\r]/.test(s)) s = "'" + s; // 防止 CSV 公式注入
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function exportCsv() {
    var r = S.records;
    if (!r || !r.items.length) return;
    var lines = [CSV_COLS.join(",")];
    r.items.forEach(function (it) { lines.push(CSV_COLS.map(function (c) { return csvCell(it[c]); }).join(",")); });
    var first = r.items[0];
    var name = [first.connectAlias, first.queueName, r.date].join("_").replace(/[\\/:*?"<>|\s]+/g, "-") + ".csv";
    var blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  // -------------------------------------------------------------------------
  // 明细区事件
  // -------------------------------------------------------------------------
  function setMonitored(iid, on) {
    var inst = draftInst(iid, true);
    inst.monitored = on;
    if (on) {
      S.editIid = iid;
      loadQueues(S.draft.region, iid);
    }
    renderDetail(true);
  }

  function onDetailClick(ev) {
    var t = ev.target.closest("[data-act]");
    if (!t) return;
    var act = t.dataset.act, iid = t.dataset.iid;
    if (t.tagName === "INPUT" || t.tagName === "SELECT") return; // 由 change 事件处理
    switch (act) {
      case "edit-inst":
      case "edit-inst-btn":
        if (ev.target.closest("input,button") && act === "edit-inst") return;
        S.editIid = iid;
        S.qFilter = "";
        loadQueues(S.draft.region, iid);
        renderDetail(true);
        var ed = $("instEditor");
        if (ed) ed.scrollIntoView({ behavior: "smooth", block: "start" });
        break;
      case "inst-toggle-btn":
        var cur = draftInst(iid, false);
        setMonitored(iid, !(cur && cur.monitored));
        break;
      case "close-editor":
        S.editIid = null;
        renderDetail(true);
        break;
      case "reload-instances":
        loadInstances(S.sel.region, true);
        break;
      case "remove-region":
        removeRegion(S.sel.region);
        break;
      case "reload-queues":
        loadQueues(S.draft.region, iid, true);
        break;
      case "wd-preset":
        draftInst(iid, true).weekdays = { work: [1, 2, 3, 4, 5], all: [1, 2, 3, 4, 5, 6, 7], none: [] }[t.dataset.v];
        renderDetail(true);
        break;
      case "ex-add":
        var input = $("exDateInput");
        var v = input && input.value;
        if (!v) { toast("请先选择要排除的日期", "warn"); return; }
        var inst = draftInst(iid, true);
        if (inst.excludedDates.indexOf(v) < 0) { inst.excludedDates.push(v); inst.excludedDates.sort(); }
        renderDetail(true);
        break;
      case "ex-del":
        var i2 = draftInst(iid, true);
        i2.excludedDates = i2.excludedDates.filter(function (d) { return d !== t.dataset.d; });
        renderDetail(true);
        break;
      case "bulk-apply":
        bulkApply(iid);
        break;
      case "save":
        save();
        break;
      case "discard":
        discard();
        break;
      case "page":
        S.page = parseInt(t.dataset.p, 10) || 1;
        renderDetail(false);
        break;
      case "reload-records":
        loadRecords(true);
        break;
      case "csv":
        exportCsv();
        break;
    }
  }

  function onDetailChange(ev) {
    var t = ev.target, act = t.dataset.act, iid = t.dataset.iid;
    if (!act) return;
    var inst;
    switch (act) {
      case "inst-toggle":
        setMonitored(iid, t.checked);
        return;
      case "field":
        inst = draftInst(iid, true);
        if (!t.value) { t.classList.add("invalid"); return; }
        t.classList.remove("invalid");
        inst[t.dataset.f] = t.value;
        if (t.dataset.f === "timezone") { renderDetail(true); return; }
        break;
      case "wd":
        inst = draftInst(iid, true);
        var d = parseInt(t.dataset.wd, 10);
        inst.weekdays = inst.weekdays.filter(function (x) { return x !== d; });
        if (t.checked) inst.weekdays.push(d);
        inst.weekdays.sort();
        t.parentNode.classList.toggle("on", t.checked);
        break;
      case "q-mon":
        inst = draftInst(iid, true);
        draftQueue(inst, t.dataset.qid, true).monitored = t.checked;
        if (t.checked) ensureInstanceMonitored(inst);
        renderDetail(true);
        return;
      case "q-all":
        inst = draftInst(iid, true);
        document.querySelectorAll('#detailBody tr.qrow input[data-act="q-mon"]').forEach(function (cb) {
          if (cb.closest("tr").style.display === "none") return;
          draftQueue(inst, cb.dataset.qid, true).monitored = t.checked;
        });
        if (t.checked) ensureInstanceMonitored(inst);
        renderDetail(true);
        return;
      case "q-num":
        onQueueNumber(t);
        break;
      case "page-size":
        S.pageSize = parseInt(t.value, 10) || 20;
        S.page = 1;
        renderDetail(false);
        return;
      case "page-jump":
        var p = parseInt(t.value, 10);
        if (p >= 1) { S.page = p; renderDetail(false); }
        return;
    }
    refreshSaveRow();
    updateStatusPills();
  }

  /** 勾选队列即表示要监控: 实例未开启监控时一起开启, 否则队列不会出现在左侧目录, 也不会被轮询 */
  function ensureInstanceMonitored(inst) {
    if (inst.monitored) return;
    inst.monitored = true;
    toast("已同时开启实例「" + (inst.instanceAlias || inst.instanceId) + "」的监控，保存后队列会出现在左侧目录", "ok", 4500);
  }

  /** 实例未监控、但已勾选了队列(旧配置或手动取消了实例监控)的队列数 */
  function pendingQueueCount(inst) {
    if (!inst || inst.monitored) return 0;
    return values(inst.queues).filter(function (q) { return q.monitored; }).length;
  }

  function onQueueNumber(t) {
    var inst = draftInst(t.dataset.iid, true);
    var q = draftQueue(inst, t.dataset.qid, true);
    var v = Number(t.value);
    if (!Number.isInteger(v) || v < 1) { t.classList.add("invalid"); q[t.dataset.k] = NaN; return; }
    q[t.dataset.k] = v;
    var row = t.closest("tr");
    var thrInput = row.querySelector('[data-k="threshold"]');
    var bad = q.monitored && q.threshold < q.frequency;
    t.classList.remove("invalid");
    thrInput.classList.toggle("invalid", bad);
    if (bad) thrInput.title = "阈值不能小于监控频率"; else thrInput.removeAttribute("title");
  }

  function onDetailInput(ev) {
    var t = ev.target;
    if (t.dataset.act === "qfilter") { S.qFilter = t.value; applyQueueFilter(); }
    else if (t.dataset.act === "q-num") { onQueueNumber(t); refreshSaveRow(); }
  }

  function bulkApply(iid) {
    var f = Number($("bulkFreq").value), th = Number($("bulkThr").value);
    if (!Number.isInteger(f) || f < 1 || !Number.isInteger(th) || th < 1) { toast("频率和阈值必须是 ≥1 的整数(分钟)", "warn"); return; }
    if (th < f) { toast("阈值不能小于监控频率", "warn"); return; }
    var inst = draftInst(iid, true), n = 0;
    values(inst.queues).forEach(function (q) { if (q.monitored) { q.frequency = f; q.threshold = th; n++; } });
    if (!n) { toast("请先勾选要监控的队列", "warn"); return; }
    renderDetail(true);
    toast("已更新 " + n + " 个队列，记得保存", "ok");
  }

  function save() {
    if (S.readOnly) return;
    var errs = validateDraft();
    if (errs.length) { toast(errs.slice(0, 5).join("\n"), "err"); return; }
    var cfg = clone(S.config);
    cfg.regions = cfg.regions || {};
    cfg.regions[S.draft.region] = { instances: clone(S.draft.instances) };
    busy(true);
    Api.put("/config", { config: cfg, baseVersion: S.version }).then(function (r) {
      S.config = r.config;
      S.version = r.version;
      var region = S.draft.region;
      enterDraft(region);
      // 保存后展开该区域与已监控实例, 让新配置在树上可见
      S.expanded["r:" + region] = true;
      values(cfgInstances(region)).forEach(function (i) { if (monitoredQueues(i).length) S.expanded["i:" + i.instanceId] = true; });
      renderTree();
      renderDetail(true);
      toast("配置已保存", "ok");
      return Promise.all([refreshScheduler(), refreshAlerts()]).then(function () { renderTree(); updateStatusPills(); });
    }).catch(function (e) {
      toast((e.status === 409 ? "" : "保存失败：") + e.message + (e.status === 409 ? "\n可点击「放弃修改并重新加载」获取最新配置。" : ""), "err");
    }).then(function () { busy(false); });
  }

  function discard() {
    if (isDirty() && !window.confirm("确定放弃所有未保存的修改吗？")) return;
    busy(true);
    reloadConfig().then(function () {
      enterDraft(S.sel.region);
      renderTree();
      renderDetail(true);
    }).catch(function (e) { toast("重新加载失败：" + e.message, "err"); })
      .then(function () { busy(false); });
  }

  // -------------------------------------------------------------------------
  // 区域: 添加 / 移除(只允许提供 Connect Customer 服务、且账号已开通的区域)
  // -------------------------------------------------------------------------
  function applyRegionResult(r) {
    S.config = r.config;
    S.version = r.version;
    S.regions = r.regions || S.regions;
  }

  function openAddRegion() {
    if (S.readOnly) return;
    var modal = $("regionModal");
    $("regionModalBody").innerHTML = '<div class="hint">正在读取提供 Connect Customer 服务的区域…</div>';
    $("regionModalHint").textContent = "";
    $("btnRegionAddConfirm").disabled = true;
    modal.classList.add("show");
    Api.get("/regions/available").then(function (r) {
      var rows = (r.regions || []).map(function (x) {
        var blocked = x.added || x.enabledInAccount === false;
        var tag = x.added ? '<span class="pill on">已添加</span>'
          : (x.enabledInAccount === false ? '<span class="pill warn" title="请先在 AWS 账号设置中启用该区域">账号未开通</span>' : "");
        return '<label class="region-opt' + (blocked ? " disabled" : "") + '">' +
          '<input type="checkbox" data-act="region-pick" value="' + esc(x.region) + '"' + (blocked ? " disabled" : "") + (x.added ? " checked" : "") + ">" +
          '<span class="code">' + esc(x.region) + '</span><span class="name">' + esc(x.name) + "</span>" + tag + "</label>";
      });
      $("regionModalBody").innerHTML = rows.length ? rows.join("") : '<div class="hint">没有可用的区域</div>';
      $("regionModalHint").textContent = r.accountCheck ? "列表来自 AWS 公布的 Connect Customer 区域" :
        "无法读取账号已开通的区域，添加未开通的区域后将无法列出实例";
    }).catch(function (e) {
      $("regionModalBody").innerHTML = '<div class="hint" style="color:var(--red)">读取区域列表失败：' + esc(e.message) + "</div>";
    });
  }
  function closeModal() { $("regionModal").classList.remove("show"); }
  function pickedRegions() {
    return Array.prototype.slice.call(document.querySelectorAll('#regionModalBody input[data-act="region-pick"]:checked:not(:disabled)'))
      .map(function (el) { return el.value; });
  }
  function confirmAddRegions() {
    var picks = pickedRegions();
    if (!picks.length) return;
    busy(true);
    var added = [], failed = [];
    // 逐个添加: 每次都基于服务器上最新的配置
    picks.reduce(function (p, region) {
      return p.then(function () {
        return Api.post("/regions", { region: region }).then(function (r) {
          applyRegionResult(r);
          added.push(region);
          S.expanded["r:" + region] = true;
          loadInstances(region);
        }).catch(function (e) { failed.push(region + "：" + e.message); });
      });
    }, Promise.resolve()).then(function () {
      busy(false);
      closeModal();
      renderTree();
      if (added.length) toast("已添加区域：" + added.join("、"), "ok");
      if (failed.length) toast("添加失败：\n" + failed.join("\n"), "err");
    });
  }

  function removeRegion(region) {
    if (S.readOnly) return;
    var hasMonitored = values(cfgInstances(region)).some(function (i) { return i.monitored; });
    if (hasMonitored) {
      toast("区域 " + region + " 还有监控中的实例，请先取消监控并保存，再移除区域", "warn", 6000);
      return;
    }
    if (isSel("region", region) && isDirty() && !window.confirm("该区域有未保存的修改，移除后将丢失，确定继续吗？")) return;
    if (!window.confirm("确定从监控目录移除区域 " + region + " 吗？\n该区域下未监控实例的配置(时间、队列设置)会一起删除；之后可以重新添加。")) return;
    busy(true);
    Api.request("DELETE", "/regions", { region: region }).then(function (r) {
      applyRegionResult(r);
      delete S.expanded["r:" + region];
      if (S.sel && S.sel.region === region) {
        S.sel = null;
        S.draft = null;
        S.editIid = null;
        $("detailHead").innerHTML = '<span class="title">监控明细</span>';
        $("detailBody").innerHTML = '<div class="empty">区域 ' + esc(region) + " 已移除</div>";
      }
      renderTree();
      toast("已移除区域：" + region, "ok");
    }).catch(function (e) { toast("移除失败：" + e.message, "err"); })
      .then(function () { busy(false); });
  }

  // -------------------------------------------------------------------------
  // 左右分隔条: 拖动调整宽度, 比例(%)保存在 localStorage, 下次登录恢复
  // -------------------------------------------------------------------------
  function splitKey() { return LS_SPLIT + ":" + (S.email || "default"); }
  /** 把百分比限制在左右最小宽度之内 */
  function clampSplit(pct) {
    var w = document.querySelector(".layout").clientWidth || window.innerWidth;
    var sw = $("splitter").offsetWidth || 8;
    var lo = Math.min(50, SPLIT_MIN_LEFT / w * 100), hi = Math.max(50, 100 - (SPLIT_MIN_RIGHT + sw) / w * 100);
    return Math.round(Math.min(hi, Math.max(lo, pct)) * 10) / 10;
  }
  function applySplit(pct) {
    S.splitPct = clampSplit(pct);
    document.querySelector(".layout").style.setProperty("--left-pct", S.splitPct + "%");
    $("splitter").setAttribute("aria-valuenow", String(Math.round(S.splitPct)));
  }
  function saveSplit() {
    try { localStorage.setItem(splitKey(), String(S.splitPct)); } catch (e) { /* 隐私模式等不可写时忽略 */ }
  }
  function loadSplit() {
    var v = parseFloat(localStorage.getItem(splitKey()) || "");
    applySplit(v > 0 && v < 100 ? v : SPLIT_DEFAULT);
  }
  function initSplitter() {
    var el = $("splitter"), layout = document.querySelector(".layout");
    el.setAttribute("aria-valuemin", "0");
    el.setAttribute("aria-valuemax", "100");
    var dragging = false;
    function pctAt(clientX) {
      var r = layout.getBoundingClientRect();
      return (clientX - r.left) / r.width * 100;
    }
    el.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      dragging = true;
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
      document.body.classList.add("resizing");
      e.preventDefault();
    });
    el.addEventListener("pointermove", function (e) {
      if (dragging) applySplit(pctAt(e.clientX));
    });
    function end(e) {
      if (!dragging) return;
      dragging = false;
      if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
      el.classList.remove("dragging");
      document.body.classList.remove("resizing");
      saveSplit();
    }
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("dblclick", function () { applySplit(SPLIT_DEFAULT); saveSplit(); });
    el.addEventListener("keydown", function (e) {
      var step = e.shiftKey ? 5 : 1;
      if (e.key === "ArrowLeft") applySplit(S.splitPct - step);
      else if (e.key === "ArrowRight") applySplit(S.splitPct + step);
      else if (e.key === "Home") applySplit(SPLIT_DEFAULT);
      else return;
      e.preventDefault();
      saveSplit();
    });
    // 窗口变窄时重新限制, 但不覆盖已保存的比例
    window.addEventListener("resize", function () {
      var saved = parseFloat(localStorage.getItem(splitKey()) || "");
      applySplit(saved > 0 ? saved : S.splitPct);
    });
  }

  // -------------------------------------------------------------------------
  // 启动
  // -------------------------------------------------------------------------
  var bound = false;
  function bindOnce() {
    if (bound) return;
    bound = true;
    initSplitter();
    $("tree").addEventListener("click", onTreeClick);
    $("btnExpandAll").addEventListener("click", expandAll);
    $("btnAddRegion").addEventListener("click", openAddRegion);
    $("btnRegionAddConfirm").addEventListener("click", confirmAddRegions);
    $("regionModal").addEventListener("click", function (e) {
      if (e.target.id === "regionModal" || e.target.closest('[data-act="modal-close"]')) closeModal();
    });
    $("regionModal").addEventListener("change", function () {
      $("btnRegionAddConfirm").disabled = !pickedRegions().length;
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && $("regionModal").classList.contains("show")) closeModal();
    });
    $("btnCollapseAll").addEventListener("click", collapseAll);
    $("btnStart").addEventListener("click", function () { setScheduler("start"); });
    $("btnStop").addEventListener("click", function () { setScheduler("stop"); });
    $("btnRefreshNow").addEventListener("click", function () { refreshAll(); });
    $("refreshInput").addEventListener("change", onRefreshInput);
    $("btnSignOut").addEventListener("click", function () {
      if (confirmLeave()) window.Auth.signOut();
    });
    var body = $("detailBody");
    body.addEventListener("click", onDetailClick);
    body.addEventListener("change", onDetailChange);
    body.addEventListener("input", onDetailInput);
    window.addEventListener("beforeunload", function (e) {
      if (isDirty()) { e.preventDefault(); e.returnValue = ""; }
    });
  }

  function setRole(role) {
    S.role = role === "admin" ? "admin" : "viewer";
    S.readOnly = S.role !== "admin";
    var b = $("roleBadge");
    b.style.display = "";
    b.className = "pill " + (S.readOnly ? "info" : "on");
    b.textContent = S.readOnly ? "只读" : "管理员";
    b.title = S.readOnly ? "可以查看，不能修改配置或开始/结束轮询" : "可以修改配置、开始/结束轮询";
    $("btnAddRegion").style.display = S.readOnly ? "none" : "";
    renderScheduler();
  }

  function boot(opts) {
    S.email = opts.email || "";
    S.paused = false;
    $("userEmail").textContent = S.email;
    var saved = parseInt(localStorage.getItem(LS_REFRESH) || "1", 10);
    S.refreshMinutes = saved >= 1 ? saved : 1;
    $("refreshInput").value = S.refreshMinutes;
    bindOnce();
    loadSplit();
    busy(true);
    var me = Api.get("/me").catch(function () { return { role: "viewer" }; }); // 读取失败按只读处理
    Promise.all([Api.get("/config"), Api.get("/regions"), me]).then(function (res) {
      setRole(res[2].role);
      S.config = res[0].config;
      S.version = res[0].version;
      var regions = (res[1].regions || []).slice();
      Object.keys(S.config.regions || {}).forEach(function (r) { if (regions.indexOf(r) < 0) regions.push(r); });
      S.regions = regions;
      // 直接用持久化的 JSON 显示: 已监控的区域/实例默认展开
      S.regions.forEach(function (r) {
        values(cfgInstances(r)).forEach(function (i) {
          if (monitoredQueues(i).length) { S.expanded["r:" + r] = true; S.expanded["i:" + i.instanceId] = true; }
        });
      });
      renderTree();
      S.regions.forEach(function (r) { loadInstances(r); });
      return refreshAll();
    }).catch(function (e) {
      $("tree").innerHTML = '<div class="tree-hint err">加载配置失败：' + esc(e.message) + "</div>";
    }).then(function () {
      busy(false);
      startTimer();
    });
  }

  if ((window.__AWS_CONFIG__ || {}).mock) window.__MONITOR_STATE__ = S; // 仅本地调试

  window.MonitorApp = {
    boot: boot,
    resume: function (opts) {
      S.paused = false;
      S.email = opts.email;
      $("userEmail").textContent = S.email;
      loadSplit();
      // 换了账号登录时角色可能不同: 重新读取角色并重绘
      Api.get("/me").catch(function () { return { role: "viewer" }; }).then(function (me) {
        setRole(me.role);
        renderTree();
        if (S.sel) renderDetail(true);
        refreshAll();
        startTimer();
      });
    },
    pause: function () { S.paused = true; if (S.timer) clearInterval(S.timer); S.timer = null; },
  };
})();

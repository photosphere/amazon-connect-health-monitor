/* mock-api.js — 本地预览用的内存版 API(仅 dev/serve_local.py 注入, 不会部署)
 *
 * 模拟 api_handler.py 的全部路由; 配置保存在 localStorage(cm_mock_config)。
 * 记录数据按「实例时区当天 00:00 → 现在」按频率生成, CONTACTS_HANDLED 随机增长,
 * 部分队列会出现长时间不变化, 用于验证高亮与红色计数。
 * 角色: 默认 admin; 地址加 ?role=viewer 可预览只读用户界面(写操作返回 403)。
 */
(function () {
  "use strict";
  var LS = "cm_mock_config";
  var REGIONS = ["us-east-1", "ap-southeast-1", "ap-northeast-1"];   // 部署配置的默认区域
  // 模拟 SSM 公共参数里提供 Connect Customer 服务的区域; enabled=false 表示账号未开通
  var CONNECT_REGIONS = [
    ["af-south-1", "Africa (Cape Town)", false], ["ap-northeast-1", "Asia Pacific (Tokyo)", true],
    ["ap-northeast-2", "Asia Pacific (Seoul)", true], ["ap-southeast-1", "Asia Pacific (Singapore)", true],
    ["ap-southeast-2", "Asia Pacific (Sydney)", true], ["ca-central-1", "Canada (Central)", true],
    ["eu-central-1", "Europe (Frankfurt)", true], ["eu-west-2", "Europe (London)", true],
    ["us-east-1", "US East (N. Virginia)", true], ["us-west-2", "US West (Oregon)", true],
  ];
  function regionList(cfg) {
    var list = (cfg.regionList || REGIONS).slice();
    Object.keys(cfg.regions || {}).forEach(function (r) { if (list.indexOf(r) < 0) list.push(r); });
    return list;
  }
  function saveCfg(c) {
    c.updatedAt = new Date().toISOString();
    c.updatedBy = "dev@example.com";
    localStorage.setItem(LS, JSON.stringify(c));
    return { config: c, version: c.updatedAt, regions: regionList(c) };
  }
  var ACCT = "123456789012";
  function uuid(seed) {
    var s = "";
    for (var part = 0; part < 4; part++) {
      var h = 2166136261 ^ part;
      for (var i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
      s += ("00000000" + h.toString(16)).slice(-8);
    }
    return s.slice(0, 8) + "-" + s.slice(8, 12) + "-" + s.slice(12, 16) + "-" + s.slice(16, 20) + "-" + s.slice(20, 32);
  }
  function mkInst(region, alias) {
    var id = uuid(region + alias);
    return { instanceId: id, instanceArn: "arn:aws:connect:" + region + ":" + ACCT + ":instance/" + id, instanceAlias: alias, status: "ACTIVE" };
  }
  var INSTANCES = {
    "us-east-1": [mkInst("us-east-1", "midea-cs-prod"), mkInst("us-east-1", "midea-cs-uat"), mkInst("us-east-1", "midea-sales")],
    "ap-southeast-1": [mkInst("ap-southeast-1", "midea-sg-prod")],
    "ap-northeast-1": [],
    "us-west-2": [mkInst("us-west-2", "midea-us-west")],
  };
  var QNAMES = ["BasicQueue", "Aftersales_CN", "Aftersales_EN", "Installation", "VIP_Hotline", "Complaints", "Billing", "Callback", "Chat_Support", "Tech_L2"];
  var QUEUES = {};
  Object.keys(INSTANCES).forEach(function (r) {
    INSTANCES[r].forEach(function (inst, n) {
      QUEUES[inst.instanceId] = QNAMES.slice(0, 6 + n * 2).map(function (name) {
        var id = uuid(inst.instanceId + name);
        return { queueId: id, queueArn: inst.instanceArn + "/queue/" + id, queueName: name };
      });
    });
  });

  function initialConfig() {
    var inst = INSTANCES["us-east-1"][0], qs = QUEUES[inst.instanceId];
    var queues = {};
    [[0, 1, 5], [1, 1, 10], [4, 2, 6]].forEach(function (x) {
      var q = qs[x[0]];
      queues[q.queueId] = { queueId: q.queueId, queueArn: q.queueArn, queueName: q.queueName, monitored: true, frequency: x[1], threshold: x[2] };
    });
    var cfg = { version: 1, updatedAt: null, updatedBy: null, regions: { "us-east-1": { instances: {} } } };
    cfg.regions["us-east-1"].instances[inst.instanceId] = {
      instanceId: inst.instanceId, instanceArn: inst.instanceArn, instanceAlias: inst.instanceAlias, monitored: true,
      timezone: "Asia/Shanghai", startTime: "00:00", endTime: "23:59", weekdays: [1, 2, 3, 4, 5, 6, 7],
      excludedDates: ["2026-12-25"], queues: queues,
    };
    return cfg;
  }
  function getConfig() {
    try { var c = JSON.parse(localStorage.getItem(LS)); if (c) return c; } catch (e) { /* ignore */ }
    var c0 = initialConfig();
    localStorage.setItem(LS, JSON.stringify(c0));
    return c0;
  }
  var ruleEnabled = true;
  var ROLE = new URLSearchParams(location.search).get("role") === "viewer" ? "viewer" : "admin";

  function tzParts(tz, d) {
    var p = {};
    new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })
      .formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
    return p;
  }
  function rnd(seed) { var x = Math.sin(seed) * 10000; return x - Math.floor(x); }
  function hash(s) { var h = 0; for (var i = 0; i < s.length; i++) h = (h * 33 + s.charCodeAt(i)) >>> 0; return h; }

  function genRecords(region, inst, q, dateOverride) {
    var tz = inst.timezone, now = new Date();
    now.setSeconds(0, 0);
    var p = tzParts(tz, now);
    var date = dateOverride || (p.year + "-" + p.month + "-" + p.day);
    var minsNow = parseInt(p.hour, 10) * 60 + parseInt(p.minute, 10);
    var start = parseInt(inst.startTime.split(":")[0], 10) * 60 + parseInt(inst.startTime.split(":")[1], 10);
    var end = parseInt(inst.endTime.split(":")[0], 10) * 60 + parseInt(inst.endTime.split(":")[1], 10);
    var last = Math.min(minsNow, end);
    var seed = hash(q.queueId), ch = 0, rows = [], byMin = {};
    var stallFrom = 60 + (seed % 300), stallLen = 8 + (seed % 25);
    for (var m = start; m <= last; m += q.frequency) {
      var inStall = m >= stallFrom && m < stallFrom + stallLen;
      if (!inStall && rnd(seed + m) > 0.45) ch += 1 + Math.floor(rnd(seed * 3 + m) * 3);
      byMin[m] = ch;
      var t = new Date(now.getTime() - (minsNow - m) * 60000);
      var cmpMin = null;
      for (var k = m - q.threshold; k > m - q.threshold - q.frequency; k--) { if (byMin[k] != null) { cmpMin = k; break; } }
      var rec = {
        pk: inst.instanceId + "#" + q.queueId + "#" + date, lastUpdate: t.toISOString().replace(/\.\d+Z$/, "Z"),
        lastUpdateLocal: date + " " + String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0") + ":00",
        monitorDate: date, timezone: tz, region: region, connectArn: inst.instanceArn, connectAlias: inst.instanceAlias,
        instanceId: inst.instanceId, queueId: q.queueId, queueName: q.queueName, queueArn: q.queueArn,
        frequency: q.frequency, threshold: q.threshold, CONTACTS_HANDLED: ch, stalled: false,
        CONTACTS_IN_QUEUE: Math.floor(rnd(seed + m * 2) * 6), OLDEST_CONTACT_AGE: Math.floor(rnd(seed + m * 5) * 240),
        AGENTS_ONLINE: 8 + Math.floor(rnd(seed + m * 7) * 4), AGENTS_AVAILABLE: Math.floor(rnd(seed + m * 11) * 5), AGENTS_ON_CONTACT: Math.floor(rnd(seed + m * 13) * 6),
      };
      if (cmpMin != null) {
        var ct = new Date(now.getTime() - (minsNow - cmpMin) * 60000);
        rec.compareLastUpdate = ct.toISOString().replace(/\.\d+Z$/, "Z");
        rec.compareContactsHandled = byMin[cmpMin];
        rec.delta = ch - byMin[cmpMin];
        rec.stalled = rec.delta === 0;
      }
      if (seed % 7 === 0 && m % 97 === 0) { rec.error = "GetCurrentMetricData: ThrottlingException (mock)"; }
      rows.push(rec);
    }
    return { date: date, timezone: tz, items: rows.reverse() };
  }

  function monitored(cfg) {
    var out = [];
    Object.keys(cfg.regions || {}).forEach(function (r) {
      var insts = cfg.regions[r].instances || {};
      Object.keys(insts).forEach(function (iid) {
        var inst = insts[iid];
        if (!inst.monitored) return;
        Object.keys(inst.queues || {}).forEach(function (qid) {
          if (inst.queues[qid].monitored) out.push([r, inst, inst.queues[qid]]);
        });
      });
    });
    return out;
  }
  function schedSummary(cfg) {
    var now = new Date(), day = now.toISOString().slice(0, 10), slots = {}, polls = 0;
    monitored(cfg).forEach(function (x) {
      var inst = x[1], q = x[2];
      var s = parseInt(inst.startTime.slice(0, 2), 10) * 60 + parseInt(inst.startTime.slice(3), 10);
      for (var m = 0; m < 1440; m++) {
        var t = new Date(day + "T00:00:00Z");
        t.setUTCMinutes(m);
        var p = tzParts(inst.timezone, t);
        var lm = parseInt(p.hour, 10) * 60 + parseInt(p.minute, 10);
        var e = parseInt(inst.endTime.slice(0, 2), 10) * 60 + parseInt(inst.endTime.slice(3), 10);
        if (lm >= s && lm <= e && (lm - s) % q.frequency === 0) {
          var key = t.toISOString().slice(11, 16);
          slots[key] = 1; polls++;
        }
      }
    });
    var keys = Object.keys(slots).sort(), hhmm = now.toISOString().slice(11, 16);
    return {
      ruleName: "connect-monitor-" + ACCT + "-every-minute" /* 与 setup_monitor.py 默认命名一致 */, state: ruleEnabled ? "ENABLED" : "DISABLED", enabled: ruleEnabled,
      today: { slots: keys.length, polls: polls, earliest: keys[0] || null, latest: keys[keys.length - 1] || null,
        nextPoll: keys.filter(function (k) { return k >= hhmm; })[0] || null, utcDate: day },
    };
  }
  function err(status, msg) { var e = new Error(msg); e.status = status; return e; }
  function delay(v) { return new Promise(function (res) { setTimeout(function () { res(v); }, 150 + Math.random() * 250); }); }
  function fail(e) { return new Promise(function (_, rej) { setTimeout(function () { rej(e); }, 150); }); }

  window.__MOCK_API__ = function (method, path, params, body) {
    var cfg = getConfig();
    var key = method + " " + path;
    if (key === "GET /me") return delay({ email: "dev@example.com", role: ROLE });
    if ((key === "PUT /config" || key === "POST /scheduler" || key === "POST /regions" || key === "DELETE /regions") && ROLE !== "admin") {
      return fail(err(403, "只读用户无权执行此操作，请联系管理员"));
    }
    if (key === "GET /config") return delay({ config: cfg, version: cfg.updatedAt });
    if (key === "PUT /config") {
      if ((cfg.updatedAt || null) !== (body.baseVersion || null)) return fail(err(409, "配置已被其他用户修改，请刷新后重试"));
      var c = JSON.parse(JSON.stringify(body.config));
      c.regionList = cfg.regionList || null;   // 与后端一致: 保存实例配置不改区域列表
      return delay(saveCfg(c));
    }
    if (key === "GET /regions") return delay({ regions: regionList(cfg), defaults: REGIONS });
    if (key === "GET /regions/available") {
      var cur = regionList(cfg);
      return delay({ accountCheck: true, regions: CONNECT_REGIONS.map(function (x) {
        return { region: x[0], name: x[1], enabledInAccount: x[2], added: cur.indexOf(x[0]) >= 0 };
      }) });
    }
    if (key === "POST /regions") {
      var hit = CONNECT_REGIONS.filter(function (x) { return x[0] === body.region; })[0];
      if (!hit) return fail(err(400, body.region + " 不是提供 Connect Customer 服务的区域"));
      if (!hit[2]) return fail(err(400, "本账号尚未开通区域 " + body.region));
      var list = regionList(cfg);
      if (list.indexOf(body.region) >= 0) return fail(err(409, "区域 " + body.region + " 已在监控目录中"));
      cfg.regionList = list.concat([body.region]);
      return delay(saveCfg(cfg));
    }
    if (key === "DELETE /regions") {
      var insts = ((cfg.regions || {})[params.region] || {}).instances || {};
      var busy = Object.keys(insts).filter(function (k) { return insts[k].monitored; });
      if (busy.length) return fail(err(409, "区域 " + params.region + " 还有监控中的实例，请先取消监控并保存"));
      cfg.regionList = regionList(cfg).filter(function (r) { return r !== params.region; });
      delete (cfg.regions || {})[params.region];
      return delay(saveCfg(cfg));
    }
    if (key === "GET /instances") return delay({ region: params.region, instances: INSTANCES[params.region] || [] });
    if (key === "GET /queues") return delay({ queues: QUEUES[params.instanceId] || [] });
    if (key === "GET /records") {
      var hit = monitored(cfg).filter(function (x) { return x[1].instanceId === params.instanceId && x[2].queueId === params.queueId; })[0];
      if (!hit) return delay({ date: "", timezone: "UTC", items: [], count: 0, stalledCount: 0 });
      var r = genRecords(hit[0], hit[1], hit[2]);
      r.count = r.items.length;
      r.stalledCount = r.items.filter(function (i) { return i.stalled; }).length;
      return delay(r);
    }
    if (key === "GET /alerts") {
      var alerts = {};
      monitored(cfg).forEach(function (x) {
        var r2 = genRecords(x[0], x[1], x[2]);
        alerts[x[1].instanceId + "#" + x[2].queueId] = {
          region: x[0], date: r2.date, checkCount: r2.items.length,
          stalledCount: r2.items.filter(function (i) { return i.stalled; }).length,
          lastUpdate: r2.items[0] && r2.items[0].lastUpdate,
        };
      });
      return delay({ alerts: alerts });
    }
    if (key === "GET /scheduler") return delay(schedSummary(cfg));
    if (key === "POST /scheduler") { ruleEnabled = body.action === "start"; return delay(schedSummary(cfg)); }
    return fail(err(404, "mock: 未知路由 " + key));
  };
})();

#!/usr/bin/env python3
"""
monitor_lib.py — 监控配置(JSON)的校验与「每分钟轮询字典」构建

不依赖 boto3，poller.py 与 api_handler.py 共用，也可以在本地直接运行自测:
    python3 monitor_lib.py <monitor-config.json> [YYYY-MM-DD]

持久化 JSON 结构(存放在 S3: s3://<data-bucket>/config/monitor-config.json):

{
  "version": 1,
  "updatedAt": "2026-11-19T08:00:00Z",          # 每次保存由 API 写入, 用作乐观锁
  "updatedBy": "user@example.com",
  "regionList": ["us-east-1", "us-west-2"],     # 左侧目录显示的区域(网页上添加/移除), 顺序即显示顺序
  "regions": {
    "us-east-1": {
      "instances": {
        "<instanceId>": {
          "instanceId": "...", "instanceArn": "...", "instanceAlias": "...",
          "monitored": true,
          "timezone": "Asia/Shanghai",
          "startTime": "08:00", "endTime": "20:00",   # 结束 <= 开始 表示跨午夜
          "weekdays": [1, 2, 3, 4, 5],                # ISO: 1=周一 ... 7=周日
          "excludedDates": ["2026-12-25"],            # 实例时区下的本地日期
          "queues": {
            "<queueId>": {
              "queueId": "...", "queueArn": "...", "queueName": "...",
              "monitored": true,
              "frequency": 1,                         # 轮询频率(分钟, >=1)
              "threshold": 15                         # 阈值(分钟): 多少分钟内 CONTACTS_HANDLED 无变化
            }
          }
        }
      }
    }
  }
}

轮询字典(build_minute_schedule 的返回值):
    { "HH:MM"(UTC): [target, ...], ... }   —— 只包含需要轮询的分钟; 没有任何监控队列时为空字典 {}
    target = {region, instanceId, instanceArn, instanceAlias, timezone,
              queueId, queueArn, queueName, frequency, threshold}
字典按 UTC 自然日构建(不同实例的时区不同, 统一折算到 UTC 后才能合并到同一个字典);
每个实例的「星期几 / 排除日期」都按该实例自己的时区判断。
"""
import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
ALL_WEEKDAYS = [1, 2, 3, 4, 5, 6, 7]
DEFAULT_FREQUENCY = 1
DEFAULT_THRESHOLD = 15
MAX_MINUTES = 1440

_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def empty_config():
    return {
        "version": 1,
        "updatedAt": None,
        "updatedBy": None,
        "regionList": None,  # None = 尚未在网页上调整过, 使用部署配置的 regions
        "regions": {},
    }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    pass


def _parse_hhmm(value):
    m = _HHMM.match(value or "")
    if not m:
        raise ConfigError(f"时间格式应为 HH:MM: {value!r}")
    return int(m.group(1)), int(m.group(2))


def _int_in_range(value, name, lo, hi):
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是整数: {value!r}")
    if isinstance(value, int):
        iv = value
    elif isinstance(value, float) and value.is_integer():
        iv = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        iv = int(value.strip())
    else:
        raise ConfigError(f"{name} 必须是整数: {value!r}")
    if iv < lo or iv > hi:
        raise ConfigError(f"{name} 必须在 {lo}~{hi} 之间: {iv}")
    return iv


def get_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ConfigError(f"无效的时区: {name!r}")


def normalize_config(cfg):
    """校验并规范化配置; 返回新的 dict。非法时抛出 ConfigError。"""
    if not isinstance(cfg, dict):
        raise ConfigError("配置必须是 JSON 对象")
    out = empty_config()
    out["updatedAt"] = cfg.get("updatedAt")
    out["updatedBy"] = cfg.get("updatedBy")

    regions = cfg.get("regions") or {}
    if not isinstance(regions, dict):
        raise ConfigError("regions 必须是对象")
    for region, rcfg in regions.items():
        if not _REGION.match(region or ""):
            raise ConfigError(f"无效的区域名: {region!r}")
        instances = (rcfg or {}).get("instances") or {}
        if not isinstance(instances, dict):
            raise ConfigError(f"[{region}] instances 必须是对象")
        out_instances = {}
        for iid, inst in instances.items():
            out_instances[iid] = _normalize_instance(region, iid, inst or {})
        out["regions"][region] = {"instances": out_instances}

    region_list = cfg.get("regionList")
    if region_list is not None:
        if not isinstance(region_list, list):
            raise ConfigError("regionList 必须是数组")
        seen = []
        for r in region_list:
            if not isinstance(r, str) or not _REGION.match(r):
                raise ConfigError(f"regionList 中有无效的区域名: {r!r}")
            if r not in seen:
                seen.append(r)
        # 已有配置的区域必须在列表里, 否则左侧目录看不到它们
        for r in out["regions"]:
            if r not in seen:
                seen.append(r)
        out["regionList"] = seen
    return out


def effective_regions(cfg, default_regions):
    """左侧目录显示的区域: 网页上调整过就用 regionList, 否则用部署配置的 regions; 再补上已有配置的区域。"""
    base = cfg.get("regionList")
    out = list(base if base is not None else default_regions)
    for r in (cfg.get("regions") or {}):
        if r not in out:
            out.append(r)
    return out


def _normalize_instance(region, iid, inst):
    label = f"[{region}/{inst.get('instanceAlias') or iid}]"
    arn = inst.get("instanceArn") or ""
    if not arn.startswith("arn:") or not arn.endswith("/" + iid):
        raise ConfigError(f"{label} instanceArn 与 instanceId 不匹配")
    tz = inst.get("timezone") or "UTC"
    get_zone(tz)
    start = inst.get("startTime") or "00:00"
    end = inst.get("endTime") or "23:59"
    try:
        _parse_hhmm(start)
        _parse_hhmm(end)
    except ConfigError as e:
        raise ConfigError(f"{label} {e}")
    weekdays = inst.get("weekdays")
    if weekdays is None:
        weekdays = list(ALL_WEEKDAYS)
    weekdays = sorted({_int_in_range(d, f"{label} 星期", 1, 7) for d in weekdays})
    excluded = []
    for d in inst.get("excludedDates") or []:
        if not _DATE.match(str(d)):
            raise ConfigError(f"{label} 排除日期格式应为 YYYY-MM-DD: {d!r}")
        try:
            date.fromisoformat(d)
        except ValueError:
            raise ConfigError(f"{label} 排除日期不合法: {d!r}")
        excluded.append(d)
    excluded = sorted(set(excluded))
    monitored = bool(inst.get("monitored"))
    if monitored and not weekdays:
        raise ConfigError(f"{label} 监控中的实例至少要选择一天(周一~周日)")

    queues = {}
    for qid, q in (inst.get("queues") or {}).items():
        q = q or {}
        qlabel = f"{label} 队列 {q.get('queueName') or qid}"
        freq = _int_in_range(q.get("frequency", DEFAULT_FREQUENCY), f"{qlabel} 频率", 1, MAX_MINUTES)
        thr = _int_in_range(q.get("threshold", DEFAULT_THRESHOLD), f"{qlabel} 阈值", 1, MAX_MINUTES)
        if q.get("monitored") and thr < freq:
            raise ConfigError(f"{qlabel} 阈值({thr}) 不能小于监控频率({freq})")
        queues[qid] = {
            "queueId": qid,
            "queueArn": q.get("queueArn") or "",
            "queueName": q.get("queueName") or qid,
            "monitored": bool(q.get("monitored")),
            "frequency": freq,
            "threshold": thr,
        }

    return {
        "instanceId": iid,
        "instanceArn": arn,
        "instanceAlias": inst.get("instanceAlias") or iid,
        "monitored": monitored,
        "timezone": tz,
        "startTime": start,
        "endTime": end,
        "weekdays": weekdays,
        "excludedDates": excluded,
        "queues": queues,
    }


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------


def iter_monitored(cfg):
    """遍历「实例已监控 且 至少一个队列已监控」的组合: (region, inst, [queue, ...])。"""
    for region, rcfg in sorted((cfg.get("regions") or {}).items()):
        for _iid, inst in sorted((rcfg.get("instances") or {}).items()):
            if not inst.get("monitored"):
                continue
            queues = [q for q in (inst.get("queues") or {}).values() if q.get("monitored")]
            if queues:
                yield region, inst, sorted(queues, key=lambda q: q.get("queueName") or "")


def is_monitor_day(inst, local_date):
    weekdays = inst.get("weekdays") or []
    if local_date.isoweekday() not in weekdays:
        return False
    return local_date.isoformat() not in set(inst.get("excludedDates") or [])


def instance_windows(inst, utc_start, utc_end):
    """实例在 [utc_start, utc_end) 内可能涉及的每个本地监控窗口。

    返回 [(window_start_utc, window_end_utc, local_date)]，窗口两端都包含。
    结束时间 <= 开始时间表示跨午夜(结束在次日)。
    """
    tz = get_zone(inst.get("timezone") or "UTC")
    sh, sm = _parse_hhmm(inst.get("startTime") or "00:00")
    eh, em = _parse_hhmm(inst.get("endTime") or "23:59")
    overnight = (eh, em) <= (sh, sm)
    first = utc_start.astimezone(tz).date() - timedelta(days=1)
    last = utc_end.astimezone(tz).date()
    windows = []
    d = first
    while d <= last:
        if is_monitor_day(inst, d):
            ws = datetime.combine(d, time(sh, sm), tzinfo=tz).astimezone(UTC)
            end_date = d + timedelta(days=1) if overnight else d
            we = datetime.combine(end_date, time(eh, em), tzinfo=tz).astimezone(UTC)
            if we >= utc_start and ws < utc_end:
                windows.append((ws, we, d))
        d += timedelta(days=1)
    return windows


def build_minute_schedule(cfg, utc_day):
    """构建 utc_day(UTC 自然日)的轮询字典 {"HH:MM": [target, ...]}。

    某个队列在窗口内的轮询分钟 = 窗口开始 + k * frequency (k >= 0, 且不晚于窗口结束)。
    同一分钟同一队列只出现一次(跨午夜窗口与次日窗口重叠时去重)。
    """
    if isinstance(utc_day, datetime):
        utc_day = utc_day.astimezone(UTC).date()
    day_start = datetime.combine(utc_day, time(0, 0), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    schedule = {}
    seen = set()
    for region, inst, queues in iter_monitored(cfg):
        for ws, we, _local_date in instance_windows(inst, day_start, day_end):
            for q in queues:
                freq = int(q.get("frequency") or DEFAULT_FREQUENCY)
                t = ws
                if t < day_start:
                    # 跳到当天第一个对齐点
                    skip = (day_start - t) // timedelta(minutes=1)
                    k = -(-skip // freq)  # ceil
                    t = ws + timedelta(minutes=k * freq)
                step = timedelta(minutes=freq)
                while t <= we and t < day_end:
                    key = t.strftime("%H:%M")
                    dedup = (key, inst["instanceId"], q["queueId"])
                    if dedup not in seen:
                        seen.add(dedup)
                        schedule.setdefault(key, []).append(
                            {
                                "region": region,
                                "instanceId": inst["instanceId"],
                                "instanceArn": inst["instanceArn"],
                                "instanceAlias": inst.get("instanceAlias") or inst["instanceId"],
                                "timezone": inst.get("timezone") or "UTC",
                                "queueId": q["queueId"],
                                "queueArn": q.get("queueArn") or "",
                                "queueName": q.get("queueName") or q["queueId"],
                                "frequency": freq,
                                "threshold": int(q.get("threshold") or DEFAULT_THRESHOLD),
                            }
                        )
                    t += step
    return dict(sorted(schedule.items()))


def schedule_summary(schedule):
    if not schedule:
        return {"slots": 0, "polls": 0, "earliest": None, "latest": None}
    keys = list(schedule.keys())
    return {
        "slots": len(keys),
        "polls": sum(len(v) for v in schedule.values()),
        "earliest": keys[0],
        "latest": keys[-1],
    }


def local_today(tz_name, now=None):
    now = now or datetime.now(UTC)
    return now.astimezone(get_zone(tz_name)).date().isoformat()


def find_instance(cfg, region, instance_id):
    return ((cfg.get("regions") or {}).get(region) or {}).get("instances", {}).get(instance_id)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        config = normalize_config(json.load(f))
    day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(UTC).date()
    sched = build_minute_schedule(config, day)
    print(json.dumps(schedule_summary(sched), ensure_ascii=False))
    for k, v in sched.items():
        print(k, [f"{t['instanceAlias']}/{t['queueName']}" for t in v])

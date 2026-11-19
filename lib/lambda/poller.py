#!/usr/bin/env python3
"""
poller.py — 每分钟由 EventBridge 规则触发的队列轮询 Lambda

流程:
  1) 从 S3 读取持久化的监控配置 JSON(按 ETag 缓存, 配置变化或跨 UTC 日时重建字典)。
  2) monitor_lib.build_minute_schedule() 把配置展开成当天(UTC)的轮询字典:
         {"HH:MM": [target, ...]}   没有任何监控队列时为空字典
  3) 当前分钟早于字典最早时间或晚于最晚时间 → 直接返回(当天轮询未开始/已结束);
     第二天重新构建字典继续同样逻辑, 排除日期/非监控星期不会出现在字典里。
  4) 取当前分钟对应的 target 列表, 按实例分组:
       - GetCurrentMetricData: 实时指标(CONTACTS_IN_QUEUE / AGENTS_ONLINE / ...)
       - GetMetricDataV2:       CONTACTS_HANDLED(实例时区当天 00:00 至当前分钟的累计值)
         注: CONTACTS_HANDLED 不是 GetCurrentMetricData 支持的实时指标,
             只能通过历史指标接口 GetMetricDataV2 取得。
  5) 每个队列写一条记录到 DynamoDB 指标表; 与「阈值分钟前」的记录比较
     CONTACTS_HANDLED, 差为 0 记为 stalled, 并在状态表累加当天的 stalledCount。

环境变量(由 setup_monitor.py 注入):
  DATA_BUCKET     配置 JSON 所在桶
  CONFIG_KEY      配置 JSON 的 key(默认 config/monitor-config.json)
  METRICS_TABLE   指标记录表(pk=<instanceId>#<queueId>#<本地日期>, sk=lastUpdate)
  STATUS_TABLE    队列当天状态表(pk=<instanceId>#<queueId>, sk=monitorDate)
  RETENTION_DAYS  记录保留天数(DynamoDB TTL), 默认 30
"""
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

import monitor_lib

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

UTC = timezone.utc
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "config/monitor-config.json")
METRICS_TABLE = os.environ.get("METRICS_TABLE", "")
STATUS_TABLE = os.environ.get("STATUS_TABLE", "")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30") or 30)
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")  # S3 ExpectedBucketOwner: 防止读到他人账号的同名桶

# GetCurrentMetricData 每次请求的实时指标
CURRENT_METRICS = [
    {"Name": "CONTACTS_IN_QUEUE", "Unit": "COUNT"},
    {"Name": "OLDEST_CONTACT_AGE", "Unit": "SECONDS"},
    {"Name": "AGENTS_ONLINE", "Unit": "COUNT"},
    {"Name": "AGENTS_AVAILABLE", "Unit": "COUNT"},
    {"Name": "AGENTS_ON_CONTACT", "Unit": "COUNT"},
]
QUEUE_BATCH = 100  # 两个 API 的队列过滤器上限都是 100

_retry = Config(retries={"max_attempts": 6, "mode": "adaptive"}, connect_timeout=5, read_timeout=15)
_s3 = boto3.client("s3", config=_retry)
_connect_clients = {}
_connect_lock = threading.Lock()
_local = threading.local()  # boto3 resource 不是线程安全的: 每个线程一个

_cache = {"etag": None, "config": None, "utcDate": None, "schedule": None}


def _tables():
    if not hasattr(_local, "tables"):
        ddb = boto3.session.Session().resource("dynamodb", config=_retry)
        _local.tables = (ddb.Table(METRICS_TABLE), ddb.Table(STATUS_TABLE))
    return _local.tables


def _connect(region):
    with _connect_lock:  # client 线程安全, 但创建过程要加锁
        if region not in _connect_clients:
            _connect_clients[region] = boto3.client("connect", region_name=region, config=_retry)
        return _connect_clients[region]


def _iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v):
    """float/int → DynamoDB 可接受的 Decimal。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    f = float(v)
    return Decimal(int(f)) if f.is_integer() else Decimal(str(round(f, 3)))


# ---------------------------------------------------------------------------
# 配置与调度字典
# ---------------------------------------------------------------------------


def load_config():
    try:
        kw = {"ExpectedBucketOwner": ACCOUNT_ID} if ACCOUNT_ID else {}
        obj = _s3.get_object(Bucket=DATA_BUCKET, Key=CONFIG_KEY, **kw)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return monitor_lib.empty_config(), None
        raise
    etag = obj.get("ETag")
    if etag and etag == _cache["etag"] and _cache["config"] is not None:
        return _cache["config"], etag
    cfg = monitor_lib.normalize_config(json.loads(obj["Body"].read()))
    _cache.update({"etag": etag, "config": cfg, "utcDate": None, "schedule": None})
    return cfg, etag


def get_schedule(now):
    cfg, _etag = load_config()
    day = now.date()
    if _cache["schedule"] is None or _cache["utcDate"] != day:
        _cache["schedule"] = monitor_lib.build_minute_schedule(cfg, day)
        _cache["utcDate"] = day
        LOG.info(
            "rebuilt schedule for %s: %s",
            day,
            json.dumps(monitor_lib.schedule_summary(_cache["schedule"])),
        )
    return _cache["schedule"]


# ---------------------------------------------------------------------------
# Connect 指标
# ---------------------------------------------------------------------------


def fetch_current_metrics(client, instance_id, queue_ids):
    """GetCurrentMetricData(按 QUEUE 分组) → {queueId: {metric: value}}, snapshotTime"""
    out, snapshot, token = {}, None, None
    while True:
        kw = {
            "InstanceId": instance_id,
            "Filters": {"Queues": queue_ids},
            "Groupings": ["QUEUE"],
            "CurrentMetrics": CURRENT_METRICS,
            "MaxResults": 100,
        }
        if token:
            kw["NextToken"] = token
        resp = client.get_current_metric_data(**kw)
        snapshot = resp.get("DataSnapshotTime") or snapshot
        for r in resp.get("MetricResults", []):
            qid = ((r.get("Dimensions") or {}).get("Queue") or {}).get("Id")
            if not qid:
                continue
            vals = out.setdefault(qid, {})
            for c in r.get("Collections", []):
                vals[c["Metric"]["Name"]] = c.get("Value")
        token = resp.get("NextToken")
        if not token:
            break
    return out, snapshot


def fetch_contacts_handled(client, instance_arn, queue_ids, tz_name, now):
    """GetMetricDataV2 CONTACTS_HANDLED(TOTAL, 实例时区当天 00:00 ~ now) → {queueId: value}

    参考 https://docs.aws.amazon.com/boto3/latest/reference/services/connect/client/get_metric_data_v2.html
      - ResourceArn 为实例 ARN; Filters 最多 5 个 key, 每个 FilterValues 最多 100 个(调用方按 100 分批)
      - IntervalPeriod=TOTAL 时 StartTime 与 EndTime 相差需小于 35 天, EndTime 不能晚于当前时间
      - 不支持座席队列(只监控 STANDARD 队列); MaxResults 最大 100, 结果用 NextToken 翻页
      - 返回值为 null(数据不足) 或队列不在 MetricResults 中(当天没有数据)时按 0 处理
    """
    tz = monitor_lib.get_zone(tz_name)
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = local_midnight.astimezone(UTC)
    if now <= start:
        return {q: 0 for q in queue_ids}
    out, token = {}, None
    while True:
        kw = {
            "ResourceArn": instance_arn,
            "StartTime": start,
            "EndTime": now,
            "Interval": {"TimeZone": tz_name, "IntervalPeriod": "TOTAL"},
            "Filters": [{"FilterKey": "QUEUE", "FilterValues": queue_ids}],
            "Groupings": ["QUEUE"],
            "Metrics": [{"Name": "CONTACTS_HANDLED"}],
            "MaxResults": 100,
        }
        if token:
            kw["NextToken"] = token
        resp = client.get_metric_data_v2(**kw)
        for r in resp.get("MetricResults", []):
            dims = r.get("Dimensions") or {}
            qid = dims.get("QUEUE") or ""
            if qid.startswith("arn:"):
                qid = qid.rsplit("/", 1)[-1]
            for c in r.get("Collections", []):
                if c["Metric"].get("Name") == "CONTACTS_HANDLED":
                    v = c.get("Value")
                    out[qid] = 0 if v is None else v
        token = resp.get("NextToken")
        if not token:
            break
    # 当天没有处理过联系的队列不会出现在结果里, 视为 0
    return {q: out.get(q, 0) for q in queue_ids}


# ---------------------------------------------------------------------------
# 写入 DynamoDB
# ---------------------------------------------------------------------------


def find_compare_record(table, pk, now, threshold, frequency):
    """找「阈值分钟前」那条记录: lastUpdate ∈ (now-threshold-frequency, now-threshold]，取最新一条。"""
    t0 = now - timedelta(minutes=threshold)
    lower = t0 - timedelta(minutes=frequency) + timedelta(minutes=1)
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(pk) & Key("lastUpdate").between(_iso(lower), _iso(t0)),
        ScanIndexForward=False,
        Limit=1,
        ConsistentRead=True,
    )
    items = resp.get("Items") or []
    return items[0] if items else None


def write_record(target, now, metrics, handled, snapshot, error):
    metrics_table, status_table = _tables()
    tz = monitor_lib.get_zone(target["timezone"])
    local = now.astimezone(tz)
    monitor_date = local.date().isoformat()
    qkey = f"{target['instanceId']}#{target['queueId']}"
    pk = f"{qkey}#{monitor_date}"
    ttl = int((now + timedelta(days=RETENTION_DAYS)).timestamp())

    item = {
        "pk": pk,
        "lastUpdate": _iso(now),
        "lastUpdateLocal": local.strftime("%Y-%m-%d %H:%M:%S"),
        "monitorDate": monitor_date,
        "timezone": target["timezone"],
        "region": target["region"],
        "connectArn": target["instanceArn"],
        "connectAlias": target["instanceAlias"],
        "instanceId": target["instanceId"],
        "queueId": target["queueId"],
        "queueName": target["queueName"],
        "queueArn": target["queueArn"],
        "frequency": target["frequency"],
        "threshold": target["threshold"],
        "polledAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "ttl": ttl,
        "stalled": False,
    }
    if snapshot:
        item["dataSnapshotTime"] = _iso(snapshot) if isinstance(snapshot, datetime) else str(snapshot)
    if error:
        item["error"] = str(error)[:500]
    if handled is not None:
        item["CONTACTS_HANDLED"] = _num(handled)
    for m in CURRENT_METRICS:
        v = (metrics or {}).get(m["Name"])
        if v is not None:
            item[m["Name"]] = _num(v)

    if handled is not None:
        prev = find_compare_record(metrics_table, pk, now, target["threshold"], target["frequency"])
        if prev is not None and prev.get("CONTACTS_HANDLED") is not None:
            delta = _num(handled) - prev["CONTACTS_HANDLED"]
            item["compareLastUpdate"] = prev["lastUpdate"]
            item["compareContactsHandled"] = prev["CONTACTS_HANDLED"]
            item["delta"] = delta
            item["stalled"] = delta == 0

    try:
        # 条件写入: EventBridge 至少一次投递, 同一分钟重复调用时不重复计数
        metrics_table.put_item(Item=item, ConditionExpression="attribute_not_exists(lastUpdate)")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            LOG.info("duplicate poll skipped: %s %s", pk, item["lastUpdate"])
            return None
        raise

    sets = [
        "lastUpdate = :lu",
        "lastUpdateLocal = :lul",
        "queueName = :qn",
        "connectAlias = :ca",
        "#rg = :rg",
        "#ttl = :ttl",
    ]
    values = {
        ":lu": item["lastUpdate"],
        ":lul": item["lastUpdateLocal"],
        ":qn": item["queueName"],
        ":ca": item["connectAlias"],
        ":rg": item["region"],
        ":ttl": ttl,
        ":one": 1,
        ":st": 1 if item["stalled"] else 0,
    }
    if "CONTACTS_HANDLED" in item:
        sets.append("lastContactsHandled = :ch")
        values[":ch"] = item["CONTACTS_HANDLED"]
    if item["stalled"]:
        sets.append("lastStalledAt = :lu")
    status_table.update_item(
        Key={"pk": qkey, "monitorDate": monitor_date},
        UpdateExpression="SET " + ", ".join(sets) + " ADD checkCount :one, stalledCount :st",
        ExpressionAttributeNames={"#rg": "region", "#ttl": "ttl"},
        ExpressionAttributeValues=values,
    )
    return item


def poll_instance(targets, now):
    """同一实例的所有 target 一起轮询。返回 (ok_count, stalled_count, errors)。"""
    first = targets[0]
    client = _connect(first["region"])
    queue_ids = [t["queueId"] for t in targets]
    metrics, handled, errors = {}, {}, []
    snapshot = None
    for i in range(0, len(queue_ids), QUEUE_BATCH):
        chunk = queue_ids[i : i + QUEUE_BATCH]
        try:
            m, snapshot = fetch_current_metrics(client, first["instanceId"], chunk)
            metrics.update(m)
        except Exception as e:  # noqa: BLE001 — 单个实例失败不影响其它实例
            LOG.exception("GetCurrentMetricData failed: %s", first["instanceAlias"])
            errors.append(f"GetCurrentMetricData: {e}")
        try:
            handled.update(
                fetch_contacts_handled(client, first["instanceArn"], chunk, first["timezone"], now)
            )
        except Exception as e:  # noqa: BLE001
            LOG.exception("GetMetricDataV2 failed: %s", first["instanceAlias"])
            errors.append(f"GetMetricDataV2: {e}")

    ok = stalled = 0
    err_text = "; ".join(errors) if errors else None
    for t in targets:
        try:
            item = write_record(
                t, now, metrics.get(t["queueId"]), handled.get(t["queueId"]), snapshot, err_text
            )
            if item is None:  # 同一分钟的重复投递
                continue
            ok += 1
            stalled += 1 if item.get("stalled") else 0
        except Exception as e:  # noqa: BLE001
            LOG.exception("write record failed: %s/%s", t["instanceAlias"], t["queueName"])
            errors.append(f"write {t['queueName']}: {e}")
    return ok, stalled, errors


def _event_minute(event):
    """EventBridge 定时事件的 time 字段是计划触发时间; 手动调用可传 {"time": "...Z"}。"""
    raw = (event or {}).get("time")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt.astimezone(UTC).replace(second=0, microsecond=0)
        except ValueError:
            pass
    return datetime.now(UTC).replace(second=0, microsecond=0)


def handler(event, context):
    now = _event_minute(event)
    schedule = get_schedule(now)
    if not schedule:
        return {"status": "idle", "reason": "没有需要监控的队列(字典为空)"}
    keys = list(schedule.keys())
    hhmm = now.strftime("%H:%M")
    if hhmm < keys[0]:
        return {"status": "idle", "reason": f"未到当天最早轮询时间 {keys[0]} UTC"}
    if hhmm > keys[-1]:
        return {"status": "idle", "reason": f"已过当天最晚轮询时间 {keys[-1]} UTC"}
    targets = schedule.get(hhmm) or []
    if not targets:
        return {"status": "idle", "reason": f"{hhmm} UTC 没有需要轮询的队列"}

    groups = {}
    for t in targets:
        groups.setdefault((t["region"], t["instanceId"]), []).append(t)

    total_ok = total_stalled = 0
    all_errors = []
    with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
        for ok, stalled, errors in pool.map(lambda g: poll_instance(g, now), groups.values()):
            total_ok += ok
            total_stalled += stalled
            all_errors.extend(errors)

    result = {
        "status": "polled",
        "minute": _iso(now),
        "instances": len(groups),
        "queues": len(targets),
        "written": total_ok,
        "stalled": total_stalled,
        "errors": all_errors[:20],
    }
    LOG.info(json.dumps(result, ensure_ascii=False))
    return result

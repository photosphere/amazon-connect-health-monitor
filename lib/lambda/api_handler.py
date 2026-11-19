#!/usr/bin/env python3
"""
api_handler.py — API Gateway(REST, {proxy+}, Cognito Authorizer) 后端 Lambda

路由(全部需要 Cognito IdToken, 放在 Authorization 头; 标 [admin] 的只允许 admin 组用户):
  GET  /me                         当前用户的邮箱与角色(admin / viewer)
  GET  /config                     读取持久化的监控配置 JSON
  PUT  /config                     [admin] 保存配置 {config, baseVersion}; baseVersion 与当前 updatedAt 不一致返回 409
  GET  /regions                    左侧目录显示的区域(网页上调整过的 regionList, 否则为部署配置的 regions)
  GET  /regions/available          提供 Connect Customer 服务的全部区域(SSM 公共参数), 以及本账号是否已开通
  POST /regions {region}           [admin] 添加区域(只能是提供 Connect Customer 且账号已开通的区域)
  DELETE /regions?region=          [admin] 移除区域(该区域还有监控中的实例时拒绝)
  GET  /instances?region=          列出该区域的 Connect Customer 实例(ListInstances)
  GET  /queues?region=&instanceId= 列出实例下的标准队列(ListQueues, STANDARD)
  GET  /records?region=&instanceId=&queueId=[&date=YYYY-MM-DD][&limit=&nextToken=]
                                   队列当天(实例时区)的所有记录, 按 lastUpdate 由新到旧
  GET  /alerts                     所有监控队列当天的 stalledCount(红色标记数字)
  GET  /scheduler                  轮询开关状态(EventBridge 规则)与当天轮询字典摘要
  POST /scheduler {action}         [admin] action = start | stop, 启用/停用轮询规则

环境变量(由 setup_monitor.py 注入):
  DATA_BUCKET / CONFIG_KEY / METRICS_TABLE / STATUS_TABLE / RULE_NAME / REGIONS(逗号分隔, 初始显示的区域)
  ACCOUNT_ID(S3 ExpectedBucketOwner) / ADMIN_GROUP(默认 admin)
"""
import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

import monitor_lib

UTC = timezone.utc
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "config/monitor-config.json")
METRICS_TABLE = os.environ.get("METRICS_TABLE", "")
STATUS_TABLE = os.environ.get("STATUS_TABLE", "")
RULE_NAME = os.environ.get("RULE_NAME", "")
REGIONS = [r.strip() for r in os.environ.get("REGIONS", "").split(",") if r.strip()]
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "admin")
_OWNER = {"ExpectedBucketOwner": ACCOUNT_ID} if ACCOUNT_ID else {}
MAX_RECORDS = 5000
MAX_BODY_BYTES = 1024 * 1024

_retry = Config(retries={"max_attempts": 5, "mode": "adaptive"})
_s3 = boto3.client("s3", config=_retry)
_ddb = boto3.resource("dynamodb", config=_retry)
_events = boto3.client("events", config=_retry)
_ssm = boto3.client("ssm", config=_retry)
_connect_clients = {}
_available_cache = {"at": 0, "items": None}
AVAILABLE_TTL = 6 * 3600
SSM_CONNECT_REGIONS = "/aws/service/global-infrastructure/services/connect/regions"
SSM_REGION_NAME = "/aws/service/global-infrastructure/regions/{}/longName"


def _connect(region):
    if region not in _connect_clients:
        _connect_clients[region] = boto3.client("connect", region_name=region, config=_retry)
    return _connect_clients[region]


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------


def _headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization,Content-Type",
        "Access-Control-Allow-Methods": "GET,PUT,POST,DELETE,OPTIONS",
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    }


def _default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"not serializable: {type(obj).__name__}")


def _ok(body, code=200):
    return {
        "statusCode": code,
        "headers": _headers(),
        "body": json.dumps(body, ensure_ascii=False, default=_default),
    }


def _err(code, msg):
    return _ok({"error": msg}, code)


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _groups(claims):
    """cognito:groups 在 REST API 的 claims 里是字符串, 例如 "admin" 或 "[admin viewer]"。"""
    raw = claims.get("cognito:groups") or ""
    if isinstance(raw, list):
        return {str(g) for g in raw}
    return {g for g in re.split(r"[\[\],\s]+", str(raw)) if g}


def _role(claims):
    return "admin" if ADMIN_GROUP in _groups(claims) else "viewer"


def _require_admin(claims):
    if _role(claims) != "admin":
        raise ApiError(403, "只读用户无权执行此操作，请联系管理员")


def _require(params, *names):
    vals = []
    for n in names:
        v = (params.get(n) or "").strip()
        if not v:
            raise ApiError(400, f"缺少参数 {n}")
        vals.append(v)
    return vals


def _check_region(region, cfg=None):
    """只允许访问左侧目录里已添加的区域。"""
    allowed = monitor_lib.effective_regions(cfg if cfg is not None else _read_config(), REGIONS)
    if region not in allowed:
        raise ApiError(400, f"区域未添加到监控目录: {region}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _read_config():
    try:
        obj = _s3.get_object(Bucket=DATA_BUCKET, Key=CONFIG_KEY, **_OWNER)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return monitor_lib.empty_config()
        raise
    return monitor_lib.normalize_config(json.loads(obj["Body"].read()))


def _write_config(cfg, claims):
    cfg["updatedAt"] = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    cfg["updatedBy"] = claims.get("email") or claims.get("cognito:username") or ""
    _s3.put_object(
        Bucket=DATA_BUCKET,
        Key=CONFIG_KEY,
        Body=json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        **_OWNER,
    )
    return {"config": cfg, "version": cfg["updatedAt"],
            "regions": monitor_lib.effective_regions(cfg, REGIONS)}


def get_config(_params, _body, _claims):
    cfg = _read_config()
    return {"config": cfg, "version": cfg.get("updatedAt")}


def get_me(_params, _body, claims):
    return {"email": claims.get("email") or "", "role": _role(claims)}


def put_config(_params, body, claims):
    _require_admin(claims)
    if not isinstance(body, dict) or "config" not in body:
        raise ApiError(400, "请求体应为 {config, baseVersion}")
    current = _read_config()
    if current.get("updatedAt") != body.get("baseVersion"):
        raise ApiError(
            409,
            f"配置已被 {current.get('updatedBy') or '其他用户'} 于 {current.get('updatedAt')} 修改，请刷新后重试",
        )
    raw = dict(body["config"]) if isinstance(body["config"], dict) else body["config"]
    if isinstance(raw, dict):
        # 区域列表只能通过 POST/DELETE /regions 修改, 保存实例配置时沿用服务器上的值
        raw["regionList"] = current.get("regionList")
    try:
        new_cfg = monitor_lib.normalize_config(raw)
    except monitor_lib.ConfigError as e:
        raise ApiError(400, str(e))
    allowed = monitor_lib.effective_regions(current, REGIONS)
    for region in new_cfg["regions"]:
        if region not in allowed:
            raise ApiError(400, f"区域未添加到监控目录: {region}")
    return _write_config(new_cfg, claims)


# ---------------------------------------------------------------------------
# Connect 资源
# ---------------------------------------------------------------------------


def get_regions(_params, _body, _claims):
    return {"regions": monitor_lib.effective_regions(_read_config(), REGIONS), "defaults": REGIONS}


def _connect_regions():
    """提供 Connect Customer 服务的区域及名称(AWS 公开的 SSM 参数), 缓存 6 小时。"""
    now = time.time()
    if _available_cache["items"] is not None and now - _available_cache["at"] < AVAILABLE_TTL:
        return _available_cache["items"]
    codes, token = [], None
    while True:
        kw = {"Path": SSM_CONNECT_REGIONS}
        if token:
            kw["NextToken"] = token
        resp = _ssm.get_parameters_by_path(**kw)
        codes.extend(p["Value"] for p in resp.get("Parameters", []))
        token = resp.get("NextToken")
        if not token:
            break
    # 只保留与部署区域同一分区的区域(例如商业区不能管理 GovCloud 的 us-gov-*)
    partition = boto3.session.Session().get_partition_for_region(_ssm.meta.region_name)
    sess = boto3.session.Session()
    codes = sorted({c for c in codes if sess.get_partition_for_region(c) == partition})
    names = {}
    for i in range(0, len(codes), 10):
        resp = _ssm.get_parameters(Names=[SSM_REGION_NAME.format(c) for c in codes[i : i + 10]])
        for p in resp.get("Parameters", []):
            names[p["Name"].split("/")[-2]] = p["Value"]
    items = [{"region": c, "name": names.get(c, c)} for c in codes]
    _available_cache.update({"at": now, "items": items})
    return items


def _enabled_regions():
    """本账号已开通的区域; 无权限或调用失败时返回 None(不做开通检查)。"""
    try:
        client = boto3.client("account", config=_retry)
        out, token = set(), None
        while True:
            kw = {"RegionOptStatusContains": ["ENABLED", "ENABLED_BY_DEFAULT"], "MaxResults": 50}
            if token:
                kw["NextToken"] = token
            resp = client.list_regions(**kw)
            out.update(r["RegionName"] for r in resp.get("Regions", []))
            token = resp.get("NextToken")
            if not token:
                return out
    except Exception as e:  # noqa: BLE001
        print(f"account:ListRegions 失败, 跳过开通检查: {e!r}")
        return None


def get_available_regions(_params, _body, _claims):
    try:
        items = _connect_regions()
    except ClientError as e:
        raise ApiError(502, f"读取 Connect Customer 区域列表失败: {e.response['Error'].get('Message')}")
    enabled = _enabled_regions()
    current = monitor_lib.effective_regions(_read_config(), REGIONS)
    return {
        "regions": [
            {**i, "enabledInAccount": None if enabled is None else i["region"] in enabled,
             "added": i["region"] in current}
            for i in items
        ],
        "accountCheck": enabled is not None,
    }


def add_region(_params, body, claims):
    _require_admin(claims)
    region = ((body or {}).get("region") or "").strip()
    try:
        items = _connect_regions()
    except ClientError as e:
        raise ApiError(502, f"读取 Connect Customer 区域列表失败: {e.response['Error'].get('Message')}")
    if region not in {i["region"] for i in items}:
        raise ApiError(400, f"{region or '(空)'} 不是提供 Connect Customer 服务的区域")
    enabled = _enabled_regions()
    if enabled is not None and region not in enabled:
        raise ApiError(400, f"本账号尚未开通区域 {region}，请先在账号设置中启用该区域")
    cfg = _read_config()
    regions = monitor_lib.effective_regions(cfg, REGIONS)
    if region in regions:
        raise ApiError(409, f"区域 {region} 已在监控目录中")
    cfg["regionList"] = regions + [region]
    return _write_config(cfg, claims)


def remove_region(params, _body, claims):
    _require_admin(claims)
    (region,) = _require(params, "region")
    cfg = _read_config()
    regions = monitor_lib.effective_regions(cfg, REGIONS)
    if region not in regions:
        raise ApiError(404, f"区域 {region} 不在监控目录中")
    busy = [i.get("instanceAlias") or iid
            for iid, i in ((cfg.get("regions") or {}).get(region) or {}).get("instances", {}).items()
            if i.get("monitored")]
    if busy:
        raise ApiError(409, f"区域 {region} 还有监控中的实例({', '.join(busy)})，请先取消监控并保存")
    cfg["regionList"] = [r for r in regions if r != region]
    (cfg.get("regions") or {}).pop(region, None)  # 未监控实例的配置一起删除
    return _write_config(cfg, claims)


def list_instances(params, _body, _claims):
    (region,) = _require(params, "region")
    _check_region(region)
    items = []
    try:
        for page in _connect(region).get_paginator("list_instances").paginate():
            for i in page.get("InstanceSummaryList", []):
                items.append(
                    {
                        "instanceId": i["Id"],
                        "instanceArn": i["Arn"],
                        "instanceAlias": i.get("InstanceAlias") or i["Id"],
                        "status": i.get("InstanceStatus"),
                    }
                )
    except ClientError as e:
        raise ApiError(502, f"ListInstances 失败({region}): {e.response['Error'].get('Message')}")
    items.sort(key=lambda x: x["instanceAlias"].lower())
    return {"region": region, "instances": items}


def list_queues(params, _body, _claims):
    region, instance_id = _require(params, "region", "instanceId")
    _check_region(region)
    items = []
    try:
        pages = _connect(region).get_paginator("list_queues").paginate(
            InstanceId=instance_id, QueueTypes=["STANDARD"]
        )
        for page in pages:
            for q in page.get("QueueSummaryList", []):
                items.append({"queueId": q["Id"], "queueArn": q["Arn"], "queueName": q.get("Name") or q["Id"]})
    except ClientError as e:
        raise ApiError(502, f"ListQueues 失败: {e.response['Error'].get('Message')}")
    items.sort(key=lambda x: x["queueName"].lower())
    return {"region": region, "instanceId": instance_id, "queues": items}


# ---------------------------------------------------------------------------
# 记录 / 告警
# ---------------------------------------------------------------------------


def _encode_token(key):
    return base64.urlsafe_b64encode(json.dumps(key, default=_default).encode()).decode()


def _decode_token(token):
    try:
        return json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except Exception:  # noqa: BLE001
        raise ApiError(400, "nextToken 无效")


def get_records(params, _body, _claims):
    region, instance_id, queue_id = _require(params, "region", "instanceId", "queueId")
    cfg = _read_config()
    inst = monitor_lib.find_instance(cfg, region, instance_id) or {}
    tz = inst.get("timezone") or "UTC"
    day = (params.get("date") or "").strip() or monitor_lib.local_today(tz)
    pk = f"{instance_id}#{queue_id}#{day}"
    table = _ddb.Table(METRICS_TABLE)
    limit = int(params.get("limit") or 0)
    kw = {"KeyConditionExpression": Key("pk").eq(pk), "ScanIndexForward": False}
    if params.get("nextToken"):
        kw["ExclusiveStartKey"] = _decode_token(params["nextToken"])
    items, next_key = [], None
    while True:
        if limit:
            kw["Limit"] = limit - len(items)
        resp = table.query(**kw)
        items.extend(resp.get("Items", []))
        next_key = resp.get("LastEvaluatedKey")
        if not next_key or (limit and len(items) >= limit) or len(items) >= MAX_RECORDS:
            break
        kw["ExclusiveStartKey"] = next_key
    stalled = sum(1 for i in items if i.get("stalled"))
    return {
        "date": day,
        "timezone": tz,
        "items": items,
        "count": len(items),
        "stalledCount": stalled,
        "nextToken": _encode_token(next_key) if next_key else None,
    }


def get_alerts(_params, _body, _claims):
    cfg = _read_config()
    keys, meta = [], {}
    for region, inst, queues in monitor_lib.iter_monitored(cfg):
        day = monitor_lib.local_today(inst.get("timezone") or "UTC")
        for q in queues:
            qkey = f"{inst['instanceId']}#{q['queueId']}"
            keys.append({"pk": qkey, "monitorDate": day})
            meta[qkey] = {"region": region, "date": day}
    result = {k: {**v, "stalledCount": 0, "checkCount": 0} for k, v in meta.items()}
    for i in range(0, len(keys), 100):
        req = {STATUS_TABLE: {"Keys": keys[i : i + 100]}}
        for _ in range(5):
            resp = _ddb.batch_get_item(RequestItems=req)
            for it in resp.get("Responses", {}).get(STATUS_TABLE, []):
                r = result.get(it["pk"])
                if r is None:
                    continue
                r.update(
                    {
                        "stalledCount": it.get("stalledCount", 0),
                        "checkCount": it.get("checkCount", 0),
                        "lastUpdate": it.get("lastUpdate"),
                        "lastContactsHandled": it.get("lastContactsHandled"),
                        "lastStalledAt": it.get("lastStalledAt"),
                    }
                )
            req = resp.get("UnprocessedKeys") or {}
            if not req:
                break
    return {"alerts": result, "generatedAt": datetime.now(UTC).isoformat()}


# ---------------------------------------------------------------------------
# 轮询开关
# ---------------------------------------------------------------------------


def _scheduler_state():
    try:
        rule = _events.describe_rule(Name=RULE_NAME)
        state = rule.get("State")
    except ClientError as e:
        raise ApiError(502, f"读取轮询规则失败: {e.response['Error'].get('Message')}")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    sched = monitor_lib.build_minute_schedule(_read_config(), now.date())
    summary = monitor_lib.schedule_summary(sched)
    hhmm = now.strftime("%H:%M")
    summary["nextPoll"] = next((k for k in sched if k >= hhmm), None)
    summary["utcDate"] = now.date().isoformat()
    return {"ruleName": RULE_NAME, "state": state, "enabled": state == "ENABLED", "today": summary}


def get_scheduler(_params, _body, _claims):
    return _scheduler_state()


def post_scheduler(_params, body, claims):
    _require_admin(claims)
    action = (body or {}).get("action")
    if action == "start":
        _events.enable_rule(Name=RULE_NAME)
    elif action == "stop":
        _events.disable_rule(Name=RULE_NAME)
    else:
        raise ApiError(400, "action 只能是 start 或 stop")
    return _scheduler_state()


ROUTES = {
    ("GET", "/me"): get_me,
    ("GET", "/config"): get_config,
    ("PUT", "/config"): put_config,
    ("GET", "/regions"): get_regions,
    ("GET", "/regions/available"): get_available_regions,
    ("POST", "/regions"): add_region,
    ("DELETE", "/regions"): remove_region,
    ("GET", "/instances"): list_instances,
    ("GET", "/queues"): list_queues,
    ("GET", "/records"): get_records,
    ("GET", "/alerts"): get_alerts,
    ("GET", "/scheduler"): get_scheduler,
    ("POST", "/scheduler"): post_scheduler,
}


def handler(event, _context):
    method = (event.get("httpMethod") or "GET").upper()
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _headers(), "body": ""}
    path = "/" + (event.get("path") or "/").strip("/")
    func = ROUTES.get((method, path))
    if not func:
        return _err(404, f"未知路由: {method} {path}")
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    params = event.get("queryStringParameters") or {}
    body = None
    raw = event.get("body")
    if raw:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            return _err(413, "请求体过大")
        try:
            body = json.loads(raw)
        except ValueError:
            return _err(400, "请求体不是合法 JSON")
    try:
        return _ok(func(params, body, claims))
    except ApiError as e:
        return _err(e.code, e.msg)
    except monitor_lib.ConfigError as e:
        return _err(500, f"已保存的配置不合法: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"unhandled error: {e!r}")
        return _err(500, "服务器内部错误，请查看 Lambda 日志")

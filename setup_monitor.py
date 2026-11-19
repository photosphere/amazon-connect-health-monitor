#!/usr/bin/env python3
"""
setup_monitor.py — Connect Customer 队列监控网站一键部署(可重复执行)

参考 amazon-connect-ai-agent-cloudwatch-setup-m/log/cloudfront/setup_cloudfront.py 的部署方式:
静态站点放 S3, 经 CloudFront(OAC) 发布; Cognito User Pool 登录; API Gateway(REST,
Cognito Authorizer) → Lambda; 资源名以账号 ID 为后缀, 按名称复用, 重复执行即更新。

创建/更新的资源(默认名称 = <prefix>-<account_id>-<用途>, prefix 默认 connect-monitor;
可在配置文件里用 prefix 改前缀、用 resource_names 单独改任意资源名):
  S3   <prefix>-<acct>-data          监控配置 JSON: config/monitor-config.json(开启版本控制)
  S3   <prefix>-<acct>-web           前端静态文件(仅允许 CloudFront OAC 读取)
  DDB  <prefix>-<acct>-metrics       每次轮询的记录 pk=<instanceId>#<queueId>#<本地日期>, sk=lastUpdate
  DDB  <prefix>-<acct>-status        每队列每天的 stalledCount   pk=<instanceId>#<queueId>, sk=monitorDate
  IAM  <prefix>-<acct>-lambda-role
  λ    <prefix>-<acct>-poller        每分钟轮询 Connect 指标
  λ    <prefix>-<acct>-api           网站后端
  EventBridge 规则 <prefix>-<acct>-every-minute(首次创建为停用, 网页上点「开始」启用)
  Cognito 用户池 <prefix>-<acct> + 应用客户端 <prefix>-<acct>-web + 登录用户
  API Gateway <prefix>-<acct>-api (stage prod)
  CloudFront 分配(Comment = <prefix>-<acct>) + OAC <prefix>-<acct>-oac
             + 安全响应头策略 <prefix>-<acct>-security-headers(CSP / HSTS / X-Frame-Options 等)

安全设置: Cognito 用户组 admin / viewer(只读), TOTP MFA(默认必须), 密码至少 12 位;
API Gateway stage 限流; S3 拒绝非 HTTPS 并校验桶所有者; tzdata 固定版本并校验 sha256。

用法:
  python3 setup_monitor.py [--config FILE] [--profile NAME] [--prefix PREFIX] [-i] [--dry-run] [-y] [--keep]
    -i, --interactive  交互式部署: 逐项询问(回车用默认值), 可逐个修改资源名, 确认后再部署, 并可保存为配置文件
    --dry-run          只校验配置并打印部署计划(资源名称), 不调用 AWS
    -y, --yes          跳过「确认部署」提问(用于脚本/CI)

依赖: python3、boto3(pip install boto3)。Lambda 打包时会用 pip 下载 tzdata(时区数据库)。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("错误: 需要 boto3。可以任选一种方式安装:\n"
          f"  直接安装到当前用户:  {sys.executable} -m pip install --user boto3\n"
          "  或使用虚拟环境:      python3 -m venv .venv && source .venv/bin/activate && pip install boto3\n"
          "如果提示 externally-managed-environment(Homebrew 或部分 Linux 系统的 Python), 请使用虚拟环境,\n"
          "或在 macOS 上改用自带的 /usr/bin/python3。", file=sys.stderr)
    sys.exit(1)

CF_CACHE_POLICY_ID = "658327ea-f89d-4fab-a63d-7e88639e58f6"  # 托管策略 CachingOptimized
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.join(SCRIPT_DIR, "lib", "lambda")
WEB_DIR = os.path.join(SCRIPT_DIR, "lib", "web")
LAMBDA_FILES = ["monitor_lib.py", "poller.py", "api_handler.py"]
WEB_FILES = ["index.html", "app.js", "auth.js"]
CONFIG_KEY = "config/monitor-config.json"
RUNTIME = "python3.12"
# Lambda 打包用的 tzdata: 固定版本并校验 sha256, 防止 PyPI 供应链篡改(升级时同时改这两行)
TZDATA_VERSION = "2026.6"
TZDATA_SHA256 = "0a10eec6e13ef16b627fabed9a5f2ba61a62b1c67f327305f4e7e412a70b4aec"
# Cognito 用户组: admin 可修改配置、开始/结束轮询; viewer(以及不在任何组的用户)只读
ADMIN_GROUP, VIEWER_GROUP = "admin", "viewer"
ROLES = (ADMIN_GROUP, VIEWER_GROUP)
MFA_MODES = {"required": "ON", "optional": "OPTIONAL", "off": "OFF"}

_PROFILE = None  # None = 不指定 profile, 使用 boto3 默认凭证链(环境变量 AK/SK、CloudShell、EC2/ECS 角色等)

# boto3 凭证来源(Credentials.method) → 说明
CREDENTIAL_SOURCES = {
    "env": "环境变量 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY",
    "shared-credentials-file": "~/.aws/credentials",
    "config-file": "~/.aws/config",
    "sso": "IAM Identity Center(SSO)",
    "assume-role": "AssumeRole(profile 中的 role_arn)",
    "assume-role-with-web-identity": "Web Identity",
    "container-role": "容器凭证(CloudShell / ECS)",
    "iam-role": "EC2 实例角色",
    "custom-process": "credential_process",
    "login": "aws login",
}


def resolve_profile(cli_profile, cfg_profile, acct):
    """决定使用哪个 profile。返回 (profile 或 None, 说明)。

    优先级: --profile > 配置文件 profile > 环境里已设置凭证(AK/SK 环境变量或 AWS_PROFILE)时用默认凭证链
            > 存在与账号 ID 同名的 profile 时用它(兼容参考项目的约定) > 默认凭证链。
    """
    if cli_profile:
        return cli_profile, "命令行 --profile"
    if cfg_profile:
        return cfg_profile, "配置文件 profile"
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return None, "当前环境凭证"
    try:
        if acct and acct in boto3.Session().available_profiles:
            return acct, "与账号 ID 同名的 profile"
    except Exception:  # noqa: BLE001
        pass
    return None, "当前环境凭证"


def credential_label():
    """当前会使用的凭证来源(本地解析, 不调用 AWS 接口)。"""
    try:
        kw = {"profile_name": _PROFILE} if _PROFILE else {}
        creds = boto3.Session(**kw).get_credentials()
    except Exception as e:  # noqa: BLE001
        return f"无法解析凭证: {e}"
    if creds is None:
        return "未找到凭证"
    label = CREDENTIAL_SOURCES.get(creds.method, creds.method)
    if _PROFILE:
        return f"profile {_PROFILE} ({label})"
    env_profile = os.environ.get("AWS_PROFILE")
    return f"{label}" + (f", AWS_PROFILE={env_profile}" if env_profile else "")


def _client(service, region=None):
    kw = {}
    if _PROFILE:
        kw["profile_name"] = _PROFILE
    if region:
        kw["region_name"] = region
    return boto3.Session(**kw).client(service)


def step(msg):
    print(f"==> {msg}", flush=True)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


DEFAULT_PREFIX = "connect-monitor"
DEFAULTS = {
    "profile": "",
    "deploy_region": "us-east-1",
    "retention_days": 30,
    "extra_users": [],
    "prefix": DEFAULT_PREFIX,
    "resource_names": {},
    "mfa": "required",
    "api_rate_limit": 20,
    "api_burst_limit": 40,
    "point_in_time_recovery": False,
}

# 资源名: (key, 说明, 默认模板, 校验规则)。模板与自定义名称都可以使用 {prefix} 和 {account_id} 占位符
RESOURCES = [
    ("data_bucket", "S3 数据桶(监控配置 JSON)", "{prefix}-{account_id}-data", "s3"),
    ("web_bucket", "S3 网站桶", "{prefix}-{account_id}-web", "s3"),
    ("metrics_table", "DynamoDB 指标记录表", "{prefix}-{account_id}-metrics", "dynamodb"),
    ("status_table", "DynamoDB 队列状态表", "{prefix}-{account_id}-status", "dynamodb"),
    ("lambda_role", "IAM Lambda 执行角色", "{prefix}-{account_id}-lambda-role", "iam"),
    ("poller_function", "轮询 Lambda", "{prefix}-{account_id}-poller", "lambda"),
    ("api_function", "API Lambda", "{prefix}-{account_id}-api", "lambda"),
    ("schedule_rule", "EventBridge 每分钟规则", "{prefix}-{account_id}-every-minute", "events"),
    ("user_pool", "Cognito 用户池", "{prefix}-{account_id}", "cognito"),
    ("user_pool_client", "Cognito 应用客户端", "{prefix}-{account_id}-web", "cognito"),
    ("rest_api", "API Gateway REST API", "{prefix}-{account_id}-api", "apigateway"),
    ("cloudfront_comment", "CloudFront 分配标识(Comment)", "{prefix}-{account_id}", "cloudfront"),
    ("cloudfront_oac", "CloudFront 源访问控制(OAC)", "{prefix}-{account_id}-oac", "oac"),
    ("cloudfront_headers_policy", "CloudFront 安全响应头策略", "{prefix}-{account_id}-security-headers", "cfpolicy"),
]
RESOURCE_KEYS = [r[0] for r in RESOURCES]

# 各服务的命名规则(按 AWS 官方限制, 略收紧以避免特殊字符)
NAME_RULES = {
    "s3": (re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"), "3~63 位小写字母、数字、点、连字符, 首尾为字母或数字"),
    "dynamodb": (re.compile(r"^[A-Za-z0-9_.-]{3,255}$"), "3~255 位字母、数字、_ . -"),
    "iam": (re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$"), "1~64 位字母、数字及 +=,.@_-"),
    "lambda": (re.compile(r"^[A-Za-z0-9_-]{1,64}$"), "1~64 位字母、数字、_ -"),
    "events": (re.compile(r"^[A-Za-z0-9._-]{1,64}$"), "1~64 位字母、数字、. _ -"),
    "cognito": (re.compile(r"^[A-Za-z0-9+=,.@_-]{1,128}$"), "1~128 位字母、数字及 +=,.@_-"),
    "apigateway": (re.compile(r"^[A-Za-z0-9._-]{1,128}$"), "1~128 位字母、数字、. _ -"),
    "cloudfront": (re.compile(r"^[A-Za-z0-9._-]{1,128}$"), "1~128 位字母、数字、. _ -"),
    "oac": (re.compile(r"^[A-Za-z0-9._-]{1,64}$"), "1~64 位字母、数字、. _ -"),
    "cfpolicy": (re.compile(r"^[A-Za-z0-9_-]{1,128}$"), "1~128 位字母、数字、_ -"),
}
PREFIX_RE = re.compile(r"^[a-z]([a-z0-9-]{0,28}[a-z0-9])?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


class SettingsError(ValueError):
    pass


def find_configs():
    return sorted(
        f for f in os.listdir(SCRIPT_DIR)
        if f.startswith("config-") and f.endswith(".json") and f != "config-example.json"
    )


def pick_config(arg, allow_none=False):
    """返回配置文件路径; 没有任何 config-*.json 且 allow_none 时返回 None。"""
    if arg:
        path = arg if os.path.isabs(arg) else os.path.join(SCRIPT_DIR, arg)
        if not os.path.isfile(path):
            if allow_none:
                return None
            sys.exit(f"错误: 找不到配置文件 {path}")
        return path
    cands = find_configs()
    if not cands:
        if allow_none:
            return None
        sys.exit("错误: 未找到 config-<account_id>.json。请参考 config-example.json 创建, "
                 "或使用 -i 进入交互式部署")
    if len(cands) == 1:
        step(f"使用配置文件: {cands[0]}")
        return os.path.join(SCRIPT_DIR, cands[0])
    print("请选择要部署的配置文件:")
    for i, n in enumerate(cands, 1):
        print(f"  {i}) {n}")
    while True:
        raw = input(f"请输入序号 [1-{len(cands)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(cands):
            return os.path.join(SCRIPT_DIR, cands[int(raw) - 1])


def read_raw_config(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        sys.exit(f"错误: 配置文件必须是 JSON 对象: {path}")
    return raw


# ---- 单字段校验(交互式与文件模式共用) ----
def check_email(v):
    v = (v or "").strip()
    if not EMAIL_RE.match(v):
        raise SettingsError(f"email 格式不正确: {v!r}")
    return v


def check_account(v):
    v = str(v or "").strip()
    if not re.match(r"^\d{12}$", v):
        raise SettingsError(f"account_id 必须是 12 位数字: {v!r}")
    return v


def check_region(v):
    v = (v or "").strip()
    if not REGION_RE.match(v):
        raise SettingsError(f"区域名不合法: {v!r}")
    return v


def check_regions(v):
    items = v.split(",") if isinstance(v, str) else (v or [])
    out = []
    for r in items:
        r = str(r).strip()
        if r and r not in out:
            out.append(check_region(r))
    if not out:
        raise SettingsError("regions 不能为空(要监控的 Connect Customer 实例所在区域)")
    return out


def check_retention(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise SettingsError(f"retention_days 必须是整数: {v!r}")
    if not 1 <= n <= 3650:
        raise SettingsError("retention_days 必须在 1~3650 之间")
    return n


def check_users(v):
    """邮箱列表(逗号分隔字符串或列表)。"""
    items = v.split(",") if isinstance(v, str) else (v or [])
    return [check_email(e) for e in (str(x).strip() for x in items) if e]


def check_extra_users(v):
    """extra_users: ["a@x.com", {"email": "b@x.com", "role": "admin"}]; 只写邮箱时为只读用户(viewer)。"""
    out = []
    for item in v or []:
        if isinstance(item, str):
            email, role = item, VIEWER_GROUP
        elif isinstance(item, dict):
            email, role = item.get("email"), (item.get("role") or VIEWER_GROUP)
        else:
            raise SettingsError(f"extra_users 的每一项应为邮箱字符串或 {{email, role}}: {item!r}")
        if role not in ROLES:
            raise SettingsError(f"extra_users 中 {email} 的 role 只能是 {' 或 '.join(ROLES)}: {role!r}")
        email = check_email(email)
        if any(u["email"].lower() == email.lower() for u in out):
            raise SettingsError(f"extra_users 中邮箱重复: {email}")
        out.append({"email": email, "role": role})
    return out


def check_mfa(v):
    v = (v or "").strip().lower()
    if v not in MFA_MODES:
        raise SettingsError(f"mfa 只能是 {' / '.join(MFA_MODES)}: {v!r}")
    return v


def check_int_range(name, lo, hi):
    def _check(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise SettingsError(f"{name} 必须是整数: {v!r}")
        if not lo <= n <= hi:
            raise SettingsError(f"{name} 必须在 {lo}~{hi} 之间")
        return n
    return _check


def check_bool(name):
    def _check(v):
        if isinstance(v, bool):
            return v
        raise SettingsError(f"{name} 必须是 true 或 false: {v!r}")
    return _check


def check_prefix(v):
    v = (v or "").strip()
    if not PREFIX_RE.match(v):
        raise SettingsError(f"prefix 不合法: {v!r}(小写字母开头, 1~30 位小写字母、数字、连字符, 不能以连字符结尾)")
    return v


def render_name(template, prefix, acct):
    try:
        return template.format(prefix=prefix, account_id=acct)
    except (KeyError, IndexError, ValueError):
        raise SettingsError(f"资源名只支持 {{prefix}} 和 {{account_id}} 两个占位符: {template!r}")


def check_resource_name(key, value):
    rule = dict((r[0], r[3]) for r in RESOURCES)[key]
    pattern, desc = NAME_RULES[rule]
    if not pattern.match(value or "") or (rule == "s3" and (".." in value or re.match(r"^\d+\.\d+\.\d+\.\d+$", value))):
        raise SettingsError(f"{key} 名称不合法: {value!r}({desc})")
    return value


def resolve_names(settings):
    """按 prefix 生成默认名称, 再用 resource_names 中的自定义值覆盖。返回 {key: name}。"""
    prefix, acct = settings["prefix"], settings["account_id"]
    overrides = settings.get("resource_names") or {}
    unknown = sorted(set(overrides) - set(RESOURCE_KEYS))
    if unknown:
        raise SettingsError(f"resource_names 中有未知的键: {', '.join(unknown)}(可用: {', '.join(RESOURCE_KEYS)})")
    names = {}
    for key, _label, template, _rule in RESOURCES:
        custom = (overrides.get(key) or "").strip()
        names[key] = check_resource_name(key, render_name(custom or template, prefix, acct))
    for a, b in (("data_bucket", "web_bucket"), ("metrics_table", "status_table"), ("poller_function", "api_function")):
        if names[a] == names[b]:
            raise SettingsError(f"{a} 与 {b} 不能同名: {names[a]}")
    return names


def normalize_settings(raw, cli_profile="", cli_prefix=""):
    """校验配置文件(或交互式输入)内容, 返回规范化后的 settings。"""
    s = {
        "email": check_email(raw.get("email")),
        "account_id": check_account(raw.get("account_id")),
        "profile": (cli_profile or raw.get("profile") or "").strip(),
        "deploy_region": check_region(raw.get("deploy_region") or DEFAULTS["deploy_region"]),
        "regions": check_regions(raw.get("regions")),
        "retention_days": check_retention(raw.get("retention_days") or DEFAULTS["retention_days"]),
        "extra_users": check_extra_users(raw.get("extra_users")),
        "prefix": check_prefix(cli_prefix or raw.get("prefix") or DEFAULT_PREFIX),
        "resource_names": {k: v for k, v in (raw.get("resource_names") or {}).items() if v},
        "mfa": check_mfa(raw.get("mfa") or DEFAULTS["mfa"]),
        "api_rate_limit": check_int_range("api_rate_limit", 1, 10000)(raw.get("api_rate_limit", DEFAULTS["api_rate_limit"])),
        "api_burst_limit": check_int_range("api_burst_limit", 1, 5000)(raw.get("api_burst_limit", DEFAULTS["api_burst_limit"])),
        "point_in_time_recovery": check_bool("point_in_time_recovery")(
            raw.get("point_in_time_recovery", DEFAULTS["point_in_time_recovery"])),
    }
    if any(u["email"].lower() == s["email"].lower() for u in s["extra_users"]):
        raise SettingsError(f"extra_users 中不需要再写管理员邮箱 email: {s['email']}")
    # 登录用户: email 固定为管理员, 其余按 extra_users 的 role
    s["users"] = [{"email": s["email"], "role": ADMIN_GROUP}] + s["extra_users"]
    s["names"] = resolve_names(s)
    return s


def settings_to_json(s):
    """写回配置文件的内容(不含派生字段)。"""
    return {
        "email": s["email"],
        "account_id": s["account_id"],
        "profile": s["profile"],
        "deploy_region": s["deploy_region"],
        "regions": s["regions"],
        "retention_days": s["retention_days"],
        "extra_users": s["extra_users"],
        "prefix": s["prefix"],
        "resource_names": s["resource_names"],
        "mfa": s["mfa"],
        "api_rate_limit": s["api_rate_limit"],
        "api_burst_limit": s["api_burst_limit"],
        "point_in_time_recovery": s["point_in_time_recovery"],
    }


def _display_width(text):
    """终端显示宽度: 中文等全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def print_plan(s, cred_desc):
    n = s["names"]
    print("===================================================================")
    print(" 部署计划")
    print("-------------------------------------------------------------------")
    print(f" 账号: {s['account_id']}   部署区域: {s['deploy_region']}")
    print(f" 凭证: {cred_desc}")
    print(f" 监控区域: {', '.join(s['regions'])}")
    role_label = {ADMIN_GROUP: "管理员", VIEWER_GROUP: "只读"}
    print(f" 登录用户: {', '.join(u['email'] + '(' + role_label[u['role']] + ')' for u in s['users'])}")
    print(f" MFA: {s['mfa']}   API 限流: {s['api_rate_limit']} 次/秒(突发 {s['api_burst_limit']})"
          f"   DynamoDB 时间点恢复: {'开启' if s['point_in_time_recovery'] else '关闭'}")
    print(f" 记录保留: {s['retention_days']} 天   资源前缀: {s['prefix']}")
    print("-------------------------------------------------------------------")
    width = max(_display_width(r[1]) for r in RESOURCES)
    for key, label, _t, _r in RESOURCES:
        mark = "  (自定义)" if key in s["resource_names"] else ""
        print(f"   {label}{' ' * (width - _display_width(label))}  {n[key]}{mark}")
    print("===================================================================")


# ---- 交互式 ----
def _ask(label, default="", check=None, allow_empty=False):
    shown = f" [{default}]" if default not in ("", None) else ""
    while True:
        try:
            raw = input(f"  {label}{shown}: ").strip()
        except EOFError:
            sys.exit("\n已取消")
        value = raw if raw else ("" if default is None else str(default))
        if not value and not allow_empty:
            print("    不能为空")
            continue
        if check is None:
            return value
        try:
            return check(value)
        except SettingsError as e:
            print(f"    {e}")


def _yes(label, default=False):
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"  {label} [{hint}]: ").strip().lower()
        except EOFError:
            sys.exit("\n已取消")
        if not raw:
            return default
        if raw in ("y", "yes", "是"):
            return True
        if raw in ("n", "no", "否"):
            return False


def detect_account(profile, region):
    """profile 为 None 时使用默认凭证链。"""
    global _PROFILE
    _PROFILE = profile or None
    try:
        return _client("sts", region).get_caller_identity()["Account"]
    except Exception as e:  # noqa: BLE001
        who = f"profile {profile!r}" if profile else "当前环境凭证"
        print(f"    无法使用{who}获取账号: {e}")
        return ""


def interactive_settings(raw, cli_profile, cli_prefix):
    """逐项询问部署参数, 回车使用方括号里的默认值。返回 (raw_dict, profile)。"""
    print("===================================================================")
    print(" 交互式部署: 直接回车使用方括号里的值")
    print("===================================================================")
    r = dict(DEFAULTS)
    r.update(raw)
    deploy_region = _ask("网站与 Lambda 部署区域 deploy_region", r.get("deploy_region"), check_region)
    profile_default, _src = resolve_profile(cli_profile, r.get("profile"), r.get("account_id"))
    print("  AWS profile: 留空(或输入 -)表示使用当前环境的凭证, 例如环境变量 AK/SK、CloudShell、EC2 角色")
    while True:
        profile = _ask("AWS profile", profile_default or "", allow_empty=True)
        profile = "" if profile == "-" else profile
        detected = detect_account(profile or None, deploy_region)
        if detected:
            print(f"    {credential_label()} 属于账号 {detected}")
            break
        if _yes("仍然使用这组凭证并手动输入账号 ID?", False):
            break
    acct = _ask("账号 ID account_id", detected or r.get("account_id"), check_account)
    if detected and acct != detected:
        print(f"    警告: 输入的账号与 profile 所属账号 {detected} 不一致, 部署前会再次校验并停止")
    email = _ask("管理员登录邮箱 email", r.get("email"), check_email)
    regions = _ask("监控的 Connect Customer 区域 regions(逗号分隔)",
                   ",".join(r.get("regions") or [deploy_region]), check_regions)
    retention = _ask("记录保留天数 retention_days", r.get("retention_days"), check_retention)
    try:
        old_extra = check_extra_users(r.get("extra_users"))
    except SettingsError:
        old_extra = []
    admins = _ask("其他管理员邮箱(可修改配置、开始/结束轮询; 逗号分隔, 可留空)",
                  ",".join(u["email"] for u in old_extra if u["role"] == ADMIN_GROUP), check_users, allow_empty=True)
    viewers = _ask("只读用户邮箱(只能查看; 逗号分隔, 可留空)",
                   ",".join(u["email"] for u in old_extra if u["role"] == VIEWER_GROUP), check_users, allow_empty=True)
    extra = [{"email": e, "role": ADMIN_GROUP} for e in admins] + [{"email": e, "role": VIEWER_GROUP} for e in viewers]
    mfa = _ask("登录 MFA(required 必须 / optional 可选 / off 关闭)", r.get("mfa") or DEFAULTS["mfa"], check_mfa)
    prefix = _ask("资源名称前缀 prefix", cli_prefix or r.get("prefix") or DEFAULT_PREFIX, check_prefix)

    overrides = dict(r.get("resource_names") or {})
    print()
    print("  资源名称默认为 <prefix>-<account_id>-<用途>:")
    for key, label, template, _rule in RESOURCES:
        print(f"    {label}: {overrides.get(key) and render_name(overrides[key], prefix, acct) or render_name(template, prefix, acct)}"
              + ("  (自定义)" if overrides.get(key) else ""))
    if _yes("是否逐个修改资源名称?", False):
        print("  回车保留当前值; 输入 - 恢复默认值; 可使用 {prefix} {account_id} 占位符")
        for key, label, template, _rule in RESOURCES:
            default_name = render_name(template, prefix, acct)
            current = render_name(overrides[key], prefix, acct) if overrides.get(key) else default_name

            def _check(v, key=key):
                return check_resource_name(key, render_name(v, prefix, acct))

            while True:
                try:
                    val = input(f"  {label} [{current}]: ").strip()
                except EOFError:
                    sys.exit("\n已取消")
                if val == "-":
                    overrides.pop(key, None)
                    break
                if not val:
                    break
                try:
                    name = _check(val)
                except SettingsError as e:
                    print(f"    {e}")
                    continue
                if name == default_name:
                    overrides.pop(key, None)
                else:
                    overrides[key] = val
                break
    new_raw = {
        "email": email, "account_id": acct, "profile": profile or "",
        "deploy_region": deploy_region, "regions": regions, "retention_days": retention,
        "extra_users": extra, "prefix": prefix, "resource_names": overrides, "mfa": mfa,
        # 以下高级选项交互式不询问, 沿用配置文件或默认值
        "api_rate_limit": r.get("api_rate_limit", DEFAULTS["api_rate_limit"]),
        "api_burst_limit": r.get("api_burst_limit", DEFAULTS["api_burst_limit"]),
        "point_in_time_recovery": r.get("point_in_time_recovery", DEFAULTS["point_in_time_recovery"]),
    }
    return new_raw, profile


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------


def build_lambda_zip(work):
    """lib/lambda/*.py + tzdata(zoneinfo 时区数据, Lambda 运行时不一定自带)。"""
    pkg = os.path.join(work, "pkg")
    os.makedirs(pkg, exist_ok=True)
    step(f"下载 tzdata=={TZDATA_VERSION}(校验 sha256)并打包 Lambda 代码 ...")
    req = os.path.join(work, "requirements-lambda.txt")
    with open(req, "w", encoding="utf-8") as f:
        f.write(f"tzdata=={TZDATA_VERSION} --hash=sha256:{TZDATA_SHA256}\n")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             "--no-deps", "--only-binary", ":all:", "--require-hashes", "--target", pkg, "-r", req],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("错误: pip 安装 tzdata 失败(需要能访问 PyPI; 若提示 hash 不匹配, 说明下载内容被篡改或版本号/哈希不一致)")
    zpath = os.path.join(work, "lambda.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in LAMBDA_FILES:
            zf.write(os.path.join(LAMBDA_DIR, name), name)
        for root, _dirs, files in os.walk(pkg):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, pkg)
                if "__pycache__" in rel or rel.endswith(".pyc"):
                    continue
                zf.write(full, rel)
    with open(zpath, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 资源
# ---------------------------------------------------------------------------


def bucket_exists(s3, name, acct):
    """桶存在且属于本账号 → True; 不存在 → False; 属于其他账号或无权访问 → 退出。"""
    try:
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=acct)
        return True
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code"))
        if code in ("404", "NoSuchBucket", "NotFound"):
            return False
        sys.exit(f"错误: S3 桶 {name} 已被其他账号占用或无权访问({code})。"
                 "请在 resource_names 中换一个桶名, 或用 --prefix 换前缀")


def tls_only_statement(bucket):
    """拒绝非 HTTPS 的访问。"""
    return {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}}}


def ensure_bucket(s3, name, region, acct):
    if bucket_exists(s3, name, acct):
        step(f"S3 桶已存在, 复用: {name}")
    else:
        step(f"创建 S3 桶: {name}")
        if region == "us-east-1":
            s3.create_bucket(Bucket=name)
        else:
            s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": region})
    s3.put_public_access_block(
        Bucket=name, ExpectedBucketOwner=acct,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )


def ensure_table(ddb, name, hash_key, range_key, pitr=False):
    try:
        ddb.describe_table(TableName=name)
        step(f"DynamoDB 表已存在, 复用: {name}")
    except ClientError:
        step(f"创建 DynamoDB 表: {name}")
        ddb.create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": hash_key, "KeyType": "HASH"},
                       {"AttributeName": range_key, "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": hash_key, "AttributeType": "S"},
                                  {"AttributeName": range_key, "AttributeType": "S"}],
        )
        ddb.get_waiter("table_exists").wait(TableName=name)
    ttl = ddb.describe_time_to_live(TableName=name).get("TimeToLiveDescription", {})
    if ttl.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING"):
        ddb.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
    status = ddb.describe_continuous_backups(TableName=name)["ContinuousBackupsDescription"]
    enabled = status.get("PointInTimeRecoveryDescription", {}).get("PointInTimeRecoveryStatus") == "ENABLED"
    if pitr and not enabled:
        for attempt in range(10):  # 新表创建后需要等一会儿才能开启
            try:
                ddb.update_continuous_backups(TableName=name, PointInTimeRecoverySpecification={
                    "PointInTimeRecoveryEnabled": True})
                print(f"    已开启时间点恢复(PITR): {name}")
                break
            except ClientError as e:
                if e.response["Error"]["Code"] != "ContinuousBackupsUnavailableException" or attempt == 9:
                    raise
                time.sleep(6)
    elif not pitr and enabled:
        # 不自动关闭, 避免丢失已有的恢复点
        print(f"    提示: {name} 已开启 PITR, 配置为 false 时脚本不会自动关闭, 如需关闭请在控制台操作")


def check_connect_regions(region, wanted):
    """确认 regions 中的每个区域都提供 Connect Customer 服务(AWS 公布的 SSM 公共参数)。"""
    try:
        ssm = _client("ssm", region)
        codes, token = set(), None
        while True:
            kw = {"Path": "/aws/service/global-infrastructure/services/connect/regions"}
            if token:
                kw["NextToken"] = token
            resp = ssm.get_parameters_by_path(**kw)
            codes.update(p["Value"] for p in resp.get("Parameters", []))
            token = resp.get("NextToken")
            if not token:
                break
    except Exception as e:  # noqa: BLE001
        print(f"    警告: 无法读取 Connect Customer 区域列表, 跳过区域检查: {e}", file=sys.stderr)
        return
    bad = [r for r in wanted if r not in codes]
    if bad:
        sys.exit(f"错误: 以下区域不提供 Connect Customer 服务: {', '.join(bad)}\n"
                 f"      可用区域: {', '.join(sorted(codes))}")
    step(f"区域检查通过: {', '.join(wanted)} 均提供 Connect Customer 服务")


def lambda_policy(region, acct, data_bucket, metrics_table, status_table, rule_name, fn_names):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": [f"arn:aws:logs:{region}:{acct}:log-group:/aws/lambda/{n}:*" for n in fn_names]
             + [f"arn:aws:logs:{region}:{acct}:log-group:/aws/lambda/{n}" for n in fn_names]},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
             "Resource": f"arn:aws:s3:::{data_bucket}/config/*"},
            {"Effect": "Allow",
             "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
                        "dynamodb:BatchGetItem"],
             "Resource": [f"arn:aws:dynamodb:{region}:{acct}:table/{metrics_table}",
                          f"arn:aws:dynamodb:{region}:{acct}:table/{status_table}"]},
            {"Effect": "Allow", "Action": ["events:DescribeRule", "events:EnableRule", "events:DisableRule"],
             "Resource": f"arn:aws:events:{region}:{acct}:rule/{rule_name}"},
            # ListInstances 不支持资源级权限; 其余只读指标/列表接口限定在本账号的实例
            {"Effect": "Allow", "Action": ["connect:ListInstances", "ds:DescribeDirectories"], "Resource": "*"},
            # 网页「添加区域」: 读取 AWS 公布的 Connect Customer 区域列表(公共参数)与本账号已开通的区域
            {"Effect": "Allow", "Action": ["ssm:GetParametersByPath", "ssm:GetParameters"],
             "Resource": "arn:aws:ssm:*::parameter/aws/service/global-infrastructure/*"},
            {"Effect": "Allow", "Action": "account:ListRegions", "Resource": "*"},
            {"Effect": "Allow",
             "Action": ["connect:ListQueues", "connect:DescribeInstance", "connect:GetCurrentMetricData",
                        "connect:GetMetricDataV2"],
             "Resource": [f"arn:aws:connect:*:{acct}:instance/*",
                          f"arn:aws:connect:*:{acct}:instance/*/queue/*"]},
        ],
    }


def ensure_role(iam, role_name, policy):
    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                       "Action": "sts:AssumeRole"}],
    })
    try:
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        step(f"复用 Lambda 执行角色: {role_name}")
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=trust)
        created = False
    except ClientError:
        step(f"创建 Lambda 执行角色: {role_name}")
        arn = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=trust)["Role"]["Arn"]
        created = True
    iam.put_role_policy(RoleName=role_name, PolicyName="connect-monitor", PolicyDocument=json.dumps(policy))
    if created:
        print("    等待 IAM 角色生效 ...")
        time.sleep(10)
    return arn


def _wait_lambda(lam, name):
    for _ in range(60):
        cfg = lam.get_function_configuration(FunctionName=name)
        if cfg.get("State") == "Active" and cfg.get("LastUpdateStatus") in (None, "Successful"):
            return cfg
        if cfg.get("LastUpdateStatus") == "Failed" or cfg.get("State") == "Failed":
            sys.exit(f"错误: Lambda {name} 更新失败: {cfg.get('LastUpdateStatusReason') or cfg.get('StateReason')}")
        time.sleep(3)
    return lam.get_function_configuration(FunctionName=name)


def ensure_lambda(lam, name, handler, role_arn, zip_bytes, env, timeout, memory):
    params = dict(Runtime=RUNTIME, Role=role_arn, Handler=handler, Timeout=timeout, MemorySize=memory,
                  Environment={"Variables": env})
    try:
        lam.get_function(FunctionName=name)
        step(f"更新 Lambda: {name}")
        lam.update_function_code(FunctionName=name, ZipFile=zip_bytes)
        _wait_lambda(lam, name)
        lam.update_function_configuration(FunctionName=name, **params)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        step(f"创建 Lambda: {name}")
        for attempt in range(1, 7):
            try:
                lam.create_function(FunctionName=name, Code={"ZipFile": zip_bytes}, **params)
                break
            except ClientError:
                if attempt == 6:
                    raise
                print(f"    等待执行角色生效, 重试 ({attempt}/6) ...")
                time.sleep(5)
    return _wait_lambda(lam, name)["FunctionArn"]


def ensure_log_group(logs, fn_name, days=30):
    name = f"/aws/lambda/{fn_name}"
    try:
        logs.create_log_group(logGroupName=name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=name, retentionInDays=days)


def ensure_permission(lam, fn, sid, principal, source_arn):
    try:
        lam.remove_permission(FunctionName=fn, StatementId=sid)
    except ClientError:
        pass
    lam.add_permission(FunctionName=fn, StatementId=sid, Action="lambda:InvokeFunction",
                       Principal=principal, SourceArn=source_arn)


def ensure_rule(events, lam, rule_name, poller_name, poller_arn):
    try:
        state = events.describe_rule(Name=rule_name)["State"]
        step(f"复用 EventBridge 规则: {rule_name} (保持当前状态 {state})")
    except ClientError:
        state = "DISABLED"
        step(f"创建 EventBridge 规则: {rule_name} (初始停用, 在网页上点「开始」启用)")
    rule_arn = events.put_rule(
        Name=rule_name, ScheduleExpression="rate(1 minute)", State=state,
        Description="Connect Customer 队列监控: 每分钟触发轮询 Lambda",
    )["RuleArn"]
    events.put_targets(Rule=rule_name, Targets=[{"Id": "poller", "Arn": poller_arn}])
    ensure_permission(lam, poller_name, "events-invoke", "events.amazonaws.com", rule_arn)
    return state


def ensure_user_pool(cog, pool_name, client_name, mfa):
    pools, token = [], None
    while True:
        kw = {"MaxResults": 60}
        if token:
            kw["NextToken"] = token
        resp = cog.list_user_pools(**kw)
        pools.extend(resp.get("UserPools", []))
        token = resp.get("NextToken")
        if not token:
            break
    hit = next((p for p in pools if p["Name"] == pool_name), None)
    if hit:
        step(f"复用 Cognito 用户池: {hit['Id']}")
        pool_id = hit["Id"]
    else:
        step(f"创建 Cognito 用户池: {pool_name}")
        pool_id = cog.create_user_pool(
            PoolName=pool_name,
            UsernameAttributes=["email"],
            AutoVerifiedAttributes=["email"],
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            Policies={"PasswordPolicy": {"MinimumLength": 12, "RequireUppercase": True, "RequireLowercase": True,
                                         "RequireNumbers": True, "RequireSymbols": False,
                                         "TemporaryPasswordValidityDays": 7}},
            AccountRecoverySetting={"RecoveryMechanisms": [{"Name": "verified_email", "Priority": 1}]},
        )["UserPool"]["Id"]
    # MFA: 只启用 TOTP(验证器 App), 不使用短信, 不需要 SNS
    current = cog.get_user_pool_mfa_config(UserPoolId=pool_id).get("MfaConfiguration", "OFF")
    target = MFA_MODES[mfa]
    if current != target:
        try:
            kw = {"SoftwareTokenMfaConfiguration": {"Enabled": target != "OFF"}}
            cog.set_user_pool_mfa_config(UserPoolId=pool_id, MfaConfiguration=target, **kw)
            print(f"    MFA: {current} → {target}")
        except ClientError as e:
            print(f"    警告: 无法把 MFA 从 {current} 改为 {target}: {e.response['Error'].get('Message')}", file=sys.stderr)
    for group, desc in ((ADMIN_GROUP, "可修改监控配置、开始/结束轮询"), (VIEWER_GROUP, "只读")):
        try:
            cog.create_group(UserPoolId=pool_id, GroupName=group, Description=desc)
            print(f"    创建用户组: {group}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "GroupExistsException":
                raise
    clients = cog.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60).get("UserPoolClients", [])
    existing = next((c["ClientId"] for c in clients if c["ClientName"] == client_name), None)
    client_kw = dict(ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
                     RefreshTokenValidity=1440, PreventUserExistenceErrors="ENABLED")
    if existing:
        cog.update_user_pool_client(UserPoolId=pool_id, ClientId=existing, ClientName=client_name, **client_kw)
        client_id = existing
    else:
        step("创建 Cognito 应用客户端")
        client_id = cog.create_user_pool_client(
            UserPoolId=pool_id, ClientName=client_name, GenerateSecret=False, **client_kw
        )["UserPoolClient"]["ClientId"]
    return pool_id, client_id


def ensure_user(cog, pool_id, email, role):
    """创建用户(已存在则保留), 并让其只属于 role 对应的组。"""
    try:
        cog.admin_get_user(UserPoolId=pool_id, Username=email)
        step(f"登录用户已存在: {email}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "UserNotFoundException":
            raise
        step(f"创建登录用户, 一次性密码将发送到: {email}")
        cog.admin_create_user(
            UserPoolId=pool_id, Username=email,
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
            DesiredDeliveryMediums=["EMAIL"],
        )
    groups = {g["GroupName"] for g in cog.admin_list_groups_for_user(
        UserPoolId=pool_id, Username=email, Limit=60).get("Groups", [])}
    if role not in groups:
        cog.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=role)
    for other in set(ROLES) - {role}:
        if other in groups:
            cog.admin_remove_user_from_group(UserPoolId=pool_id, Username=email, GroupName=other)
    print(f"    角色: {role}")


def ensure_api(apigw, lam, region, acct, api_name, pool_id, api_fn_name, api_fn_arn, rate_limit, burst_limit):
    apis = apigw.get_rest_apis(limit=500).get("items", [])
    hit = next((a for a in apis if a["name"] == api_name), None)
    if hit:
        api_id = hit["id"]
        step(f"复用 API Gateway: {api_id}")
    else:
        step(f"创建 API Gateway: {api_name}")
        api_id = apigw.create_rest_api(name=api_name, description="Connect Customer 队列监控",
                                       endpointConfiguration={"types": ["REGIONAL"]})["id"]
    pool_arn = f"arn:aws:cognito-idp:{region}:{acct}:userpool/{pool_id}"
    auths = apigw.get_authorizers(restApiId=api_id).get("items", [])
    auth = next((a for a in auths if a["name"] == "cognito-auth"), None)
    if auth and pool_arn not in auth.get("providerARNs", []):
        apigw.delete_authorizer(restApiId=api_id, authorizerId=auth["id"])
        auth = None
    auth_id = auth["id"] if auth else apigw.create_authorizer(
        restApiId=api_id, name="cognito-auth", type="COGNITO_USER_POOLS", providerARNs=[pool_arn],
        identitySource="method.request.header.Authorization",
    )["id"]

    resources = apigw.get_resources(restApiId=api_id, limit=500)["items"]
    root_id = next(r["id"] for r in resources if r["path"] == "/")
    proxy = next((r for r in resources if r.get("pathPart") == "{proxy+}" and r.get("parentId") == root_id), None)
    proxy_id = proxy["id"] if proxy else apigw.create_resource(
        restApiId=api_id, parentId=root_id, pathPart="{proxy+}")["id"]
    uri = f"arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/{api_fn_arn}/invocations"
    for method, auth_type in (("ANY", "COGNITO_USER_POOLS"), ("OPTIONS", "NONE")):
        try:
            apigw.get_method(restApiId=api_id, resourceId=proxy_id, httpMethod=method)
        except ClientError:
            kw = {"authorizerId": auth_id} if auth_type == "COGNITO_USER_POOLS" else {}
            apigw.put_method(restApiId=api_id, resourceId=proxy_id, httpMethod=method,
                             authorizationType=auth_type, **kw)
        apigw.put_integration(restApiId=api_id, resourceId=proxy_id, httpMethod=method,
                              type="AWS_PROXY", integrationHttpMethod="POST", uri=uri)
    # 鉴权失败等网关错误也带 CORS 头, 浏览器才能读到 401 并触发刷新 Token
    for rtype in ("DEFAULT_4XX", "DEFAULT_5XX"):
        apigw.put_gateway_response(
            restApiId=api_id, responseType=rtype,
            responseParameters={
                "gatewayresponse.header.Access-Control-Allow-Origin": "'*'",
                "gatewayresponse.header.Access-Control-Allow-Headers": "'Authorization,Content-Type'",
                "gatewayresponse.header.Access-Control-Allow-Methods": "'GET,PUT,POST,DELETE,OPTIONS'",
            },
        )
    ensure_permission(lam, api_fn_name, "apigw-invoke", "apigateway.amazonaws.com",
                      f"arn:aws:execute-api:{region}:{acct}:{api_id}/*/*")
    apigw.create_deployment(restApiId=api_id, stageName="prod")
    # stage 级限流: 防止 Token 泄露或脚本误用时把 Lambda / Connect API 打满
    apigw.update_stage(restApiId=api_id, stageName="prod", patchOperations=[
        {"op": "replace", "path": "/*/*/throttling/rateLimit", "value": str(rate_limit)},
        {"op": "replace", "path": "/*/*/throttling/burstLimit", "value": str(burst_limit)},
    ])
    return f"https://{api_id}.execute-api.{region}.amazonaws.com/prod"


def upload_web(s3, bucket, aws_config_js, acct):
    step(f"发布站点到 s3://{bucket}/ ...")
    ver = str(int(time.time()))
    js = "application/javascript; charset=utf-8"
    for name in ("app.js", "auth.js"):
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            s3.put_object(Bucket=bucket, Key=name, Body=f.read(), ContentType=js, ExpectedBucketOwner=acct)
    s3.put_object(Bucket=bucket, Key="aws-config.js", Body=aws_config_js.encode("utf-8"), ContentType=js,
                  CacheControl="no-cache", ExpectedBucketOwner=acct)
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    for name in ("aws-config.js", "auth.js", "app.js"):
        html = html.replace(f'src="./{name}"', f'src="./{name}?v={ver}"')
    s3.put_object(Bucket=bucket, Key="index.html", Body=html.encode("utf-8"),
                  ContentType="text/html; charset=utf-8", CacheControl="no-cache", ExpectedBucketOwner=acct)


def build_csp(region, api_url):
    """只允许本站脚本; 浏览器只能连接 Cognito 与本项目的 API Gateway。"""
    api_origin = "/".join(api_url.split("/")[:3])
    return "; ".join([
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",  # index.html 内联样式
        "img-src 'self' data:",
        f"connect-src 'self' https://cognito-idp.{region}.amazonaws.com {api_origin}",
        "font-src 'self'",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    ])


def ensure_headers_policy(cf, name, csp):
    config = {
        "Name": name,
        "Comment": "Security headers for Connect Customer queue monitor",
        "SecurityHeadersConfig": {
            "ContentSecurityPolicy": {"Override": True, "ContentSecurityPolicy": csp},
            "ContentTypeOptions": {"Override": True},
            "FrameOptions": {"Override": True, "FrameOption": "DENY"},
            "ReferrerPolicy": {"Override": True, "ReferrerPolicy": "strict-origin-when-cross-origin"},
            "StrictTransportSecurity": {"Override": True, "AccessControlMaxAgeSec": 31536000,
                                        "IncludeSubdomains": True, "Preload": False},
        },
        "CustomHeadersConfig": {"Quantity": 1, "Items": [{
            "Header": "Permissions-Policy", "Override": True,
            "Value": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        }]},
    }
    items = (cf.list_response_headers_policies(Type="custom").get("ResponseHeadersPolicyList", {})
             .get("Items", []) or [])
    hit = next((i["ResponseHeadersPolicy"] for i in items
                if i["ResponseHeadersPolicy"]["ResponseHeadersPolicyConfig"]["Name"] == name), None)
    if hit:
        etag = cf.get_response_headers_policy_config(Id=hit["Id"])["ETag"]
        cf.update_response_headers_policy(Id=hit["Id"], IfMatch=etag, ResponseHeadersPolicyConfig=config)
        step(f"更新 CloudFront 安全响应头策略: {name}")
        return hit["Id"]
    step(f"创建 CloudFront 安全响应头策略: {name}")
    return cf.create_response_headers_policy(ResponseHeadersPolicyConfig=config)["ResponseHeadersPolicy"]["Id"]


def ensure_cloudfront(cf, s3, acct, region, web_bucket, comment, oac_name, headers_policy_id):
    oacs = cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", []) or []
    oac = next((o for o in oacs if o["Name"] == oac_name), None)
    oac_id = oac["Id"] if oac else cf.create_origin_access_control(OriginAccessControlConfig={
        "Name": oac_name, "Description": f"OAC for {web_bucket}", "SigningProtocol": "sigv4",
        "SigningBehavior": "always", "OriginAccessControlOriginType": "s3",
    })["OriginAccessControl"]["Id"]

    origin = f"{web_bucket}.s3.{region}.amazonaws.com"
    dists, marker = [], None
    while True:
        kw = {"Marker": marker} if marker else {}
        dl = cf.list_distributions(**kw).get("DistributionList", {})
        dists.extend(dl.get("Items", []) or [])
        marker = dl.get("NextMarker")
        if not dl.get("IsTruncated"):
            break
    hit = next((d for d in dists if d.get("Comment") == comment), None)
    if hit:
        dist_id = hit["Id"]
        step(f"复用 CloudFront 分配: {dist_id}")
        resp = cf.get_distribution_config(Id=dist_id)
        dcfg = resp["DistributionConfig"]
        if dcfg["DefaultCacheBehavior"].get("ResponseHeadersPolicyId") != headers_policy_id:
            dcfg["DefaultCacheBehavior"]["ResponseHeadersPolicyId"] = headers_policy_id
            cf.update_distribution(Id=dist_id, IfMatch=resp["ETag"], DistributionConfig=dcfg)
            print("    已为分配挂上安全响应头策略")
    else:
        step("创建 CloudFront 分配 ...")
        dist_id = cf.create_distribution(DistributionConfig={
            "CallerReference": f"{comment}-{int(time.time())}",
            "Comment": comment,
            "Enabled": True,
            "DefaultRootObject": "index.html",
            "Origins": {"Quantity": 1, "Items": [{
                "Id": "s3-web", "DomainName": origin, "OriginAccessControlId": oac_id,
                "S3OriginConfig": {"OriginAccessIdentity": ""},
            }]},
            "DefaultCacheBehavior": {
                "TargetOriginId": "s3-web", "ViewerProtocolPolicy": "redirect-to-https",
                "CachePolicyId": CF_CACHE_POLICY_ID, "Compress": True,
                "ResponseHeadersPolicyId": headers_policy_id,
                "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                                   "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            },
            "CustomErrorResponses": {"Quantity": 2, "Items": [
                {"ErrorCode": 403, "ResponseCode": "200", "ResponsePagePath": "/index.html", "ErrorCachingMinTTL": 10},
                {"ErrorCode": 404, "ResponseCode": "200", "ResponsePagePath": "/index.html", "ErrorCachingMinTTL": 10},
            ]},
        })["Distribution"]["Id"]
    dist = cf.get_distribution(Id=dist_id)["Distribution"]
    dist_arn = f"arn:aws:cloudfront::{acct}:distribution/{dist_id}"
    s3.put_bucket_policy(Bucket=web_bucket, ExpectedBucketOwner=acct, Policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Sid": "AllowCloudFrontOAC", "Effect": "Allow",
                       "Principal": {"Service": "cloudfront.amazonaws.com"}, "Action": "s3:GetObject",
                       "Resource": f"arn:aws:s3:::{web_bucket}/*",
                       "Condition": {"StringEquals": {"AWS:SourceArn": dist_arn}}},
                      tls_only_statement(web_bucket)],
    }))
    cf.create_invalidation(DistributionId=dist_id, InvalidationBatch={
        "Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": str(time.time())})
    step("等待 CloudFront 部署完成(新建通常 3~10 分钟)...")
    status, waited = dist["Status"], 0
    while status != "Deployed" and waited < 1200:
        time.sleep(15)
        waited += 15
        status = cf.get_distribution(Id=dist_id)["Distribution"]["Status"]
        # \x1b[K 清除行尾残留(状态文字变短时, 例如 InProgress → Deployed)
        print(f"\r    状态: {status} · 已等待 {waited}s\x1b[K", end="", flush=True)
    print()
    return dist_id, dist["DomainName"], status


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="部署 Connect Customer 队列监控网站", epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="", help="配置文件, 默认自动查找 config-*.json")
    ap.add_argument("--profile", default="", help="AWS profile; 不指定时用配置文件的 profile, 都没有则使用当前环境凭证")
    ap.add_argument("--prefix", default="", help=f"资源名称前缀, 覆盖配置文件里的 prefix(默认 {DEFAULT_PREFIX})")
    ap.add_argument("-i", "--interactive", action="store_true", help="交互式部署")
    ap.add_argument("--dry-run", action="store_true", help="只打印部署计划, 不调用 AWS")
    ap.add_argument("-y", "--yes", action="store_true", help="不询问确认, 直接部署")
    ap.add_argument("--keep", action="store_true", help="保留临时打包目录")
    args = ap.parse_args()

    for name in LAMBDA_FILES:
        if not os.path.isfile(os.path.join(LAMBDA_DIR, name)):
            sys.exit(f"错误: 缺少 lib/lambda/{name}")
    for name in WEB_FILES:
        if not os.path.isfile(os.path.join(WEB_DIR, name)):
            sys.exit(f"错误: 缺少 lib/web/{name}")

    tty = sys.stdin.isatty()
    interactive = args.interactive
    config_path = pick_config(args.config, allow_none=interactive or tty)
    if not config_path and not interactive:
        print("未找到 config-<account_id>.json。")
        if not _yes("是否进入交互式部署?", True):
            sys.exit("已取消。可参考 config-example.json 创建配置文件后再执行。")
        interactive = True
    if interactive and not tty:
        sys.exit("错误: 交互式部署需要在终端中运行")
    raw = read_raw_config(config_path)

    profile = args.profile
    if interactive:
        raw, profile = interactive_settings(raw, args.profile, args.prefix)
    try:
        cfg = normalize_settings(raw, profile, "" if interactive else args.prefix)
    except SettingsError as e:
        sys.exit(f"错误: {e}")
    acct, region, names = cfg["account_id"], cfg["deploy_region"], cfg["names"]
    global _PROFILE
    _PROFILE, profile_source = resolve_profile(profile, cfg["profile"], acct)
    cred_desc = f"{credential_label()}  [选择依据: {profile_source}]"

    print()
    print_plan(cfg, cred_desc)

    if interactive:
        target = config_path or os.path.join(SCRIPT_DIR, f"config-{acct}.json")
        new_json = settings_to_json(cfg)
        if read_raw_config(target if os.path.isfile(target) else None) != new_json:
            if _yes(f"保存以上设置到 {os.path.basename(target)}(下次可直接部署)?", True):
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(new_json, f, ensure_ascii=False, indent=4)
                    f.write("\n")
                step(f"已保存: {target}")

    if args.dry_run:
        print("dry-run: 未调用任何 AWS 接口。")
        return

    # 安全检查: 凭证必须属于配置中的账号, 避免部署到错误账号
    try:
        caller = _client("sts", region).get_caller_identity()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"错误: 无法使用 {credential_label()} 调用 AWS: {e}\n"
                 "      请用 --profile 指定 profile, 或设置 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY(临时凭证还需 AWS_SESSION_TOKEN)")
    if caller["Account"] != acct:
        sys.exit(f"错误: 当前凭证属于账号 {caller['Account']}, 与配置中的 account_id {acct} 不一致, 已停止")
    step(f"凭证校验通过: {caller['Arn']}")
    check_connect_regions(region, cfg["regions"])

    if (interactive or tty) and not args.yes:
        if not _yes("确认按以上计划开始部署?", interactive):
            sys.exit("已取消, 未创建任何资源。")

    work = tempfile.mkdtemp(prefix=f"{cfg['prefix']}.")
    try:
        s3 = _client("s3", region)
        ddb = _client("dynamodb", region)
        iam = _client("iam")
        lam = _client("lambda", region)
        logs = _client("logs", region)
        events = _client("events", region)
        cog = _client("cognito-idp", region)
        apigw = _client("apigateway", region)
        cf = _client("cloudfront")

        zip_bytes = build_lambda_zip(work)

        # 1. 数据桶 + 初始配置 JSON
        ensure_bucket(s3, names["data_bucket"], region, acct)
        s3.put_bucket_versioning(Bucket=names["data_bucket"], VersioningConfiguration={"Status": "Enabled"},
                                 ExpectedBucketOwner=acct)
        s3.put_bucket_policy(Bucket=names["data_bucket"], ExpectedBucketOwner=acct, Policy=json.dumps({
            "Version": "2012-10-17", "Statement": [tls_only_statement(names["data_bucket"])]}))
        try:
            s3.head_object(Bucket=names["data_bucket"], Key=CONFIG_KEY, ExpectedBucketOwner=acct)
            step(f"监控配置已存在, 保留: s3://{names['data_bucket']}/{CONFIG_KEY}")
        except ClientError as e:
            # 只有确认不存在(404)才写入空配置; 403 等错误直接退出, 避免覆盖已有配置
            if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                raise
            step(f"写入空的监控配置: s3://{names['data_bucket']}/{CONFIG_KEY}")
            empty = {"version": 1, "updatedAt": None, "updatedBy": None, "regions": {}}
            s3.put_object(Bucket=names["data_bucket"], Key=CONFIG_KEY, Body=json.dumps(empty).encode(),
                          ContentType="application/json; charset=utf-8", ExpectedBucketOwner=acct)

        # 2. DynamoDB
        ensure_table(ddb, names["metrics_table"], "pk", "lastUpdate", cfg["point_in_time_recovery"])
        ensure_table(ddb, names["status_table"], "pk", "monitorDate", cfg["point_in_time_recovery"])

        # 3. IAM + Lambda
        policy = lambda_policy(region, acct, names["data_bucket"], names["metrics_table"], names["status_table"],
                               names["schedule_rule"], [names["poller_function"], names["api_function"]])
        role_arn = ensure_role(iam, names["lambda_role"], policy)
        env = {
            "DATA_BUCKET": names["data_bucket"], "CONFIG_KEY": CONFIG_KEY,
            "METRICS_TABLE": names["metrics_table"], "STATUS_TABLE": names["status_table"],
            "RETENTION_DAYS": str(cfg["retention_days"]), "RULE_NAME": names["schedule_rule"],
            "REGIONS": ",".join(cfg["regions"]), "ACCOUNT_ID": acct, "ADMIN_GROUP": ADMIN_GROUP,
        }
        for fn in (names["poller_function"], names["api_function"]):
            ensure_log_group(logs, fn)
        poller_arn = ensure_lambda(lam, names["poller_function"], "poller.handler", role_arn, zip_bytes, env, 55, 512)
        api_fn_arn = ensure_lambda(lam, names["api_function"], "api_handler.handler", role_arn, zip_bytes, env, 30, 512)
        # 轮询 Lambda 不做自动重试: 过了这一分钟重试已无意义
        lam.put_function_event_invoke_config(FunctionName=names["poller_function"], MaximumRetryAttempts=0,
                                             MaximumEventAgeInSeconds=60)

        # 4. EventBridge 每分钟规则
        rule_state = ensure_rule(events, lam, names["schedule_rule"], names["poller_function"], poller_arn)

        # 5. Cognito + API Gateway
        pool_id, client_id = ensure_user_pool(cog, names["user_pool"], names["user_pool_client"], cfg["mfa"])
        api_url = ensure_api(apigw, lam, region, acct, names["rest_api"], pool_id, names["api_function"], api_fn_arn,
                             cfg["api_rate_limit"], cfg["api_burst_limit"])
        for user in cfg["users"]:
            ensure_user(cog, pool_id, user["email"], user["role"])

        # 6. 前端 + CloudFront
        ensure_bucket(s3, names["web_bucket"], region, acct)
        aws_config_js = (
            "/* 由 setup_monitor.py 自动生成, 请勿手工编辑 */\n"
            "window.__AWS_CONFIG__ = " + json.dumps({
                "region": region, "userPoolId": pool_id, "clientId": client_id, "apiUrl": api_url,
            }, ensure_ascii=False) + ";\n"
        )
        upload_web(s3, names["web_bucket"], aws_config_js, acct)
        headers_policy_id = ensure_headers_policy(cf, names["cloudfront_headers_policy"], build_csp(region, api_url))
        dist_id, domain, cf_status = ensure_cloudfront(cf, s3, acct, region, names["web_bucket"],
                                                      names["cloudfront_comment"], names["cloudfront_oac"],
                                                      headers_policy_id)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f" 临时目录已保留: {work}")

    print()
    print("===================================================================")
    print(" 部署完成")
    print("-------------------------------------------------------------------")
    print(f" 访问地址:          https://{domain}  (CloudFront 状态: {cf_status})")
    print(f" 管理员邮箱:        {cfg['email']}  (首次登录用邮件里的一次性密码, 再设置新密码"
          + (", 并绑定验证器 App" if cfg["mfa"] == "required" else "") + ")")
    print(f" 轮询规则:          {names['schedule_rule']}  当前状态: {rule_state}")
    print(f" 监控配置 JSON:     s3://{names['data_bucket']}/{CONFIG_KEY}")
    print(f" 指标记录表:        {names['metrics_table']}")
    print(f" 队列状态表:        {names['status_table']}")
    print(f" 轮询 Lambda:       {names['poller_function']}")
    print(f" API Lambda:        {names['api_function']}")
    print(f" API Gateway:       {api_url}")
    print(f" Cognito 用户池:    {pool_id}   应用客户端: {client_id}")
    print(f" CloudFront 分配:   {dist_id}")
    print("-------------------------------------------------------------------")
    profile_arg = f" --profile {_PROFILE}" if _PROFILE else ""
    print(f" 查看轮询日志: aws logs tail /aws/lambda/{names['poller_function']} --follow{profile_arg} --region {region}")
    print("===================================================================")


if __name__ == "__main__":
    main()

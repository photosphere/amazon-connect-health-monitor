# Connect Customer 队列监控网站

监控 Amazon Connect Customer 实例中各队列的 `CONTACTS_HANDLED`：在设定的阈值分钟内没有变化就高亮记录，并在左侧目录上用红色数字标出次数。
部署方式和页面样式参考 [amazon-connect-ai-agent-cloudwatch-setup-m/log](https://github.com/photosphere/amazon-connect-ai-agent-cloudwatch-setup-m/tree/main/log)：Python + boto3 一键部署脚本，S3 + CloudFront(OAC) 托管静态站点，Cognito 登录，API Gateway → Lambda，暗色主题的原生 JS 前端。

## 架构

```
浏览器 ──(Cognito 登录, IdToken)──> CloudFront ─> S3 web 桶 (index.html / app.js / auth.js / aws-config.js)
   │
   └─> API Gateway /prod/{proxy+} (Cognito Authorizer) ─> api Lambda
          ├─ S3 data 桶 config/monitor-config.json      (监控配置 JSON, 开启版本控制)
          ├─ Connect ListInstances / ListQueues          (左侧树与配置页的数据来源)
          ├─ DynamoDB metrics / status 表                (队列记录与当天异常计数)
          └─ EventBridge EnableRule / DisableRule        (菜单栏「开始 / 结束」)

EventBridge rate(1 minute) ─> poller Lambda
          ├─ 读配置 JSON → monitor_lib.build_minute_schedule() → {"HH:MM"(UTC): [待轮询队列, ...]}
          ├─ GetCurrentMetricData (实时: 排队数 / 最长等待 / 坐席数)
          ├─ GetMetricDataV2      (CONTACTS_HANDLED, 实例时区当天 00:00 起累计)
          └─ 写 DynamoDB，与阈值分钟前的记录比较，差为 0 记为 stalled
```

## 目录

| 路径 | 说明 |
| --- | --- |
| `setup_monitor.py` | 一键部署 / 更新脚本(幂等，可重复执行) |
| `config-example.json` | 部署配置示例，复制为 `config-<account_id>.json` 使用 |
| `lib/lambda/monitor_lib.py` | 配置校验 + 每分钟轮询字典构建(无 boto3 依赖，poller 与 api 共用) |
| `lib/lambda/poller.py` | 轮询 Lambda |
| `lib/lambda/api_handler.py` | 网站后端 Lambda |
| `lib/web/` | 前端：`index.html`(样式)、`auth.js`(Cognito 登录 + API 封装)、`app.js`(页面逻辑) |
| `dev/serve_local.py`, `dev/mock-api.js` | 本地预览(内存 mock 数据，不连接 AWS，不会被部署) |

## 部署

1. 准备：Python 3.9 及以上(macOS / Linux / Windows / AWS CloudShell 均可)、boto3，以及目标账号的 AWS 凭证。凭证可以是 profile，也可以是当前环境的凭证(环境变量 AK/SK、CloudShell 等)，选择规则见[「AWS 凭证」](#aws-凭证)；需要的权限见[「权限」](#权限)。打包 Lambda 时脚本会用 pip 从 PyPI 下载 `tzdata`，本机需要能访问 PyPI。

   安装 boto3 不一定要用虚拟环境，按所用的 Python 选择：

   | 环境 | 安装方式 |
   | --- | --- |
   | AWS CloudShell | 已预装，无需安装 |
   | macOS 自带 `/usr/bin/python3`(3.9) | `/usr/bin/python3 -m pip install --user boto3`，装到当前用户目录，不需要 sudo |
   | Windows(python.org 安装包) | `py -m pip install boto3` |
   | Homebrew Python、Debian 12+ / Ubuntu 23.04+ 的系统 Python | 这些 Python 会拒绝直接 `pip install`(报 `externally-managed-environment`，加 `--user` 也一样)。可以改用上面的 macOS 自带 Python，或用虚拟环境，见下方 |

   虚拟环境(任何系统都适用，依赖与系统 Python 隔离)：

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install boto3
   python3 setup_monitor.py --dry-run
   ```

   不建议用 `pip install --break-system-packages` 强行装进 Homebrew 或系统的 Python，可能和系统包管理器装的包冲突。

   无论哪种方式，脚本打包 Lambda 时用 `pip install --target` 下载 `tzdata` 到临时目录，不会写入你的 Python 环境，在 Homebrew Python 下也能正常执行。macOS 自带的 `/usr/bin/python3`(3.9)和 Homebrew 的 Python 都可以使用。Lambda 部署包只包含纯 Python 代码和 `tzdata` 时区数据，没有编译的二进制文件，所以在 macOS(包括 Apple 芯片)上打包也能直接在 Lambda 的 Linux 环境运行。
2. 准备配置，两种方式任选：
   - **交互式**：`python3 setup_monitor.py -i`，按提示逐项输入(见下方[「交互式部署」](#交互式部署))。目录下没有 `config-*.json` 时直接运行脚本也会提示进入交互式。
   - **配置文件**：`cp config-example.json config-<account_id>.json` 并修改：

   | 字段 | 必填 | 默认值 | 说明 |
   | --- | --- | --- | --- |
   | `email` | 是 | — | 管理员登录邮箱(角色 admin)，Cognito 会把一次性密码发到这里 |
   | `account_id` | 是 | — | 12 位账号 ID，脚本会校验 profile 确实属于该账号 |
   | `regions` | 是 | — | 左侧目录**初始**显示的区域；部署时会检查它们都提供 Connect Customer 服务。部署后可以在网页上添加/移除区域，见[「区域管理」](#区域管理) |
   | `profile` | 否 | 空 | AWS profile 名；留空时按[「AWS 凭证」](#aws-凭证)的规则选择 |
   | `deploy_region` | 否 | `us-east-1` | 网站与 Lambda 部署区域 |
   | `retention_days` | 否 | `30` | DynamoDB 记录保留天数(TTL)，1~3650 |
   | `extra_users` | 否 | `[]` | 其他登录用户。只写邮箱字符串为只读用户；写成 `{"email": "...", "role": "admin"}` 为管理员。见[「用户与角色」](#用户与角色) |
   | `mfa` | 否 | `required` | 登录多因素认证：`required` 必须绑定验证器 App / `optional` / `off`，见[「登录与 MFA」](#登录与-mfa) |
   | `api_rate_limit` / `api_burst_limit` | 否 | `20` / `40` | API Gateway 限流：每秒请求数 / 突发上限 |
   | `point_in_time_recovery` | 否 | `false` | 为两张 DynamoDB 表开启时间点恢复(PITR，按数据量计费) |
   | `prefix` | 否 | `connect-monitor` | 所有资源名称的前缀，见[「资源命名」](#资源命名) |
   | `resource_names` | 否 | `{}` | 单独指定某些资源的名称，见[「资源命名」](#资源命名) |

3. 执行：`python3 setup_monitor.py`(目录下只有一个 `config-*.json` 时自动选用；有多个时会列出让你选；也可 `--config` 指定)。脚本先打印**部署计划**(账号、区域、每个资源的名称)，在终端中运行时要输入 `y` 确认后才会创建资源。
4. 打开输出的 CloudFront 地址，用邮件里的一次性密码登录，设置新密码，再按提示绑定验证器 App(`mfa` 为 `required` 时)。
5. 在网页上配置实例和队列，点击菜单栏「▶ 开始」启用轮询。**轮询规则第一次创建时是停用状态**，重复部署会保持当时的状态。

### AWS 凭证

脚本按下面的顺序决定使用哪组凭证，并在部署计划里显示「凭证: …」和选择依据：

1. 命令行 `--profile NAME`
2. 配置文件里的 `profile`
3. 环境里已经设置了 `AWS_ACCESS_KEY_ID`(或 `AWS_PROFILE`)时，直接使用当前环境的凭证
4. 本机存在与账号 ID 同名的 profile 时使用它(兼容参考项目的约定)
5. 以上都没有时，使用 boto3 默认凭证链：环境变量、`~/.aws` 的 default profile、CloudShell / ECS 容器凭证、EC2 实例角色等

无论用哪种凭证，脚本都会先调用 `sts:GetCallerIdentity` 确认它属于配置里的 `account_id`，不一致就停止，不会创建任何资源。

**使用当前环境的 AK/SK**：

```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...        # 临时凭证才需要
python3 setup_monitor.py
```

### 在 AWS CloudShell 中部署

CloudShell 预装了 Python 3、pip 和 boto3，并自动使用当前控制台登录用户的凭证，不需要配置 profile 或 AK/SK。在任意区域(例如 us-east-1)的 CloudShell 中：

```bash
# 1. 上传代码: 右上角「Actions → Upload file」上传项目 zip 后解压，或 git clone
unzip connect-monitor.zip -d connect-monitor && cd connect-monitor

# 2. 交互式部署(profile 一项直接回车)，或准备好 config-<account_id>.json 后直接运行
python3 setup_monitor.py -i
```

- 部署区域由配置里的 `deploy_region` 决定，与打开 CloudShell 的区域无关。
- 使用的是控制台登录用户/角色的权限，它需要具备[「部署时」](#部署时运行脚本的-profile)列出的权限。
- 脚本需要从 PyPI 下载 `tzdata`。CloudShell 默认可以访问互联网；如果使用的是不能访问互联网的 VPC 环境，打包这一步会失败。
- CloudShell 在 20~30 分钟没有键盘或鼠标操作时会结束会话。新建 CloudFront 分配的等待最长 20 分钟，期间请保持页面活动；如果会话中断，重新运行脚本即可，已创建的资源会被复用。
- `$HOME` 下的文件会保留，交互式保存的 `config-<account_id>.json` 下次可以直接使用。

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--config FILE` | 指定配置文件；默认自动查找脚本目录下的 `config-*.json`(不含 `config-example.json`) |
| `--profile NAME` | 使用指定的 AWS profile，覆盖配置里的 `profile` |
| `--prefix PREFIX` | 覆盖配置里的 `prefix` |
| `-i`, `--interactive` | 交互式部署 |
| `--dry-run` | 只校验配置并打印部署计划，不调用任何 AWS 接口，可用来预览资源名称 |
| `-y`, `--yes` | 跳过「确认部署」提问；不在终端中运行(如 CI)时本来就不提问 |
| `--keep` | 保留临时打包目录 |

### 交互式部署

`python3 setup_monitor.py -i` 依次询问：部署区域 → AWS profile(留空表示使用当前环境凭证；输入后自动查出所属账号) → 账号 ID → 管理员邮箱 → 监控区域 → 记录保留天数 → 其他管理员邮箱 → 只读用户邮箱 → MFA → 资源前缀 → 是否逐个修改资源名称。API 限流和 PITR 不在交互中询问，沿用配置文件或默认值。

- 方括号里是默认值，直接回车即采用。已有配置文件时，默认值取自该文件。
- 每一项输入后立即校验，不合法会说明原因并重新询问。
- 逐个修改资源名称时：回车保留当前值，输入 `-` 恢复默认值，可以使用 `{prefix}`、`{account_id}` 占位符。
- 最后打印部署计划，询问是否把这些设置保存为 `config-<account_id>.json`(下次可直接部署)，再询问是否开始部署。回答否则不会创建任何资源。
- 只想生成配置文件、不部署：`python3 setup_monitor.py -i --dry-run`。

### 资源命名

所有资源都有默认名称，格式为 `<prefix>-<account_id>-<用途>`，`prefix` 默认是 `connect-monitor`。带上账号 ID 是为了让 S3 桶名全局唯一，也方便同一套代码部署到多个账号。

| `resource_names` 的键 | 资源 | 默认名称 | 命名规则 |
| --- | --- | --- | --- |
| `data_bucket` | S3 数据桶(监控配置 JSON) | `<prefix>-<acct>-data` | 3~63 位小写字母、数字、`.`、`-`，S3 桶名全球唯一 |
| `web_bucket` | S3 网站桶 | `<prefix>-<acct>-web` | 同上 |
| `metrics_table` | DynamoDB 指标记录表 | `<prefix>-<acct>-metrics` | 3~255 位字母、数字、`_.-` |
| `status_table` | DynamoDB 队列状态表 | `<prefix>-<acct>-status` | 同上 |
| `lambda_role` | IAM Lambda 执行角色 | `<prefix>-<acct>-lambda-role` | 1~64 位字母、数字、`+=,.@_-` |
| `poller_function` | 轮询 Lambda | `<prefix>-<acct>-poller` | 1~64 位字母、数字、`_-` |
| `api_function` | API Lambda | `<prefix>-<acct>-api` | 同上 |
| `schedule_rule` | EventBridge 每分钟规则 | `<prefix>-<acct>-every-minute` | 1~64 位字母、数字、`._-` |
| `user_pool` | Cognito 用户池 | `<prefix>-<acct>` | 1~128 位字母、数字、`+=,.@_-` |
| `user_pool_client` | Cognito 应用客户端 | `<prefix>-<acct>-web` | 同上 |
| `rest_api` | API Gateway REST API | `<prefix>-<acct>-api` | 1~128 位字母、数字、`._-` |
| `cloudfront_comment` | CloudFront 分配标识(分配的 Comment，用于查找已有分配) | `<prefix>-<acct>` | 同上 |
| `cloudfront_oac` | CloudFront 源访问控制 | `<prefix>-<acct>-oac` | 1~64 位字母、数字、`._-` |
| `cloudfront_headers_policy` | CloudFront 安全响应头策略 | `<prefix>-<acct>-security-headers` | 1~128 位字母、数字、`_-` |

- **改前缀**：配置里写 `"prefix": "midea-cc"`，或运行时加 `--prefix midea-cc`。前缀要求小写字母开头，1~30 位小写字母、数字、`-`，不能以 `-` 结尾。
- **改单个资源**：在 `resource_names` 里只写要改的键，其余仍用默认名称，例如：

  ```json
  "prefix": "midea-cc",
  "resource_names": {
      "metrics_table": "CC_Queue_Metrics",
      "poller_function": "{prefix}-queue-poller"
  }
  ```

- 脚本会校验每个名称是否符合该服务的命名规则，以及两个桶、两张表、两个 Lambda 是否重名；未知的键会报错。
- 不能改名的部分：S3 上配置文件的路径固定为 `config/monitor-config.json`；API Gateway 的 stage 固定为 `prod`；执行角色的内联策略名固定为 `connect-monitor`。

> **注意：已经部署过之后再改前缀或资源名，脚本会按新名称创建一套新资源，旧资源不会删除或迁移。** 网页上保存的监控配置(在旧数据桶里)和历史记录(在旧表里)不会出现在新部署中，登录用户也需要重新创建。如需保留，请先手动复制 `config/monitor-config.json` 到新数据桶，再删除旧资源。改名前可以先用 `--dry-run` 确认新名称。

本地预览(不需要 AWS)：`python3 dev/serve_local.py`，然后打开 http://localhost:8765/ 。

## 权限

权限分两类：**部署时**运行 `setup_monitor.py` 的 profile 需要的权限，以及**运行时**两个 Lambda 共用的执行角色的权限(由脚本自动创建，不需要手工配置)。网站用户登录用的是 Cognito，不需要任何 IAM 权限；Connect Customer 实例本身也不需要额外配置。

### 部署时(运行脚本的 profile)

下表按脚本实际调用的 API 整理，`<prefix>` 为资源前缀(默认 `connect-monitor`)，`<acct>` 为账号 ID，`<region>` 为 `deploy_region`。表中是默认名称；用 `resource_names` 自定义过的资源，请换成实际名称。使用管理员权限可以直接部署；如果需要最小权限，可参考表格后面的策略示例。

| 服务 | 操作 | 资源范围 | 用途 |
| --- | --- | --- | --- |
| STS | `GetCallerIdentity` | — | 确认 profile 属于 `account_id`(该操作不需要授权) |
| SSM | `GetParametersByPath` | `arn:aws:ssm:*::parameter/aws/service/global-infrastructure/*`(AWS 公共参数) | 检查 `regions` 都提供 Connect Customer 服务；没有权限时只警告、不中断 |
| S3 | `CreateBucket`, `ListBucket`, `PutBucketPublicAccessBlock`, `PutBucketVersioning`, `PutBucketPolicy`, `GetObject`, `PutObject` | `<prefix>-<acct>-data`、`<prefix>-<acct>-web` 两个桶 | 建桶、关闭公共访问、开启版本控制、写初始配置 JSON、上传前端、设置桶策略(只允许 CloudFront 读取网站桶、两个桶都拒绝非 HTTPS 访问) |
| DynamoDB | `CreateTable`, `DescribeTable`, `DescribeTimeToLive`, `UpdateTimeToLive`, `DescribeContinuousBackups`, `UpdateContinuousBackups` | `<prefix>-<acct>-metrics`、`<prefix>-<acct>-status` | 建表、开启 TTL、按配置开启 PITR |
| IAM | `GetRole`, `CreateRole`, `UpdateAssumeRolePolicy`, `PutRolePolicy`, `PassRole` | `<prefix>-<acct>-lambda-role` | 创建 Lambda 执行角色并写内联策略；创建/更新 Lambda 时需要 `PassRole` |
| Lambda | `GetFunction`, `GetFunctionConfiguration`, `CreateFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `AddPermission`, `RemovePermission`, `PutFunctionEventInvokeConfig` | `<prefix>-<acct>-poller`、`<prefix>-<acct>-api` | 部署两个函数，允许 EventBridge / API Gateway 调用 |
| CloudWatch Logs | `CreateLogGroup`, `PutRetentionPolicy` | `/aws/lambda/<prefix>-<acct>-*` | 预建日志组，保留 30 天 |
| EventBridge | `DescribeRule`, `PutRule`, `PutTargets` | 规则 `<prefix>-<acct>-every-minute` | 每分钟触发轮询 |
| Cognito | `ListUserPools`, `CreateUserPool`, `GetUserPoolMfaConfig`, `SetUserPoolMfaConfig`, `CreateGroup`, `ListUserPoolClients`, `CreateUserPoolClient`, `UpdateUserPoolClient`, `AdminGetUser`, `AdminCreateUser`, `AdminListGroupsForUser`, `AdminAddUserToGroup`, `AdminRemoveUserFromGroup` | 用户池(创建前 ID 未知，只能用 `*`) | 用户池、MFA、admin/viewer 用户组、应用客户端、登录用户及其角色 |
| API Gateway | `apigateway:GET/POST/PUT/PATCH/DELETE` | `arn:aws:apigateway:<region>::/restapis`、`/restapis/*` | REST API、Cognito Authorizer、`{proxy+}` 路由、CORS 网关响应、部署 `prod`、stage 限流(PATCH) |
| CloudFront | `ListDistributions`, `GetDistribution`, `GetDistributionConfig`, `CreateDistribution`, `UpdateDistribution`, `CreateInvalidation`, `ListOriginAccessControls`, `CreateOriginAccessControl`, `ListResponseHeadersPolicies`, `GetResponseHeadersPolicyConfig`, `CreateResponseHeadersPolicy`, `UpdateResponseHeadersPolicy` | `*`(分配 ID 在创建前未知) | 发布网站、刷新缓存、安全响应头策略 |

<details>
<summary>最小权限策略示例(按默认前缀 connect-monitor 编写；把 123456789012、us-east-1 换成实际值，改过前缀或资源名时同步修改 Resource)</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3",
      "Effect": "Allow",
      "Action": ["s3:CreateBucket", "s3:ListBucket", "s3:PutBucketPublicAccessBlock",
                 "s3:PutBucketVersioning", "s3:PutBucketPolicy", "s3:GetObject", "s3:PutObject"],
      "Resource": ["arn:aws:s3:::connect-monitor-123456789012-data", "arn:aws:s3:::connect-monitor-123456789012-data/*",
                   "arn:aws:s3:::connect-monitor-123456789012-web", "arn:aws:s3:::connect-monitor-123456789012-web/*"]
    },
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": ["dynamodb:CreateTable", "dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive", "dynamodb:UpdateTimeToLive",
                 "dynamodb:DescribeContinuousBackups", "dynamodb:UpdateContinuousBackups"],
      "Resource": ["arn:aws:dynamodb:us-east-1:123456789012:table/connect-monitor-123456789012-metrics",
                   "arn:aws:dynamodb:us-east-1:123456789012:table/connect-monitor-123456789012-status"]
    },
    {
      "Sid": "IamRole",
      "Effect": "Allow",
      "Action": ["iam:GetRole", "iam:CreateRole", "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy"],
      "Resource": "arn:aws:iam::123456789012:role/connect-monitor-123456789012-lambda-role"
    },
    {
      "Sid": "PassRoleToLambda",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::123456789012:role/connect-monitor-123456789012-lambda-role",
      "Condition": {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}}
    },
    {
      "Sid": "Lambda",
      "Effect": "Allow",
      "Action": ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:CreateFunction",
                 "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:AddPermission",
                 "lambda:RemovePermission", "lambda:PutFunctionEventInvokeConfig"],
      "Resource": "arn:aws:lambda:us-east-1:123456789012:function:connect-monitor-123456789012-*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
      "Resource": "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/connect-monitor-123456789012-*"
    },
    {
      "Sid": "EventBridge",
      "Effect": "Allow",
      "Action": ["events:DescribeRule", "events:PutRule", "events:PutTargets"],
      "Resource": "arn:aws:events:us-east-1:123456789012:rule/connect-monitor-123456789012-every-minute"
    },
    {
      "Sid": "Cognito",
      "Effect": "Allow",
      "Action": ["cognito-idp:ListUserPools", "cognito-idp:CreateUserPool", "cognito-idp:GetUserPoolMfaConfig",
                 "cognito-idp:SetUserPoolMfaConfig", "cognito-idp:CreateGroup", "cognito-idp:ListUserPoolClients",
                 "cognito-idp:CreateUserPoolClient", "cognito-idp:UpdateUserPoolClient",
                 "cognito-idp:AdminGetUser", "cognito-idp:AdminCreateUser", "cognito-idp:AdminListGroupsForUser",
                 "cognito-idp:AdminAddUserToGroup", "cognito-idp:AdminRemoveUserFromGroup"],
      "Resource": "*"
    },
    {
      "Sid": "ApiGateway",
      "Effect": "Allow",
      "Action": ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"],
      "Resource": ["arn:aws:apigateway:us-east-1::/restapis", "arn:aws:apigateway:us-east-1::/restapis/*"]
    },
    {
      "Sid": "ConnectRegionCheck",
      "Effect": "Allow",
      "Action": "ssm:GetParametersByPath",
      "Resource": "arn:aws:ssm:*::parameter/aws/service/global-infrastructure/*"
    },
    {
      "Sid": "CloudFront",
      "Effect": "Allow",
      "Action": ["cloudfront:ListDistributions", "cloudfront:GetDistribution", "cloudfront:GetDistributionConfig",
                 "cloudfront:CreateDistribution", "cloudfront:UpdateDistribution", "cloudfront:CreateInvalidation",
                 "cloudfront:ListOriginAccessControls", "cloudfront:CreateOriginAccessControl",
                 "cloudfront:ListResponseHeadersPolicies", "cloudfront:GetResponseHeadersPolicyConfig",
                 "cloudfront:CreateResponseHeadersPolicy", "cloudfront:UpdateResponseHeadersPolicy"],
      "Resource": "*"
    }
  ]
}
```

</details>

### 运行时(Lambda 执行角色 `<prefix>-<acct>-lambda-role`)

脚本自动创建该角色，并写入内联策略 `connect-monitor`(资源范围按实际资源名生成，改名后自动跟随)，`poller` 和 `api` 两个函数共用：

| 服务 | 操作 | 资源范围 | 用途 |
| --- | --- | --- | --- |
| CloudWatch Logs | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | 两个函数各自的日志组 | 写 Lambda 日志 |
| S3 | `GetObject`, `PutObject` | `<prefix>-<acct>-data/config/*` | 读写监控配置 JSON |
| DynamoDB | `GetItem`, `PutItem`, `UpdateItem`, `Query`, `BatchGetItem` | metrics、status 两张表 | 写记录、查当天记录与告警计数 |
| EventBridge | `DescribeRule`, `EnableRule`, `DisableRule` | 规则 `<prefix>-<acct>-every-minute` | 网页上的「开始 / 结束」 |
| Connect | `ListInstances` + `ds:DescribeDirectories` | `*`(ListInstances 不支持资源级权限) | 左侧树列出实例 |
| SSM | `GetParametersByPath`, `GetParameters` | `arn:aws:ssm:*::parameter/aws/service/global-infrastructure/*`(AWS 公共参数) | 「添加区域」时读取提供 Connect Customer 服务的区域及名称 |
| Account | `ListRegions` | `*` | 「添加区域」时判断本账号是否已开通该区域；没有权限时跳过此检查 |
| Connect | `ListQueues`, `DescribeInstance`, `GetCurrentMetricData`, `GetMetricDataV2` | `arn:aws:connect:*:<acct>:instance/*` 及 `.../queue/*` | 列队列、采集指标；区域写成 `*`，所以可以访问本账号任意区域的实例 |

运行时角色没有删除权限(不能删 S3 对象、DynamoDB 记录或任何资源)，也不能访问其他账号的资源。

## 用户与角色

| 角色 | Cognito 用户组 | 能做什么 |
| --- | --- | --- |
| 管理员 | `admin` | 查看全部内容；修改并保存监控配置；开始 / 结束轮询 |
| 只读 | `viewer`(以及不属于任何组的用户) | 查看目录、配置、监控记录，翻页、导出 CSV；不能修改配置，不能开始 / 结束轮询 |

- `email` 固定为管理员；`extra_users` 里只写邮箱的是只读用户，写成 `{"email": "...", "role": "admin"}` 的是管理员：

  ```json
  "extra_users": [
      "viewer1@example.com",
      {"email": "ops@example.com", "role": "admin"}
  ]
  ```

- 每次部署会按配置把用户放进对应的组，并移出另一个组；配置里没有列出的用户不会被改动或删除。
- 权限在后端强制执行：`PUT /config` 和 `POST /scheduler` 只允许 `admin` 组，其他用户直接调用接口会得到 403。页面右上角显示当前角色，只读用户看到的编辑控件都是禁用状态。
- 在 Cognito 控制台手动创建的用户默认没有任何组，按只读处理。

## 登录与 MFA

- 用户只能由管理员创建(脚本或 Cognito 控制台)，不能自己注册。
- 密码至少 12 位，包含大小写字母和数字；首次登录用的临时密码 7 天内有效。
- `mfa` 默认为 `required`：首次登录设置新密码后，页面会显示一串设置密钥，在手机验证器 App(Google Authenticator、Microsoft Authenticator、1Password 等)中选择「输入设置密钥」添加，再输入 6 位验证码完成绑定。之后每次登录都要输入验证器上的 6 位验证码。只使用 TOTP，不发短信，不需要配置 SNS。
- 页面暂不显示二维码，需要手动输入密钥；在手机上打开页面时，可以点「在本机的验证器中打开」直接添加。
- 用户更换手机时，管理员可在 Cognito 控制台重置该用户的 MFA，或删除用户后重新部署创建。
- 修改 `mfa` 后重新部署即可生效。从 `required` 改为 `optional` 或 `off` 时，如果 Cognito 不允许变更，脚本会打印警告并保持原设置。

## 安全加固

除了 Cognito 登录和 API 鉴权，部署脚本还做了以下设置：

| 项目 | 做法 |
| --- | --- |
| 安全响应头 | CloudFront 挂自定义响应头策略：CSP(只允许本站脚本，浏览器只能连接 Cognito 与本项目的 API)、HSTS(1 年)、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy`、`Permissions-Policy` |
| 不加载外部脚本 | 前端只有 `index.html`、`auth.js`、`app.js`、`aws-config.js`，不使用 CDN；输出到页面的内容都做了转义，CSV 导出防公式注入 |
| 限流 | API Gateway `prod` stage 默认每秒 20 次、突发 40 次，可用 `api_rate_limit` / `api_burst_limit` 调整 |
| S3 | 两个桶都关闭公共访问并拒绝非 HTTPS 请求；网站桶只允许本项目的 CloudFront 分配读取；所有读写都带 `ExpectedBucketOwner`，同名桶被其他账号占用时脚本会报错停止 |
| 数据恢复 | 配置 JSON 所在的数据桶开启版本控制；DynamoDB 可用 `point_in_time_recovery` 开启时间点恢复 |
| 部署依赖 | Lambda 打包时只下载固定版本的 `tzdata`(`TZDATA_VERSION`)，并用 `--require-hashes` 校验 sha256，哈希不符时停止 |
| 最小权限 | Lambda 执行角色没有删除权限，只能访问本项目的资源和本账号的 Connect 实例 |

本地预览服务器(`dev/serve_local.py`)也会发送同样的 CSP，开发时如果写了内联脚本或引用外部资源，会在浏览器控制台看到报错。

## 区域管理

左侧目录的区域可以在网页上调整(仅管理员)：

- **添加**：目录上方「＋ 添加区域」，从列表中勾选一个或多个区域。列表来自 AWS 公布的 Connect Customer 服务区域(SSM 公共参数 `/aws/service/global-infrastructure/services/connect/regions`)，只包含与部署区域同一分区的区域(商业区不会出现 GovCloud)。已添加的区域显示「已添加」；本账号尚未开通的区域(例如需要手动启用的 `af-south-1`)显示「账号未开通」，不能选择。
- **移除**：点击区域，在明细页右上角点「移除区域」。该区域还有**监控中**的实例时会拒绝，需要先取消监控并保存；未监控实例的配置会一起删除。
- 后端同样检查：添加的区域必须提供 Connect Customer 服务且账号已开通；列出实例、队列和保存配置只允许已添加的区域；只读用户调用这些接口返回 403。
- 区域列表保存在配置 JSON 的 `regionList` 字段，下次打开直接显示。部署配置里的 `regions` 只是初始值：在网页上调整过之后，重新部署不会覆盖网页上的区域列表。
- 新区域不需要重新部署：Lambda 执行角色的 Connect 权限对所有区域生效。

## 页面说明

- **菜单栏**：轮询状态、开始 / 结束按钮(启用/停用 EventBridge 规则)、当天轮询字典的最早/最晚时间、刷新频率(分钟，最小 1，保存在浏览器本地)。
- **左右分隔条**：默认左 4 右 6。拖动中间的分隔条可调整宽度，聚焦后也可用 ← → 键(按住 Shift 每次 5%)，双击恢复 4:6。宽度比例按登录邮箱保存在浏览器 localStorage(`cm_split_left_pct:<email>`)，退出或关闭浏览器后再次登录仍保持；左侧最窄 240px，右侧最窄 420px。
- **左侧(默认 40%)**：区域 → 实例 → 已监控队列，支持单个节点展开/收缩和一键全部展开/收缩。队列右侧的红色数字是**当天**无变化记录的条数，大于 0 时该队列闪烁高亮；实例和区域上的红框数字是下属队列的合计。
- **右侧(默认 60%)**
  - 点击区域：右上角「移除区域」(管理员)；下方是该区域的全部实例，显示是否监控、开始/结束时间、时区、监控日、排除日期和监控状态，可以添加/取消监控，点「配置」进入实例配置。
  - 实例配置：开始/结束时间(结束 ≤ 开始表示跨午夜)、时区、周一到周日勾选、排除日期(日期控件)、队列列表(是否监控 / 频率 / 阈值，支持筛选、全选、批量设置)。点「保存配置」后写入 JSON，左侧目录同步更新。
  - 点击队列：当天(实例时区)的全部记录，按 lastUpdate 由新到旧，支持分页和导出 CSV；无变化的记录以红色高亮。

## 持久化 JSON(`s3://<prefix>-<acct>-data/config/monitor-config.json`)

```json
{
  "version": 1,
  "updatedAt": "2026-11-19T08:00:00.000Z",
  "updatedBy": "admin@example.com",
  "regionList": ["us-east-1", "us-west-2"],
  "regions": {
    "us-east-1": {
      "instances": {
        "<instanceId>": {
          "instanceId": "<instanceId>",
          "instanceArn": "arn:aws:connect:us-east-1:123456789012:instance/<instanceId>",
          "instanceAlias": "my-instance",
          "monitored": true,
          "timezone": "Asia/Shanghai",
          "startTime": "08:00",
          "endTime": "20:00",
          "weekdays": [1, 2, 3, 4, 5],
          "excludedDates": ["2026-12-25"],
          "queues": {
            "<queueId>": {
              "queueId": "<queueId>", "queueArn": "...", "queueName": "BasicQueue",
              "monitored": true, "frequency": 1, "threshold": 15
            }
          }
        }
      }
    }
  }
}
```

`regionList` 是左侧目录显示的区域，只能通过网页的「添加区域 / 移除区域」修改，保存实例配置时不会改动它；为空(`null`)时使用部署配置的 `regions`。`weekdays` 采用 ISO 编号(1 = 周一 … 7 = 周日)；星期和排除日期都按实例时区判断。`updatedAt` 用作乐观锁：两个人同时修改时，后保存的人会收到 409 提示，需要重新加载。

## 轮询字典与调度规则

- `monitor_lib.build_minute_schedule(config, utc_date)` 返回 `{"HH:MM": [target, ...]}`。key 是 **UTC** 分钟：不同实例时区不同，统一换算到 UTC 后才能放进同一个字典。没有任何监控队列时返回 `{}`。
- `target` 包含 `region, instanceId, instanceArn, instanceAlias, timezone, queueId, queueArn, queueName, frequency, threshold`。
- 某个队列的轮询分钟 = 窗口开始 + k × 频率(不晚于窗口结束)。
- EventBridge 每分钟触发一次 poller：当前分钟早于字典最早时间或晚于最晚时间时直接返回；UTC 跨日后重新构建字典；配置变化(S3 ETag 变化)也会重建。
- 本地查看某天的字典：`python3 lib/lambda/monitor_lib.py monitor-config.json 2026-11-20`

## DynamoDB

**`<prefix>-<acct>-metrics`**，每次轮询每个队列一条记录

| 属性 | 说明 |
| --- | --- |
| `pk` (HASH) | `<instanceId>#<queueId>#<实例时区日期>` |
| `lastUpdate` (RANGE) | 轮询分钟，UTC ISO，如 `2026-11-19T00:15:00Z` |
| `region`, `connectArn`, `connectAlias`, `queueName`, `queueArn`, `queueId`, `instanceId` | 资源信息 |
| `CONTACTS_HANDLED` | 实例时区当天 00:00 至该分钟的累计值 |
| `compareLastUpdate`, `compareContactsHandled`, `delta`, `stalled` | 与阈值分钟前记录的比较结果 |
| `CONTACTS_IN_QUEUE`, `OLDEST_CONTACT_AGE`, `AGENTS_ONLINE`, `AGENTS_AVAILABLE`, `AGENTS_ON_CONTACT` | GetCurrentMetricData 实时指标 |
| `lastUpdateLocal`, `monitorDate`, `timezone`, `frequency`, `threshold`, `dataSnapshotTime`, `polledAt`, `error`, `ttl` | 辅助字段 |

**`<prefix>-<acct>-status`**：`pk = <instanceId>#<queueId>`，`monitorDate = 本地日期`，字段 `stalledCount`、`checkCount`、`lastUpdate`、`lastContactsHandled`，左侧红色数字从这张表读取。

**无变化判定**：在同一天的分区里，找 `lastUpdate ∈ (当前 − 阈值 − 频率, 当前 − 阈值]` 中最新的一条记录。如果两条记录的 `CONTACTS_HANDLED` 差为 0，就把当前记录标为 `stalled = true`，同时让当天的 `stalledCount` 加 1。同一分钟重复投递时使用条件写入，不会重复计数。

## 注意事项

- `CONTACTS_HANDLED` **不是** GetCurrentMetricData 支持的实时指标(该接口只支持 CONTACTS_IN_QUEUE、AGENTS_ONLINE 等)，因此用 [GetMetricDataV2](https://docs.aws.amazon.com/boto3/latest/reference/services/connect/client/get_metric_data_v2.html) 读取当天累计值；实时指标仍由 GetCurrentMetricData 提供。调用参数：`ResourceArn` = 实例 ARN，`Interval = {TimeZone: 实例时区, IntervalPeriod: TOTAL}`，`StartTime` = 实例时区当天 00:00，`EndTime` = 当前轮询分钟，`Filters = [{FilterKey: QUEUE, FilterValues: 最多 100 个队列 ID}]`，`Groupings = [QUEUE]`，`Metrics = [{Name: CONTACTS_HANDLED}]`。返回 null 或没有该队列的数据时按 0 处理。历史指标可能有几分钟的延迟。
- 只有 `admin` 组的用户可以修改配置和开始/停止轮询，见[「用户与角色」](#用户与角色)。
- 脚本不提供删除资源的功能；如需清理，请手动删除上面列出的资源。
- `config-*.json` 含账号与邮箱信息，已在 `.gitignore` 中排除。

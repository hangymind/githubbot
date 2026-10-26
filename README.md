# GitHub Commit → QQ 群聊 / 私聊广播机器人

私有仓库新提交 → HTTPS Webhook → SQLite 队列 → 官方 QQ 机器人。默认全部分支、每个新 SHA 一条纯文本消息，支持绑定一个 QQ 群或一个私聊用户（两者择一，不同时订阅）。AppID 已预设为 1906010968。

## 无法加群时：直接使用私聊推送

无需先加入 QQ 群。更新代码后，在 QQ 开发平台为同一个 Webhook 回调订阅 **C2C_MESSAGE_CREATE**，建议同时订阅 **C2C_MSG_REJECT、FRIEND_DEL**。回调地址仍是 https://你的域名/webhooks/qq，不需要 WebSocket 或新的 AppSecret。

先确保你可以通过平台提供的机器人资料页/测试入口打开私聊，且账号在开发测试允许范围内，机器人具备单聊与主动消息权限。加群失败的具体原因需看平台后台、发布状态和测试群配置；私聊接入也需要对应权限，代码不能替代平台授权。

在服务器项目目录执行（宝塔目录为 /www/wwwroot/qbot）：

~~~bash
cd /www/wwwroot/qbot
sudo -u qbot .venv/bin/python -m qbot bind-token --target private
~~~

然后直接在机器人私聊中发送 **/绑定 刚生成的口令**，不需要 @。收到绑定成功后，新 commit 会主动发送到该私聊。私聊支持 /帮助、/状态。用户标识从签名事件 author.user_openid 读取，不填写个人 QQ 号。

口令区分群聊和私聊，私聊口令不能在群里使用。默认 bind-token 仍生成群聊口令，兼容已有教程。当前已绑定群时，先按“管理、恢复与换群”步骤停服并解绑，再生成私聊口令，不会自动把群积压转到私聊。删除好友或关闭主动通知会暂停推送，恢复关系与权限后在服务器 resume，不会自动恢复。

### 已部署版本升级

~~~bash
cd /www/wwwroot/qbot
sudo systemctl stop qbot
sudo -u qbot .venv/bin/python -m qbot backup /www/wwwroot/qbot/data/before-private.sqlite3
sudo -u qbot git pull --ff-only
sudo -u qbot .venv/bin/python -m pip install -e .
sudo systemctl start qbot
sudo -u qbot .venv/bin/python -m qbot bind-token --target private
~~~

若之前通过压缩包上传、没有 .git，则上传新代码覆盖源码，保留 .env 和 data。备份目标需不存在。已有绑定时最后一步会拒绝生成口令，先人工处理旧队列并解绑。升级会自动增加目的地类型字段，原群绑定、口令与队列保留，不要删库或重建 baseline。旧版程序无法读取新版表结构，请勿直接降级；确需回退时停服并恢复升级前备份。

私聊主动推送使用 /v2/users/{user_openid}/messages；commit 不携带过期 msg_id，也不使用召回消息额度。请开启允许机器人主动发送。限频、拒收、权限不足和发送结果不确定仍按队列机制处理。命令回复保守地在 4 分钟内发送。

## 1. 准备与配置

### 没有主动推送权限时：/更新

在已绑定群中由原绑定操作者发送 **@机器人 /更新**，或在已绑定私聊直接发送 **/更新**。使用这条新指令的 msg_id 被动回复，不需要主动发送成功。每次合并领取最早最多 2 条积压提交，按 3500 字节保守限制；还有积压就再次发送 /更新。只读取本地已入库队列，不临时扫描 GitHub 历史。

支持领取 pending 提交及因 qq_40034105 明确失败的 commit；不会把主动测试消息当提交。暂停主动广播的绑定仍可由原操作者手动领取。结果不明、发送中或其他错误的最早提交不会被越过，需要服务器核查。

领取时先保留任务，QQ 明确返回成功才将相应 commit 标为 sent。失败/回复过期会释放提交供下次 /更新；结果不明或发送中断后保留 uncertain，避免盲目重复。此时用 jobs 找到 kind=pull 的任务，核对群消息后 resolve 为 sent/skipped，或承担重复风险 retry；过期回复会释放，不复用已过期 msg_id。领取过程中重复指令不会重复预留同一提交。该功能不改变原主动广播配置，也不会自动重试 failed commit。

### 联通诊断与 qq_40034105

在已绑定群中由原绑定操作者发送 **@机器人 /测试**，或在已绑定私聊发送 **/测试**。显示 AppID、正式/沙箱环境、目标场景、被动回复模式、QQ token 可用性、GitHub 读取提交 HTTP 状态、队列状态，以及近期脱敏 HTTP/错误码/TraceID。不输出密钥、目标 OpenID、仓库内容或平台原始响应。详细诊断仅允许原绑定会话的操作者，60 秒冷却。

发送 **/测试 主动** 会额外发送一条不含 msg_id 的主动测试消息，然后通过当前指令的 msg_id 被动回复诊断报告。主动测试不会自动重试，限频/拒绝也会记录并返回报告，不能据此绕过平台权限。/测试 不改变或重试失败的 commit；诊断请求能越过被失败 commit 阻塞的队列。

40034105 表示平台拒绝主动发送，管理端勾选接口权限不等于目标关系和当前运行环境实际允许主动发送。检查：目标用户/群的主动消息开关、单聊与群聊场景对应的主动权限、机器人发布/测试范围、AppID 和 production/sandbox 是否与实际使用的机器人一致。被动回复成功只证明回调及回复链路可用，不证明主动推送已放开。修复后运行 /测试 主动，若仍失败，把脱敏 TraceID 与时间提交 QQ 平台排查，不要发 AppSecret。

按官方群聊发送接口核对：主动文本请求为 content + msg_type=0，不携带 msg_id/event_id；msg_seq 用于与 msg_id 联合去重，不是开启主动消息的参数。文档示例携带 msg_id，因此示例展示的是被动回复；不能把历史消息 ID 填进去冒充主动推送。GROUP_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE 是接收事件订阅，不等于主动发送授权。

发送请求现补齐官方 botpy SDK 使用的 X-Union-Appid 请求头（取 QQ_APP_ID），并显式设置 Content-Type: application/json; charset=utf-8。当前 API 调用指南仅要求 QQBot access_token，未说明该附加头能解决 40034105；此项是兼容性修正，不能承诺解除权限拒绝。/测试 报告会显示这两项请求头及主被动字段区别，不输出凭证或目标 OpenID。

升级后先执行 /测试 主动，确认主动测试消息真正到达，再处理旧失败提交：服务器运行 .venv/bin/python -m qbot jobs 查看任务，按 .venv/bin/python -m qbot retry --help 逐条重试；不要直接改数据库或批量重投结果不明的消息。若仍为 40034105，保留最新 TraceID 和测试时间交平台核查，可继续使用 /更新 领取积压。

核对依据：[官方群聊发送接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)、[官方 API 调用指南](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html)、[官方 botpy HTTP 实现](https://github.com/tencent-connect/botpy/blob/master/botpy/http.py)。

若连被动报告都未收到，在服务器运行 .venv/bin/python -m qbot diagnostics，并查看 journalctl -u qbot -n 100；新增日志会记录 job、stage（private_active/private_reply/group_active/group_reply/token）、HTTP、业务码、TraceID 和耗时。只保存最近 500 条 API 诊断记录；升级前旧失败任务不会补出当时不存在的 TraceID。GitHub 读取成功不证明 GitHub Webhook 已配置成功，需在 Recent Deliveries 检查。

修复权限后重试日志中的任务 4：先 sudo systemctl stop qbot，再在项目目录 sudo -u qbot .venv/bin/python -m qbot retry 4，最后 sudo systemctl start qbot。保持原有安全机制，不把 commit 伪装成过期被动回复。

需要 Python 3.12+（推荐 Ubuntu 24.04 LTS）、域名及 HTTPS、所选 QQ 群聊或单聊场景及主动消息权限、私有 GitHub 仓库的 Webhook 设置权限。GitHub fine-grained token 仅授权目标仓库，授予 Contents: Read-only；组织仓库可能需要组织审批/SSO。

**现在到填写配置这一步才需要 AppSecret：从 QQ 开发平台复制，在自己的服务器中填入 .env，不要发到聊天里。** AppSecret 用于换取 QQ access_token 与回调签名，不是旧 Token，也不是 GitHub Webhook Secret。

Linux 本地安装：

~~~bash
cd /path/to/qbot
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
~~~

Windows PowerShell：

~~~powershell
cd 'E:\this computer\mine\webproject\qbot'
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
Copy-Item .env.example .env
~~~

编辑 .env，实际值只放在服务器：

~~~dotenv
QQ_APP_ID=1906010968
QQ_APP_SECRET=开发平台AppSecret
GITHUB_REPOSITORY=owner/private-repository
GITHUB_TOKEN=只读fine-grained-token
GITHUB_WEBHOOK_SECRET=单独生成的随机长字符串
DATABASE_PATH=./data/qbot.sqlite3
HOST=127.0.0.1
PORT=8000
INCLUDE_COMMIT_URL=false
SEND_INTERVAL_SECONDS=4
QQ_ENVIRONMENT=production
~~~

生成 GitHub Webhook Secret：

~~~bash
.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'
~~~

QQ_ENVIRONMENT=sandbox 仅在平台明确指定沙箱时使用，开发测试群不必然是 sandbox。

### 先建立历史基线

~~~bash
.venv/bin/python -m qbot baseline
.venv/bin/python -m qbot serve
curl http://127.0.0.1:8000/healthz
~~~

Windows 将 .venv/bin/python 换成 .\.venv\Scripts\python。baseline 扫描所有现有分支可达的历史 SHA，只记录去重，不广播。扫描期间暂时停止仓库写入，结束后尽快启用 Webhook；这个间隙的推送不会自动补发。大仓库消耗 API 配额，失败后修复再重试，全部成功才提交基线。成功后不可覆盖重建，以免吞掉新提交。

未建立配置仓库的基线时 push 返回 503。基线不能识别此前已删除且当前不可达的历史 SHA，日后重新推入可能视为新提交。更换仓库应停服、备份并使用新数据库重新初始化。

healthz 只报告进程/数据库可访问，不代表 QQ 权限、外网或队列健康；请用 status 检查队列。

## 2. Ubuntu 部署

以下假设 Ubuntu 24.04，源码已上传到 /opt/qbot。不要上传 Windows .venv、真实 .env 或测试数据库。

~~~bash
sudo apt update
sudo apt install -y python3.12-venv nginx certbot python3-certbot-nginx
sudo useradd --system --user-group --home-dir /opt/qbot --shell /usr/sbin/nologin qbot
sudo mkdir -p /opt/qbot/data
sudo chown -R qbot:qbot /opt/qbot
cd /opt/qbot
sudo -u qbot python3.12 -m venv .venv
sudo -u qbot .venv/bin/python -m pip install -e .
sudo -u qbot cp .env.example .env
sudo chmod 600 .env
sudo nano .env
# 此时填写 AppSecret、仓库、GitHub token 和独立 Webhook Secret
sudo -u qbot .venv/bin/python -m qbot baseline
sudo cp deploy/qbot.service /etc/systemd/system/qbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qbot
sudo systemctl status qbot --no-pager
curl http://127.0.0.1:8000/healthz
~~~

已有 qbot 用户时跳过 useradd。Ubuntu 22.04 默认 Python 3.10 不够，需要安装 Python 3.12+ 或升级系统。

将下面站点写到 /etc/nginx/sites-available/qbot，将域名换成自己的：

~~~nginx
server {
    listen 80;
    server_name bot.example.com;
    location / { return 404; }
}
~~~

~~~bash
sudo ln -s /etc/nginx/sites-available/qbot /etc/nginx/sites-enabled/qbot
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d bot.example.com
~~~

证书配置好后，编辑站点，在 HTTPS server 块内将原 location / 替换为 deploy/nginx-locations.conf 中的所有 location，保留证书和 HTTP 跳转配置。不要把 location 放在 server 外。

~~~bash
sudo nginx -t
sudo systemctl reload nginx
curl https://bot.example.com/healthz
~~~

云安全组与防火墙开放 443；申请证书与跳转通常需要 80。8000 只监听本机，不开放公网。QQ 控制台要求 IP 白名单时填写服务器出口公网 IP。

## 3. 宝塔面板 + Ubuntu 部署

让宝塔管理 Nginx/证书，systemd 单独管理 Python。不要同时配置宝塔 Python 守护、Supervisor、PM2 或多 worker。无需 PHP、MySQL、Redis。

1. 推荐 Ubuntu 24.04，在宝塔安装 Nginx。
2. 将源码上传解压到 /www/wwwroot/qbot，另建空目录 /www/wwwroot/qbot-public。
3. 宝塔“网站”添加自己的域名，根目录选择 qbot-public 空目录，选静态站点，申请并部署 SSL。**不要把 qbot 代码目录当网站根目录，以免公开 .env 和数据库。**
4. 宝塔终端执行：

~~~bash
sudo apt update
sudo apt install -y python3.12-venv
sudo useradd --system --user-group --home-dir /www/wwwroot/qbot --shell /usr/sbin/nologin qbot
sudo mkdir -p /www/wwwroot/qbot/data
sudo chown -R qbot:qbot /www/wwwroot/qbot
cd /www/wwwroot/qbot
sudo -u qbot python3.12 -m venv .venv
sudo -u qbot .venv/bin/python -m pip install -e .
sudo -u qbot cp .env.example .env
sudo chmod 600 .env
sudo nano .env
# 填写 AppSecret 等，保存退出
sudo -u qbot .venv/bin/python -m qbot baseline
sudo cp deploy/qbot.service /etc/systemd/system/qbot.service
sudo sed -i 's|/opt/qbot|/www/wwwroot/qbot|g' /etc/systemd/system/qbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qbot
curl http://127.0.0.1:8000/healthz
~~~

5. 网站“配置文件”的 HTTPS server 内加入 deploy/nginx-locations.conf；已有 location / 时替换，不重复添加。保留宝塔证书、ACME 验证与安全配置。不再通过“反向代理”菜单生成另一套相同 location。
6. 宝塔检查并重载 Nginx。终端可用 /www/server/nginx/sbin/nginx -t 检查。访问 https://你的域名/healthz。
7. 宝塔安全与云安全组开放 443 和证书所需 80，不开放 8000。

后续命令目录示例 /opt/qbot，在宝塔部署时替换为 /www/wwwroot/qbot。

## 4. QQ 回调与群绑定

在 QQ 开发平台选择 AppID 1906010968，核对 AppSecret、机器人状态及群权限，先加入允许的开发测试群。正式可用范围以平台审核为准。

- 回调方式：Webhook；地址：https://你的域名/webhooks/qq。
- 订阅 GROUP_AT_MESSAGE_CREATE，建议同时订阅 GROUP_DEL_ROBOT、GROUP_MSG_REJECT，归属 GROUP_AND_C2C_EVENT。
- 服务自动处理回调验证；正式事件验证 AppID、Ed25519 签名，持久化后返回 op=12。
- 群内开启允许机器人主动发送消息。移出群或关闭通知时暂停绑定；恢复权限后在服务器运行 resume。

服务器生成 10 分钟一次性口令（可在服务运行时执行）：

~~~bash
cd /opt/qbot
sudo -u qbot .venv/bin/python -m qbot bind-token
~~~

在目标群 @机器人，发送 /绑定 刚生成的口令。成功后记录平台 group_openid 与绑定操作者，不使用普通 QQ 群号。

持有口令的人可以绑定，请仅在可信目标群使用；应用不回显或记录口令，但你发出的群消息本身对群成员可见。/帮助 显示命令；/状态 仅允许原绑定操作者在绑定群内查询。变更操作者需要解绑重绑。

## 5. GitHub 私有仓库 Webhook

仓库 Settings → Webhooks → Add webhook：

| 字段 | 设置 |
|---|---|
| Payload URL | https://你的域名/webhooks/github |
| Content type | application/json |
| Secret | 与 GITHUB_WEBHOOK_SECRET 完全一致 |
| SSL verification | 启用 |
| Events | Just the push event |
| Active | 勾选 |

服务与基线准备好再启用。Recent Deliveries 的 ping 应为 200。向任意分支推送两个新 commit，群应收到两条消息，间隔至少 4 秒；重投同一个 delivery 不新增通知。

同仓库同 SHA 只发送一次，merge commit 是独立 SHA。删除分支和标签不广播。消息包括仓库、首次观察分支、短 SHA、作者、时间、提交标题；标题最多 300 字符，不发送代码、diff、作者邮箱或提交正文。INCLUDE_COMMIT_URL=true 才增加链接；QQ 若返回 URL 权限错误，请关闭此项并处理失败任务。

## 6. 管理、恢复与换群

在项目目录运行，确保使用与服务相同的 .env 和 DATABASE_PATH：

~~~bash
sudo -u qbot .venv/bin/python -m qbot status
sudo -u qbot .venv/bin/python -m qbot jobs
sudo -u qbot .venv/bin/python -m qbot events
sudo journalctl -u qbot -n 100 --no-pager
sudo journalctl -u qbot -f
~~~

jobs/events 显示最近 50 条状态与错误码，不输出消息正文、群标识和凭证。SQLite 包含私有提交元数据，应按私有数据保护。

| 状态 | 含义 |
|---|---|
| pending | 排队/限频退避/等待绑定，自动续发 |
| sent | QQ 返回消息 ID 并完成本地记账 |
| failed | 明确失败，修复权限、内容或凭证后人工 retry |
| uncertain | 可能已发送，等待人工核查，不自动重试 |
| expired | 群命令回复过期，请重新发送指令 |
| skipped | 操作者跳过或换群丢弃 |

任务状态/绑定变更需先停服，以免与网络发送竞争：

~~~bash
sudo systemctl stop qbot
# 明确失败发送，示例任务 ID 12
sudo -u qbot .venv/bin/python -m qbot retry 12
# 修复问题后重试失败入站事件 ID 3
sudo -u qbot .venv/bin/python -m qbot retry-event 3
# uncertain：查群记录后确认已送达
sudo -u qbot .venv/bin/python -m qbot resolve 15 --as sent
# 或跳过
sudo -u qbot .venv/bin/python -m qbot resolve 15 --as skipped
# 或承担重复风险重新发送
sudo -u qbot .venv/bin/python -m qbot resolve 15 --as retry --accept-duplicate-risk
# 修复群权限后恢复绑定
sudo -u qbot .venv/bin/python -m qbot resume
sudo systemctl start qbot
~~~

重启时 sending 转为 uncertain。异常退出后如尚未恢复，请先启动一次再停服核查。failed/uncertain 队首会阻塞后续 commit，防止越过未完成提交；命令回复仍能处理。失败入站事件会阻塞之后的入站解析。

换群前核查所有 uncertain，检查 events 并处理所有入站积压，入站未完成时拒绝解绑。下列命令**丢弃旧群所有未完成出站消息**，不会将旧群积压转发新群；已解析 SHA 保留去重。

~~~bash
sudo systemctl stop qbot
sudo -u qbot .venv/bin/python -m qbot unbind --discard-pending
sudo systemctl start qbot
sudo -u qbot .venv/bin/python -m qbot bind-token
~~~

### 完整性和平台边界

- GitHub 未送达：Recent Deliveries 手动 Redeliver。GitHub 不保证自动重投；无自动轮询兜底。已持久化但解析失败的事件应 retry-event，重投不会重置失败状态。
- 达到 2048 条提交时，使用 Compare API 分页补全并核对总量、SHA 唯一性、末端 SHA 和原 payload 包含关系；不完整则保留 failed，不静默截断。
- 超大新建分支缺乏可靠 before，会记录 large_new_branch_requires_manual_review 并暂停之后解析，避免把全仓库历史发出去。这种罕见情况需人工核对，当前版本不提供绕过完整性检查的按钮。
- GitHub 超大 payload 或极端批量分支推送可能根本不生成事件；未送达的事件无法保证被捕获。
- 远程发送和本地记账没有分布式事务，不能严格保证恰好一次。发送超时、5xx、异步响应和响应不明均进入 uncertain；仅明确未发送的连接失败、取 token 错误和限频等安全情况自动重试。
- QQ 群主动消息当前有单群 20/分钟、每日 1000 条等限制，受机器人/群权限影响。默认间隔 4 秒，限频退避保留队列；权限、禁言、通知关闭和审核不能绕过。

## 7. 备份、更新和测试

运行时可通过 SQLite backup API 做一致性备份，目标必须为新文件：

~~~bash
sudo -u qbot .venv/bin/python -m qbot backup /opt/qbot/data/backup-2026-10-26.sqlite3
~~~

不要只复制 WAL 模式的主库。备份与 .env 单独加密保管，定期监控磁盘和清理旧备份；本服务不自动删除去重表和队列记录。恢复时停服，归档原主库及 -wal、-shm，把备份恢复到原路径并设置 qbot 所有权，不能混用旧 WAL。恢复旧备份可能丢失后续去重记录，需核对群消息以防重复。

更新时停服、备份、更新代码、重新安装依赖，保留 .env 与 data，不重跑 baseline。未来不兼容 schema 更新需独立迁移，不能删库解决。requirements.txt 记录本次测试的直接依赖版本；部署希望保持这些版本时，先安装 .venv/bin/python -m pip install -r requirements.txt，再安装项目。

~~~bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
~~~

测试使用临时库和 Mock HTTP，不访问真实 QQ/GitHub。真实验收还需：两个平台回调成功、未签名请求被拒绝、.env 不可被下载；推送两个 commit 收两条；重投不重复；跨分支 SHA 去重；重启保留状态；队列无 failed/uncertain；日志无密钥或私有正文。

## 官方参考

- [QQ 访问凭证](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)
- [QQ Webhook](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html)
- [QQ Ed25519 签名](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html)
- [QQ 群消息发送](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)
- [QQ 单聊消息发送](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)
- [QQ 单聊事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)
- [GitHub Webhook 事件](https://docs.github.com/en/webhooks/webhook-events-and-payloads#push)
- [GitHub Compare API](https://docs.github.com/en/rest/commits/commits#compare-two-commits)

文档核对日期：2026-10-26。菜单和额度可能调整，以平台权限与接口实际返回为准。未提供真实凭证前只能自动化测试，不能视为已完成真实联调。

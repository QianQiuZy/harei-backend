# harei-backend

## 项目简介
基于 FastAPI + SQLAlchemy（MySQL）+ Redis 的后端服务，提供认证、留言箱、音乐、黄豆排行、标签、舰长与舰礼等 API。

## 环境要求
- Python 3.11+
- MySQL 5.7.44、8.4.13 或 9.7.4，使用 InnoDB 和 utf8mb4。
- Redis 6+
- libheif

## 系统依赖安装（HEIF/HEIC 解码）
- CentOS/RHEL/OpenCloudOS
  ```bash
  sudo yum install -y libheif libheif-devel
  ```
- Debian/Ubuntu
  ```bash
  sudo apt-get install -y libheif1 libheif-dev
  ```

## 安装依赖
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置环境变量
```bash
cp env.example .env
```
按需修改 `.env` 中的数据库、Redis 与认证配置。生产部署设 `APP_ENV=production`，首次启用账号功能前生成至少 32 字节的随机 `APP_SECRET_KEY` 并持久保存。已有地址使用该密钥派生加密时，换钥前须迁移旧密文。

如需月底在舰列表邮件增加抄送，可配置 `EMAIL_CC`（多个邮箱使用英文逗号分隔）。

### 管理账号权限

- `AUTH_USERNAME` / `AUTH_PASSWORD_HASH`：主后台账号，可通过 `/login` 与 `/music-manage/login` 登录，均获得 `admin` 和 `music:manage` 权限，使用 `TOKEN_TTL_SECONDS`。
- `MUSIC_AUTH_USERNAME` / `MUSIC_AUTH_PASSWORD_HASH`：音乐管理账号，仅可通过 `/music-manage/login` 登录，获得 `music:manage` 权限；使用 `MUSIC_TOKEN_TTL_SECONDS`，未设置时沿用 `TOKEN_TTL_SECONDS`。不能登录主后台或访问要求 `admin` 权限的接口。

## B站直播监听

监听器默认关闭。生产环境必须在 `.env` 中设置：

```bash
BILI_MONITOR_ENABLED=true
BILI_ROOM_IDS=1820703922
BILI_SESSDATA=
BILI_BILI_JCT=
BILI_DEDEUSERID=
BILI_DEDEUSERID_CKMD5=
BILI_SID=
BILI_BUVID3=
BILI_DEVICE_FINGERPRINT=
```

启动日志应依次出现“所有 UID 已成功获取”、“后台监听已启动”和“已连接房间”。`GET /live/status` 返回的是当前进程的内存状态，因此运行监听器时应使用单个 Uvicorn worker，避免多个进程重复连接和状态不一致。

项目根目录中的 `blivedm/` 是随服务部署的 vendored 副本；当前消息协议同步自 `VR_douchong/blivedm`，包括 `SEND_GIFT_V2` 支持。

### 月底在舰名单与邮件

- 按北京时间，每月最后一天 **23:55** 抓取目标房间 `1820703922` 的完整陪伴榜，包括独立返回的 `top3`，按 UID 去重并校验接口总人数。
- 31 天的月份，在 **30 号 23:55** 额外抓取一次。31 号合并两份名单，重复 UID 使用最新昵称和两天中较高的档位。
- 最后一天 **23:55（含）至当日结束** 收到的目标房间 `USER_TOAST_MSG_V2` 实付上舰事件也会缓存，抓取完成及发信前各合并一次，同 UID 保留最高档位。零价事件保留在上舰流水中。
- Redis 使用 `harei:guard-report:1820703922:YYYYMM:*` 键，保存 30 号快照、月底快照、晚间付费事件和发送状态，保留 90 天。实际采样的在舰名单另存于 `download_files/.snapshots/`，供结算在 Redis 快照过期或丢失后恢复。
- 次月 1 号 **00:01** 发送在舰名单与当月上舰记录。抓取和发送独立调度，发送会等待进行中的快照；重启后可恢复 Redis 快照/本地附件，并从附件内容计算人数，不依赖内存计数。成功发送状态用于避免重启后重复发送。
- 分页失败会重试，人数不符会整轮重抓；失败不写入半份快照。31 天月份缺少 30 号快照，或次月缺少月底快照时，记录错误并停止该份报告，**不以次月实时名单替代上月快照**。需在 30 号任务之前部署并保证 Redis 与监听器运行。
- 邮件失败会重试最多 3 次；启动或重启仅等待下一次计划发送时间，不补发历史月份的报告。SMTP 已接收但发送状态尚未写入 Redis 时发生异常，任务重试仍可能导致重复邮件，不能保证严格恰好发送一次。

相关回归测试（不读取真实配置或发送真实邮件）：

```bash
.venv/bin/python -m pytest -q tests/test_bili_guard_report.py
```

本机安装 `redis-server` 时，测试还会启动一个无 TCP 端口、无磁盘持久化的隔离 Redis 实例验证缓存读写；未安装时仅跳过该项。

## 运行服务（Uvicorn）
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
```

## 目录结构
- `app/api/`：API 路由模块
- `app/models/`：SQLAlchemy ORM 模型
- `app/schemas/`：Pydantic 请求/响应模型
- `app/core/`：配置与 Redis 连接
- `app/db/`：数据库连接与会话

## 说明
- Token 存储于 Redis，仅用于鉴权与会话管理。
- 认证凭据来源于 `.env` 中的 `AUTH_USERNAME` 与 `AUTH_PASSWORD_HASH`。

## 普通账号与舰礼发放

个人中心位于 `/personal`，注册和申诉入口位于 `/account/register` 和 `/account/appeal`。主后台从 `/admin/member-gifts` 管理月度收集，`/admin/member-gifts/records` 处理申诉并查看操作记录。整月名单发往后端 `GIFT_ROSTER_RECIPIENT` 配置的邮箱，默认是 `harei0301@126.com`，已填写和未填写分表。收集和全部补填期结束后可下载本月单号模板，编辑「寄送单号」列并上传。普通用户使用 HttpOnly Cookie 登录，管理接口使用现有主后台 Bearer Token。个人资料和导出响应均设置为私有且禁止缓存。

注册与重置密码的验证码按客户端 IP 共享每天 10 条。验证原邮箱、验证新邮箱和注销共用每个账号每天 10 条的额度。换绑保留账号 ID 和当日用量。每天北京时间 00:00 重置，单邮箱仍须间隔 60 秒。每日额度按受理的验证码请求计数。重复点击被冷却拒绝不占用次数。找回未注册邮箱也返回统一受理结果，已进入投递流程的请求保留额度。超限返回 HTTP 429，`Retry-After` 为等待秒数。

留言箱、验证码发送和请求限速使用同一 Redis 实例的 DB 2。后台 Bearer Token 和验证码内容沿用 `REDIS_URL` 的库号。启动时自动迁移原库内的留言箱计数和旧请求限速键，保留有效期。反向代理应追加可信的 `X-Forwarded-For`，并在 `TRUSTED_PROXY_HOSTS` 列出应用的直接代理。只有在入口校验 EdgeOne 来源并覆盖客户端传入的 `Eo-Connecting-Ip` 时，才将该入口也加入 `TRUSTED_EDGEONE_PROXY_HOSTS`。启动命令使用 `--no-proxy-headers`，由应用统一识别连接对端和转发链。

主后台和音乐后台登录共用 DB 2 的 IP 限额。每个 IP 每天可失败 10 次，成功登录不占额度。达到上限后的请求返回 HTTP 412 和 `Retry-After`，北京时间次日零点恢复。

| Redis 库 | 数据 | 有效期 |
| --- | --- | --- |
| DB 3 | 加密账号资料和密码校验值、邮箱与已验证 UID 的登录索引 | 写入或回填起 30 天 |
| DB 4 | 加密的默认地址、UID 地址和个人舰礼寄送状态 | 写入或回填起 30 天，正式截止与补填截止时重新核算 |
| DB 5 | 普通用户 Cookie 对应的服务端会话 | 由 `TOKEN_TTL_SECONDS` 控制，现有会话迁移时保留剩余时长 |
| DB 6 | 歌曲列表、详情、导出 JSON、公开标签、下载目录和舰礼留档目录 | 写入或回填起 30 天 |

MySQL 保存业务主数据。读取热数据直接访问 Redis。修改业务数据时，事务先封住旧缓存，提交成功后发布新值并刷新有效期。歌单修改同步更新最近缓存的列表、详情和导出，其余筛选与分页在下次读取时按新版本回填。公开数据在 Redis 临时不可用时可从 MySQL 读取。涉及热数据的修改若无法封住旧值，事务回滚。实时黄豆计数与直播状态沿用各自的数据源。手工执行 SQL 或从备份恢复时，应同时清理相应业务热缓存，数据库外部写入无法触发应用中的刷新。分库共用同一 Redis 实例的内存和淘汰策略，缓存连接需要 `INFO server` 权限识别实例重启。

普通账号使用 PBKDF2-HMAC-SHA256、独立 16 字节随机盐和 600,000 次迭代。已有普通账号用原密码成功登录时自动迁移，管理账号继续使用配置的 Argon2id。MySQL 连接池默认每个进程常驻最多 5 个连接，可临时增加 3 个，等待 5 秒后返回 HTTP 503，连接回收时间为 1800 秒。可用 `MYSQL_POOL_SIZE`、`MYSQL_MAX_OVERFLOW`、`MYSQL_POOL_TIMEOUT` 和 `MYSQL_POOL_RECYCLE` 调整。部署时按所有进程的合计连接上限给 MySQL 预留余量。

完整寄送地址仅在所属账号的个人接口和寄给配置收件人的 Excel 附件中提供。管理名单显示填写状态，单号模板只含月份、UID、昵称、原单号和寄送单号。地址附件在内存中生成。

新建默认库 `harei` 时运行 `db.sql`，该文件内的 `USE harei` 会切换数据库。升级已有库时，在面板同一连接中先用 `USE` 选中后端 `MYSQL_DATABASE` 指定的库，再用 `SELECT DATABASE()` 核对库名，随后依次执行 [001_member_gifts.sql](migrations/001_member_gifts.sql)、[002_binding_names_and_address_refill.sql](migrations/002_binding_names_and_address_refill.sql)、[003_member_address_autofill.sql](migrations/003_member_address_autofill.sql)、[004_returned_address.sql](migrations/004_returned_address.sql)、[005_hot_cache_fences.sql](migrations/005_hot_cache_fences.sql)、[006_live_gift_eligibility.sql](migrations/006_live_gift_eligibility.sql) 和 [007_captain_startup_backfill.sql](migrations/007_captain_startup_backfill.sql)。这些脚本均可重复执行。首次执行时记录北京时间的上线月。

从上线月起，目标房间的上舰消息按北京时间归月。实付金瓜子数大于零时立即建立该 UID 的当月资格，即使尚未设置截止日也可填写地址。零价盲盒及缺失付款金额的事件只记入上舰流水。月底 23:55 采样，31 天的月份取 30、31 日两份在舰名单的并集和各 UID 的最高档位。次月 1 日零点后合并付费与在舰资格，每个 UID 每月一份。已发货后自动升档会保留原单号并标记需人工补寄，上传新的补寄单号后解除标记。每次启动按北京时间本月读取一次 `captains.joined_month`，按 UID 合并到已有资格。迁移标记的旧版记录统一视为本房间付费上舰。新版流水保存房间号与实际单价，金额为零、缺失金额和其他房间的记录均排除在付费资格之外。

后端 `.env` 中启用监听并配置目标房间和 Cookie。注册邮箱验证码和 8 位纯数字 UID 弹幕验证码均有效 10 分钟，重发间隔 60 秒。用户邮件只投给对应收件人。正式截止前 48 小时，只有关闭自动填写且有当月舰礼尚未提交地址的账号会收到提醒。同一邮箱合并一封，投递时再核对自动填写设置及每个 UID 的提交状态。邮件中的个人中心链接使用 `MEMBER_SITE_URL`。自动填写地址默认开启，依次使用 UID 专属地址和账号默认地址。关闭后可单独为某份舰礼选用默认地址或手动填写。收件地址及冻结快照在数据库内加密。可设置独立的 Fernet 密钥 `GIFT_ADDRESS_KEY`，未设置时从 `APP_SECRET_KEY` 派生。备份时保留相应密钥，更换前需要迁移已加密的收件信息。发货后完整地址的保留期限待确定。

账号验证码、舰礼地址填写提醒和月度在舰报表共用舰礼发信配置。修改正式截止时间会重新安排尚未发送的提醒，同月已提醒的账号只收一封。默认通过 `gz-smtp.qcloudmail.com:465` 使用 TLS，以 `noreply@harei.cn` 登录和发件。将此邮箱的 SMTP 密码填在后端私有 `.env` 的 `GIFT_SMTP_PASS` 中。直播监听的 Cookie 告警使用原有 `SMTP_*` 配置。以 `noreply@harei.cn` 发件或登录的所有 SMTP 入口通过 Redis DB 2 共享限额。任意连续一秒最多投递 20 个收件人，抄送计入限额。默认使用应用的 Redis。多个环境同时使用这个发件账号时，可设置相同的 `GIFT_MAIL_RATE_REDIS_URL` 共享发信额度。

### 本地 Docker 联调

前后端仓库放在同级目录。每轮本地修改完成后，在后端根目录运行以下命令并保留服务，供浏览器继续测试。

```sh
bash docker/start-local.sh
docker compose --env-file /dev/null -f compose.local.yaml ps
```

打开 `http://localhost:3000/personal` 或 `http://localhost:3000/admin/member-gifts`。后端监听 `http://localhost:6555`。Compose 只将这两个端口发布到本机。MySQL 和 Redis 在容器内使用本地回环的 3306、6379，数据保存到项目专用卷。初始化进程从后端的 `.env` 读取数据库名称、普通数据库用户和密码，后端只读挂载同一文件。SMTP 与直播监听均取该文件中的配置。Next.js 的服务器端代理通过同一网络命名空间直连 `127.0.0.1:6555`。

在独立的临时 MySQL、Redis、SMTP 与 Mailpit 中可运行 `tests/test_member_gifts_integration.py`。测试使用模拟的弹幕回调和可控的北京时间，覆盖从注册、双 UID、月度结算、改期和宽限到导出、单号及注销的完整流程。

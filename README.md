# harei-backend

## 项目简介
基于 FastAPI + SQLAlchemy（MySQL）+ Redis 的后端服务，提供认证、留言箱、音乐、黄豆排行、标签、舰长与舰礼等 API。

## 环境要求
- Python 3.11+
- MySQL 8.0+
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
按需修改 `.env` 中的数据库、Redis 与认证配置。

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

- 按服务进程的本地时间（生产环境应使用北京时间），每月最后一天 **23:55** 抓取目标房间 `1820703922` 的完整陪伴榜，包括独立返回的 `top3`，按 UID 去重并校验接口总人数。
- 31 天的月份，在 **30 号 23:55** 额外抓取一次并存入 Redis；31 号将两次完整抓取的名单合并，重复 UID 采用 31 号的姓名、等级。因此该名单是双日并集，不是单一时刻的实时在舰人数。
- 最后一天 **23:55（含）至当日结束** 收到的目标房间 `USER_TOAST_V2` 上舰事件也会缓存，在抓取完成及邮件发送前各合并一次；同 UID 的后续事件更新姓名、等级，次月事件不加入上月名单。
- Redis 使用 `harei:guard-report:1820703922:YYYYMM:*` 键，保存 30 号快照、月底快照、晚间事件和发送状态，保留 90 天。沿用已有的 `REDIS_URL`，无需新增配置。
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
uvicorn app.main:app --host 0.0.0.0 --port 8000
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

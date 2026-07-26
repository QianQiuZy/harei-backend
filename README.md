# harei-backend

## 项目简介
基于 FastAPI + SQLAlchemy（MySQL）+ Redis 的后端服务，提供认证、留言箱、音乐、黄豆排行、标签、舰长与舰礼等 API。

## Music catalog

每次发布后端代码、重启服务前，必须先在相同 `.env` 环境中执行数据库升级：

```bash
alembic upgrade head
```

迁移支持已有 `songs` 数据但缺少修订表或审计表的数据库，会补齐 `music_catalog_revision` 和 `music_audit_events`，不会删除现有歌曲。曲库使用规范化的歌曲和演出记录表，修订号与审计记录均存储在数据库中。

如果数据库只有旧版 `music` 表而没有 `songs`，迁移会保留旧表，但不会自动转换旧数据；发布前应先按 `data/README.md` 完成曲库导入。

`POST /login` grants `admin` and `music:manage`; `POST /music-manage/login` grants only `music:manage`. Configure `MUSIC_AUTH_USERNAME`, `MUSIC_AUTH_PASSWORD_HASH`, and optionally `MUSIC_TOKEN_TTL_SECONDS`.

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

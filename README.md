# harei-backend

## 项目简介
基于 FastAPI + SQLAlchemy（MySQL）+ Redis 的后端服务，提供认证、留言箱、音乐、黄豆排行、标签、舰长与舰礼等 API。

## 环境要求
- Python 3.11+
- MySQL 8.0+
- Redis 6+

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

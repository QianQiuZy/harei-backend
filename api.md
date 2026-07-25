# Harei API 清单

> 说明：除特别标注外，所有需要鉴权的接口使用 `Authorization: Bearer <token>` 请求头。

## 认证
### POST `/login`（无需 Token）
**请求体**
```json
{
  "username": "string",
  "password": "string"
}
```
**响应**
```json
{
  "code": 0,
  "token": "string",
  "user": { "username": "string" },
  "scopes": ["admin", "music:manage"],
  "expires_at": "2026-07-26T12:00:00Z"
}
```

### POST `/logout`（需要 Token）
**响应**
```json
{ "code": 0, "success": true }
```

### GET `/auth`（需要 Token）
**响应**
```json
{
  "code": 0,
  "authenticated": true,
  "user": { "username": "string" },
  "scopes": ["admin", "music:manage"],
  "expires_at": "2026-07-26T12:00:00Z"
}
```

## 留言箱 /box
### POST `/box/uploads`（无需 Token）
**表单字段**
- `message`：string，必填
- `tag`：string，必填
- `files`：file[]，可选，最多 6 张；单个文件最大 10 MiB

**请求限制**
- 完整 multipart 请求体最大 50 MiB

**限速**
- 同一 IP 限速：30 秒内最多 1 次、1 小时内最多 3 次、每天最多 5 次
- 无法获取真实客户端 IP 时（`0.0.0.0`）不限速
- 缺少字段的请求同样计入限速

**响应**
```json
{
  "code": 0,
  "message_id": 1,
  "image_ids": [1, 2]
}
```

**超速响应**
```json
{
  "detail": {
    "retry_at": 1719999999
  }
}
```

**缺少字段响应**
```json
{
  "detail": {
    "missing_fields": ["message", "tag"]
  }
}
```

### GET `/box/image/original?path=...`（需要 Token）
**响应**：图片文件

### GET `/box/image/thumb?path=...`（需要 Token）
**响应**：图片文件

### GET `/box/image/jpg?path=...`（需要 Token）
**响应**：图片文件

### GET `/box/pending`（需要 Token）
**响应**
```json
{
  "code": 0,
  "items": [
    {
      "id": 1,
      "created_at": "2024-01-01T00:00:00Z",
      "msg": "string",
      "tag": "string",
      "images": ["uploads/original/xxx.png"],
      "images_thumb": ["uploads/thumbs/xxx.jpg"],
      "images_jpg": ["uploads/jpg/xxx.jpg"]
    }
  ]
}
```

### GET `/box/approved`（需要 Token）
响应结构同 `/box/pending`。

### POST `/box/approve`（需要 Token）
**请求体（可选）**
```json
{ "tag": "string" }
```
> 说明：不传请求体（或不传 `tag`）时，保持原行为（全量过审 `pending`）；传 `tag` 时仅过审该标签下 `pending` 消息。

**响应**
```json
{ "code": 0, "message": "X条消息已过审" }
```

### POST `/box/delete`（需要 Token）
**请求体**
```json
{ "id": 1 }
```
**响应**
```json
{ "code": 0, "message": "id1已删除" }
```

### POST `/box/archived`（需要 Token）
**请求体（可选）**
```json
{ "tag": "string" }
```
> 说明：不传请求体（或不传 `tag`）时，保持原行为（全量归档 `approved`）；传 `tag` 时仅归档该标签下 `approved` 消息。

**响应**
```json
{ "code": 0, "message": "X条消息已归档" }
```

## 下载 /download
### GET `/download/active`（无需 Token）
**响应**
```json
{
  "code": 0,
  "items": [
    {
      "download_id": 1,
      "description": "string",
      "path": "https://example.com/file.zip"
    }
  ]
}
```
> 说明：`path` 仅返回外部可访问链接，内部文件会返回 `/download/file?download_id=...` 形式的链接。

### GET `/download/file?download_id=...`（无需 Token）
**响应**：文件内容（仅支持内部路径）

### POST `/download/add`（需要 Token）
**请求体（JSON）**
```json
{ "description": "string", "path": "https://example.com/file.zip" }
```

**请求体（表单，上传文件）**
- `description`：string，必填
- `file`：file，必填

**响应**
```json
{ "code": 0, "download_id": 1, "path": "download_files/xxx.zip" }
```

## 音乐
### GET `/music`（无需 Token）
**查询参数**
- `q`：string，可选，搜索关键词
- `search_mode`：`title` 或 `artist`，默认 `title`
- `genre`：string，可选
- `language`：string，可选
- `work_type`：string，可选
- `sort`：`title`、`recent` 或 `count`，默认 `title`
- `order`：`asc` 或 `desc`，默认 `asc`
- `page`：int，默认 1
- `page_size`：int，默认 30，最大 1000

> 缓存：响应包含 `ETag`；请求头 `If-None-Match` 与当前版本一致时返回 `304`。

**响应**
```json
{
  "code": 0,
  "items": [
    {
      "song_id": 1,
      "id": "song_123",
      "source_key": "song_123",
      "title": "string",
      "artist": "string",
      "artists": ["string"],
      "genre": "华语流行",
      "language": "string",
      "workType": "翻唱",
      "notes": "",
      "metadataStatus": "complete",
      "latestPerformanceAt": "2026-07-25",
      "latestLink": "https://www.bilibili.com/video/BV1...",
      "performanceCount": 1
    }
  ],
  "total": 478,
  "page": 1,
  "page_size": 30,
  "facets": { "genres": [], "languages": [], "workTypes": [] },
  "stats": { "song_count": 478, "performance_count": 2685 },
  "revision": 0
}
```

### GET `/music/{source_key}`（无需 Token）
**响应**
```json
{
  "code": 0,
  "item": {
    "song_id": 1,
    "source_key": "song_123",
    "title": "string",
    "artist": "string",
    "performances": []
  }
}
```

### GET `/music/export`（无需 Token）
**说明**：导出当前公开歌单完整快照，响应包含 `ETag`。

**响应**
```json
{
  "code": 0,
  "schemaVersion": 1,
  "generatedAt": "2026-07-26T12:00:00Z",
  "revision": 1,
  "songs": []
}
```

## 音乐管理 /music-manage
> 说明：除登录外均需要含 `music:manage` scope 的 Token。歌曲及演出记录变更必须提交当前歌曲 `version`；版本过期返回 `409`。

### POST `/music-manage/login`（无需 Token）
**请求体**
```json
{
  "username": "string",
  "password": "string"
}
```

**响应**
```json
{
  "code": 0,
  "token": "string",
  "user": { "username": "string" },
  "scopes": ["music:manage"],
  "expires_at": "2026-07-26T12:00:00Z"
}
```

### GET `/music-manage/stats`（需要 music:manage Token）
**响应**
```json
{
  "code": 0,
  "activeSongs": 478,
  "archivedSongs": 0,
  "performanceCount": 2685,
  "revision": 1
}
```

### GET `/music-manage/songs`（需要 music:manage Token）
**查询参数**
- `q`：string，可选
- `status`：string，可选
- `page`：int，默认 1
- `page_size`：int，默认 30，最大 100

### POST `/music-manage/songs`（需要 music:manage Token）
> `source_key` 由服务端自动生成，格式为 `song_<uuid>`，客户端无需提交。

**请求体**
```json
{
  "title": "string",
  "artist": "string",
  "artists": ["string"],
  "genre": "string",
  "language": "string",
  "work_type": "翻唱",
  "notes": "",
  "metadata_status": "complete"
}
```

### GET `/music-manage/songs/{song_id}`（需要 music:manage Token）
**响应**：歌曲详情、当前 `version` 及演出记录。

### PUT `/music-manage/songs/{song_id}`（需要 music:manage Token）
**请求体**：`version` 必填；其余歌曲字段按需提交。

**响应**
```json
{ "code": 0, "version": 2, "revision": 2 }
```

### POST `/music-manage/songs/{song_id}/archive`（需要 music:manage Token）
### POST `/music-manage/songs/{song_id}/restore`（需要 music:manage Token）
**请求体**
```json
{ "version": 2 }
```

### POST `/music-manage/songs/{song_id}/performances`（需要 music:manage Token）
### PUT `/music-manage/performances/{performance_id}`（需要 music:manage Token）
> 演唱记录 `source_key` 由服务端自动生成。`stream_id` 从 `clip_url` 或 `stream_url` 自动识别 BV 号或哔哩哔哩直播间 ID，客户端无需提交。

**请求体**
```json
{
  "version": 2,
  "date": "2026-07-26",
  "platform": "哔哩哔哩",
  "stream_title": "string",
  "stream_url": "https://example.com/live",
  "clip_url": "https://example.com/video"
}
```

### GET `/music-manage/performances/template`（需要 music:manage Token）
**说明**：下载演唱记录 XLSX 导入模板。模板包含 `导入数据` 和 `歌曲列表` 两个工作表；`歌曲列表` 包含当前数据库中的全部歌曲。

**响应**：XLSX 文件

### POST `/music-manage/performances/import`（需要 music:manage Token）
**表单字段**
- `file`：`.xlsx` 文件，最大 5 MiB

`导入数据` 表头必须依次为 `歌名`、`日期`、`直播标题`、`歌切链接`。歌名按数据库标题完全匹配；未知或重名歌曲、无效日期、无效链接及文件内重复记录均返回逐行错误。整份文件使用同一事务，任一行失败时不会写入任何记录。

**成功响应**
```json
{
  "code": 0,
  "imported_count": 2,
  "affected_song_count": 1,
  "revision": 3
}
```

**校验失败响应（422）**
```json
{
  "detail": {
    "error": "invalid_workbook",
    "errors": [
      {
        "row": 3,
        "field": "歌名",
        "code": "SONG_NOT_FOUND",
        "message": "数据库中不存在完全同名歌曲"
      }
    ]
  }
}
```

### DELETE `/music-manage/performances/{performance_id}?version=...`（需要 music:manage Token）
**查询参数**
- `version`：int，必填，当前歌曲版本

### GET `/music-manage/audit`（需要 music:manage Token）
**查询参数**
- `page`：int，默认 1
- `page_size`：int，默认 50，最大 100

**响应**：按时间倒序返回审计记录、总数及分页信息。

## 黄豆排行 /huangdou
### GET `/huangdou/rank`（无需 Token）
**响应**
```json
{
  "code": 0,
  "items": [
    { "uid": "string", "name": "string", "count": 100 }
  ]
}
```

### GET `/huangdou/uid?uid=...`（无需 Token）
**响应**
```json
{ "code": 0, "uid": "string", "name": "string", "count": 100 }
```

## 标签 /tag
### GET `/tag/active`（无需 Token）
**响应**
```json
{ "code": 0, "items": ["tag1", "tag2"] }
```

### POST `/tag/add`（需要 Token）
**请求体**
```json
{ "tag_name": "string" }
```
**响应**
```json
{ "code": 0, "message": "ok" }
```

### GET `/tag/all`（需要 Token）
**响应**
```json
{
  "code": 0,
  "items": [
    {
      "tag_id": 1,
      "tag_name": "string",
      "status": "approved",
      "expires_at": null,
      "created_at": "2024-01-01T00:00:00Z"
    }
  ]
}
```

### POST `/tag/archived`（需要 Token）
**请求体**
```json
{ "tag_name": "string" }
```
**响应**
```json
{ "code": 0, "message": "ok" }
```

## 舰长 /captains
### GET `/captains`（需要 Token）
**查询参数**
- `month`：YYYYMM，可选
- `uid`：string，可选（优先级高于 month）

**响应**
```json
{
  "code": 0,
  "items": [
    {
      "uid": "string",
      "name": "string",
      "level": "舰长",
      "count": 1,
      "red_packet": false,
      "joined_at": "2024-01-01T00:00:00Z"
    }
  ]
}
```

### GET `/captains/xlsx?month=YYYYMM`（需要 Token）
**说明**：按月份下载整理好的舰长 XLSX 文件。

**响应**：XLSX 文件

**失败响应**
```json
{ "detail": "未找到YYYYMM上舰记录" }
```

## 舰礼 /captaingift
### GET `/captaingift`（无需 Token）
**响应**
```json
{
  "code": 0,
  "items": [
    { "month": "202512", "path": "uploads/captaingift/202512.jpg" }
  ]
}
```

### GET `/captaingift/image?month=YYYYMM`（无需 Token）
**响应**：图片文件

### POST `/captaingift/add`（需要 Token）
**表单字段**
- `month`：YYYYMM
- `file`：图片文件（单张）

**响应**
```json
{ "code": 0, "message": "202512已上传" }
```

## 直播监控 /live
### GET `/live/status`（无需 Token）
**响应**
```json
{
  "status": 1,
  "live_time": "2024-01-01 12:00:00",
  "title": "string"
}
```

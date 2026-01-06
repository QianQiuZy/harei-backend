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
  "user": { "username": "string" }
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
  "user": { "username": "string" }
}
```

## 留言箱 /box
### POST `/box/uploads`（无需 Token）
**表单字段**
- `message`：string，可选
- `tag`：string，可选
- `files`：file[]，可选（支持多图）

**响应**
```json
{
  "code": 0,
  "message_id": 1,
  "image_ids": [1, 2]
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
**响应**
```json
{ "code": 0, "message": "X条消息已归档" }
```

## 音乐
### GET `/music`（无需 Token）
**响应**
```json
{
  "code": 0,
  "items": [
    {
      "music_id": 1,
      "title": "string",
      "artist": "string",
      "type": "string",
      "language": "string",
      "note": "string"
    }
  ]
}
```

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

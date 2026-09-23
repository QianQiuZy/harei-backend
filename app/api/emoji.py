from pathlib import Path
from typing import TypedDict

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

router = APIRouter(prefix="/emoji")
EMOJI_ROOT = Path("emoji_assets")
IMAGE_SUFFIXES = {".apng", ".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}


class EmojiGroupsResponse(TypedDict):
    code: int
    groups: dict[str, list[str]]
    files: dict[str, dict[str, str]]
    group_icons: dict[str, str]


@router.get("", response_model=EmojiGroupsResponse)
def list_emojis() -> EmojiGroupsResponse:
    groups: dict[str, list[str]] = {}
    files: dict[str, dict[str, str]] = {}
    group_icons: dict[str, str] = {}
    if EMOJI_ROOT.is_dir():
        for group_path in sorted(EMOJI_ROOT.iterdir(), key=lambda path: path.name.casefold()):
            if not group_path.is_dir():
                continue
            image_paths = sorted(
                (
                    image_path
                    for image_path in group_path.iterdir()
                    if image_path.is_file() and image_path.suffix.lower() in IMAGE_SUFFIXES
                ),
                key=lambda path: path.name.casefold(),
            )
            names = sorted(
                {image_path.stem for image_path in image_paths},
                key=str.casefold,
            )
            if names:
                groups[group_path.name] = names
                files[group_path.name] = {}
                for image_path in image_paths:
                    if image_path.stem not in files[group_path.name]:
                        files[group_path.name][image_path.stem] = image_path.name

                icon_matches = sorted(
                    (
                        image_path
                        for image_path in EMOJI_ROOT.iterdir()
                        if image_path.is_file()
                        and image_path.stem == group_path.name
                        and image_path.suffix.lower() in IMAGE_SUFFIXES
                        and EMOJI_ROOT.resolve() in image_path.resolve().parents
                    ),
                    key=lambda path: path.name.casefold(),
                )
                if icon_matches:
                    group_icons[group_path.name] = icon_matches[0].name
    return {"code": 0, "groups": groups, "files": files, "group_icons": group_icons}


@router.get("/group-icon/{group_name}")
def get_group_icon(group_name: str) -> FileResponse:
    if group_name in {"", ".", ".."} or "/" in group_name or "\\" in group_name:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    root_dir = EMOJI_ROOT.resolve()
    group_dir = (EMOJI_ROOT / group_name).resolve()
    if root_dir not in group_dir.parents or not group_dir.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    matches = sorted(
        (
            path
            for path in root_dir.iterdir()
            if path.is_file()
            and path.stem == group_name
            and path.suffix.lower() in IMAGE_SUFFIXES
            and root_dir in path.resolve().parents
        ),
        key=lambda path: path.name.casefold(),
    )
    if not matches:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(matches[0])


@router.get("/{group_name}/{emoji_name}")
def get_emoji(group_name: str, emoji_name: str) -> FileResponse:
    if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in (group_name, emoji_name)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    group_dir = (EMOJI_ROOT / group_name).resolve()
    root_dir = EMOJI_ROOT.resolve()
    if root_dir not in group_dir.parents or not group_dir.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    matches = sorted(
        (
            path
            for path in group_dir.iterdir()
            if path.is_file()
            and path.stem == emoji_name
            and path.suffix.lower() in IMAGE_SUFFIXES
        ),
        key=lambda path: path.name.casefold(),
    )
    if not matches:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(matches[0])

#!/usr/bin/env python3
"""会话上下文管理器 - WPS 云文档后端版
在会话之间保存/恢复上下文快照，数据持久化到 WPS 云文档。"""

import sys
import os
import json
import hashlib
import tempfile
from datetime import datetime
from pathlib import Path

# ── kdocs 模块加载 ──────────────────────────────────────
_KDOCS_CANDIDATES = [
    os.path.join(os.getenv("SKILL_PATH", ""), "wps-docs", "scripts"),
    r"C:\Users\xiaox\AppData\Roaming\WPS 灵犀\serverdir\target_skills\wps-docs\scripts",
]
for _p in _KDOCS_CANDIDATES:
    if os.path.isfile(os.path.join(_p, "kdocs.py")):
        sys.path.insert(0, _p)
        break
import kdocs

# ── 常量 ────────────────────────────────────────────────
# WPS 云文档有两个云盘："自动上传文档"(API默认) 和 "我的云文档"(用户可见)
# 必须使用"我的云文档" drive，否则用户在 WPS 客户端看不到文件
MY_DRIVE_NAME = "我的云文档"
MY_DRIVE_ID = None  # 运行时自动解析
LOCAL_CACHE = Path.home() / ".lingxi-context"


def _resolve_my_drive_id():
    """解析"我的云文档"云盘 ID（kdocs 默认用"自动上传文档"云盘，用户不可见）"""
    global MY_DRIVE_ID
    if MY_DRIVE_ID:
        return MY_DRIVE_ID
    client = kdocs.WpsClient()
    resp = client._session.get(
        f"{client._v7_base}/v7/drives",
        params={"allotee_type": "user", "page_size": 10},
    )
    if resp.get("code") != 0:
        raise RuntimeError("无法获取云盘列表")
    items = (resp.get("data") or {}).get("items") or []
    for item in items:
        if item.get("name") == MY_DRIVE_NAME:
            MY_DRIVE_ID = item["id"]
            return MY_DRIVE_ID
    # fallback: 取第二个云盘（通常第一个是"自动上传文档"）
    if len(items) >= 2:
        MY_DRIVE_ID = items[1]["id"]
        return MY_DRIVE_ID
    raise RuntimeError(f"未找到'{MY_DRIVE_NAME}'云盘")


def _ensure_cloud_folder():
    """确保"我的云文档"下存在"会话上下文"文件夹，返回 folder_id"""
    did = _resolve_my_drive_id()

    # 在"我的云文档"根目录查找名为"会话上下文"的文件夹
    root_files = kdocs.list_folder_files("0", drive_id=did, filter_type="folder")
    if root_files.get("success"):
        for item in root_files.get("data", {}).get("items", []):
            if item.get("name") == "会话上下文" and item.get("type") == "folder":
                return item["file_id"]
        next_token = root_files.get("data", {}).get("next_page_token")
        while next_token:
            root_files = kdocs.list_folder_files("0", drive_id=did, page_token=next_token, filter_type="folder")
            for item in root_files.get("data", {}).get("items", []):
                if item.get("name") == "会话上下文" and item.get("type") == "folder":
                    return item["file_id"]
            next_token = root_files.get("data", {}).get("next_page_token")

    # 不存在则创建
    result = kdocs.create_folder("会话上下文", parent_id="0", drive_id=did)
    if result.get("success"):
        return result["data"]
    raise RuntimeError(f"无法创建云端文件夹: {result}")


def _gen_feature_code():
    """生成特征码: CTX-YYYYMMDD-XXXX"""
    date_str = datetime.now().strftime("%Y%m%d")
    hash_input = f"{datetime.now().isoformat()}{os.getpid()}"
    hash_hex = hashlib.md5(hash_input.encode()).hexdigest()[:4].upper()
    return f"CTX-{date_str}-{hash_hex}"


def _parse_download_path(result, save_dir):
    """从 download_file 结果中解析出实际保存路径"""
    msg = result.get("message", "")
    if "保存路径为:" in msg or "保存路径为：" in msg:
        sep = "保存路径为:" if "保存路径为:" in msg else "保存路径为："
        path = msg.split(sep, 1)[1].strip()
        return path
    # fallback: 用 save_dir + 文件名构造
    name = result.get("data", {}).get("name", "")
    if name:
        return os.path.join(save_dir, name)
    return None


# ── 命令实现 ─────────────────────────────────────────────

def cmd_save(args):
    """保存上下文快照到云端
    用法: python context_manager.py save <summary_file>
    """
    if not args:
        print("ERROR: 缺少参数。用法: save <summary_file>", file=sys.stderr)
        sys.exit(1)

    summary_file = args[0]
    if not os.path.exists(summary_file):
        print(f"ERROR: 文件不存在: {summary_file}", file=sys.stderr)
        sys.exit(1)

    with open(summary_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    code = _gen_feature_code()
    snapshot = {
        "feature_code": code,
        "created_at": datetime.now().isoformat(),
        "title": data.get("title", "未命名会话"),
        "tags": data.get("tags", []),
        "summary": data.get("summary", ""),
    }

    # 写入临时文件（以 特征码.json 命名，云端将保留此文件名）
    LOCAL_CACHE.mkdir(parents=True, exist_ok=True)
    temp_file = LOCAL_CACHE / f"{code}.json"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    # 上传到云端
    folder_id = _ensure_cloud_folder()
    result = kdocs.upload_file(str(temp_file), folder_id=folder_id)

    # 清理临时文件
    try:
        temp_file.unlink()
    except Exception:
        pass

    if result.get("success"):
        print(json.dumps({
            "status": "saved",
            "feature_code": code,
            "cloud_file_id": result["data"]["file_id"],
            "cloud_link": result["data"].get("link_url", ""),
            "message": "上下文已保存到 WPS 云文档",
        }, ensure_ascii=False))
    else:
        print(json.dumps({
            "status": "error",
            "message": f"上传失败: {result.get('message', '未知错误')}",
        }, ensure_ascii=False))
        sys.exit(1)


def cmd_restore(args):
    """从云端恢复上下文快照
    用法: python context_manager.py restore <feature_code>
    """
    if not args:
        print("ERROR: 缺少参数。用法: restore <feature_code>", file=sys.stderr)
        sys.exit(1)

    code = args[0].strip().upper()
    target_name = f"{code}.json"

    folder_id = _ensure_cloud_folder()
    files_result = kdocs.list_folder_files(folder_id)

    if not files_result.get("success"):
        print(json.dumps({
            "status": "error",
            "message": "无法列出云端文件",
        }, ensure_ascii=False))
        return

    # 在所有分页中查找匹配文件
    target_file_id = None
    items = files_result.get("data", {}).get("items", [])
    for item in items:
        if item.get("name") == target_name:
            target_file_id = item["file_id"]
            break

    # 处理分页
    next_token = files_result.get("data", {}).get("next_page_token")
    while not target_file_id and next_token:
        files_result = kdocs.list_folder_files(folder_id, page_token=next_token)
        items = files_result.get("data", {}).get("items", [])
        for item in items:
            if item.get("name") == target_name:
                target_file_id = item["file_id"]
                break
        next_token = files_result.get("data", {}).get("next_page_token")

    if not target_file_id:
        print(json.dumps({
            "status": "not_found",
            "message": f"未在云端找到特征码 '{code}' 对应的上下文快照",
        }, ensure_ascii=False))
        return

    # 下载到本地缓存
    LOCAL_CACHE.mkdir(parents=True, exist_ok=True)
    download_result = kdocs.download_file(target_file_id, save_dir=str(LOCAL_CACHE))

    if not download_result.get("success"):
        print(json.dumps({
            "status": "error",
            "message": f"下载失败: {download_result.get('message', '')}",
        }, ensure_ascii=False))
        return

    saved_path = _parse_download_path(download_result, str(LOCAL_CACHE))
    if not saved_path or not os.path.exists(saved_path):
        # fallback: 尝试用文件名直接查找
        saved_path = str(LOCAL_CACHE / target_name)

    with open(saved_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    # 清理下载的临时文件
    try:
        os.remove(saved_path)
    except Exception:
        pass

    print(json.dumps({
        "status": "found",
        "feature_code": snapshot["feature_code"],
        "created_at": snapshot["created_at"],
        "title": snapshot["title"],
        "tags": snapshot.get("tags", []),
        "summary": snapshot["summary"],
        "cloud_file_id": target_file_id,
    }, ensure_ascii=False))


def cmd_list(args):
    """列出云端所有上下文快照
    用法: python context_manager.py list
    """
    folder_id = _ensure_cloud_folder()
    files_result = kdocs.list_folder_files(folder_id)

    if not files_result.get("success"):
        print(json.dumps({
            "status": "error",
            "message": "无法列出云端文件",
        }, ensure_ascii=False))
        return

    snapshots = []
    items = files_result.get("data", {}).get("items", [])
    next_token = files_result.get("data", {}).get("next_page_token")

    all_items = list(items)
    while next_token:
        files_result = kdocs.list_folder_files(folder_id, page_token=next_token)
        all_items.extend(files_result.get("data", {}).get("items", []))
        next_token = files_result.get("data", {}).get("next_page_token")

    for item in all_items:
        name = item.get("name", "")
        if name.startswith("CTX-") and name.endswith(".json"):
            code = name.replace(".json", "")
            snapshots.append({
                "feature_code": code,
                "cloud_file_id": item["file_id"],
                "cloud_link": item.get("link_url", ""),
                "created_at": item.get("ctime", ""),
            })

    snapshots.reverse()  # 最新的排前面
    print(json.dumps({
        "status": "ok",
        "count": len(snapshots),
        "snapshots": snapshots,
    }, ensure_ascii=False))


def cmd_delete(args):
    """查找待删除的云端快照（kdocs 无删除 API，输出文件信息供后续处理）
    用法: python context_manager.py delete <feature_code>
    """
    if not args:
        print("ERROR: 缺少参数。用法: delete <feature_code>", file=sys.stderr)
        sys.exit(1)

    code = args[0].strip().upper()
    target_name = f"{code}.json"

    folder_id = _ensure_cloud_folder()
    files_result = kdocs.list_folder_files(folder_id)

    if not files_result.get("success"):
        print(json.dumps({
            "status": "error",
            "message": "无法列出云端文件",
        }, ensure_ascii=False))
        return

    items = files_result.get("data", {}).get("items", [])
    for item in items:
        if item.get("name") == target_name:
            print(json.dumps({
                "status": "found",
                "feature_code": code,
                "cloud_file_id": item["file_id"],
                "cloud_link": item.get("link_url", ""),
                "message": "kdocs 无删除 API，请通过 WPS 云文档界面手动删除，或使用 move_file 移到归档文件夹",
            }, ensure_ascii=False))
            return

    print(json.dumps({
        "status": "not_found",
        "message": f"未在云端找到特征码 '{code}' 对应的快照",
    }, ensure_ascii=False))


# ── 入口 ────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("会话上下文管理器（WPS 云文档后端）", file=sys.stderr)
        print("用法:", file=sys.stderr)
        print("  python context_manager.py save <summary_file>", file=sys.stderr)
        print("  python context_manager.py restore <feature_code>", file=sys.stderr)
        print("  python context_manager.py list", file=sys.stderr)
        print("  python context_manager.py delete <feature_code>", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]
    commands = {
        "save": cmd_save,
        "restore": cmd_restore,
        "list": cmd_list,
        "delete": cmd_delete,
    }
    if command not in commands:
        print(f"ERROR: 未知命令 '{command}'", file=sys.stderr)
        sys.exit(1)
    commands[command](args)


if __name__ == "__main__":
    main()

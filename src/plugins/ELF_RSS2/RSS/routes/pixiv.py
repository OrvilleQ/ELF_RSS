import re
import sqlite3

import httpx
from nonebot import logger
from pyquery import PyQuery as Pq
from tenacity import RetryError, TryAgain, retry, stop_after_attempt, stop_after_delay
from tinydb import Query, TinyDB

from ...config import DATA_PATH
from ..rss_class import Rss
from .Parsing import (
    ParsingBase,
    cache_db_manage,
    duplicate_exists,
    get_summary,
    write_item,
)
from .Parsing.check_update import get_item_date
from .Parsing.handle_images import (
    get_preview_gif_from_video,
    handle_img_combo,
    handle_img_combo_with_content,
)


# 如果启用了去重模式，对推送列表进行过滤
@ParsingBase.append_before_handler(priority=12, rex="pixiv")
async def handle_check_update(rss: Rss, state: dict):
    change_data = state.get("change_data")
    conn = state.get("conn")
    db = state.get("tinydb")

    # 检查是否启用去重 使用 duplicate_filter_mode 字段
    if not rss.duplicate_filter_mode:
        return {"change_data": change_data}

    if not conn:
        conn = sqlite3.connect(DATA_PATH / "cache.db")
        conn.set_trace_callback(logger.debug)

    await cache_db_manage(conn)

    delete = []
    for index, item in enumerate(change_data):
        summary = get_summary(item)
        try:
            summary_doc = Pq(summary)
            # 如果图片为动图，通过移除来跳过图片去重检查
            if re.search("类型：ugoira", str(summary_doc)):
                summary_doc.remove("img")
                summary = str(summary_doc)
        except Exception as e:
            logger.warning(e)
        is_duplicate, image_hash = await duplicate_exists(
            rss=rss,
            conn=conn,
            item=item,
            summary=summary,
        )
        if is_duplicate:
            write_item(db, item)
            delete.append(index)
        else:
            change_data[index]["image_hash"] = str(image_hash)

    change_data = [
        item for index, item in enumerate(change_data) if index not in delete
    ]

    return {
        "change_data": change_data,
        "conn": conn,
    }


# 处理图片
@ParsingBase.append_handler(parsing_type="picture", rex="pixiv")
async def handle_picture(
    rss: Rss, state: dict, item: dict, item_msg: str, tmp: str, tmp_state: dict
) -> str:

    # 判断是否开启了只推送标题
    if rss.only_title:
        return ""

    res = ""
    try:
        res += await handle_img(
            item=item,
            img_proxy=rss.img_proxy,
            img_num=rss.max_image_number,
        )
    except Exception as e:
        logger.warning(f"{rss.name} 没有正文内容！{e}")

    # 判断是否开启了只推送图片
    if rss.only_pic:
        return f"{res}\n"

    return f"{tmp + res}\n"


# 处理图片、视频
@retry(stop=(stop_after_attempt(5) | stop_after_delay(30)))
async def handle_img(item: dict, img_proxy: bool, img_num: int) -> str:
    if item.get("image_content"):
        return await handle_img_combo_with_content(
            item.get("gif_url"), item.get("image_content")
        )
    html = Pq(get_summary(item))
    link = item["link"]
    img_str = ""
    # 处理动图
    if re.search("类型：ugoira", str(html)):
        ugoira_id = re.search(r"\d+", link).group()
        try:
            url = await get_ugoira_video(ugoira_id)
            url = await get_preview_gif_from_video(url)
            img_str += await handle_img_combo(url, img_proxy)
        except RetryError:
            logger.warning(f"动图[{link}]的预览图获取失败，将发送原动图封面")
            url = html("img").attr("src")
            img_str += await handle_img_combo(url, img_proxy)
    else:
        # 处理图片
        doc_img = list(html("img").items())
        # 只发送限定数量的图片，防止刷屏
        if 0 < img_num < len(doc_img):
            img_str += f"\n因启用图片数量限制，目前只有 {img_num} 张图片："
            doc_img = doc_img[:img_num]
        for img in doc_img:
            url = img.attr("src")
            img_str += await handle_img_combo(url, img_proxy)

    return img_str


# 获取动图为视频
@retry(stop=(stop_after_attempt(5) | stop_after_delay(30)))
async def get_ugoira_video(ugoira_id: str) -> str:
    async with httpx.AsyncClient() as client:
        data = {"id": ugoira_id, "type": "ugoira"}
        response = await client.post("https://ugoira.huggy.moe/api/illusts", data=data)
        url = response.json().get("data")[0].get("url")
        if not url:
            raise TryAgain
        return url


# 处理来源
@ParsingBase.append_handler(parsing_type="source", rex="pixiv")
async def handle_source(
    rss: Rss, state: dict, item: dict, item_msg: str, tmp: str, tmp_state: dict
) -> str:
    source = item["link"]
    # 缩短 pixiv 链接
    str_link = re.sub("https://www.pixiv.net/artworks/", "https://pixiv.net/i/", source)
    return "链接：" + str_link + "\n"


# 检查更新
@ParsingBase.append_before_handler(rex="pixiv/ranking", priority=10)
async def handle_check_update(rss: Rss, state: dict):
    db = state.get("tinydb")
    change_data = await check_update(db, state.get("new_data"))
    return {"change_data": change_data}


# 检查更新
async def check_update(db: TinyDB, new: list) -> list:

    # 发送失败超过 3 次的消息不再发送
    to_send_list = db.search((Query().to_send.exists()) & (Query().count <= 3))

    if not new and not to_send_list:
        return []

    old_link_list = [i["link"] for i in db.all()]
    to_send_list.extend([i for i in new if i["link"] not in old_link_list])

    # 对结果按照发布时间排序
    to_send_list.sort(key=get_item_date)

    return to_send_list

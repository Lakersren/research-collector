"""Render the local collector's execution data as a readable Markdown report."""
from datetime import datetime
import re
from urllib.parse import quote, urlsplit

from aris.daily_policy import SHANGHAI


STATUS = {"success": "成功", "failed": "失败", "interrupted": "中断"}


def cell(value):
    if value is None or value == "":
        return "—"
    text = " ".join(str(value).split())
    return re.sub(r"[&<>\\|`*_\[\]~]", lambda match: f"&#{ord(match[0])};", text)


def source_link(url):
    if not url:
        return "—"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "—"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "—"
    destination = quote(url, safe=":/?#[]@!$&'()*+,;=%-._~")
    return f"[原文](<{destination}>)"


def beijing_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def render(report):
    sources = report["sources"]
    target = report["target_date"]
    counts = {
        status: sum(len(source["articles"][status]) for source in sources)
        for status in ("success", "failed", "skipped")
    }
    successful_sources = sum(source["collection"]["status"] == "success" for source in sources)
    wrong_dates = sum(
        article["publication_date_beijing"] != target
        for source in sources
        for article in source["articles"]["success"]
    )
    date_check = "没有保存文章，暂无可核对记录"
    if wrong_dates:
        date_check = f"有 {wrong_dates} 篇已保存文章的发布日期与目标日期不符"
    elif counts["success"]:
        date_check = "全部已保存文章的北京时间发布日期与目标日期一致"

    lines = [
        "# 采集任务完成表",
        "",
        f"**整轮结果 {STATUS[report['status']]}** · 目标发布日期 **{target}**（北京时间）",
        "",
        f"执行时间 {beijing_time(report['started_at'])} 至 {beijing_time(report['finished_at'])}（北京时间）",
        "",
        f"来源成功 **{successful_sources}/{len(sources)}** · 来源未成功 **{len(sources) - successful_sources}**"
        f" · 保存 **{counts['success']}** 篇 · 失败 **{counts['failed']}** 篇 · 跳过 **{counts['skipped']}** 篇",
        "",
        f"**日期核对**　{date_check}",
        "",
        "## 来源汇总",
        "",
        "| 来源 | 结果 | 保存 | 失败 | 跳过 | 说明 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for source in sources:
        collection = source["collection"]
        articles = source["articles"]
        status = STATUS[collection["status"]]
        note = collection.get("error")
        if status == "成功" and not articles["success"]:
            note = "目标日期无可保存文章"
        lines.append(
            f"| {cell(source['source_name'])} | {status} | {len(articles['success'])}"
            f" | {len(articles['failed'])} | {len(articles['skipped'])} | {cell(note)} |"
        )

    for source in sources:
        lines += ["", f"## {cell(source['source_name'])}", ""]
        collection = source["collection"]
        if collection.get("error"):
            lines += [f"**来源说明**　{cell(collection['error'])}", ""]
        if any(source["articles"].values()):
            lines += [
                "| 结果 | 北京日期 | 记录的发布时间 | 文章 | 链接 | 说明 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for status, label in (("failed", "失败"), ("success", "已保存"), ("skipped", "跳过")):
                for article in source["articles"][status]:
                    note = article.get("reason")
                    if status == "success":
                        if article["publication_date_beijing"] != target:
                            note = "发布日期与目标日期不符"
                        elif article.get("images_failed"):
                            note = f"{article['images_failed']} 张图片下载失败"
                    lines.append(
                        f"| {label} | {cell(article['publication_date_beijing'])} | {cell(article['published_at'])}"
                        f" | {cell(article.get('title') or '未取得标题')} | {source_link(article.get('url'))} | {cell(note)} |"
                    )
        else:
            lines.append("本轮没有逐篇记录。")
    latest = report.get('latest_attempt', report)
    if latest['job_id'] != report['job_id']:
        lines += ["", f"最近一次执行 {STATUS[latest['status']]}，已保留此前的文章结果。"]
    lines += ["", f"最近任务 ID `{latest['job_id']}` · [JSON 报告](report.json)", ""]
    return "\n".join(lines)

"""Locale negotiation and response-only presentation helpers.

The domain stores stable codes and canonical payloads.  This module is used
only at the HTTP boundary to render human-facing copy in the requested
locale, so changing a language never changes an evidence hash, confirmation
token, audit record, or idempotency command.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from typing import Any, Mapping


Locale = str
DEFAULT_LOCALE = "zh-Hant"
SUPPORTED_LOCALES = {"zh-Hant", "en"}
_locale_context: ContextVar[Locale] = ContextVar("zhufulian_locale", default=DEFAULT_LOCALE)


def normalize_locale(value: str | None) -> Locale:
    """Resolve an explicit locale or an Accept-Language header safely."""

    candidate = (value or "").split(",", 1)[0].split(";", 1)[0].strip().replace("_", "-").lower()
    if candidate.startswith("en"):
        return "en"
    if candidate.startswith(("zh", "yue")):
        return "zh-Hant"
    return DEFAULT_LOCALE


def locale_from_headers(x_locale: str | None, accept_language: str | None) -> Locale:
    return normalize_locale(x_locale or accept_language)


def current_locale() -> Locale:
    return _locale_context.get()


def set_current_locale(locale: str | None) -> Token[Locale]:
    return _locale_context.set(normalize_locale(locale))


def reset_current_locale(token: Token[Locale]) -> None:
    _locale_context.reset(token)


_KEYS: dict[str, dict[str, str]] = {
    "zh-Hant": {
        "error.unauthorized": "尚未驗證使用者身分。",
        "error.forbidden": "您沒有權限執行此操作。",
        "error.not_found": "找不到要求的資源。",
        "error.project_not_found": "找不到專案。",
        "error.material_not_found": "找不到物料。",
        "error.validation": "請檢查提交的資料。",
        "error.invalid_state": "此操作不適用於目前狀態。",
        "error.version_conflict": "資料已變更，請重新整理後再試。",
        "error.token_expired": "確認權杖無效或已過期。",
        "error.token_replayed": "此確認權杖已使用。",
        "error.insufficient_stock": "可用庫存不足。",
        "error.unit_mismatch": "物料單位不相符。",
        "error.missing_data": "完成此操作所需的資料不足。",
        "error.chain_unavailable": "證據鏈服務暫時無法使用。",
        "error.thread_busy": "此對話仍在處理中。",
        "error.duplicate_idempotency": "此冪等鍵已用於其他請求。",
        "error.generic": "系統無法完成此操作。",
        "ai.complete": "已完成受控工具呼叫；所有正式業務寫入仍需人工確認。",
        "ai.tool_failed": "{tool} 未能完成：{message}",
        "ai.shortage.summary": "缺料計算識別出 {count} 項需要關注的物料。",
        "ai.shortage.risk": "物料 {material_id} 預計在 {shortage_date} 低於安全庫存，缺口為 {shortage_qty} {unit}。",
        "ai.shortage.missing_data": "缺少資料：{items}",
        "ai.shortage.complete_data": "部分物料缺少完整計畫或庫存快照，未為其自動建立草稿。",
        "ai.shortage.fill_data": "補登缺少的計畫或庫存資料",
        "ai.shortage.created_draft": "已建立採購申請草稿，尚未建立正式採購申請或改變庫存。",
        "ai.shortage.review_draft": "審閱草稿並使用一次性確認權杖提交",
        "ai.shortage.retry_draft": "補齊缺失資料後再建立採購草稿",
        "ai.shortage.create_draft": "依計算結果建立採購草稿",
        "ai.purchase.summary": "目前有 {count} 份待審批採購申請。",
        "ai.purchase.item": "申請 {request_no} 的狀態為 {status}，需求日期為 {needed_by}。",
        "ai.purchase.open": "開啟採購協同查看申請明細",
        "ai.purchase.none": "目前沒有可見的待審批採購申請。",
        "ai.ppe.summary": "PPE 覆蓋檢查完成，發現 {count} 項覆蓋缺口。",
        "ai.ppe.privacy": "結果只包含匿名班組識別碼，不包含姓名或健康資訊。",
        "ai.ppe.open": "查看 PPE 發放與補貨建議",
        "ai.anomaly.summary": "異常分析產生 {count} 條覆核線索。",
        "ai.anomaly.caution": "異常只提示可能原因，不能據此認定責任。",
        "ai.anomaly.open": "在異常中心確認並記錄處置說明",
        "ai.trace.summary": "已讀取 {events} 條證據事件和 {moves} 條庫存流水。",
        "ai.trace.open": "開啟批次追溯頁面驗證雜湊",
        "ai.stock.summary": "目前授權範圍內有 {count} 條庫存餘額記錄。",
        "ai.stock.open": "查看庫存台帳",
        "ai.unknown": "無法判斷請求意圖；未呼叫寫入工具，也沒有產生業務副作用。",
        "ai.unknown.next": "請說明要查詢庫存、缺料、PPE、異常或批次追溯。",
        "daily.arrivals": "到貨和領用僅來自指定日期的已確認業務記錄。",
        "daily.shortage": "缺料計算識別出 {count} 項風險。",
        "daily.ppe": "PPE 覆蓋檢查包含 {count} 項結果。",
        "daily.no_arrivals": "該日期沒有已確認到貨記錄。",
        "daily.no_issues": "該日期沒有領用流水。",
        "daily.no_ppe": "沒有可用的 PPE 覆蓋計算結果。",
        "anomaly.plan_cause": "計畫或現場記錄需要覆核",
        "anomaly.plan_steps": "核對領用流水、班組記錄和計畫版本",
        "anomaly.repeat_cause": "短時間重複領用或登錄流程需要覆核",
        "anomaly.repeat_steps": "核對原始領用單和退庫記錄",
        "notice.thread_cleared": "僅清除工作階段可見歷史；AI 執行、工具呼叫、草稿與稽核記錄均已保留。",
    },
    "en": {
        "error.unauthorized": "Authentication is required.",
        "error.forbidden": "You do not have permission to perform this action.",
        "error.not_found": "The requested resource was not found.",
        "error.project_not_found": "The project was not found.",
        "error.material_not_found": "The material was not found.",
        "error.validation": "Check the submitted data and try again.",
        "error.invalid_state": "This action is not available in the current state.",
        "error.version_conflict": "The record changed. Refresh and try again.",
        "error.token_expired": "The confirmation token is invalid or expired.",
        "error.token_replayed": "The confirmation token has already been used.",
        "error.insufficient_stock": "Available stock is insufficient.",
        "error.unit_mismatch": "The material unit does not match.",
        "error.missing_data": "Required data is missing for this action.",
        "error.chain_unavailable": "The evidence-chain service is temporarily unavailable.",
        "error.thread_busy": "This conversation is still being processed.",
        "error.duplicate_idempotency": "This idempotency key was used for a different request.",
        "error.generic": "The system could not complete this action.",
        "ai.complete": "The controlled tool call is complete. All formal business writes still require human confirmation.",
        "ai.tool_failed": "{tool} could not be completed: {message}",
        "ai.shortage.summary": "The shortage calculation identified {count} materials that need attention.",
        "ai.shortage.risk": "Material {material_id} is expected to fall below safety stock on {shortage_date}; the shortage is {shortage_qty} {unit}.",
        "ai.shortage.missing_data": "Missing data: {items}",
        "ai.shortage.complete_data": "Some materials lack complete plan or inventory snapshots, so no drafts were created for them.",
        "ai.shortage.fill_data": "Record the missing plan or inventory data",
        "ai.shortage.created_draft": "A purchase-request draft was created. No formal request was created and inventory was not changed.",
        "ai.shortage.review_draft": "Review the draft and submit it with a one-time confirmation token",
        "ai.shortage.retry_draft": "Complete the missing data before creating a purchase draft",
        "ai.shortage.create_draft": "Create a purchase draft from the calculation",
        "ai.purchase.summary": "There are {count} purchase requests awaiting approval.",
        "ai.purchase.item": "Request {request_no} is {status}; its required date is {needed_by}.",
        "ai.purchase.open": "Open Procurement to view request details",
        "ai.purchase.none": "There are no visible purchase requests awaiting approval.",
        "ai.ppe.summary": "The PPE coverage check found {count} coverage gaps.",
        "ai.ppe.privacy": "The result contains only anonymous crew identifiers, not names or health information.",
        "ai.ppe.open": "View PPE issue and replenishment recommendations",
        "ai.anomaly.summary": "The anomaly analysis produced {count} review leads.",
        "ai.anomaly.caution": "Anomalies indicate possible causes only and cannot establish responsibility.",
        "ai.anomaly.open": "Confirm the anomaly and record the disposition in Anomalies",
        "ai.trace.summary": "Read {events} evidence events and {moves} stock movements.",
        "ai.trace.open": "Open batch traceability to verify the hash",
        "ai.stock.summary": "There are {count} stock-balance records in the authorized scope.",
        "ai.stock.open": "View the inventory ledger",
        "ai.unknown": "The request intent could not be determined. No write tool was called and no business side effect was created.",
        "ai.unknown.next": "Specify whether you want to query inventory, shortages, PPE, anomalies, or batch traceability.",
        "daily.arrivals": "Arrivals and issues include only confirmed business records for the selected date.",
        "daily.shortage": "The shortage calculation identified {count} risks.",
        "daily.ppe": "The PPE coverage check includes {count} results.",
        "daily.no_arrivals": "No confirmed arrival record exists for this date.",
        "daily.no_issues": "No issue movement exists for this date.",
        "daily.no_ppe": "No PPE coverage calculation result is available.",
        "anomaly.plan_cause": "The plan or site records need review",
        "anomaly.plan_steps": "Check issue movements, crew records, and the plan version",
        "anomaly.repeat_cause": "Repeated short-interval issues or the entry process need review",
        "anomaly.repeat_steps": "Check the original issue slip and return records",
        "notice.thread_cleared": "Only the visible conversation history was cleared; AI runs, tool calls, drafts, and audit records were retained.",
    },
}

_ERROR_KEY_BY_CODE = {
    "UNAUTHORIZED": "error.unauthorized",
    "FORBIDDEN": "error.forbidden",
    "PROJECT_NOT_FOUND": "error.project_not_found",
    "MATERIAL_NOT_FOUND": "error.material_not_found",
    "NOT_FOUND": "error.not_found",
    "DUPLICATE_IDEMPOTENCY": "error.duplicate_idempotency",
    "VERSION_CONFLICT": "error.version_conflict",
    "TOKEN_REPLAYED": "error.token_replayed",
    "INVALID_STATE": "error.invalid_state",
    "TOKEN_EXPIRED": "error.token_expired",
    "INSUFFICIENT_STOCK": "error.insufficient_stock",
    "VALIDATION_ERROR": "error.validation",
    "UNIT_MISMATCH": "error.unit_mismatch",
    "MISSING_DATA": "error.missing_data",
    "CHAIN_UNAVAILABLE": "error.chain_unavailable",
    "THREAD_BUSY": "error.thread_busy",
}

# System-owned seed values and generated prose.  User-entered free text is
# deliberately not machine-translated; unknown strings pass through unchanged.
_SOURCE_TEXT: dict[str, dict[str, str]] = {
    "zh-Hant": {
        "筑福链": "築福鏈", "海之子示范项目": "海之子示範項目", "海之子项目方": "海之子項目方",
        "供应商 A": "供應商 A", "示范供应商 A": "示範供應商 A", "项目负责人": "項目負責人",
        "采购人员": "採購人員", "现场管理员": "現場管理員", "验收人员": "驗收人員", "审计人员": "審計人員",
        "三号楼": "三號樓", "砌体阶段": "砌體階段", "三号楼砌体区": "三號樓砌體區", "主仓库": "主倉庫",
        "砌块": "砌塊", "水泥": "水泥", "钢筋": "鋼筋", "安全帽": "安全帽", "反光衣": "反光衣",
        "防护手套": "防護手套", "防护鞋": "防護鞋", "防暑用品": "防暑用品", "急救用品": "急救用品",
        "砌体班组": "砌體班組", "黄色": "黃色", "均码": "均碼", "耐磨": "耐磨", "防砸": "防砸",
        "现场防暑组合箱": "現場防暑組合箱", "现场急救组合箱": "現場急救組合箱",
        "恢复砌块安全库存": "恢復砌塊安全庫存", "由演示缺料计算生成": "由示範缺料計算產生",
        "演示待验收记录": "示範待驗收記錄", "包装破损，待隔离处理": "包裝破損，待隔離處理",
        "确认砌块采购申请并完成审批": "確認砌塊採購申請並完成審批", "确认到货批次的质量验收": "確認到貨批次的品質驗收",
        "复核砌体计划与实际消耗偏差": "覆核砌體計畫與實際消耗偏差", "计划版本或现场领用记录需要复核": "計畫版本或現場領用記錄需要覆核",
        "核对领用流水和计划版本": "核對領用流水和計畫版本", "日报仅引用已确认业务记录和带版本的计算结果": "日報僅引用已確認業務記錄和帶版本的計算結果",
        "计划或现场记录需要复核": "計畫或現場記錄需要覆核", "核对领用流水、班组记录和计划版本": "核對領用流水、班組記錄和計畫版本",
        "短时间重复领用或录入流程需要复核": "短時間重複領用或登錄流程需要覆核", "核对原始领用单和退库记录": "核對原始領用單和退庫記錄",
        "到货和领用仅来自指定日期的已确认业务记录。": "到貨和領用僅來自指定日期的已確認業務記錄。", "该日期没有已确认到货记录": "該日期沒有已確認到貨記錄",
        "该日期没有领用流水": "該日期沒有領用流水", "没有可用的 PPE 覆盖计算结果": "沒有可用的 PPE 覆蓋計算結果",
        "该次运行未生成可展示的回复。": "該次執行未產生可顯示的回覆。", "暂无备注": "暫無備註",
        "供应商视角仅开放自身订单与发货数据": "供應商視角僅開放自身訂單與發貨資料", "供应商视角不开放施工计划": "供應商視角不開放施工計畫",
        "供应商视角不开放项目库存流水": "供應商視角不開放項目庫存流水", "供应商视角不开放项目消耗汇总": "供應商視角不開放項目消耗彙總",
        "供应商视角不开放采购草稿": "供應商視角不開放採購草稿", "供应商视角不开放项目班组数据": "供應商視角不開放項目班組資料",
        "供应商视角不开放 PPE 覆盖数据": "供應商視角不開放 PPE 覆蓋資料", "供应商视角不开放项目缺料计算": "供應商視角不開放項目缺料計算",
        "供应商视角不开放库存预测": "供應商視角不開放庫存預測", "供应商视角不开放项目异常分析": "供應商視角不開放項目異常分析",
        "链上仅保存经规范化后的事件摘要和文件哈希。": "鏈上僅保存經規範化後的事件摘要和檔案雜湊。",
    },
    "en": {
        "筑福链": "Zhufu Chain", "海之子示范项目": "Hai Zhi Zi Demonstration Project", "海之子项目方": "Hai Zhi Zi Project Organization",
        "供应商 A": "Supplier A", "示范供应商 A": "Demonstration Supplier A", "项目负责人": "Project Owner",
        "采购人员": "Procurement Officer", "现场管理员": "Site Administrator", "验收人员": "Inspector", "审计人员": "Auditor",
        "三号楼": "Building 3", "砌体阶段": "Masonry Stage", "三号楼砌体区": "Building 3 Masonry Area", "主仓库": "Main Warehouse",
        "砌块": "AAC block", "水泥": "Cement", "钢筋": "Rebar", "安全帽": "Safety helmet", "反光衣": "High-visibility vest",
        "防护手套": "Protective gloves", "防护鞋": "Safety shoes", "防暑用品": "Heat-safety supplies", "急救用品": "First-aid supplies",
        "砌体班组": "Masonry crew", "黄色": "yellow", "均码": "one size", "耐磨": "abrasion-resistant", "防砸": "impact-resistant",
        "现场防暑组合箱": "Site heat-safety kit", "现场急救组合箱": "Site first-aid kit",
        "恢复砌块安全库存": "Restore AAC-block safety stock", "由演示缺料计算生成": "Generated by the demonstration shortage calculation",
        "演示待验收记录": "Demonstration inspection pending", "包装破损，待隔离处理": "Packaging damaged; pending quarantine",
        "确认砌块采购申请并完成审批": "Confirm the AAC-block purchase request and complete approval", "确认到货批次的质量验收": "Confirm quality inspection for the received batch",
        "复核砌体计划与实际消耗偏差": "Review the variance between masonry plan and actual consumption", "计划版本或现场领用记录需要复核": "The plan version or site issue records need review",
        "核对领用流水和计划版本": "Check issue movements and the plan version", "日报仅引用已确认业务记录和带版本的计算结果": "The daily report uses only confirmed business records and versioned calculation results",
        "计划或现场记录需要复核": "The plan or site records need review", "核对领用流水、班组记录和计划版本": "Check issue movements, crew records, and the plan version",
        "短时间重复领用或录入流程需要复核": "Repeated short-interval issues or the entry process need review", "核对原始领用单和退库记录": "Check the original issue slip and return records",
        "到货和领用仅来自指定日期的已确认业务记录。": "Arrivals and issues include only confirmed business records for the selected date.", "该日期没有已确认到货记录": "No confirmed arrival record exists for this date",
        "该日期没有领用流水": "No issue movement exists for this date", "没有可用的 PPE 覆盖计算结果": "No PPE coverage calculation result is available",
        "该次运行未生成可展示的回复。": "This run did not produce a displayable response.", "暂无备注": "No notes",
        "供应商视角仅开放自身订单与发货数据": "Supplier access is limited to its own orders and shipment data", "供应商视角不开放施工计划": "Supplier access does not include construction plans",
        "供应商视角不开放项目库存流水": "Supplier access does not include project stock movements", "供应商视角不开放项目消耗汇总": "Supplier access does not include project consumption summaries",
        "供应商视角不开放采购草稿": "Supplier access does not include purchase drafts", "供应商视角不开放项目班组数据": "Supplier access does not include project crew data",
        "供应商视角不开放 PPE 覆盖数据": "Supplier access does not include PPE coverage data", "供应商视角不开放项目缺料计算": "Supplier access does not include project shortage calculations",
        "供应商视角不开放库存预测": "Supplier access does not include inventory forecasts", "供应商视角不开放项目异常分析": "Supplier access does not include project anomaly analysis",
        "链上仅保存经规范化后的事件摘要和文件哈希。": "The chain stores only normalized event summaries and file hashes.",
    },
}

# Character conversion is intentionally a fallback for legacy display data.
# Canonical texts and all new generated messages use keys above instead.
_SIMPLIFIED_TO_TRADITIONAL = str.maketrans({
    "链": "鏈", "项": "項", "业": "業", "务": "務", "库": "庫", "仓": "倉", "验": "驗", "质": "質", "证": "證", "据": "據",
    "发": "發", "货": "貨", "购": "購", "请": "請", "审": "審", "计": "計", "划": "劃", "现": "現", "场": "場", "统": "統",
    "台": "臺", "账": "帳", "档": "檔", "录": "錄", "关": "關", "联": "聯", "处": "處", "理": "理", "资": "資", "料": "料",
    "数": "數", "据": "據", "历": "歷", "态": "態", "异": "異", "常": "常", "补": "補", "齐": "齊", "并": "並", "个": "個",
    "对": "對", "应": "應", "见": "見", "识": "識", "别": "別", "复": "複", "开": "開", "闭": "閉", "户": "戶", "级": "級",
    "签": "簽", "确": "確", "认": "認", "项": "項", "专": "專", "输": "輸", "码": "碼", "号": "號", "号": "號", "后": "後",
    "进": "進", "续": "續", "达": "達", "预": "預", "领": "領", "损": "損", "坏": "壞", "样": "樣", "与": "與", "从": "從",
    "为": "為", "无": "無", "过": "過", "时": "時", "间": "間", "统": "統", "级": "級", "风": "風", "险": "險",
    "来": "來", "问": "問", "题": "題", "报": "報", "废": "廢", "单": "單", "组": "組", "视": "視", "图": "圖", "页": "頁",
})


def translate_key(key: str, locale: str | None = None, params: Mapping[str, Any] | None = None) -> str:
    language = normalize_locale(locale or current_locale())
    template = _KEYS[language].get(key) or _KEYS[DEFAULT_LOCALE].get(key) or key
    try:
        return template.format(**{name: str(value) for name, value in (params or {}).items()})
    except (KeyError, ValueError):
        return template


def error_message(code: str, locale: str | None = None) -> tuple[str, str]:
    key = _ERROR_KEY_BY_CODE.get(code, "error.generic")
    return key, translate_key(key, locale)


def localize_text(value: str, locale: str | None = None) -> str:
    """Translate known system-owned text without touching opaque identifiers."""

    language = normalize_locale(locale or current_locale())
    if not value:
        return value
    mapped = _SOURCE_TEXT[language].get(value)
    if mapped is not None:
        return mapped
    demo_material = re.fullmatch(r"演示物料(\d+)", value)
    if demo_material:
        number = demo_material.group(1)
        return f"Demo material {number}" if language == "en" else f"示範物料{number}"
    if language == "zh-Hant":
        return value.translate(_SIMPLIFIED_TO_TRADITIONAL)
    # Replace safe seed/system phrases inside composite fields such as a
    # specification or trace sentence. Longest-first keeps compound terms intact.
    result = value
    for source, translated in sorted(_SOURCE_TEXT["en"].items(), key=lambda item: len(item[0]), reverse=True):
        if source in result:
            result = result.replace(source, translated)
    result = result.replace("L码", "L size")
    return result


def _render_mapping(value: Mapping[str, Any], locale: str) -> dict[str, Any]:
    # Requests, notes and reasons may be authored by a human. They are part
    # of the business record and must be shown as entered, even when the
    # surrounding system chrome is translated.
    user_authored = value.get("role") == "user"
    rendered: dict[str, Any] = {
        str(key): item if user_authored and key in {"text", "request_text", "note", "notes", "reason", "justification"}
        else render_for_locale(item, locale)
        for key, item in value.items()
    }
    key = value.get("message_key")
    if isinstance(key, str):
        params = value.get("message_params")
        rendered["text"] = translate_key(key, locale, params if isinstance(params, Mapping) else None)
    # AI stores localized copy as structured message descriptors. API clients
    # receive the rendered strings while the persisted run remains language
    # neutral and can be rendered again in another locale.
    for list_key in ("warnings", "next_actions", "actions"):
        items = value.get(list_key)
        if isinstance(items, list):
            rendered[list_key] = [
                translate_key(str(item["message_key"]), locale, item.get("message_params") if isinstance(item.get("message_params"), Mapping) else None)
                if isinstance(item, Mapping) and isinstance(item.get("message_key"), str)
                else render_for_locale(item, locale)
                for item in items
            ]
    return rendered


def render_for_locale(value: Any, locale: str | None = None) -> Any:
    """Return a localized response copy while leaving canonical storage untouched."""

    language = normalize_locale(locale or current_locale())
    if isinstance(value, Mapping):
        return _render_mapping(value, language)
    if isinstance(value, list):
        return [render_for_locale(item, language) for item in value]
    if isinstance(value, tuple):
        return [render_for_locale(item, language) for item in value]
    if isinstance(value, str):
        return localize_text(value, language)
    return value


def has_han(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


__all__ = [
    "DEFAULT_LOCALE", "SUPPORTED_LOCALES", "current_locale", "error_message", "has_han",
    "locale_from_headers", "localize_text", "normalize_locale", "render_for_locale",
    "reset_current_locale", "set_current_locale", "translate_key",
]

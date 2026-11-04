"""类目属性与合规规则。

电商商品运营的真正成本不在写文案，而在「属性填错导致搜索召回失败」和
「违规词导致下架」。这里把规则做成可测的纯函数，Agent 只能建议，不能绕过。
"""

from typing import Any

CATEGORY_RULES: dict[str, dict[str, Any]] = {
    "digital": {
        "required": ["brand", "model", "warranty_months"],
        "forbidden_keywords": ["破解", "无限流量", "终身免费"],
    },
    "apparel": {
        "required": ["material", "size_range", "season"],
        "forbidden_keywords": ["原单", "顶级复刻", "高仿"],
    },
    "beauty": {
        "required": ["shelf_life_months", "skin_type"],
        "forbidden_keywords": ["药妆", "根治", "无副作用"],
    },
    "home": {
        "required": ["material", "dimensions"],
        "forbidden_keywords": ["绝对", "永久"],
    },
}

DEFAULT_RULE = {"required": [], "forbidden_keywords": ["全网最低", "国家级", "100%有效"]}


def validate_product(payload: dict[str, Any]) -> dict[str, Any]:
    # 未配置规则的类目走默认兜底，防止新增类目绕过违规词检查。
    rule = CATEGORY_RULES.get(str(payload.get("category", "")), DEFAULT_RULE)
    attributes = payload.get("attributes") or {}
    # 空字符串与 None 都按「未填写」处理，必填属性不能靠空值蒙混通过。
    missing = [key for key in rule["required"] if not attributes.get(key)]
    # 违规词只扫描文案字段，命中即判不合规，不做模糊匹配。
    text = " ".join(
        str(payload.get(key, "")) for key in ("title", "subtitle", "description")
    )
    hit_keywords = [word for word in rule["forbidden_keywords"] if word in text]
    return {
        "valid": not missing and not hit_keywords,
        "missing_attributes": missing,
        "forbidden_keywords": hit_keywords,
        "rule_category": payload.get("category"),
    }

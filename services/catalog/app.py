import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.logging import configure_logging, get_logger

from .models import Product
from .rules import validate_product

settings = get_settings()
logger = get_logger(__name__)


SEED_PRODUCTS = [
    ("SKU-1001", "轻量降噪耳机 Pro", "40dB 主动降噪，续航 36 小时", "digital", 89900, "audio", ["新品", "降噪"], {"brand": "Aurora", "model": "ANC-Pro", "warranty_months": 12}),
    ("SKU-1002", "氮化镓快充套装 65W", "一充多用，体积缩小 40%", "digital", 19900, "charger", ["快充", "出行"], {"brand": "Volt", "model": "GaN65", "warranty_months": 24}),
    ("SKU-1003", "凉感防晒衬衫", "UPF50+，透气速干", "apparel", 25900, "shirt", ["夏季", "防晒"], {"material": "锦纶 82% / 氨纶 18%", "size_range": "S-XXL", "season": "夏"}),
    ("SKU-1004", "轻量越野跑鞋", "湿地抓地，单只 240g", "apparel", 46900, "shoes", ["运动", "越野"], {"material": "网布 + 橡胶", "size_range": "36-45", "season": "四季"}),
    ("SKU-1005", "氨基酸温和洁面", "敏感肌可用，pH5.5", "beauty", 8900, "cleanser", ["敏感肌", "温和"], {"shelf_life_months": 36, "skin_type": "敏感肌"}),
    ("SKU-1006", "玻尿酸保湿精华 30ml", "多重分子量玻尿酸", "beauty", 21900, "serum", ["保湿", "精华"], {"shelf_life_months": 24, "skin_type": "干性/混合"}),
    ("SKU-1007", "折叠收纳箱 60L", "承重 30kg，可折叠", "home", 12900, "storage", ["收纳", "露营"], {"material": "PP", "dimensions": "60x40x30cm"}),
    ("SKU-1008", "天然乳胶枕", "分区支撑，可拆洗", "home", 32900, "pillow", ["睡眠", "家居"], {"material": "天然乳胶", "dimensions": "60x36x12cm"}),
]


# 种子商品 ID 由 SKU 派生的 uuid5，保证多次启动或重建库时主键稳定，不会重复插入。
def _seed_id(sku: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"shop-agent/{sku}"))


async def seed_products() -> None:
    # 只在空表时写入种子数据，避免每次启动重复插入或覆盖人工维护的商品。
    async with get_session_factory()() as session:
        existing = await session.scalar(select(func.count()).select_from(Product))
        if existing:
            return
        for sku, title, subtitle, category, price, tag, tags, attrs in SEED_PRODUCTS:
            session.add(
                Product(
                    id=_seed_id(sku),
                    sku=sku,
                    title=title,
                    subtitle=subtitle,
                    description=f"{title}。{subtitle}。",
                    category=category,
                    price_cents=price,
                    status="published",
                    tags=tags,
                    attributes={**attrs, "series": tag},
                    image_url=f"https://picsum.photos/seed/{sku}/640/480",
                )
            )
        await session.commit()
        logger.info("已写入 %s 条种子商品", len(SEED_PRODUCTS))


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging("catalog")
    # 启动顺序固定：先建表再投种子数据，否则空库上写入种子会失败。
    await init_models()
    await seed_products()
    yield


app = FastAPI(title="catalog-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "catalog")


class ProductCreate(BaseModel):
    sku: str = Field(min_length=3, max_length=64)
    title: str = Field(min_length=2, max_length=200)
    subtitle: str = ""
    description: str = ""
    category: str
    price_cents: int = Field(gt=0)
    image_url: str = ""
    tags: list[str] = []
    attributes: dict[str, Any] = {}


class ProductUpdate(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    price_cents: int | None = Field(default=None, gt=0)
    tags: list[str] | None = None
    attributes: dict[str, Any] | None = None
    status: str | None = None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "catalog"}


@app.get("/products")
async def list_products(
    q: str | None = None,
    category: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    async with get_session_factory()() as session:
        # 前台列表只暴露已上架商品，草稿/下线商品不参与搜索与分页。
        stmt = select(Product).where(Product.status == "published")
        if q:
            keyword = f"%{q}%"
            stmt = stmt.where(
                or_(Product.title.ilike(keyword), Product.subtitle.ilike(keyword), Product.description.ilike(keyword))
            )
        if category:
            stmt = stmt.where(Product.category == category)
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            (await session.execute(stmt.order_by(Product.created_at.desc()).offset((page - 1) * size).limit(size)))
            .scalars()
            .all()
        )
        return {"items": [row.to_dict() for row in rows], "total": total, "page": page, "size": size}


@app.get("/products/{product_id}")
async def get_product(product_id: str) -> dict[str, Any]:
    async with get_session_factory()() as session:
        # 路径参数既接受主键也接受 SKU，方便前端用 SKU 直达详情页。
        product = await session.get(Product, product_id)
        if product is None:
            product = await session.scalar(select(Product).where(Product.sku == product_id))
        # 未上架商品对外统一按不存在处理，避免通过接口探测草稿内容。
        if product is None or product.status != "published":
            raise ApiError("NOT_FOUND", "商品不存在或未上架")
        return product.to_dict()


@app.get("/admin/products")
async def admin_list_products(principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    # 后台列表可见全部状态，必须要求 staff 权限，普通用户拿不到草稿数据。
    principal.require_staff()
    async with get_session_factory()() as session:
        stmt = select(Product).order_by(Product.updated_at.desc())
        if status:
            stmt = stmt.where(Product.status == status)
        rows = (await session.execute(stmt)).scalars().all()
        return {"items": [row.to_dict() for row in rows]}


@app.post("/products", status_code=201)
async def create_product(payload: ProductCreate, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        exists = await session.scalar(select(Product).where(Product.sku == payload.sku))
        if exists:
            raise ApiError("CONFLICT", "SKU 已存在")
        # 新商品一律落 draft，必须显式 PATCH 成 published 且通过合规校验才能上架。
        product = Product(
            id=str(uuid.uuid4()),
            sku=payload.sku,
            title=payload.title,
            subtitle=payload.subtitle,
            description=payload.description,
            category=payload.category,
            price_cents=payload.price_cents,
            image_url=payload.image_url,
            tags=payload.tags,
            attributes=payload.attributes,
            status="draft",
        )
        session.add(product)
        await session.commit()
        return {**product.to_dict(), "compliance": validate_product(payload.model_dump())}


@app.patch("/products/{product_id}")
async def update_product(product_id: str, payload: ProductUpdate, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    changes = payload.model_dump(exclude_unset=True)
    # 状态只允许白名单取值，防止调用方写入任意字符串绕过上下架语义。
    if changes.get("status") not in (None, "draft", "published", "offline"):
        raise ApiError("BAD_REQUEST", "非法的商品状态")
    async with get_session_factory()() as session:
        product = await session.get(Product, product_id)
        if product is None:
            raise ApiError("NOT_FOUND", "商品不存在")
        if changes.get("status") == "published":
            # 上架前用本次改动合并后的字段做合规校验，避免「先改文案再上架」绕过规则。
            check = validate_product(
                {
                    "category": product.category,
                    "title": changes.get("title", product.title),
                    "subtitle": changes.get("subtitle", product.subtitle),
                    "description": changes.get("description", product.description),
                    "attributes": changes.get("attributes", product.attributes),
                }
            )
            if not check["valid"]:
                raise ApiError("BAD_REQUEST", "商品不满足上架合规要求", details=check)
        for key, value in changes.items():
            setattr(product, key, value)
        product.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return product.to_dict()


@app.post("/products/{product_id}/compliance-check")
async def compliance_check(product_id: str, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        product = await session.get(Product, product_id)
        if product is None:
            raise ApiError("NOT_FOUND", "商品不存在")
        # 只读校验接口，不落库；结果供运营在编辑页预览合规风险。
        return validate_product(product.to_dict())

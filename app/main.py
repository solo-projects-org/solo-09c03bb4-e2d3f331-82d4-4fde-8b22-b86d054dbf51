"""FastAPI application exposing the tariff-group supply API."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .schemas import (
    BaseIn,
    BaseOut,
    ChangeRecord,
    FormIn,
    FormListOut,
    FormOut,
    IssueIn,
    IssueResult,
    MappingIn,
    MappingOut,
    SummaryOut,
    VersionListOut,
    VoucherListOut,
    VoucherOut,
)
from .store import ApiError, Store

DESCRIPTION = """新版税则拆并税号后的供数 API。

* 先 `PUT /base` 固定相邻两年各税号进口额（整数分），有版本后底表即锁定；
* 再 `POST /mappings` 按递增版号提交**完整**新旧税号对照（同号也按年份区分，
  未变税号以 `(code, code)` 出现）；
* 经对照直接或间接相连的税号归为一组，`GET /summary` 给出现行汇总
  （组号、两侧税号、各年合计）；
* `GET /changes/{version}` 按版取供数变更：首版全量，后续给被替换版号、
  应撤下组号、应写入汇总；
* 对照漏掉底表税号或引用未知税号时整版拒收（422），原供数仍可查询；
* 同版重传原对照返回当时的变更（200），改内容则冲突（409）；
* 首版对照生效后可登记/撤销核验单（`/forms`）：合计须与底表相符，撤销保留原单；
* `POST /vouchers` 按所见版号、组号、年份和至多 20 个单号签发凭证：
  从有效单中选最少张，每张整用、税号互不重叠且恰好覆盖该组当年成员；
  写入时所见版已被替换或入选单已撤销 → 409 且不生成凭证。
"""


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="税则税号分组供数 API", version="1.0.0", description=DESCRIPTION)
    store = Store(db_path)
    app.state.store = store

    @app.exception_handler(ApiError)
    async def api_error_handler(_, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    # ------------------------------------------------------------- 底表
    @app.put(
        "/base",
        response_model=BaseOut,
        tags=["底表"],
        summary="固定相邻两年各税号进口额（整数分）",
    )
    def put_base(payload: BaseIn):
        amounts = [(a.year, a.code, a.amount_cents) for a in payload.amounts]
        return store.put_base(amounts)

    @app.get("/base", tags=["底表"], summary="查看当前底表")
    def get_base():
        return store.get_base()

    # ----------------------------------------------------------- 对照版本
    @app.post(
        "/mappings",
        response_model=ChangeRecord,
        status_code=201,
        tags=["对照版本"],
        summary="按递增版号提交完整新旧税号对照",
        description="新版权返回 201 及本版变更；同版同内容重传返回 200 及当时的变更；"
        "同版不同内容返回 409；漏报/引用未知税号返回 422 且整版拒收。",
    )
    def submit_mapping(payload: MappingIn, response: Response):
        pairs = [(e.old_code, e.new_code) for e in payload.entries]
        record, created = store.submit_mapping(payload.version, pairs)
        if not created:
            response.status_code = 200
        return record

    @app.get(
        "/mappings/{version}",
        response_model=MappingOut,
        tags=["对照版本"],
        summary="查看某版对照内容",
    )
    def get_mapping(version: int):
        mapping = store.get_mapping(version)
        if mapping is None:
            raise ApiError(404, f"mapping version {version} not found")
        return mapping

    @app.get(
        "/versions",
        response_model=VersionListOut,
        tags=["对照版本"],
        summary="列出全部对照版本",
    )
    def list_versions():
        return store.list_versions()

    # --------------------------------------------------------------- 供数
    @app.get(
        "/summary",
        response_model=SummaryOut,
        tags=["供数"],
        summary="现行汇总：组号、两侧税号、各年合计",
    )
    def get_summary():
        return store.get_summary()

    @app.get(
        "/changes/{version}",
        response_model=ChangeRecord,
        tags=["供数"],
        summary="按版取供数变更",
        description="首版给全量；后续给被替换的版号、应撤下的组号和应写入的汇总。",
    )
    def get_change(version: int):
        record = store.get_change(version)
        if record is None:
            raise ApiError(404, f"version {version} not found")
        return record

    # ------------------------------------------------------------- 核验单
    @app.post(
        "/forms",
        response_model=FormOut,
        status_code=201,
        tags=["核验单"],
        summary="登记核验单（唯一单号、年份、税号清单、整数分合计）",
        description="税号须全部存在于底表且互不重复；合计必须与底表金额一致，"
        "否则 422 整单拒收。单号重复返回 409。",
    )
    def register_form(payload: FormIn):
        codes = payload.tax_codes
        return store.register_form(
            payload.form_no, payload.year, codes, payload.total_cents
        )

    @app.post(
        "/forms/{form_no}/revoke",
        response_model=FormOut,
        tags=["核验单"],
        summary="撤销核验单（保留原单，仅置为 revoked）",
        description="撤销是软删除，原单仍可查询；重复撤销幂等返回当前状态。",
    )
    def revoke_form(form_no: str):
        return store.revoke_form(form_no)

    @app.get(
        "/forms",
        response_model=FormListOut,
        tags=["核验单"],
        summary="列出核验单（可按 status=active|revoked 过滤）",
    )
    def list_forms(status: Optional[str] = None):
        if status is not None and status not in ("active", "revoked"):
            raise ApiError(422, "status must be one of: active, revoked")
        return store.list_forms(status)

    @app.get(
        "/forms/{form_no}",
        response_model=FormOut,
        tags=["核验单"],
        summary="按单号查看核验单（含已撤销单）",
    )
    def get_form(form_no: str):
        return store.get_form(form_no)

    # --------------------------------------------------------------- 凭证
    @app.post(
        "/vouchers/preview",
        response_model=IssueResult,
        tags=["凭证"],
        summary="预演选单：从有效单中求最少张精确覆盖（不写凭证）",
        description="传入所见对照版、组号、年份和至多 20 个已登记单号。"
        "版被替换返回 409；有效单无法拼齐返回 422；提交的撤销单导致拼不齐返回 409。",
    )
    def preview_voucher(payload: IssueIn):
        return store.preview_voucher(
            payload.observed_version,
            payload.group_id,
            payload.year,
            payload.form_nos,
        )

    @app.post(
        "/vouchers",
        response_model=VoucherOut,
        status_code=201,
        tags=["凭证"],
        summary="签发凭证：每张整用、税号互不重叠且恰好覆盖该组当年成员",
        description="在有效单中选最少张（字典序决胜）。写入事务内重读现行版号与入选单状态："
        "所见版已被替换或入选单已撤销则 409 冲突且不生成凭证；"
        "未入选的撤销单不影响签发。",
    )
    def issue_voucher(payload: IssueIn):
        return store.issue_voucher(
            payload.observed_version,
            payload.group_id,
            payload.year,
            payload.form_nos,
        )

    @app.get(
        "/vouchers",
        response_model=VoucherListOut,
        tags=["凭证"],
        summary="列出全部已签发凭证",
    )
    def list_vouchers():
        return store.list_vouchers()

    @app.get(
        "/vouchers/{voucher_no}",
        response_model=VoucherOut,
        tags=["凭证"],
        summary="按凭号查询：签发版号、组号、年份、所用单号和合计",
    )
    def get_voucher(voucher_no: str):
        return store.get_voucher(voucher_no)

    return app


app = create_app(os.environ.get("DB_PATH", "data/app.db"))

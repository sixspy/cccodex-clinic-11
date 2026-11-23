"""协议切流服务（甲边 · 快照表，唯一启用边）。

当场键 = site_id，每中心至多一条「现行」落点；落点行冻结方案编码/版号/
必填字段/访视编码并贴指纹。读侧（开批/抓取/入库/质控/补采）只读本表，
拒回刷；方案表翻牌不跟动，只认显式提交的新切流。并发以修订号钉点。
"""

from __future__ import annotations

import hashlib
import json

from app.db import get_db
from app.services import audit as audit_svc
from app.services.errors import ServiceError

MODE_LABEL = "甲·快照表"
CUTOVER_STATUS = ("现行", "封存")


def fingerprint(protocol_code: str, version: str, fields: list[str], visits: list[str]) -> str:
    """落点印记：冻结内容的 sha256 短指纹。"""
    raw = "|".join(
        [
            protocol_code or "",
            version or "",
            ",".join(str(f) for f in fields),
            ",".join(str(v) for v in visits),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _parse_list(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _with_lists(item: dict) -> dict:
    item["required_fields_list"] = _parse_list(item.get("required_fields"))
    item["visit_codes_list"] = _parse_list(item.get("visit_codes"))
    return item


def verify_fingerprint(row: dict) -> bool:
    """印记校验：冻结字段重算指纹须与落点所贴一致。"""
    expect = fingerprint(
        row.get("protocol_code", ""),
        row.get("protocol_version", ""),
        _parse_list(row.get("required_fields", "[]")),
        _parse_list(row.get("visit_codes", "[]")),
    )
    return expect == row.get("fingerprint")


def _current_row(conn, site_id: int):
    return conn.execute(
        "SELECT * FROM protocol_cutovers WHERE site_id=? AND status='现行'",
        (site_id,),
    ).fetchone()


def current_for_site(site_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = _current_row(conn, int(site_id))
        return _with_lists(dict(row)) if row else None


def list_cutovers(
    db_path: str | None = None,
    site_id: int | None = None,
    status: str | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if site_id:
        clauses.append("c.site_id=?")
        params.append(int(site_id))
    if status:
        if status not in CUTOVER_STATUS:
            raise ServiceError("切流状态无效")
        clauses.append("c.status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT c.*, s.name AS site_name, s.site_code
            FROM protocol_cutovers c
            JOIN trial_sites s ON s.id=c.site_id
            {where}
            ORDER BY c.site_id, c.revision DESC
            """,
            params,
        ).fetchall()
        return [_with_lists(dict(r)) for r in rows]


def get_cutover(cutover_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT c.*, s.name AS site_name, s.site_code
            FROM protocol_cutovers c
            JOIN trial_sites s ON s.id=c.site_id
            WHERE c.id=?
            """,
            (cutover_id,),
        ).fetchone()
        if not row:
            raise ServiceError("切流落点不存在", 404)
        item = _with_lists(dict(row))
        item["fingerprint_ok"] = verify_fingerprint(item)
        return item


def submit_cutover(
    site_id: int,
    protocol_id: int,
    expected_revision: int,
    note: str = "",
    db_path: str | None = None,
) -> dict:
    """切流闸：提交即落定入库；失败必须讲明白。

    - 目标方案须「生效」，否则拒；
    - 期望修订号须等于现行修订（无落点时为 0），否则 409 钉点冲突；
    - 同事务内：旧现行转封存（带修订号条件，防双成功互冲），新行接任现行。
    """
    site_id = int(site_id or 0)
    protocol_id = int(protocol_id or 0)
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError):
        raise ServiceError("切流闸：期望修订号必须是整数") from None
    with get_db(db_path) as conn:
        site = conn.execute(
            "SELECT * FROM trial_sites WHERE id=?", (site_id,)
        ).fetchone()
        if not site:
            raise ServiceError("切流闸：试验中心不存在", 404)
        proto = conn.execute(
            "SELECT * FROM protocols WHERE id=?", (protocol_id,)
        ).fetchone()
        if not proto:
            raise ServiceError("切流闸：目标方案版本不存在", 404)
        if proto["status"] != "生效":
            raise ServiceError(
                f"切流闸：目标方案 {proto['protocol_code']}@{proto['version']} "
                f"未生效（当前{proto['status']}），不能切流"
            )
        current = _current_row(conn, site_id)
        current_rev = current["revision"] if current else 0
        if current and current["protocol_id"] == protocol_id:
            raise ServiceError(
                f"切流闸：{site['name']} 现行落点已是 "
                f"{proto['protocol_code']}@{proto['version']}，无需切流"
            )
        if expected_revision != current_rev:
            raise ServiceError(
                f"切流钉点冲突：期望修订 {expected_revision}，"
                f"当前修订 {current_rev}，请刷新台账后重试",
                409,
            )
        fields = _parse_list(proto["required_fields"])
        visits = _parse_list(proto["visit_codes"])
        fp = fingerprint(proto["protocol_code"], proto["version"], fields, visits)
        if current:
            cur = conn.execute(
                """
                UPDATE protocol_cutovers
                SET status='封存', sealed_at=datetime('now')
                WHERE site_id=? AND status='现行' AND revision=?
                """,
                (site_id, current_rev),
            )
            if cur.rowcount != 1:
                raise ServiceError(
                    "切流钉点冲突：现行落点已被他人改写，请刷新台账后重试", 409
                )
        conn.execute(
            """
            INSERT INTO protocol_cutovers(
              site_id, protocol_id, protocol_code, protocol_version,
              required_fields, visit_codes, fingerprint, revision, status, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '现行', ?)
            """,
            (
                site_id,
                protocol_id,
                proto["protocol_code"],
                proto["version"],
                json.dumps(fields, ensure_ascii=False),
                json.dumps(visits, ensure_ascii=False),
                fp,
                current_rev + 1,
                (note or "").strip()[:200],
            ),
        )
    audit_svc.write_event(
        "提交协议切流",
        "protocol_cutover",
        site_id,
        f"{site['name']} → {proto['protocol_code']}@{proto['version']} r{current_rev + 1}",
        db_path=db_path,
    )
    return current_for_site(site_id, db_path)


def gate_current_conn(conn, site_id: int) -> dict:
    """闸（持连接版）：取现行落点并校验印记，失败讲明白。"""
    row = _current_row(conn, int(site_id))
    if not row:
        raise ServiceError(
            "切流闸：该中心尚无协议切流落点，已拦截；请先在试验中心页提交切流"
        )
    item = dict(row)
    if not verify_fingerprint(item):
        raise ServiceError(
            f"切流闸：落点印记校验失败（{item['protocol_code']}@"
            f"{item['protocol_version']} 指纹不符），已拦截读写，请复核快照表"
        )
    return item


def gate_batch_conn(conn, batch) -> dict:
    """闸（批次版）：无落点 / 拧花 / 印记坏，一律拦截并讲明白。"""
    cutover = gate_current_conn(conn, batch["site_id"])
    if batch["protocol_id"] != cutover["protocol_id"]:
        raise ServiceError(
            f"切流闸：批次绑旧方案（protocol_id={batch['protocol_id']}），"
            f"中心现行落点 {cutover['protocol_code']}@{cutover['protocol_version']}"
            f"（修订{cutover['revision']}），徽标拧花，已拦截旧协议抓取"
        )
    return cutover

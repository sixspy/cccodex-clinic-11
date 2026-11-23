from __future__ import annotations

import json

from app.db import get_db
from app.services import audit as audit_svc
from app.services import cutover as cutover_svc
from app.services.errors import ServiceError

SEVERITIES = ("提示", "一般", "严重")
FINDING_STATUS = ("开放", "已关闭")


def list_findings(
    db_path: str | None = None,
    status: str | None = None,
    row_id: int | None = None,
    severity: str | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if status:
        if status not in FINDING_STATUS:
            raise ServiceError("质控发现状态无效")
        clauses.append("f.status=?")
        params.append(status)
    if row_id:
        clauses.append("f.row_id=?")
        params.append(int(row_id))
    if severity:
        if severity not in SEVERITIES:
            raise ServiceError("严重度无效")
        clauses.append("f.severity=?")
        params.append(severity)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT f.*, r.subject_key, r.visit_code, r.quality, r.status AS row_status,
                   b.batch_code, s.name AS site_name
            FROM qc_findings f
            JOIN visit_rows r ON r.id=f.row_id
            JOIN visit_batches b ON b.id=r.batch_id
            JOIN trial_sites s ON s.id=b.site_id
            {where}
            ORDER BY f.id DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_finding(finding_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT f.*, r.subject_key, r.visit_code, r.payload, r.quality,
                   b.batch_code, s.name AS site_name
            FROM qc_findings f
            JOIN visit_rows r ON r.id=f.row_id
            JOIN visit_batches b ON b.id=r.batch_id
            JOIN trial_sites s ON s.id=b.site_id
            WHERE f.id=?
            """,
            (finding_id,),
        ).fetchone()
        if not row:
            raise ServiceError("质控发现不存在", 404)
        return dict(row)


def _severity_for_quality(quality: str) -> str:
    if quality == "编码空":
        return "严重"
    if quality == "重复键":
        return "一般"
    if quality == "缺字段":
        return "一般"
    return "提示"


def _field_hints(quality: str, payload: str, required: list[str]) -> list[tuple[str, str]]:
    hints: list[tuple[str, str]] = []
    if quality == "编码空":
        hints.append(("payload", "载荷为空或受试者/访视编码缺失"))
        return hints
    if quality == "重复键":
        hints.append(("subject_key", "同中心开放批内出现重复受试者访视键"))
        return hints
    if quality == "缺字段":
        missing = []
        try:
            data = json.loads(payload or "{}")
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        for field in required:
            if field in ("subject_key", "visit_code"):
                continue
            val = data.get(field)
            if val is None or str(val).strip() == "":
                missing.append(field)
        if not missing:
            missing = ["unknown_field"]
        for field in missing:
            hints.append((field, f"必填字段缺失: {field}"))
        return hints
    return [("quality", "合格行无需发现问题")]


def scan_batch(batch_id: int, db_path: str | None = None) -> dict:
    created = []
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM visit_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("访视批次不存在", 404)
        # 切流闸：拦截按旧版扫描；必填字段只读落点快照
        cutover = cutover_svc.gate_batch_conn(conn, batch)
        required = cutover_svc._parse_list(cutover["required_fields"]) or [
            "subject_key",
            "visit_code",
            "visit_date",
            "investigator",
        ]
        rows = conn.execute(
            """
            SELECT * FROM visit_rows
            WHERE batch_id=? AND quality!='合格' AND status IN ('待检','待补采')
            ORDER BY id
            """,
            (batch_id,),
        ).fetchall()
        for row in rows:
            existing = conn.execute(
                """
                SELECT COUNT(*) AS c FROM qc_findings
                WHERE row_id=? AND status='开放'
                """,
                (row["id"],),
            ).fetchone()["c"]
            if existing:
                continue
            hints = _field_hints(row["quality"], row["payload"], required)
            sev = _severity_for_quality(row["quality"])
            for field_name, message in hints:
                cur = conn.execute(
                    """
                    INSERT INTO qc_findings(row_id, field_name, severity, message, status)
                    VALUES (?, ?, ?, ?, '开放')
                    """,
                    (row["id"], field_name, sev, message),
                )
                fid = cur.lastrowid
                finding = conn.execute(
                    "SELECT * FROM qc_findings WHERE id=?", (fid,)
                ).fetchone()
                created.append(dict(finding))
            if row["status"] == "待检":
                conn.execute(
                    "UPDATE visit_rows SET status='待补采' WHERE id=?",
                    (row["id"],),
                )
    audit_svc.write_event(
        "质控扫描",
        "visit_batch",
        batch_id,
        f"新建发现{len(created)}条",
        db_path=db_path,
    )
    return {
        "created": len(created),
        "findings": created,
        "open_total": len(list_findings(db_path=db_path, status="开放")),
    }


def scan_all_open(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        batches = conn.execute(
            """
            SELECT id FROM visit_batches
            WHERE status IN ('采集中','已收齐')
            ORDER BY id
            """
        ).fetchall()
    total = 0
    details = []
    blocked = 0
    for b in batches:
        try:
            result = scan_batch(b["id"], db_path=db_path)
        except ServiceError as exc:
            # 切流闸拦截的批次不拖垮全量扫描，逐批讲明白
            blocked += 1
            details.append({"batch_id": b["id"], "created": 0, "blocked": exc.message})
            continue
        total += result["created"]
        details.append({"batch_id": b["id"], "created": result["created"]})
    return {"created": total, "blocked": blocked, "batches": details}


def close_finding(finding_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM qc_findings WHERE id=?", (finding_id,)
        ).fetchone()
        if not row:
            raise ServiceError("质控发现不存在", 404)
        if row["status"] == "已关闭":
            return get_finding(finding_id, db_path)
        conn.execute(
            "UPDATE qc_findings SET status='已关闭' WHERE id=?", (finding_id,)
        )
        open_left = conn.execute(
            """
            SELECT COUNT(*) AS c FROM qc_findings
            WHERE row_id=? AND status='开放'
            """,
            (row["row_id"],),
        ).fetchone()["c"]
        if open_left == 0:
            vr = conn.execute(
                "SELECT * FROM visit_rows WHERE id=?", (row["row_id"],)
            ).fetchone()
            if vr and vr["status"] == "待补采" and vr["quality"] == "合格":
                conn.execute(
                    "UPDATE visit_rows SET status='已入库' WHERE id=?",
                    (row["row_id"],),
                )
    audit_svc.write_event(
        "关闭质控发现", "qc_finding", finding_id, "", db_path=db_path
    )
    return get_finding(finding_id, db_path)


def reopen_finding(finding_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM qc_findings WHERE id=?", (finding_id,)
        ).fetchone()
        if not row:
            raise ServiceError("质控发现不存在", 404)
        conn.execute(
            "UPDATE qc_findings SET status='开放' WHERE id=?", (finding_id,)
        )
        conn.execute(
            """
            UPDATE visit_rows SET status='待补采'
            WHERE id=? AND status IN ('待检','已入库')
            """,
            (row["row_id"],),
        )
    audit_svc.write_event(
        "重开质控发现", "qc_finding", finding_id, "", db_path=db_path
    )
    return get_finding(finding_id, db_path)


def summary(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        open_n = conn.execute(
            "SELECT COUNT(*) AS c FROM qc_findings WHERE status='开放'"
        ).fetchone()["c"]
        closed_n = conn.execute(
            "SELECT COUNT(*) AS c FROM qc_findings WHERE status='已关闭'"
        ).fetchone()["c"]
        by_sev = conn.execute(
            """
            SELECT severity, COUNT(*) AS c
            FROM qc_findings WHERE status='开放'
            GROUP BY severity
            """
        ).fetchall()
        by_field = conn.execute(
            """
            SELECT field_name, COUNT(*) AS c
            FROM qc_findings WHERE status='开放'
            GROUP BY field_name
            ORDER BY c DESC
            LIMIT 8
            """
        ).fetchall()
        return {
            "open": open_n,
            "closed": closed_n,
            "by_severity": {r["severity"]: r["c"] for r in by_sev},
            "by_field": [dict(r) for r in by_field],
        }

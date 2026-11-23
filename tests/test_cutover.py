"""协议切流（甲·快照表）——逐条对照 docs/protocol-cutover.md 败例清单。"""

import pytest

from app.db import get_db
from app.services import backfill, cutover, protocols, qc, settings, sites, visits
from app.services.errors import ServiceError


def _new_protocol(db_path, version="2.0", visits_codes=None, fields=None):
    p = protocols.create_protocol(
        "肿瘤随访方案新版",
        version=version,
        required_fields=fields or ["subject_key", "visit_code", "visit_date", "investigator", "ecog"],
        visit_codes=visits_codes or ["V1", "V2", "FU"],
        db_path=db_path,
    )
    return protocols.set_status(p["id"], "生效", db_path=db_path)


def test_seed_landing_points_in_table(db_path):
    """落点入表：每个种子中心都有现行落点，指纹可核。"""
    rows = cutover.list_cutovers(db_path=db_path, status="现行")
    assert len(rows) >= 4
    for row in rows:
        assert row["revision"] == 1
        assert cutover.verify_fingerprint(row)
        assert row["protocol_code"] == "PROT-001"


def test_submit_cutover_seals_old_and_pins_new(db_path):
    """提交即落定入库：旧行封存、新行现行、修订 +1，封存不回刷。"""
    site = sites.list_sites(db_path)[0]
    proto = _new_protocol(db_path)
    done = cutover.submit_cutover(site["id"], proto["id"], 1, note="换版切流", db_path=db_path)
    assert done["status"] == "现行"
    assert done["revision"] == 2
    assert done["protocol_code"] == proto["protocol_code"]
    assert cutover.verify_fingerprint(done)
    ledger = cutover.list_cutovers(db_path=db_path, site_id=site["id"])
    sealed = [r for r in ledger if r["status"] == "封存"]
    live = [r for r in ledger if r["status"] == "现行"]
    assert len(sealed) == 1 and sealed[0]["revision"] == 1 and sealed[0]["sealed_at"]
    assert len(live) == 1 and live[0]["revision"] == 2
    # 方案表再翻牌，落点不跟动（拒回刷）
    protocols.set_status(proto["id"], "废止", db_path=db_path)
    still = cutover.current_for_site(site["id"], db_path=db_path)
    assert still["protocol_id"] == proto["id"]


def test_revision_conflict_blocks_double_success(db_path):
    """争用收口：两侧同事同读 r1 各交一次，一成功一 409，绝不双成功互冲。"""
    site = sites.list_sites(db_path)[0]
    proto = _new_protocol(db_path)
    cutover.submit_cutover(site["id"], proto["id"], 1, db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        cutover.submit_cutover(site["id"], proto["id"], 1, db_path=db_path)
    assert exc.value.status_code in (400, 409)
    # 换一个更新方案、仍拿旧修订号提交 → 钉点冲突 409
    proto3 = protocols.create_protocol("肿瘤随访方案丙", version="3.0", db_path=db_path)
    protocols.set_status(proto3["id"], "生效", db_path=db_path)
    with pytest.raises(ServiceError) as exc2:
        cutover.submit_cutover(site["id"], proto3["id"], 1, db_path=db_path)
    assert exc2.value.status_code == 409
    assert "钉点冲突" in exc2.value.message
    ok = cutover.submit_cutover(site["id"], proto3["id"], 2, db_path=db_path)
    assert ok["revision"] == 3


def test_gate_explains_failures(db_path):
    """闸失败要讲明白：未生效 / 同方案 / 缺中心。"""
    site = sites.list_sites(db_path)[0]
    draft = protocols.create_protocol("草稿方案", version="0.1", db_path=db_path)
    with pytest.raises(ServiceError, match="未生效"):
        cutover.submit_cutover(site["id"], draft["id"], 1, db_path=db_path)
    active = protocols.active_protocol(db_path)
    with pytest.raises(ServiceError, match="无需切流"):
        cutover.submit_cutover(site["id"], active["id"], 1, db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        cutover.submit_cutover(99999, active["id"], 0, db_path=db_path)
    assert exc.value.status_code == 404


def test_mismatch_blocks_old_protocol_ingestion(db_path):
    """拧花即拦：切流后旧版批次的抓取/追加/入库/质控/补采全被闸拦。"""
    site = next(s for s in sites.list_sites(db_path) if s["status"] == "启用")
    old_batch = visits.create_batch(site["id"], db_path=db_path)
    visits.mock_crawl(old_batch["id"], count=2, mix_bad=True, db_path=db_path)
    proto = _new_protocol(db_path)
    cutover.submit_cutover(site["id"], proto["id"], 1, db_path=db_path)
    batch = visits.get_batch(old_batch["id"], db_path)
    assert batch["cutover_state"] == "拧花"
    with pytest.raises(ServiceError, match="拧花"):
        visits.mock_crawl(old_batch["id"], count=1, db_path=db_path)
    with pytest.raises(ServiceError, match="拧花"):
        visits.add_visit_row(old_batch["id"], "X-1", "V1", payload="{}", db_path=db_path)
    with pytest.raises(ServiceError, match="拧花"):
        visits.ingest_ok_rows(old_batch["id"], db_path=db_path)
    with pytest.raises(ServiceError, match="拧花"):
        qc.scan_batch(old_batch["id"], db_path=db_path)
    gap = next(r for r in batch["items"] if r["quality"] != "合格")
    with pytest.raises(ServiceError, match="拧花"):
        backfill.enqueue_from_row(gap["id"], db_path=db_path)
    # 新批次自动挂落点方案，抓取用新访视编码
    new_batch = visits.create_batch(site["id"], db_path=db_path)
    assert new_batch["protocol_id"] == proto["id"]
    assert new_batch["cutover_state"] == "一致"
    crawled = visits.mock_crawl(new_batch["id"], count=3, mix_bad=False, db_path=db_path)
    assert {r["visit_code"] for r in crawled["items"]} <= {"V1", "V2", "FU"}
    # 显式挂非落点方案被拒（换干净中心，避开采集批次配额）
    fresh = sites.create_site("切流对照中心", db_path=db_path)
    assert fresh["cutover_protocol_id"] == proto["id"]
    with pytest.raises(ServiceError, match="不能挂接其他方案"):
        visits.create_batch(fresh["id"], protocol_id=1, db_path=db_path)


def test_no_landing_point_blocks_ingestion(db_path):
    """无落点仍抓取 = 败：无生效方案时登记的中心，开批即被闸拦。"""
    for p in protocols.list_protocols(db_path):
        if p["status"] == "生效":
            protocols.set_status(p["id"], "草稿", db_path=db_path)
    site = sites.create_site("无落点中心", db_path=db_path)
    assert site["cutover_id"] is None
    with pytest.raises(ServiceError, match="尚无协议切流落点"):
        visits.create_batch(site["id"], db_path=db_path)


def test_fingerprint_tamper_blocks_read_write(db_path):
    """印记对不齐仍放行 = 败：篡改落点字段后读写被闸拦。"""
    site = sites.list_sites(db_path)[0]
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE protocol_cutovers SET required_fields='[\"hacked\"]' "
            "WHERE site_id=? AND status='现行'",
            (site["id"],),
        )
    batch_list = visits.list_batches(db_path, site_id=site["id"], status="采集中")
    if batch_list:
        with pytest.raises(ServiceError, match="印记校验失败"):
            visits.mock_crawl(batch_list[0]["id"], count=1, db_path=db_path)
    with pytest.raises(ServiceError, match="印记校验失败"):
        visits.create_batch(site["id"], db_path=db_path)


def test_cutover_mode_setting_is_readonly(db_path):
    """台参数只展切流口径，拒改。"""
    item = settings.get_setting("protocol_cutover_mode", db_path=db_path)
    assert item["value"] == "甲·快照表"
    with pytest.raises(ServiceError, match="只读"):
        settings.update_setting("protocol_cutover_mode", "乙·归档层", db_path=db_path)


def test_scan_all_skips_blocked_batches(db_path):
    """全量质控扫描：拧花批次逐批讲明白，不拖垮整扫。"""
    site = next(s for s in sites.list_sites(db_path) if s["status"] == "启用")
    visits.create_batch(site["id"], db_path=db_path)
    proto = _new_protocol(db_path)
    cutover.submit_cutover(site["id"], proto["id"], 1, db_path=db_path)
    result = qc.scan_all_open(db_path=db_path)
    assert result["blocked"] >= 1
    assert any("blocked" in d for d in result["batches"])

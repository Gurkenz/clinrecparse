import json
from pathlib import Path

from clinrec.models.external import (
    CatalogResponse,
    ClinrecResponse,
    NkoListResponse,
    PdfDownloadResult,
)

FIXTURES = Path("tests/fixtures")


def load_json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_catalog_model_accepts_real_shape_fields() -> None:
    catalog = CatalogResponse.model_validate(load_json("catalog_843_1_real_shape.json"))

    assert catalog.success is True
    assert catalog.total == 1
    assert catalog.errors == []
    record = catalog.data[0]
    assert record.code_version == "843_1"
    assert record.npc_approved is True
    assert record.apply_status_calculated == 1
    assert record.age_category_name == "Взрослые"
    assert record.prev_cr_id is None


def test_clinrec_model_accepts_obj_sections() -> None:
    response = ClinrecResponse.model_validate(load_json("clinrec_270_3_real_shape.json"))

    assert response.obj.code_version == "270_3"
    assert response.obj.sections[0].sections[0].title == "Nested section"
    assert response.obj.prev_cr_id == 1002


def test_clinrec_model_merges_top_level_metadata_into_obj() -> None:
    response = ClinrecResponse.model_validate(
        {
            "id": "270_2",
            "code": 270,
            "version": 2,
            "name": "Top-level title",
            "obj": {"sections": [{"id": 1, "title": "Section"}]},
        }
    )

    assert response.obj.code_version == "270_2"
    assert response.obj.code == 270
    assert response.obj.version == 2
    assert response.obj.title == "Top-level title"


def test_nko_model_accepts_d_success_data_wrapper() -> None:
    response = NkoListResponse.model_validate(load_json("nko_list_real_shape.json"))

    assert response.d.success is True
    assert response.d.data[0].name == "Fixture Medical Association"
    assert response.d.data[0].short_name == "FMA"
    assert response.d.data[0].raw_short_name == "  FMA  "


def test_pdf_download_result_model() -> None:
    content = (FIXTURES / "pdf_sample.pdf").read_bytes()
    result = PdfDownloadResult(
        code_version="843_1",
        status_code=200,
        content_type="application/pdf",
        content=content,
        response_size_bytes=len(content),
        duration_seconds=0.1,
    )

    assert result.content.startswith(b"%PDF")
    assert result.response_size_bytes == len(content)

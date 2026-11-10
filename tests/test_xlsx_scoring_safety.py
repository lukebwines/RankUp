"""Sicurezza XLSX e corrispondenza tra massimali delle tre schede.

Tutti i documenti generati sono sintetici; nessuna fonte amministrativa
viene modificata o ricreata da un modello.
"""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from openpyxl.utils import get_column_letter
from pypdf import PdfReader

from pica import parse_pica
from commissioni import parse_commissioni
from test_import import pica_fixture, commissioni_fixture
from xlsx_guard import validate_xlsx
from score_rules import (
    ORDINARY_MAX, ENGLISH_MAX, PROFESSIONAL_MAX,
    INDICATOR_LIMITS, AREA_LIMITS, TOTAL_LIMITS,
    FIXED_VALUE_FIELDS, PRO_ACTIVITY_OPTIONS, PRO_PLUS_OPTIONS,
    indicator_limit, area_limit, total_limit,
)
import ordinary
import english
import professional
import pdf_import


def _edit_member(xlsx: bytes, member: str, edit) -> bytes:
    result = BytesIO()
    with ZipFile(BytesIO(xlsx)) as original, ZipFile(result,"w",ZIP_DEFLATED) as rebuilt:
        for item in original.infolist():
            data=original.read(item)
            if item.filename==member:
                data=edit(data)
            rebuilt.writestr(item.filename,data)
    return result.getvalue()


def _edit_sheet(xlsx: bytes, edit) -> bytes:
    return _edit_member(xlsx,"xl/worksheets/sheet1.xml",edit)


def test_regular_pica_and_commissioni_still_import():
    assert parse_pica("fixture.xlsx",pica_fixture())["summary"]["candidature"]==3
    assert parse_commissioni("commissioni.xlsx",commissioni_fixture(),"26-02")[
        "numero_sedi"]==2


def test_missing_dimension_does_not_hide_valid_rows_or_columns():
    def remove_dimension(xml):
        result=re.sub(rb'<dimension\s+ref="[^"]+"\s*/>',b"",xml)
        assert result!=xml
        return result
    assert parse_pica("pica.xlsx",_edit_sheet(pica_fixture(),remove_dimension))[
        "summary"]["candidature"]==3
    assert parse_commissioni("comm.xlsx",_edit_sheet(commissioni_fixture(),remove_dimension),
                           "26-02")["numero_sedi"]==2


def test_oversized_row_rejected_even_when_dimension_claims_small_range():
    def add_row(xml):
        oversized=(
            b'<row r="20001"><c r="A20001" t="inlineStr">'
            b'<is><t>illegitimate</t></is></c></row>'
        )
        return re.sub(rb'<dimension\s+ref="[^"]+"\s*/>',
                      b'<dimension ref="A1:B5"/>',xml).replace(
                      b"</sheetData>",oversized+b"</sheetData>")
    malicious=_edit_sheet(pica_fixture(),add_row)
    with pytest.raises(ValueError,match="limite effettivo"):
        parse_pica("pica.xlsx",malicious)


def test_oversized_column_rejected_even_if_pica_dimension_missing():
    assert get_column_letter(301)=="KO"
    def add_column(xml):
        bad=b'<row r="6"><c r="KO6" t="inlineStr"><is><t>X</t></is></c></row>'
        return re.sub(rb'<dimension\s+ref="[^"]+"\s*/>',b"",xml).replace(
            b"</sheetData>",bad+b"</sheetData>")
    with pytest.raises(ValueError,match="limite effettivo"):
        parse_pica("pica.xlsx",_edit_sheet(pica_fixture(),add_column))


def test_commissione_limit_rejects_far_row_when_dimension_is_absent():
    def append_row(xml):
        bad=b'<row r="10001"><c r="A10001" t="inlineStr"><is><t>X</t></is></c></row>'
        return re.sub(rb'<dimension\s+ref="[^"]+"\s*/>',b"",xml).replace(
            b"</sheetData>",bad+b"</sheetData>")
    with pytest.raises(ValueError,match="limite effettivo"):
        parse_commissioni("comm.xlsx",_edit_sheet(commissioni_fixture(),append_row),"26-02")


def test_xml_doctype_or_entity_is_rejected_before_openpyxl():
    def add_doctype(xml):
        declaration=b'<!DOCTYPE workbook [<!ENTITY local "XX">]>'
        if xml.startswith(b"<?xml"):
            head=xml.find(b"?>")
            return xml[:head+2]+declaration+xml[head+2:]
        return declaration+xml
    crafted=_edit_member(pica_fixture(),"xl/workbook.xml",add_doctype)
    with pytest.raises(ValueError,match="DOCTYPE"):
        parse_pica("pica.xlsx",crafted)


def test_decompressed_size_is_capped_independent_of_zip_file_size():
    normal=pica_fixture()
    target=BytesIO()
    with ZipFile(BytesIO(normal)) as src, ZipFile(target,"w",ZIP_DEFLATED) as out:
        for item in src.infolist():
            out.writestr(item.filename,src.read(item))
        # 2 MiB of repetitive data compresses to a few kilobytes.
        out.writestr("xl/media/test_file.bin",b"x"*(2*1024*1024))
    packed=target.getvalue()
    assert len(packed)<100_000
    with pytest.raises(ValueError,match="troppo grande dopo la decompressione"):
        validate_xlsx(packed,max_uncompressed=1024*1024,max_xml_part=512*1024,
                      max_rows=20000,max_columns=300)


def test_archive_traversal_is_rejected():
    target=BytesIO()
    with ZipFile(BytesIO(pica_fixture())) as src, ZipFile(target,"w",ZIP_DEFLATED) as out:
        for item in src.infolist():
            out.writestr(item.filename,src.read(item))
        out.writestr("../unsafe.txt",b"test")
    with pytest.raises(ValueError,match="Percorso interno"):
        parse_pica("pica.xlsx",target.getvalue())


def test_defusedxml_is_declared_and_directly_available():
    req=(Path(__file__).resolve().parents[1]/
         "rankup/app/requirements.txt").read_text(encoding="utf-8")
    assert "defusedxml" in req
    import defusedxml.ElementTree
    assert defusedxml.ElementTree is not None


def _options_as_numbers(field) -> list[Decimal]:
    options=field.get("/Opt") or []
    result=[]
    for item in options:
        value=item[0] if isinstance(item,(tuple,list)) else item
        result.append(Decimal(str(value)))
    return result


@pytest.mark.parametrize("model,make,maximums",[
    ("ordinario",ordinary.make_ordinary,ORDINARY_MAX),
    ("inglese",english.make_english,ENGLISH_MAX),
    ("professionalizzante",professional.make_professional,PROFESSIONAL_MAX),
])
def test_all_generated_pdf_field_options_match_single_scoring_source(model,make,maximums):
    record={"bando":"COLLAUDO-TEST","anno":"[DA VERIFICARE]",
            "id_pica":"001","cognome":"COGNOME SINTETICO","nome":"NOME SINTETICO",
            "cds":"CDS SINTETICO","sede":"SEDE DI TEST","insegnamento":"MODULO TEST",
            "modulo":"MODULO SINTETICO","scheda_id":"RU-COLLAUDO-TEST-001",
            "presidente":"PRESIDENTE SINTETICO","componenti":"COMPONENTI SINTETICI",
            "requisiti":"[DA VERIFICARE]"}
    pdf=make(record)
    fields=PdfReader(BytesIO(pdf)).get_fields() or {}
    assert set(maximums).issubset(fields)
    for key,limit in maximums.items():
        options=_options_as_numbers(fields[key])
        assert options and max(options)==Decimal(limit),(model,key,options)
        assert min(options)==0,(model,key,options)
        if key in FIXED_VALUE_FIELDS[model]:
            assert set(options)=={Decimal("0"),Decimal(limit)}
    assert fields["TOT"].value=="0"


def test_shared_tables_cover_generators_and_reader_without_changing_values():
    assert pdf_import.ORDINARY_MAX is ORDINARY_MAX
    assert pdf_import.ENGLISH_MAX is ENGLISH_MAX
    assert pdf_import.PROFESSIONAL_MAX is PROFESSIONAL_MAX
    assert ORDINARY_MAX["P14"]==3
    assert ENGLISH_MAX["P02"]==3 and ENGLISH_MAX["P05"]==1
    assert PROFESSIONAL_MAX["P18"]==3
    assert AREA_LIMITS["ordinario"]==(15,10,15,10,3,7)
    assert AREA_LIMITS["inglese"]==(13,20,10,7)
    assert AREA_LIMITS["professionalizzante"]==(15,10,15,7,3,10)
    assert TOTAL_LIMITS=={"ordinario":60,"inglese":50,"professionalizzante":60}
    for model,maximums in INDICATOR_LIMITS.items():
        assert all(indicator_limit(model,key)==points for key,points in maximums.items())
        assert sum(AREA_LIMITS[model])==total_limit(model)
        assert all(area_limit(model,i)==x for i,x in enumerate(AREA_LIMITS[model],1))
    for entry in ordinary.TITLES+ordinary.DIDATTICA:
        assert entry[2]==ORDINARY_MAX[entry[0]]
    for entry in professional.AREA1:
        assert entry[2]==PROFESSIONAL_MAX[entry[0]]
    for entry in professional.AREA3:
        assert entry[2]==PROFESSIONAL_MAX[entry[0]]
    for entry in (professional.AREA4,professional.AREA5,professional.AREA6):
        assert entry[2]==PROFESSIONAL_MAX[entry[0]]
    assert professional.PRO_ACTIVITY==list(PRO_ACTIVITY_OPTIONS)
    assert professional.PRO_PLUS==list(PRO_PLUS_OPTIONS)


def test_generators_refer_to_all_area_and_total_rules_without_literal_limits():
    src=(Path(__file__).resolve().parents[1]/"rankup/app")
    for model,module,area_count in (
        ("ordinario","ordinary.py",6),
        ("inglese","english.py",4),
        ("professionalizzante","professional.py",6)
    ):
        text=(src/module).read_text(encoding="utf-8")
        for n in range(1,area_count+1):
            assert f'area_limit("{model}", {n})' in text or (
                f'area_limit("{model}",{n})' in text)
        assert f'total_limit("{model}")' in text

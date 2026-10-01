from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import app


def test_todas_aets_tv():
    documentos = pd.DataFrame([
        {"documento": tipo, "vencimento": vencimento, "placa": placa, "composicao": ""}
        for tipo, vencimento, placa in [
            ("AETs", "2026-09-30", "AAA0001"),
            ("AETs", "2026-10-10", "AAA0002"),
            ("AETs", "2027-10-01", "AAA0003"),
            ("AETs", "", "AAA0004"),
            ("CRLV", "2027-10-01", "BBB0001"),
            ("CRLV", "2026-10-31", "BBB0002"),
            ("CRLV", "2026-09-30", "BBB0003"),
        ]
    ])
    resultado = app.buscar_vencimentos_proximos(documentos, date(2026, 10, 1))
    assert len(resultado) == 6
    assert len(resultado[resultado.tipo_documento == "AETs"]) == 4
    tabela = app.preparar_vencimentos_proximos_tv(resultado)
    assert tabela.iloc[-1]["Vencimento"] == "Sem data"
    assert "vencido" in tabela.iloc[0]["Prazo"]
    html = app.montar_html_painel_vencimentos_proximos(tabela, "teste")
    assert "AAA0003" in html and "AAA0004" in html and "linha-vencida" in html
    with TemporaryDirectory() as directory, \
            patch.object(app, "WEB_DATA_DIR", Path(directory)), \
            patch.object(app, "WEB_JSON_PATH", Path(directory) / "test.json"), \
            patch.object(app, "sincronizar_logo_web"), \
            patch.object(app, "carregar_documentos", return_value=documentos):
        payload = app.exportar_vencimentos_proximos_json(date(2026, 10, 1))
        assert len(payload["registros"]) == 6
        assert payload["registros"][-1]["dias_restantes"] is None


if __name__ == "__main__":
    test_todas_aets_tv()
    print("OK: todas as AETs na TV e JSON; filtro dos demais documentos preservado.")

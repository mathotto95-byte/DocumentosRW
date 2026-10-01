from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import app


def test_aets_respeitam_janela_tv():
    documentos = pd.DataFrame([
        {"documento": tipo, "vencimento": vencimento, "placa": placa, "composicao": ""}
        for tipo, vencimento, placa in [
            ("AETs", "2026-09-30", "AAA0001"),
            ("AETs", "2026-10-10", "AAA0002"),
            ("AETs", "2027-10-01", "AAA0003"),
            ("AETs", "", "AAA0004"),
            ("AETs", "2026-10-31", "AAA0005"),
            ("AETs", "2026-11-01", "AAA0006"),
            ("CRLV", "2027-10-01", "BBB0001"),
            ("CRLV", "2026-10-31", "BBB0002"),
            ("CRLV", "2026-09-30", "BBB0003"),
        ]
    ])
    resultado = app.buscar_vencimentos_proximos(documentos, date(2026, 10, 1))
    assert len(resultado) == 5
    assert len(resultado[resultado.tipo_documento == "AETs"]) == 3
    assert resultado.dias_restantes.max() == 30
    tabela = app.preparar_vencimentos_proximos_tv(resultado)
    assert "vencido" in tabela.iloc[0]["Prazo"]
    html = app.montar_html_painel_vencimentos_proximos(tabela, "teste")
    assert "AAA0005" in html and "linha-vencida" in html
    assert all(placa not in html for placa in ["AAA0003", "AAA0004", "AAA0006"])
    with TemporaryDirectory() as directory, \
            patch.object(app, "WEB_DATA_DIR", Path(directory)), \
            patch.object(app, "WEB_JSON_PATH", Path(directory) / "test.json"), \
            patch.object(app, "sincronizar_logo_web"), \
            patch.object(app, "carregar_documentos", return_value=documentos):
        payload = app.exportar_vencimentos_proximos_json(date(2026, 10, 1))
        assert len(payload["registros"]) == 5
        assert all(item["dias_restantes"] <= 30 for item in payload["registros"])


if __name__ == "__main__":
    test_aets_respeitam_janela_tv()
    print("OK: AETs e demais documentos respeitam 30 dias; vencidos preservados.")

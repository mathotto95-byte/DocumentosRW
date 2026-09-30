import base64
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import URLError
from zoneinfo import ZoneInfo

import app


def test_backup_diario():
    now = datetime(2026, 9, 30, 21, 59, tzinfo=ZoneInfo("America/Sao_Paulo"))
    config = {
        "token": "test-token", "repository": "test/test", "branch": "main",
        "latest_path": app.GITHUB_BACKUP_PATH, "auto_backup": True,
    }
    with TemporaryDirectory() as directory, \
            patch.object(app, "DATA_DIR", Path(directory)), \
            patch.object(app, "DB_PATH", Path(directory) / "test.db"), \
            patch.object(app, "agora_local", side_effect=lambda: now), \
            patch.object(app, "request_github") as github:
        app.inicializar_banco()
        with closing(app.conectar()) as conn, conn:
            conn.execute("""INSERT INTO documentos
                (placa, documento, vencimento, composicao, importado_em, importado_por)
                VALUES ('ABC1D23', 'CRLV', '2026-10-01', 'ABC1D23', '2026-09-30', 'teste')""")
            conn.execute("""INSERT INTO manutencoes_programadas
                (ordem, placa, manutencao_programada, data_saida, atualizado_em)
                VALUES (0, 'ABC1D23', 'Freios', NULL, '2026-09-30')""")
            conn.execute("""INSERT INTO manutencoes_pneus
                (ordem, nome_recapadora, servico, atualizado_em)
                VALUES (0, 'Teste', 'Recapagem', '2026-09-30')""")
            conn.execute("""INSERT INTO base_composicoes_ativa
                (id, conteudo_json, total_linhas, atualizado_em, atualizado_por)
                VALUES (1, '[]', 0, '2026-09-30', 'teste')""")

        # Backups antigos de manutencao nao substituem o primeiro completo.
        app.registrar_backup_manutencoes_github("SUCESSO", "Backup antigo")
        assert app.backup_manutencoes_github_devido()
        github.side_effect = lambda method, *args: {"sha": "previous-sha"} if method == "GET" else {}
        assert app.fazer_backup_manutencoes_github(config=config)["status"] == "SUCESSO"
        puts = [call.args for call in github.call_args_list if call.args[0] == "PUT"]
        assert len(puts) == 1
        assert puts[0][1].endswith(app.GITHUB_BACKUP_PATH)
        assert puts[0][3]["sha"] == "previous-sha"
        payload = json.loads(base64.b64decode(puts[0][3]["content"]))
        assert payload["records"] == 3
        with closing(sqlite3.connect(":memory:")) as restored, closing(app.conectar()) as source:
            restored.executescript(payload["database_sql"])
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
            tables = source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for row in tables:
                table = row[0]
                if table in {"manutencoes_github_backups", "sqlite_sequence"}:
                    continue  # O sucesso do envio e registrado depois do snapshot.
                original = [tuple(record) for record in source.execute(f'SELECT * FROM "{table}"')]
                assert restored.execute(f'SELECT * FROM "{table}"').fetchall() == original

        assert not app.backup_manutencoes_github_devido()
        now = now.replace(hour=22, minute=0)
        assert app.backup_manutencoes_github_devido()
        github.side_effect = URLError("offline")
        assert app.fazer_backup_manutencoes_github(config=config)["status"] == "ERRO"
        assert app.backup_manutencoes_github_devido()
        github.side_effect = lambda *args: {}
        assert app.fazer_backup_manutencoes_github(config=config)["status"] == "SUCESSO"
        assert not app.backup_manutencoes_github_devido()
        now = now.replace(month=10, day=1, hour=8)
        assert not app.backup_manutencoes_github_devido()
        now = now.replace(day=2)
        assert app.backup_manutencoes_github_devido()


if __name__ == "__main__":
    test_backup_diario()
    print("OK: backup unico, restauracao completa, horario 22h e retentativa.")

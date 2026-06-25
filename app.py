import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st


# =========================================================
# CONFIGURAÇÕES
# =========================================================
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "painel_vencimentos.db"

COR_CABECALHO = "#020D3F"
COR_TEXTO = "#B5911B"

TIPOS_DOCUMENTO = [
    "CIV",
    "CIPP",
    "AFERIÇÃO",
    "AGENDAMENTO AFERIÇÃO",
    "CRONOTACÓGRAFO",
    "IBAMA",
    "CR IBAMA",
    "AETs",
    "CRLV",
]

STATUS_ORDEM = {
    "VENCIDO": 0,
    "VENCE HOJE": 1,
    "VENCE NA SEMANA": 2,
    "VENCE NO MÊS": 3,
    "OK": 4,
    "SEM DATA": 5,
}

COMANDO_RESTAURAR = "RESTAURAR"
COMANDO_RESTAURAR_BASE = "RESTAURAR BASE"
COMANDO_ZERAR_BANCO = "ZERAR BANCO"
COLUNAS_MINIMAS_BASE = 8


# =========================================================
# UTILIDADES
# =========================================================
def agora_local() -> datetime:
    try:
        return datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        return datetime.now().astimezone()


def normalizar_texto(valor) -> str:
    if valor is None or pd.isna(valor):
        return ""
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.upper()).strip()


def limpar_placa(valor) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalizar_texto(valor))


def data_excel_serial(valor):
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return pd.NaT
    if not 30000 <= numero <= 60000:
        return pd.NaT
    return pd.Timestamp(datetime(1899, 12, 30) + timedelta(days=numero)).normalize()


def converter_data(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return pd.NaT
    if isinstance(valor, pd.Timestamp):
        return valor.normalize()
    if isinstance(valor, datetime):
        return pd.Timestamp(valor).normalize()
    if isinstance(valor, date):
        return pd.Timestamp(valor).normalize()
    serial = data_excel_serial(valor)
    if not pd.isna(serial):
        return serial
    texto = str(valor).strip()
    if not texto:
        return pd.NaT
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
        data_convertida = pd.to_datetime(texto, format="%Y-%m-%d", errors="coerce")
    else:
        data_convertida = pd.to_datetime(texto, dayfirst=True, errors="coerce")
    return pd.NaT if pd.isna(data_convertida) else pd.Timestamp(data_convertida).normalize()


def converter_data_hora_iso(valor) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    if isinstance(valor, pd.Timestamp):
        convertido = valor.to_pydatetime()
    elif isinstance(valor, datetime):
        convertido = valor
    elif isinstance(valor, date):
        convertido = datetime.combine(valor, datetime.min.time())
    else:
        try:
            numero = float(valor)
            if not 30000 <= numero <= 60000:
                return ""
            convertido = datetime(1899, 12, 30) + timedelta(days=numero)
        except (TypeError, ValueError):
            texto = str(valor).strip()
            if not texto:
                return ""
            if re.match(r"^\d{4}-\d{2}-\d{2}", texto):
                convertido = pd.to_datetime(texto, errors="coerce")
            else:
                convertido = pd.to_datetime(texto, dayfirst=True, errors="coerce")
            if pd.isna(convertido):
                return ""
            convertido = convertido.to_pydatetime()
    return convertido.isoformat(timespec="seconds")


def classificar_documento(valor) -> str | None:
    texto = normalizar_texto(valor)
    if not texto:
        return None
    if "CR IBAMA" in texto or "CERTIFICADO DE REGULARIDADE" in texto:
        return "CR IBAMA"
    if "CRLV" in texto:
        return "CRLV"
    if re.search(r"(^|\W)AETS?($|\W)", texto):
        return "AETs"
    if "IBAMA" in texto:
        return "IBAMA"
    if "CIPP" in texto:
        return "CIPP"
    if re.search(r"(^|\W)CIV($|\W)", texto):
        return "CIV"
    if "AGEND" in texto and ("AFERICAO" in texto or "AFER" in texto):
        return "AGENDAMENTO AFERIÇÃO"
    if "AFERICAO" in texto or texto.startswith("AFER"):
        return "AFERIÇÃO"
    if "CRONOT" in texto:
        return "CRONOTACÓGRAFO"
    return None


def periodos(data_referencia: date) -> dict:
    ref = pd.Timestamp(data_referencia).normalize()
    inicio_semana = ref - pd.Timedelta(days=ref.weekday())
    fim_semana = inicio_semana + pd.Timedelta(days=6)
    inicio_mes = pd.Timestamp(date(ref.year, ref.month, 1))
    if ref.month == 12:
        fim_mes = pd.Timestamp(date(ref.year, 12, 31))
    else:
        fim_mes = pd.Timestamp(date(ref.year, ref.month + 1, 1)) - pd.Timedelta(days=1)
    return {
        "ref": ref,
        "inicio_semana": inicio_semana,
        "fim_semana": fim_semana,
        "inicio_mes": inicio_mes,
        "fim_mes": fim_mes,
    }


def status_vencimento(vencimento, data_referencia: date) -> str:
    vencimento = converter_data(vencimento)
    if pd.isna(vencimento):
        return "SEM DATA"
    p = periodos(data_referencia)
    if vencimento < p["ref"]:
        return "VENCIDO"
    if vencimento == p["ref"]:
        return "VENCE HOJE"
    if vencimento <= p["fim_semana"]:
        return "VENCE NA SEMANA"
    if vencimento <= p["fim_mes"]:
        return "VENCE NO MÊS"
    return "OK"


def valor_posicional(row: pd.Series, indice: int):
    return row.iloc[indice] if indice < len(row) else None


def data_iso(valor) -> str | None:
    convertido = converter_data(valor)
    return None if pd.isna(convertido) else convertido.strftime("%Y-%m-%d")


def formatar_data(valor) -> str:
    convertido = converter_data(valor)
    return "" if pd.isna(convertido) else convertido.strftime("%d/%m/%Y")


def formatar_data_hora(valor) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    try:
        convertido = datetime.fromisoformat(str(valor))
    except (TypeError, ValueError):
        convertido = pd.to_datetime(valor, errors="coerce")
        if pd.isna(convertido):
            return ""
    return convertido.strftime("%d/%m/%Y %H:%M:%S")


def criar_nomes_unicos(colunas) -> list[str]:
    usados: dict[str, int] = {}
    novas = []
    for coluna in colunas:
        base = normalizar_texto(coluna) or "COLUNA"
        usados[base] = usados.get(base, 0) + 1
        novas.append(base if usados[base] == 1 else f"{base}_{usados[base]}")
    return novas


# =========================================================
# BANCO DE DADOS, BACKUP E AUDITORIA
# =========================================================
def conectar() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(DB_PATH, timeout=30)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    conexao.execute("PRAGMA journal_mode = WAL")
    return conexao


def inicializar_banco() -> None:
    with conectar() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS importacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT NOT NULL,
                usuario TEXT NOT NULL,
                arquivo_base TEXT,
                arquivo_documentos TEXT,
                total_recebidos INTEGER NOT NULL DEFAULT 0,
                inseridos INTEGER NOT NULL DEFAULT 0,
                atualizados INTEGER NOT NULL DEFAULT 0,
                ignorados INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS base_composicoes_ativa (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                conteudo_json TEXT NOT NULL,
                nome_arquivo TEXT,
                nome_aba TEXT,
                total_linhas INTEGER NOT NULL,
                atualizado_em TEXT NOT NULL,
                atualizado_por TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backup_bases_composicoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conteudo_json TEXT NOT NULL,
                nome_arquivo TEXT,
                nome_aba TEXT,
                total_linhas INTEGER NOT NULL,
                atualizado_em TEXT NOT NULL,
                atualizado_por TEXT NOT NULL,
                backup_em TEXT NOT NULL,
                backup_por TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS historico_bases_composicoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT NOT NULL,
                usuario TEXT NOT NULL,
                nome_arquivo TEXT,
                nome_aba TEXT,
                total_linhas INTEGER NOT NULL,
                acao TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                placa TEXT NOT NULL,
                documento TEXT NOT NULL,
                vencimento TEXT NOT NULL,
                composicao TEXT NOT NULL,
                placa_cavalo TEXT,
                placa_carreta_1 TEXT,
                placa_carreta_2 TEXT,
                equipamento TEXT,
                origem TEXT,
                importado_em TEXT NOT NULL,
                importado_por TEXT NOT NULL,
                importacao_id INTEGER,
                UNIQUE (placa, documento),
                FOREIGN KEY (importacao_id) REFERENCES importacoes(id)
            );

            CREATE TABLE IF NOT EXISTS historico_documentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                documento_id_original INTEGER,
                placa TEXT NOT NULL,
                documento TEXT NOT NULL,
                vencimento TEXT NOT NULL,
                composicao TEXT NOT NULL,
                placa_cavalo TEXT,
                placa_carreta_1 TEXT,
                placa_carreta_2 TEXT,
                equipamento TEXT,
                origem TEXT,
                importado_em TEXT,
                importado_por TEXT,
                importacao_original_id INTEGER,
                substituido_em TEXT NOT NULL,
                substituido_por TEXT NOT NULL,
                substituido_na_importacao_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS historico_atualizacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT NOT NULL,
                usuario TEXT NOT NULL,
                documento TEXT NOT NULL,
                placa TEXT,
                composicao TEXT,
                vencimento_anterior TEXT,
                novo_vencimento TEXT NOT NULL,
                origem TEXT,
                acao TEXT NOT NULL,
                importacao_id INTEGER,
                FOREIGN KEY (importacao_id) REFERENCES importacoes(id)
            );

            CREATE TABLE IF NOT EXISTS backup_documentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                importacao_id INTEGER NOT NULL,
                documento_id_original INTEGER,
                placa TEXT NOT NULL,
                documento TEXT NOT NULL,
                vencimento TEXT NOT NULL,
                composicao TEXT NOT NULL,
                placa_cavalo TEXT,
                placa_carreta_1 TEXT,
                placa_carreta_2 TEXT,
                equipamento TEXT,
                origem TEXT,
                importado_em TEXT,
                importado_por TEXT,
                importacao_original_id INTEGER,
                backup_em TEXT NOT NULL,
                FOREIGN KEY (importacao_id) REFERENCES importacoes(id)
            );

            CREATE INDEX IF NOT EXISTS idx_documentos_vencimento
                ON documentos(vencimento);
            CREATE INDEX IF NOT EXISTS idx_historico_bases_data
                ON historico_bases_composicoes(data_hora DESC);
            CREATE INDEX IF NOT EXISTS idx_historico_placa_documento
                ON historico_documentos(placa, documento);
            CREATE INDEX IF NOT EXISTS idx_atualizacoes_data
                ON historico_atualizacoes(data_hora DESC);
            CREATE INDEX IF NOT EXISTS idx_backup_importacao
                ON backup_documentos(importacao_id);
            """
        )
        if conn.execute("SELECT COUNT(*) FROM historico_atualizacoes").fetchone()[0] == 0:
            # Migração transparente para bancos criados por versões anteriores.
            conn.execute(
                """
                INSERT INTO historico_atualizacoes (
                    data_hora, usuario, documento, placa, composicao,
                    vencimento_anterior, novo_vencimento, origem, acao,
                    importacao_id
                )
                SELECT h.substituido_em, h.substituido_por, h.documento, h.placa,
                       h.composicao, h.vencimento,
                       COALESCE(d.vencimento, h.vencimento),
                       COALESCE(d.origem, h.origem), 'ALTERAÇÃO',
                       h.substituido_na_importacao_id
                FROM historico_documentos h
                LEFT JOIN documentos d
                  ON d.placa = h.placa AND d.documento = h.documento
                ORDER BY h.id
                """
            )
            conn.execute(
                """
                INSERT INTO historico_atualizacoes (
                    data_hora, usuario, documento, placa, composicao,
                    vencimento_anterior, novo_vencimento, origem, acao,
                    importacao_id
                )
                SELECT d.importado_em, d.importado_por, d.documento, d.placa,
                       d.composicao, NULL, d.vencimento, d.origem, 'IMPORTAÇÃO',
                       d.importacao_id
                FROM documentos d
                WHERE NOT EXISTS (
                    SELECT 1 FROM historico_atualizacoes a
                    WHERE a.documento = d.documento
                      AND a.placa = d.placa
                      AND a.importacao_id = d.importacao_id
                )
                """
            )


def serializar_base_composicoes(df_base: pd.DataFrame) -> str:
    base = df_base.copy()
    base.columns = [str(coluna) for coluna in base.columns]
    return base.to_json(
        orient="split", date_format="iso", force_ascii=False, default_handler=str
    )


def desserializar_base_composicoes(conteudo_json: str) -> pd.DataFrame:
    if not conteudo_json:
        return pd.DataFrame()
    return pd.read_json(StringIO(conteudo_json), orient="split", dtype=False)


def carregar_base_composicoes() -> tuple[pd.DataFrame, dict | None]:
    with conectar() as conn:
        registro = conn.execute(
            "SELECT * FROM base_composicoes_ativa WHERE id = 1"
        ).fetchone()
    if registro is None:
        return pd.DataFrame(), None
    metadata = {
        "nome_arquivo": registro["nome_arquivo"],
        "nome_aba": registro["nome_aba"],
        "total_linhas": registro["total_linhas"],
        "atualizado_em": registro["atualizado_em"],
        "atualizado_por": registro["atualizado_por"],
    }
    return desserializar_base_composicoes(registro["conteudo_json"]), metadata


def salvar_base_composicoes(
    df_base: pd.DataFrame,
    usuario: str,
    nome_arquivo: str,
    nome_aba: str,
) -> dict:
    if df_base.empty:
        raise ValueError("A base de composições importada está vazia.")
    momento = agora_local().isoformat(timespec="seconds")
    conteudo_json = serializar_base_composicoes(df_base)
    total_linhas = len(df_base)
    with conectar() as conn:
        anterior = conn.execute(
            "SELECT * FROM base_composicoes_ativa WHERE id = 1"
        ).fetchone()
        acao = "BASE INICIAL" if anterior is None else "SUBSTITUIÇÃO DA BASE"
        if anterior is not None:
            conn.execute(
                """
                INSERT INTO backup_bases_composicoes (
                    conteudo_json, nome_arquivo, nome_aba, total_linhas,
                    atualizado_em, atualizado_por, backup_em, backup_por
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anterior["conteudo_json"], anterior["nome_arquivo"],
                    anterior["nome_aba"], anterior["total_linhas"],
                    anterior["atualizado_em"], anterior["atualizado_por"],
                    momento, usuario,
                ),
            )
        conn.execute(
            """
            INSERT INTO base_composicoes_ativa (
                id, conteudo_json, nome_arquivo, nome_aba, total_linhas,
                atualizado_em, atualizado_por
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                conteudo_json = excluded.conteudo_json,
                nome_arquivo = excluded.nome_arquivo,
                nome_aba = excluded.nome_aba,
                total_linhas = excluded.total_linhas,
                atualizado_em = excluded.atualizado_em,
                atualizado_por = excluded.atualizado_por
            """,
            (
                conteudo_json, nome_arquivo, nome_aba, total_linhas,
                momento, usuario,
            ),
        )
        conn.execute(
            """
            INSERT INTO historico_bases_composicoes (
                data_hora, usuario, nome_arquivo, nome_aba, total_linhas, acao
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (momento, usuario, nome_arquivo, nome_aba, total_linhas, acao),
        )
    return {
        "nome_arquivo": nome_arquivo,
        "nome_aba": nome_aba,
        "total_linhas": total_linhas,
        "atualizado_em": momento,
        "atualizado_por": usuario,
        "acao": acao,
    }


def carregar_ultimo_backup_base() -> tuple[pd.DataFrame, dict | None]:
    with conectar() as conn:
        registro = conn.execute(
            "SELECT * FROM backup_bases_composicoes ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if registro is None:
        return pd.DataFrame(), None
    metadata = {
        "nome_arquivo": registro["nome_arquivo"],
        "nome_aba": registro["nome_aba"],
        "total_linhas": registro["total_linhas"],
        "atualizado_em": registro["atualizado_em"],
        "atualizado_por": registro["atualizado_por"],
        "backup_em": registro["backup_em"],
        "backup_por": registro["backup_por"],
    }
    return desserializar_base_composicoes(registro["conteudo_json"]), metadata


def carregar_historico_bases() -> pd.DataFrame:
    historico = consultar_sql(
        """
        SELECT data_hora AS "Data/hora", usuario AS "Usuário",
               nome_arquivo AS "Arquivo", nome_aba AS "Aba",
               total_linhas AS "Linhas", acao AS "Ação"
        FROM historico_bases_composicoes
        ORDER BY id DESC
        """
    )
    if not historico.empty:
        historico["Data/hora"] = historico["Data/hora"].apply(formatar_data_hora)
    return historico


def salvar_importacao(
    registros: list[dict], usuario: str, arquivo_base: str, arquivo_documentos: str
) -> dict:
    momento = agora_local().isoformat(timespec="seconds")
    usuario = usuario.strip()
    estatisticas = {
        "recebidos": len(registros),
        "inseridos": 0,
        "atualizados": 0,
        "ignorados": 0,
    }

    # Dentro da planilha importada, placa + documento repetidos ficam com o
    # maior vencimento. Duplicatas exatas também são eliminadas aqui.
    consolidados: dict[tuple[str, str], dict] = {}
    for registro in registros:
        chave = (registro["placa"], registro["documento"])
        anterior = consolidados.get(chave)
        prioridade_registro = (
            registro["vencimento"],
            registro.get("alterado_em_origem", ""),
            registro.get("origem") == "PLANILHA DE DOCUMENTOS",
        )
        prioridade_anterior = (
            anterior["vencimento"],
            anterior.get("alterado_em_origem", ""),
            anterior.get("origem") == "PLANILHA DE DOCUMENTOS",
        ) if anterior else None
        if anterior is None or prioridade_registro > prioridade_anterior:
            consolidados[chave] = registro

    with conectar() as conn:
        cursor = conn.execute(
            """
            INSERT INTO importacoes
                (data_hora, usuario, arquivo_base, arquivo_documentos, total_recebidos)
            VALUES (?, ?, ?, ?, ?)
            """,
            (momento, usuario, arquivo_base, arquivo_documentos, len(registros)),
        )
        importacao_id = int(cursor.lastrowid)

        # Snapshot completo imediatamente anterior a esta importação.
        conn.execute(
            """
            INSERT INTO backup_documentos (
                importacao_id, documento_id_original, placa, documento, vencimento,
                composicao, placa_cavalo, placa_carreta_1, placa_carreta_2,
                equipamento, origem, importado_em, importado_por,
                importacao_original_id, backup_em
            )
            SELECT ?, id, placa, documento, vencimento, composicao, placa_cavalo,
                   placa_carreta_1, placa_carreta_2, equipamento, origem,
                   importado_em, importado_por, importacao_id, ?
            FROM documentos
            """,
            (importacao_id, momento),
        )

        for registro in consolidados.values():
            alterado_em_registro = registro.get("alterado_em_origem") or momento
            alterado_por_registro = registro.get("alterado_por_origem") or usuario
            existente = conn.execute(
                "SELECT * FROM documentos WHERE placa = ? AND documento = ?",
                (registro["placa"], registro["documento"]),
            ).fetchone()

            if existente is None:
                conn.execute(
                    """
                    INSERT INTO documentos (
                        placa, documento, vencimento, composicao, placa_cavalo,
                        placa_carreta_1, placa_carreta_2, equipamento, origem,
                        importado_em, importado_por, importacao_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        registro["placa"], registro["documento"], registro["vencimento"],
                        registro["composicao"], registro["placa_cavalo"],
                        registro["placa_carreta_1"], registro["placa_carreta_2"],
                        registro["equipamento"], registro["origem"],
                        alterado_em_registro, alterado_por_registro, importacao_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO historico_atualizacoes (
                        data_hora, usuario, documento, placa, composicao,
                        vencimento_anterior, novo_vencimento, origem, acao,
                        importacao_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alterado_em_registro, alterado_por_registro,
                        registro["documento"], registro["placa"],
                        registro["composicao"], None, registro["vencimento"],
                        registro["origem"], "INSERÇÃO", importacao_id,
                    ),
                )
                estatisticas["inseridos"] += 1
                continue

            if registro["vencimento"] < existente["vencimento"]:
                estatisticas["ignorados"] += 1
                continue

            if registro["vencimento"] == existente["vencimento"]:
                alterado_em_origem = registro.get("alterado_em_origem", "")
                mesmos_dados_controle = (
                    alterado_em_origem == existente["importado_em"]
                    and alterado_por_registro == existente["importado_por"]
                )
                if not alterado_em_origem or mesmos_dados_controle:
                    estatisticas["ignorados"] += 1
                    continue
                conn.execute(
                    """
                    UPDATE documentos SET
                        composicao = ?, placa_cavalo = ?, placa_carreta_1 = ?,
                        placa_carreta_2 = ?, equipamento = ?, origem = ?,
                        importado_em = ?, importado_por = ?, importacao_id = ?
                    WHERE id = ?
                    """,
                    (
                        registro["composicao"], registro["placa_cavalo"],
                        registro["placa_carreta_1"], registro["placa_carreta_2"],
                        registro["equipamento"], registro["origem"],
                        alterado_em_registro, alterado_por_registro, importacao_id,
                        existente["id"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO historico_atualizacoes (
                        data_hora, usuario, documento, placa, composicao,
                        vencimento_anterior, novo_vencimento, origem, acao,
                        importacao_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alterado_em_registro, alterado_por_registro,
                        registro["documento"], registro["placa"],
                        registro["composicao"], existente["vencimento"],
                        registro["vencimento"], registro["origem"],
                        "ATUALIZAÇÃO DE CONTROLE", importacao_id,
                    ),
                )
                estatisticas["atualizados"] += 1
                continue

            conn.execute(
                """
                INSERT INTO historico_documentos (
                    documento_id_original, placa, documento, vencimento, composicao,
                    placa_cavalo, placa_carreta_1, placa_carreta_2, equipamento,
                    origem, importado_em, importado_por, importacao_original_id,
                    substituido_em, substituido_por, substituido_na_importacao_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    existente["id"], existente["placa"], existente["documento"],
                    existente["vencimento"], existente["composicao"],
                    existente["placa_cavalo"], existente["placa_carreta_1"],
                    existente["placa_carreta_2"], existente["equipamento"],
                    existente["origem"], existente["importado_em"],
                    existente["importado_por"], existente["importacao_id"],
                    momento, usuario, importacao_id,
                ),
            )
            conn.execute(
                """
                UPDATE documentos SET
                    vencimento = ?, composicao = ?, placa_cavalo = ?,
                    placa_carreta_1 = ?, placa_carreta_2 = ?, equipamento = ?,
                    origem = ?, importado_em = ?, importado_por = ?, importacao_id = ?
                WHERE id = ?
                """,
                (
                    registro["vencimento"], registro["composicao"],
                    registro["placa_cavalo"], registro["placa_carreta_1"],
                    registro["placa_carreta_2"], registro["equipamento"],
                    registro["origem"], alterado_em_registro,
                    alterado_por_registro, importacao_id,
                    existente["id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO historico_atualizacoes (
                    data_hora, usuario, documento, placa, composicao,
                    vencimento_anterior, novo_vencimento, origem, acao,
                    importacao_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alterado_em_registro, alterado_por_registro,
                    registro["documento"], registro["placa"],
                    registro["composicao"], existente["vencimento"],
                    registro["vencimento"], registro["origem"], "ALTERAÇÃO",
                    importacao_id,
                ),
            )
            estatisticas["atualizados"] += 1

        estatisticas["ignorados"] += len(registros) - len(consolidados)
        conn.execute(
            """
            UPDATE importacoes
            SET inseridos = ?, atualizados = ?, ignorados = ?
            WHERE id = ?
            """,
            (
                estatisticas["inseridos"], estatisticas["atualizados"],
                estatisticas["ignorados"], importacao_id,
            ),
        )
        estatisticas["importacao_id"] = importacao_id
    return estatisticas


def consultar_sql(sql: str, parametros: tuple = ()) -> pd.DataFrame:
    with conectar() as conn:
        return pd.read_sql_query(sql, conn, params=parametros)


def carregar_documentos() -> pd.DataFrame:
    df = consultar_sql(
        """
        SELECT placa, documento, vencimento, composicao, placa_cavalo,
               placa_carreta_1, placa_carreta_2, equipamento, origem,
               importado_em, importado_por, importacao_id
        FROM documentos
        """
    )
    if not df.empty:
        df["vencimento"] = pd.to_datetime(df["vencimento"], errors="coerce")
    return df


def carregar_importacoes() -> pd.DataFrame:
    return consultar_sql(
        """
        SELECT id AS importacao_id, data_hora, usuario, arquivo_base,
               arquivo_documentos, total_recebidos, inseridos, atualizados, ignorados
        FROM importacoes ORDER BY id DESC LIMIT 100
        """
    )


def carregar_importacoes_reversao() -> pd.DataFrame:
    return consultar_sql(
        """
        SELECT i.id AS importacao_id, i.data_hora, i.usuario, i.arquivo_base,
               i.arquivo_documentos, i.total_recebidos, i.inseridos, i.atualizados,
               i.ignorados,
               (
                   SELECT COUNT(*)
                   FROM backup_documentos b
                   WHERE b.importacao_id = i.id
               ) AS registros_backup
        FROM importacoes i
        ORDER BY i.id DESC
        LIMIT 50
        """
    )


def restaurar_backup_importacao(importacao_id: int, usuario: str) -> dict:
    usuario = usuario.strip()
    momento = agora_local().isoformat(timespec="seconds")
    with conectar() as conn:
        importacao = conn.execute(
            "SELECT * FROM importacoes WHERE id = ?", (importacao_id,)
        ).fetchone()
        if importacao is None:
            raise ValueError("Importacao nao encontrada.")

        backup = conn.execute(
            """
            SELECT placa, documento, vencimento, composicao, placa_cavalo,
                   placa_carreta_1, placa_carreta_2, equipamento, origem,
                   importado_em, importado_por, importacao_original_id
            FROM backup_documentos
            WHERE importacao_id = ?
            ORDER BY id
            """,
            (importacao_id,),
        ).fetchall()
        total_antes = conn.execute("SELECT COUNT(*) FROM documentos").fetchone()[0]

        conn.execute("DELETE FROM documentos")
        for registro in backup:
            conn.execute(
                """
                INSERT INTO documentos (
                    placa, documento, vencimento, composicao, placa_cavalo,
                    placa_carreta_1, placa_carreta_2, equipamento, origem,
                    importado_em, importado_por, importacao_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    registro["placa"], registro["documento"],
                    registro["vencimento"], registro["composicao"],
                    registro["placa_cavalo"], registro["placa_carreta_1"],
                    registro["placa_carreta_2"], registro["equipamento"],
                    registro["origem"], registro["importado_em"],
                    registro["importado_por"], registro["importacao_original_id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO historico_atualizacoes (
                    data_hora, usuario, documento, placa, composicao,
                    vencimento_anterior, novo_vencimento, origem, acao,
                    importacao_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    momento, usuario, registro["documento"], registro["placa"],
                    registro["composicao"], None, registro["vencimento"],
                    f"RESTAURACAO DO BACKUP ANTERIOR A IMPORTACAO {importacao_id}",
                    "RESTAURACAO", importacao_id,
                ),
            )

    return {
        "importacao_id": importacao_id,
        "registros_antes": total_antes,
        "registros_restaurados": len(backup),
    }


def restaurar_ultimo_backup_base_composicoes(usuario: str) -> dict:
    usuario = usuario.strip()
    momento = agora_local().isoformat(timespec="seconds")
    with conectar() as conn:
        backup = conn.execute(
            "SELECT * FROM backup_bases_composicoes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if backup is None:
            raise ValueError("Ainda nao existe backup anterior da base de composicoes.")

        atual = conn.execute(
            "SELECT * FROM base_composicoes_ativa WHERE id = 1"
        ).fetchone()
        if atual is not None:
            conn.execute(
                """
                INSERT INTO backup_bases_composicoes (
                    conteudo_json, nome_arquivo, nome_aba, total_linhas,
                    atualizado_em, atualizado_por, backup_em, backup_por
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    atual["conteudo_json"], atual["nome_arquivo"],
                    atual["nome_aba"], atual["total_linhas"],
                    atual["atualizado_em"], atual["atualizado_por"],
                    momento, usuario,
                ),
            )

        conn.execute(
            """
            INSERT INTO base_composicoes_ativa (
                id, conteudo_json, nome_arquivo, nome_aba, total_linhas,
                atualizado_em, atualizado_por
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                conteudo_json = excluded.conteudo_json,
                nome_arquivo = excluded.nome_arquivo,
                nome_aba = excluded.nome_aba,
                total_linhas = excluded.total_linhas,
                atualizado_em = excluded.atualizado_em,
                atualizado_por = excluded.atualizado_por
            """,
            (
                backup["conteudo_json"], backup["nome_arquivo"],
                backup["nome_aba"], backup["total_linhas"], momento, usuario,
            ),
        )
        conn.execute(
            """
            INSERT INTO historico_bases_composicoes (
                data_hora, usuario, nome_arquivo, nome_aba, total_linhas, acao
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                momento, usuario, backup["nome_arquivo"], backup["nome_aba"],
                backup["total_linhas"], "RESTAURACAO DO BACKUP",
            ),
        )

    df_restaurada = desserializar_base_composicoes(backup["conteudo_json"])
    vinculos = atualizar_vinculos_documentos(df_restaurada)
    return {
        "nome_arquivo": backup["nome_arquivo"],
        "total_linhas": backup["total_linhas"],
        "vinculos_atualizados": vinculos,
    }


def zerar_banco_dados(usuario: str) -> dict:
    usuario = usuario.strip()
    with conectar() as conn:
        totais = {
            "documentos": conn.execute("SELECT COUNT(*) FROM documentos").fetchone()[0],
            "importacoes": conn.execute("SELECT COUNT(*) FROM importacoes").fetchone()[0],
            "bases": conn.execute(
                "SELECT COUNT(*) FROM base_composicoes_ativa"
            ).fetchone()[0],
        }
        for tabela in [
            "documentos",
            "historico_documentos",
            "historico_atualizacoes",
            "backup_documentos",
            "importacoes",
            "base_composicoes_ativa",
            "backup_bases_composicoes",
            "historico_bases_composicoes",
        ]:
            conn.execute(f"DELETE FROM {tabela}")
        conn.execute(
            """
            DELETE FROM sqlite_sequence
            WHERE name IN (
                'importacoes', 'documentos', 'historico_documentos',
                'historico_atualizacoes', 'backup_documentos',
                'backup_bases_composicoes', 'historico_bases_composicoes'
            )
            """
        )
    return {**totais, "usuario": usuario}


def carregar_historico() -> pd.DataFrame:
    df = consultar_sql(
        """
        SELECT placa, documento, vencimento, composicao, equipamento, origem,
               importado_em, importado_por, substituido_em, substituido_por,
               substituido_na_importacao_id
        FROM historico_documentos
        ORDER BY id DESC
        """
    )
    return df


def carregar_historico_atualizacoes() -> pd.DataFrame:
    return consultar_sql(
        """
        SELECT data_hora, usuario, documento, placa, composicao,
               vencimento_anterior, novo_vencimento, origem, acao, importacao_id
        FROM historico_atualizacoes
        ORDER BY data_hora DESC, id DESC
        """
    )


def carregar_ultimo_backup() -> tuple[pd.DataFrame, int | None]:
    ultima = consultar_sql("SELECT MAX(importacao_id) AS id FROM backup_documentos")
    if ultima.empty or pd.isna(ultima.iloc[0]["id"]):
        return pd.DataFrame(), None
    importacao_id = int(ultima.iloc[0]["id"])
    df = consultar_sql(
        """
        SELECT placa, documento, vencimento, composicao, equipamento, origem,
               importado_em, importado_por, backup_em
        FROM backup_documentos
        WHERE importacao_id = ? ORDER BY composicao, placa, documento
        """,
        (importacao_id,),
    )
    return df, importacao_id


# =========================================================
# LEITURA E CONSOLIDAÇÃO DAS PLANILHAS
# =========================================================
def localizar_linha_cabecalho_documentos(df_bruto: pd.DataFrame) -> int:
    for indice in range(min(30, len(df_bruto))):
        linha = [normalizar_texto(x) for x in df_bruto.iloc[indice].tolist()]
        tem_placa = any(c == "PLACA" or c.startswith("PLACA ") for c in linha)
        tem_documento = any(
            c in {"LAUDO", "DOCUMENTO", "TIPO DE DOCUMENTO", "TIPO LAUDO"}
            or "LAUDO" in c
            for c in linha
        )
        tem_vencimento = any("VENC" in c or "VALIDADE" in c for c in linha)
        if tem_placa and tem_documento and tem_vencimento:
            return indice
    raise ValueError(
        "Não localizei um cabeçalho com Placa, Documento/Laudo e Vencimento."
    )


def placa_modelo_valida(valor) -> bool:
    placa = limpar_placa(valor)
    return bool(re.fullmatch(r"[A-Z]{3}[0-9][A-Z0-9][0-9]{2}", placa))


def validar_modelo_base(df_base: pd.DataFrame) -> None:
    if df_base.empty:
        raise ValueError("A base de composicoes importada esta vazia.")
    if len(df_base.columns) < COLUNAS_MINIMAS_BASE:
        raise ValueError(
            "A base de composicoes nao esta no modelo padrao. "
            "Use a planilha com as colunas: cavalo, CIV cavalo, cronotacografo, "
            "carreta 1, carreta 2, CIV carretas, CIPP carretas e afericao."
        )

    linhas_validas = 0
    for _, row in df_base.iterrows():
        if (
            placa_modelo_valida(valor_posicional(row, 0))
            or placa_modelo_valida(valor_posicional(row, 3))
            or placa_modelo_valida(valor_posicional(row, 4))
        ):
            linhas_validas += 1
    if linhas_validas == 0:
        raise ValueError(
            "A base de composicoes nao parece seguir o modelo padrao: "
            "nao encontrei placas validas nas colunas de cavalo/carreta."
        )


def ler_planilha_documentos(arquivo, aba: str) -> pd.DataFrame:
    arquivo.seek(0)
    bruto = pd.read_excel(arquivo, sheet_name=aba, header=None, dtype=object)
    linha_cabecalho = localizar_linha_cabecalho_documentos(bruto)
    df = bruto.iloc[linha_cabecalho + 1 :].copy()
    df.columns = criar_nomes_unicos(bruto.iloc[linha_cabecalho].tolist())
    return df.dropna(how="all")


def ler_base(arquivo, aba: str) -> pd.DataFrame:
    arquivo.seek(0)
    df_base = pd.read_excel(arquivo, sheet_name=aba, dtype=object).dropna(how="all")
    validar_modelo_base(df_base)
    return df_base


def localizar_coluna(colunas, candidatos: list[str], contem: list[str] | None = None):
    normalizadas = {coluna: normalizar_texto(coluna) for coluna in colunas}
    for candidato in candidatos:
        alvo = normalizar_texto(candidato)
        for original, normalizada in normalizadas.items():
            if normalizada == alvo:
                return original
    if contem:
        termos = [normalizar_texto(x) for x in contem]
        for original, normalizada in normalizadas.items():
            if all(termo in normalizada for termo in termos):
                return original
    return None


def extrair_documentos_origem(df: pd.DataFrame) -> list[dict]:
    col_placa = localizar_coluna(df.columns, ["PLACA"], contem=["PLACA"])
    col_tipo = localizar_coluna(
        df.columns,
        ["LAUDO", "DOCUMENTO", "TIPO DE DOCUMENTO", "TIPO LAUDO"],
        contem=["LAUDO"],
    )
    col_vencimento = localizar_coluna(
        df.columns,
        ["DATA VENCIMENTO", "VENCIMENTO", "DATA DE VENCIMENTO", "VALIDADE"],
        contem=["VENC"],
    )
    if col_vencimento is None:
        col_vencimento = localizar_coluna(df.columns, [], contem=["VALIDADE"])
    col_alterado_em = localizar_coluna(
        df.columns,
        ["ALTERADO EM", "ATUALIZADO EM", "MODIFICADO EM", "DATA DA ALTERAÇÃO"],
        contem=["ALTERADO", "EM"],
    )
    col_alterado_por = localizar_coluna(
        df.columns,
        ["ALTERADO POR", "ATUALIZADO POR", "MODIFICADO POR", "USUÁRIO"],
        contem=["ALTERADO", "POR"],
    )
    if col_placa is None or col_tipo is None or col_vencimento is None:
        raise ValueError("A planilha de documentos não possui as colunas obrigatórias.")

    registros = []
    for _, row in df.iterrows():
        placa = limpar_placa(row.get(col_placa))
        documento = classificar_documento(row.get(col_tipo))
        vencimento = data_iso(row.get(col_vencimento))
        if placa and documento and vencimento:
            valor_usuario = row.get(col_alterado_por) if col_alterado_por else ""
            alterado_por = (
                "" if valor_usuario is None or pd.isna(valor_usuario)
                else str(valor_usuario).strip()
            )
            registros.append(
                {
                    "placa": placa,
                    "documento": documento,
                    "vencimento": vencimento,
                    "origem": "PLANILHA DE DOCUMENTOS",
                    "alterado_em_origem": converter_data_hora_iso(
                        row.get(col_alterado_em) if col_alterado_em else None
                    ),
                    "alterado_por_origem": alterado_por,
                }
            )
    if not registros:
        raise ValueError(
            "A planilha de documentos nao esta no modelo padrao ou nao possui "
            "linhas validas com Placa, Documento/Laudo e Vencimento."
        )
    return registros


def nome_coluna_para_documento(coluna) -> str | None:
    return classificar_documento(coluna)


def extrair_composicoes_e_fallbacks(df_base: pd.DataFrame) -> tuple[dict, list[dict]]:
    mapa_placas: dict[str, dict] = {}
    fallbacks: list[dict] = []

    # Compatibilidade com a estrutura original:
    # 0 cavalo | 1 CIV cavalo | 2 cronotacógrafo | 3 carreta 1 |
    # 4 carreta 2 | 5 CIV carretas | 6 CIPP carretas | 7 aferição carretas.
    for _, row in df_base.iterrows():
        cavalo = limpar_placa(valor_posicional(row, 0))
        carreta_1 = limpar_placa(valor_posicional(row, 3))
        carreta_2 = limpar_placa(valor_posicional(row, 4))
        placas = [placa for placa in [cavalo, carreta_1, carreta_2] if placa]
        if not placas:
            continue
        composicao = " + ".join(placas)
        dados_composicao = {
            "composicao": composicao,
            "placa_cavalo": cavalo,
            "placa_carreta_1": carreta_1,
            "placa_carreta_2": carreta_2,
        }
        for placa, equipamento in [
            (cavalo, "Cavalo"),
            (carreta_1, "Carreta 1"),
            (carreta_2, "Carreta 2"),
        ]:
            if placa:
                mapa_placas[placa] = {**dados_composicao, "equipamento": equipamento}

        candidatos = [
            (cavalo, "CIV", valor_posicional(row, 1), "Cavalo"),
            (cavalo, "CRONOTACÓGRAFO", valor_posicional(row, 2), "Cavalo"),
            (carreta_1, "CIV", valor_posicional(row, 5), "Carreta 1"),
            (carreta_2, "CIV", valor_posicional(row, 5), "Carreta 2"),
            (carreta_1, "CIPP", valor_posicional(row, 6), "Carreta 1"),
            (carreta_2, "CIPP", valor_posicional(row, 6), "Carreta 2"),
            (carreta_1, "AFERIÇÃO", valor_posicional(row, 7), "Carreta 1"),
            (carreta_2, "AFERIÇÃO", valor_posicional(row, 7), "Carreta 2"),
        ]
        for placa, documento, vencimento, equipamento in candidatos:
            vencimento_iso = data_iso(vencimento)
            if placa and vencimento_iso:
                fallbacks.append(
                    {
                        "placa": placa,
                        "documento": documento,
                        "vencimento": vencimento_iso,
                        **dados_composicao,
                        "equipamento": equipamento,
                        "origem": "BASE DE COMPOSIÇÕES",
                    }
                )

        # Colunas nomeadas permitem trazer os documentos adicionais da base.
        # Para documentos compartilhados, a composição é representada pelo cavalo.
        for coluna in df_base.columns:
            documento = nome_coluna_para_documento(coluna)
            if documento not in {
                "IBAMA", "CR IBAMA", "CRLV", "AETs", "AGENDAMENTO AFERIÇÃO"
            }:
                continue
            nome = normalizar_texto(coluna)
            destinos = []
            if "CAVALO" in nome:
                destinos = [(cavalo, "Cavalo")]
            elif "CARRETA 1" in nome:
                destinos = [(carreta_1, "Carreta 1")]
            elif "CARRETA 2" in nome:
                destinos = [(carreta_2, "Carreta 2")]
            elif documento in {"IBAMA", "CR IBAMA", "AETs"}:
                destinos = [(cavalo, "Composição")]
            for placa, equipamento in destinos:
                vencimento_iso = data_iso(row.get(coluna))
                if placa and vencimento_iso:
                    fallbacks.append(
                        {
                            "placa": placa,
                            "documento": documento,
                            "vencimento": vencimento_iso,
                            **dados_composicao,
                            "equipamento": equipamento,
                            "origem": "BASE DE COMPOSIÇÕES",
                        }
                    )
    return mapa_placas, fallbacks


def atualizar_vinculos_documentos(df_base: pd.DataFrame) -> int:
    mapa_placas, _ = extrair_composicoes_e_fallbacks(df_base)
    atualizados = 0
    with conectar() as conn:
        documentos = conn.execute("SELECT id, placa FROM documentos").fetchall()
        for documento in documentos:
            vinculo = mapa_placas.get(documento["placa"])
            if vinculo:
                valores = (
                    vinculo["composicao"], vinculo["placa_cavalo"],
                    vinculo["placa_carreta_1"], vinculo["placa_carreta_2"],
                    vinculo["equipamento"], documento["id"],
                )
            else:
                valores = (
                    documento["placa"], "", "", "", "Não vinculado",
                    documento["id"],
                )
            conn.execute(
                """
                UPDATE documentos SET
                    composicao = ?, placa_cavalo = ?, placa_carreta_1 = ?,
                    placa_carreta_2 = ?, equipamento = ?
                WHERE id = ?
                """,
                valores,
            )
            atualizados += 1
    return atualizados


def preparar_registros_importacao(
    df_base: pd.DataFrame, df_documentos: pd.DataFrame
) -> list[dict]:
    mapa_placas, fallbacks = extrair_composicoes_e_fallbacks(df_base)
    documentos_origem = extrair_documentos_origem(df_documentos)
    registros = list(fallbacks)

    for item in documentos_origem:
        vinculo = mapa_placas.get(item["placa"])
        if vinculo:
            registros.append({**item, **vinculo})
        else:
            registros.append(
                {
                    **item,
                    "composicao": item["placa"],
                    "placa_cavalo": "",
                    "placa_carreta_1": "",
                    "placa_carreta_2": "",
                    "equipamento": "Não vinculado",
                }
            )
    return registros


# =========================================================
# FILTROS E PAINÉIS
# =========================================================
def enriquecer_status(df: pd.DataFrame, data_referencia: date) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    resultado = df.copy()
    resultado["Status"] = resultado["vencimento"].apply(
        lambda valor: status_vencimento(valor, data_referencia)
    )
    ref = pd.Timestamp(data_referencia).normalize()
    resultado["Dias"] = (resultado["vencimento"] - ref).dt.days
    resultado["_ordem_status"] = resultado["Status"].map(STATUS_ORDEM).fillna(99)
    return resultado.sort_values(
        ["_ordem_status", "vencimento", "composicao", "documento"]
    ).drop(columns="_ordem_status")


def aplicar_filtros(
    df: pd.DataFrame,
    placa: str,
    data_inicio: date | None,
    data_fim: date | None,
    documentos: list[str],
    filtro_card: str,
) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    resultado = df.copy()
    placa_limpa = limpar_placa(placa)
    if placa_limpa:
        resultado = resultado[
            resultado["placa"].str.contains(placa_limpa, na=False)
            | resultado["composicao"].str.replace(" ", "", regex=False).str.contains(
                placa_limpa, na=False
            )
        ]
    if data_inicio:
        resultado = resultado["vencimento"].ge(pd.Timestamp(data_inicio))
    if data_fim:
        resultado = resultado["vencimento"].le(pd.Timestamp(data_fim))
    if documentos:
        resultado = resultado[resultado["documento"].isin(documentos)]

    filtros_card = {
        "VENCIDOS": ["VENCIDO"],
        "HOJE": ["VENCE HOJE"],
        "SEMANA": ["VENCE HOJE", "VENCE NA SEMANA"],
        "MÊS": ["VENCE NO MÊS"],
        "OK": ["OK"],
    }
    if filtro_card in filtros_card:
        resultado = resultado[resultado["Status"].isin(filtros_card[filtro_card])]
    return resultado


def resumir_composicoes(df: pd.DataFrame) -> pd.DataFrame:
    colunas = [
        "Placas da composição",
        "Placa do documento",
        "Documento/Laudo",
        "Data de vencimento",
        "Alterado em",
        "Alterado por",
    ]
    if df.empty:
        return pd.DataFrame(columns=colunas)

    def juntar_unicos(serie) -> str:
        return "\n".join(dict.fromkeys(str(x) for x in serie if str(x).strip()))

    temporario = df.copy()
    temporario["vencimento_formatado"] = temporario["vencimento"].dt.strftime("%d/%m/%Y")
    temporario["alterado_em_formatado"] = temporario["importado_em"].apply(
        formatar_data_hora
    )
    resumo = (
        temporario.groupby("composicao", sort=False)
        .agg(
            **{
                "Placa do documento": ("placa", juntar_unicos),
                "Documento/Laudo": ("documento", juntar_unicos),
                "Data de vencimento": ("vencimento_formatado", juntar_unicos),
                "Alterado em": ("alterado_em_formatado", juntar_unicos),
                "Alterado por": ("importado_por", juntar_unicos),
            }
        )
        .reset_index()
        .rename(columns={"composicao": "Placas da composição"})
    )
    return resumo[colunas]


def resumir_afericoes(df: pd.DataFrame) -> pd.DataFrame:
    colunas = ["Composição", "Data de vencimento"]
    if df.empty:
        return pd.DataFrame(columns=colunas)
    temporario = df.sort_values(["vencimento", "composicao"]).copy()
    temporario["Data de vencimento"] = temporario["vencimento"].dt.strftime("%d/%m/%Y")
    return (
        temporario[["composicao", "Data de vencimento"]]
        .drop_duplicates()
        .rename(columns={"composicao": "Composição"})
        .reset_index(drop=True)
    )


def resumir_ibama_aets(df: pd.DataFrame) -> pd.DataFrame:
    colunas = ["Documento/Laudo", "Data de vencimento"]
    if df.empty:
        return pd.DataFrame(columns=colunas)
    temporario = df.sort_values(["documento", "vencimento"]).copy()
    temporario["Data de vencimento"] = temporario["vencimento"].dt.strftime("%d/%m/%Y")
    return (
        temporario[["documento", "Data de vencimento"]]
        .drop_duplicates()
        .rename(columns={"documento": "Documento/Laudo"})
        .reset_index(drop=True)
    )


def resumir_crlv(df: pd.DataFrame) -> pd.DataFrame:
    colunas = ["Placa da composição", "Data de vencimento"]
    if df.empty:
        return pd.DataFrame(columns=colunas)
    temporario = df.sort_values(["vencimento", "placa"]).copy()
    temporario["Data de vencimento"] = temporario["vencimento"].dt.strftime("%d/%m/%Y")
    return (
        temporario[["placa", "Data de vencimento"]]
        .drop_duplicates()
        .rename(columns={"placa": "Placa da composição"})
        .reset_index(drop=True)
    )


def preparar_detalhe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "Status", "Composição", "Equipamento", "Placa", "Documento/Laudo",
                "Vencimento", "Dias", "Origem", "Alterado por", "Alterado em",
            ]
        )
    saida = df.copy()
    saida["vencimento"] = saida["vencimento"].dt.strftime("%d/%m/%Y")
    saida["importado_em"] = saida["importado_em"].apply(formatar_data_hora)
    saida = saida.rename(
        columns={
            "composicao": "Composição",
            "equipamento": "Equipamento",
            "placa": "Placa",
            "documento": "Documento/Laudo",
            "vencimento": "Vencimento",
            "origem": "Origem",
            "importado_por": "Alterado por",
            "importado_em": "Alterado em",
        }
    )
    colunas = [
        "Status", "Composição", "Equipamento", "Placa", "Documento/Laudo",
        "Vencimento", "Dias", "Origem", "Alterado por", "Alterado em",
    ]
    return saida[colunas]


def preparar_historico_atualizacoes(df: pd.DataFrame) -> pd.DataFrame:
    colunas = [
        "Data/hora da atualização",
        "Usuário",
        "Tipo de documento",
        "Placa ou composição",
        "Vencimento anterior",
        "Nova data de vencimento",
        "Origem da atualização/importação",
    ]
    if df.empty:
        return pd.DataFrame(columns=colunas)
    saida = df.copy()
    saida["Placa ou composição"] = saida["composicao"].where(
        saida["composicao"].astype(str).str.strip().ne(""), saida["placa"]
    )
    saida["data_hora"] = saida["data_hora"].apply(formatar_data_hora)
    saida["vencimento_anterior"] = saida["vencimento_anterior"].apply(formatar_data)
    saida["novo_vencimento"] = saida["novo_vencimento"].apply(formatar_data)
    saida = saida.rename(
        columns={
            "data_hora": "Data/hora da atualização",
            "usuario": "Usuário",
            "documento": "Tipo de documento",
            "vencimento_anterior": "Vencimento anterior",
            "novo_vencimento": "Nova data de vencimento",
            "origem": "Origem da atualização/importação",
        }
    )
    return saida[colunas]


def mostrar_ultimos_atualizados(
    auditoria: pd.DataFrame, dados_painel: pd.DataFrame
) -> None:
    eventos = pd.DataFrame()
    if not auditoria.empty and not dados_painel.empty:
        chaves = dados_painel[["documento", "placa"]].drop_duplicates()
        eventos = auditoria.merge(chaves, on=["documento", "placa"], how="inner")

    if not eventos.empty:
        labels = [
            f"{row.documento} {row.placa}".strip()
            for row in eventos.itertuples(index=False)
        ]
    elif not dados_painel.empty:
        fallback = dados_painel.sort_values("importado_em", ascending=False)
        labels = [
            f"{row.documento} {row.placa}".strip()
            for row in fallback.itertuples(index=False)
        ]
    else:
        labels = []

    labels = list(dict.fromkeys(labels))[:10]
    texto = ", ".join(labels) if labels else "Nenhum registro."
    st.markdown(f"**Últimos atualizados:** {texto}")


def painel_status(
    titulo: str,
    df: pd.DataFrame,
    status: list[str],
    mensagem_vazia: str,
    auditoria: pd.DataFrame,
) -> None:
    st.subheader(titulo)
    dados = df[df["Status"].isin(status)] if not df.empty else df
    if dados.empty:
        st.success(mensagem_vazia)
        mostrar_ultimos_atualizados(auditoria, dados)
        return
    resumo = resumir_composicoes(dados)
    st.dataframe(
        estilizar_tabela(resumo),
        use_container_width=True,
        hide_index=True,
        height=min(460, 75 + len(resumo) * 36),
    )
    mostrar_ultimos_atualizados(auditoria, dados)


def estilizar_tabela(df: pd.DataFrame):
    return df.style.set_table_styles(
        [
            {
                "selector": "th",
                "props": [
                    ("background-color", COR_CABECALHO),
                    ("color", COR_TEXTO),
                    ("font-weight", "700"),
                ],
            },
            {"selector": "td", "props": [("color", COR_TEXTO)]},
        ]
    )


def gerar_excel(
    documentos_filtrados: pd.DataFrame,
    historico: pd.DataFrame,
    importacoes: pd.DataFrame,
    auditoria: pd.DataFrame,
) -> bytes:
    output = BytesIO()
    detalhe = preparar_detalhe(documentos_filtrados)
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        detalhe.to_excel(writer, sheet_name="DOCUMENTOS", index=False)
        resumir_composicoes(documentos_filtrados).to_excel(
            writer, sheet_name="COMPOSICOES", index=False
        )
        historico.to_excel(writer, sheet_name="HISTORICO", index=False)
        preparar_historico_atualizacoes(auditoria).to_excel(
            writer, sheet_name="HISTORICO_ATUALIZACAO", index=False
        )
        importacoes.to_excel(writer, sheet_name="IMPORTACOES", index=False)
        workbook = writer.book
        formato_cabecalho = workbook.add_format(
            {
                "bold": True,
                "bg_color": COR_CABECALHO,
                "font_color": COR_TEXTO,
                "border": 1,
                "align": "center",
                "valign": "vcenter",
            }
        )
        formato_texto = workbook.add_format(
            {"font_color": COR_TEXTO, "border": 1, "valign": "top", "text_wrap": True}
        )
        for nome_aba, planilha in {
            "DOCUMENTOS": detalhe,
            "COMPOSICOES": resumir_composicoes(documentos_filtrados),
            "HISTORICO": historico,
            "HISTORICO_ATUALIZACAO": preparar_historico_atualizacoes(auditoria),
            "IMPORTACOES": importacoes,
        }.items():
            ws = writer.sheets[nome_aba]
            ws.freeze_panes(1, 0)
            for indice, coluna in enumerate(planilha.columns):
                ws.write(0, indice, coluna, formato_cabecalho)
                largura = min(max(len(str(coluna)) + 4, 15), 38)
                ws.set_column(indice, indice, largura, formato_texto)
            if len(planilha) and len(planilha.columns):
                ws.autofilter(0, 0, len(planilha), len(planilha.columns) - 1)
    return output.getvalue()


# =========================================================
# INTERFACE
# =========================================================
st.set_page_config(
    page_title="Painel de Vencimentos",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    f"""
    <style>
    :root {{ --cabecalho: {COR_CABECALHO}; --texto: {COR_TEXTO}; }}
    .stApp {{ color: var(--texto); }}
    .block-container {{ padding-top: 1.2rem; max-width: 1550px; }}
    h1, h2, h3, label, p {{ color: var(--texto); }}
    .titulo {{ font-size: 2.15rem; font-weight: 850; color: var(--texto); }}
    .subtitulo {{ color: #4B536F; margin: 0.1rem 0 1rem; }}
    .faixa {{ background: var(--cabecalho); color: var(--texto); padding: .75rem 1rem;
              border-radius: 12px; font-weight: 800; margin: .7rem 0; }}
    div[data-testid="stButton"] > button {{
        width: 100%; min-height: 92px; border-radius: 15px;
        border: 1px solid var(--cabecalho); background: #FFFDF5;
        color: var(--texto); font-weight: 800; font-size: 1rem;
        white-space: pre-line;
    }}
    div[data-testid="stButton"] > button:hover {{
        background: var(--cabecalho); color: var(--texto); border-color: var(--cabecalho);
    }}
    div[data-testid="stDataFrame"] {{ border: 1px solid #D8C98D; border-radius: 12px; }}
    .stDownloadButton > button {{
        border-color: var(--cabecalho); color: var(--texto); font-weight: 750;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


def main() -> None:
    inicializar_banco()
    st.markdown(
        '<div class="titulo">Painel de vencimentos por composição</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="subtitulo">Banco persistente, histórico de alterações e '
        'composições mantidas em uma única linha lógica.</div>',
        unsafe_allow_html=True,
    )

    base_salva, metadata_base = carregar_base_composicoes()

    with st.expander("Importar e atualizar o banco de dados", expanded=False):
        if metadata_base:
            st.success(
                "Base de composições salva: "
                f"{metadata_base['nome_arquivo']} · {metadata_base['total_linhas']} linhas · "
                f"alterada em {formatar_data_hora(metadata_base['atualizado_em'])} "
                f"por {metadata_base['atualizado_por']}."
            )
        else:
            st.warning(
                "Nenhuma base de composições encontrada. Importe a base inicial para "
                "iniciar o sistema."
            )
        col_usuario, col_base, col_documentos = st.columns([0.7, 1.15, 1.15])
        with col_usuario:
            usuario = st.text_input(
                "Usuário responsável *", placeholder="Nome do usuário"
            )
        with col_base:
            arquivo_base = st.file_uploader(
                "Nova base de composições (opcional)",
                type=["xlsx", "xls"],
                key="base",
            )
        with col_documentos:
            arquivo_documentos = st.file_uploader(
                "Laudos/documentos (opcional)",
                type=["xlsx", "xls"],
                key="documentos",
            )

        aba_base = aba_documentos = None
        if arquivo_base:
            arquivo_base.seek(0)
            abas_base = pd.ExcelFile(arquivo_base).sheet_names
        else:
            abas_base = []
        if arquivo_documentos:
            arquivo_documentos.seek(0)
            abas_documentos = pd.ExcelFile(arquivo_documentos).sheet_names
        else:
            abas_documentos = []
        c1, c2 = st.columns(2)
        if abas_base:
            with c1:
                aba_base = st.selectbox("Aba da base", abas_base)
        if abas_documentos:
            with c2:
                aba_documentos = st.selectbox("Aba dos documentos", abas_documentos)

        if st.button("Importar e atualizar banco", key="executar_importacao"):
            if not usuario.strip():
                st.error("Informe o usuário responsável pela importação.")
            elif not arquivo_base and not arquivo_documentos:
                st.error("Envie uma nova base de composições ou os Laudos/Documentos.")
            elif not arquivo_base and metadata_base is None:
                st.error(
                    "Nenhuma base de composições encontrada. Importe a base inicial para "
                    "iniciar o sistema."
                )
            else:
                try:
                    with st.spinner("Validando, criando backup e atualizando o banco..."):
                        nova_base = arquivo_base is not None
                        df_base_usada = (
                            ler_base(arquivo_base, aba_base) if nova_base else base_salva
                        )
                        registros = []
                        if arquivo_documentos:
                            df_documentos = ler_planilha_documentos(
                                arquivo_documentos, aba_documentos
                            )
                            registros = preparar_registros_importacao(
                                df_base_usada, df_documentos
                            )
                            if not registros:
                                raise ValueError("Nenhum documento válido foi encontrado.")
                        elif nova_base:
                            _, registros = extrair_composicoes_e_fallbacks(df_base_usada)

                        resultado_base = None
                        if nova_base:
                            resultado_base = salvar_base_composicoes(
                                df_base_usada, usuario, arquivo_base.name, aba_base
                            )
                            atualizar_vinculos_documentos(df_base_usada)

                        resultado = None
                        if registros:
                            nome_base = (
                                arquivo_base.name if nova_base
                                else metadata_base.get("nome_arquivo", "BASE SALVA")
                            )
                            resultado = salvar_importacao(
                                registros,
                                usuario,
                                nome_base,
                                arquivo_documentos.name if arquivo_documentos else "",
                            )

                    mensagens = []
                    if resultado_base:
                        mensagens.append(
                            f"Base de composições salva ({resultado_base['total_linhas']} linhas)"
                        )
                    if resultado:
                        mensagens.append(
                            f"importação {resultado['importacao_id']}: "
                            f"{resultado['inseridos']} inseridos, "
                            f"{resultado['atualizados']} atualizados e "
                            f"{resultado['ignorados']} ignorados"
                        )
                    st.success(" · ".join(mensagens) + ".")
                except Exception as erro:
                    st.error(f"Não foi possível importar: {erro}")

    base_salva, metadata_base = carregar_base_composicoes()

    with st.expander("Seguranca do banco: voltar ou zerar", expanded=False):
        mensagem_seguranca = st.session_state.pop("mensagem_seguranca", None)
        if mensagem_seguranca:
            st.success(mensagem_seguranca)
        st.warning(
            "Use estas opcoes somente quando uma importacao tiver sido feita com "
            "dados errados. Para voltar, o sistema restaura o snapshot salvo antes "
            "da importacao escolhida."
        )
        tab_voltar, tab_base, tab_zerar = st.tabs(
            ["Voltar importacao", "Voltar base", "Zerar banco"]
        )
        with tab_voltar:
            importacoes_reversao = carregar_importacoes_reversao()
            if importacoes_reversao.empty:
                st.info("Ainda nao ha importacoes para restaurar.")
            else:
                opcoes = importacoes_reversao.to_dict("records")
                selecionada = st.selectbox(
                    "Escolha a importacao que deseja desfazer",
                    opcoes,
                    format_func=lambda item: (
                        f"#{item['importacao_id']} - "
                        f"{formatar_data_hora(item['data_hora'])} - "
                        f"{item['usuario']} - "
                        f"{item.get('arquivo_documentos') or item.get('arquivo_base') or 'sem arquivo'}"
                    ),
                    key="importacao_para_restaurar",
                )
                st.caption(
                    f"Snapshot anterior: {selecionada['registros_backup']} registro(s). "
                    "Se o snapshot tiver 0 registros, o banco voltara para vazio antes "
                    "daquela importacao."
                )
                usuario_reversao = st.text_input(
                    "Usuario responsavel pela restauracao",
                    key="usuario_reversao",
                )
                confirma_reversao = st.text_input(
                    f"Para confirmar, digite {COMANDO_RESTAURAR}",
                    key="confirma_reversao",
                )
                if st.button("Restaurar estado anterior", key="btn_restaurar_backup"):
                    if not usuario_reversao.strip():
                        st.error("Informe o usuario responsavel pela restauracao.")
                    elif confirma_reversao.strip().upper() != COMANDO_RESTAURAR:
                        st.error(f"Digite exatamente {COMANDO_RESTAURAR} para confirmar.")
                    else:
                        try:
                            resultado_reversao = restaurar_backup_importacao(
                                int(selecionada["importacao_id"]), usuario_reversao
                            )
                            st.session_state.mensagem_seguranca = (
                                f"Importacao {resultado_reversao['importacao_id']} "
                                f"desfeita. O banco saiu de "
                                f"{resultado_reversao['registros_antes']} registro(s) "
                                f"ativos para "
                                f"{resultado_reversao['registros_restaurados']} "
                                "registro(s) restaurado(s)."
                            )
                            st.rerun()
                        except Exception as erro:
                            st.error(f"Nao foi possivel restaurar: {erro}")

        with tab_base:
            backup_base, metadata_backup_base = carregar_ultimo_backup_base()
            if metadata_backup_base is None:
                st.info("Ainda nao ha backup anterior da base de composicoes.")
            else:
                st.caption(
                    f"Ultima base em backup: {metadata_backup_base['nome_arquivo']} - "
                    f"{metadata_backup_base['total_linhas']} linhas - backup criado em "
                    f"{formatar_data_hora(metadata_backup_base['backup_em'])}."
                )
                usuario_base = st.text_input(
                    "Usuario responsavel pela restauracao da base",
                    key="usuario_restaurar_base",
                )
                confirma_base = st.text_input(
                    f"Para confirmar, digite {COMANDO_RESTAURAR_BASE}",
                    key="confirma_restaurar_base",
                )
                if st.button("Restaurar ultima base em backup", key="btn_restaurar_base"):
                    if not usuario_base.strip():
                        st.error("Informe o usuario responsavel pela restauracao.")
                    elif confirma_base.strip().upper() != COMANDO_RESTAURAR_BASE:
                        st.error(
                            f"Digite exatamente {COMANDO_RESTAURAR_BASE} para confirmar."
                        )
                    else:
                        try:
                            resultado_base = restaurar_ultimo_backup_base_composicoes(
                                usuario_base
                            )
                            st.session_state.mensagem_seguranca = (
                                "Base de composicoes restaurada: "
                                f"{resultado_base['nome_arquivo']} - "
                                f"{resultado_base['total_linhas']} linhas - "
                                f"{resultado_base['vinculos_atualizados']} vinculo(s) "
                                "de documento atualizados."
                            )
                            st.rerun()
                        except Exception as erro:
                            st.error(f"Nao foi possivel restaurar a base: {erro}")

        with tab_zerar:
            st.error(
                "Atencao: esta acao apaga documentos, historicos, backups, "
                "importacoes e a base de composicoes salva."
            )
            usuario_zerar = st.text_input(
                "Usuario responsavel pela zeragem", key="usuario_zerar_banco"
            )
            confirma_zerar = st.text_input(
                f"Para confirmar, digite {COMANDO_ZERAR_BANCO}",
                key="confirma_zerar_banco",
            )
            if st.button("Zerar banco de dados", key="btn_zerar_banco"):
                if not usuario_zerar.strip():
                    st.error("Informe o usuario responsavel pela zeragem.")
                elif confirma_zerar.strip().upper() != COMANDO_ZERAR_BANCO:
                    st.error(f"Digite exatamente {COMANDO_ZERAR_BANCO} para confirmar.")
                else:
                    try:
                        resultado_zeragem = zerar_banco_dados(usuario_zerar)
                        st.session_state.filtro_card = "TODOS"
                        st.session_state.mensagem_seguranca = (
                            "Banco zerado com sucesso. Foram apagados "
                            f"{resultado_zeragem['documentos']} documento(s), "
                            f"{resultado_zeragem['importacoes']} importacao(oes) "
                            f"e {resultado_zeragem['bases']} base(s) ativa(s)."
                        )
                        st.rerun()
                    except Exception as erro:
                        st.error(f"Nao foi possivel zerar o banco: {erro}")

    if metadata_base is None:
        st.warning(
            "Nenhuma base de composições encontrada. Importe a base inicial para iniciar "
            "o sistema."
        )
        return

    documentos_banco = carregar_documentos()
    if documentos_banco.empty:
        st.info(
            "A base de composições está salva. Importe os Laudos/Documentos para iniciar "
            "a análise dos vencimentos."
        )
        return

    st.markdown('<div class="faixa">Filtros principais</div>', unsafe_allow_html=True)
    f1, f2, f3, f4, f5 = st.columns([1.25, 0.85, 0.85, 1.45, 0.65])
    with f1:
        filtro_placa = st.text_input("Placa ou composição")
    with f2:
        filtro_inicio = st.date_input("Vencimento inicial", value=None, format="DD/MM/YYYY")
    with f3:
        filtro_fim = st.date_input("Vencimento final", value=None, format="DD/MM/YYYY")
    with f4:
        filtro_documentos = st.multiselect(
            "Documento/Laudo", TIPOS_DOCUMENTO, placeholder="Todos"
        )
    with f5:
        data_referencia = st.date_input(
            "Referência", value=date.today(), format="DD/MM/YYYY"
        )

    documentos_status = enriquecer_status(documentos_banco, data_referencia)
    auditoria = carregar_historico_atualizacoes()
    if "filtro_card" not in st.session_state:
        st.session_state.filtro_card = "TODOS"

    base_dos_cards = aplicar_filtros(
        documentos_status,
        filtro_placa,
        filtro_inicio,
        filtro_fim,
        filtro_documentos,
        "TODOS",
    )
    contagens = {
        "TODOS": len(base_dos_cards),
        "VENCIDOS": int((base_dos_cards["Status"] == "VENCIDO").sum()),
        "HOJE": int((base_dos_cards["Status"] == "VENCE HOJE").sum()),
        "SEMANA": int(
            base_dos_cards["Status"].isin(["VENCE HOJE", "VENCE NA SEMANA"]).sum()
        ),
        "MÊS": int((base_dos_cards["Status"] == "VENCE NO MÊS").sum()),
        "OK": int((base_dos_cards["Status"] == "OK").sum()),
    }
    card_cols = st.columns(6)
    labels = {
        "TODOS": "Todos",
        "VENCIDOS": "Vencidos",
        "HOJE": "Vencem hoje",
        "SEMANA": "Vencem na semana",
        "MÊS": "Vencem no mês",
        "OK": "Regulares",
    }
    for coluna, chave in zip(card_cols, labels):
        with coluna:
            ativo = "✓ " if st.session_state.filtro_card == chave else ""
            if st.button(
                f"{ativo}{labels[chave]}\n{contagens[chave]}", key=f"card_{chave}"
            ):
                st.session_state.filtro_card = chave
                st.rerun()

    filtrados = aplicar_filtros(
        base_dos_cards,
        "",
        None,
        None,
        [],
        st.session_state.filtro_card,
    )
    st.caption(
        f"Filtro de card ativo: {labels[st.session_state.filtro_card]} · "
        f"{len(filtrados)} documento(s) · "
        f"referência {pd.Timestamp(data_referencia).strftime('%d/%m/%Y')}"
    )

    st.markdown('<div class="faixa">Painéis por período</div>', unsafe_allow_html=True)
    with st.expander("Vencidos", expanded=False):
        painel_status(
            "Documentos vencidos", filtrados, ["VENCIDO"],
            "Nenhum documento vencido.", auditoria
        )
    with st.expander("Vencimentos na semana", expanded=False):
        painel_status(
            "Vencimentos desta semana",
            filtrados,
            ["VENCE HOJE", "VENCE NA SEMANA"],
            "Nenhum documento vence nesta semana.",
            auditoria,
        )
    with st.expander("Vencimentos no mês", expanded=False):
        painel_status(
            "Vencimentos após esta semana, ainda neste mês",
            filtrados,
            ["VENCE NO MÊS"],
            "Nenhum documento vence no restante do mês.",
            auditoria,
        )

    st.markdown('<div class="faixa">Painéis exclusivos</div>', unsafe_allow_html=True)
    with st.expander("Aferições", expanded=False):
        dados = filtrados[
            filtrados["documento"].isin(["AFERIÇÃO", "AGENDAMENTO AFERIÇÃO"])
        ]
        st.subheader("Aferições")
        if dados.empty:
            st.info("Nenhuma aferição encontrada para os filtros atuais.")
        else:
            st.dataframe(
                estilizar_tabela(resumir_afericoes(dados)),
                use_container_width=True,
                hide_index=True,
                height=360,
            )
        mostrar_ultimos_atualizados(auditoria, dados)
    with st.expander("IBAMA · CR IBAMA · AETs", expanded=False):
        dados = filtrados[
            filtrados["documento"].isin(["IBAMA", "CR IBAMA", "AETs"])
        ]
        st.subheader("IBAMA / CR IBAMA / AETs")
        if dados.empty:
            st.info("Nenhum documento deste grupo foi encontrado.")
        else:
            st.dataframe(
                estilizar_tabela(resumir_ibama_aets(dados)),
                use_container_width=True,
                hide_index=True,
                height=280,
            )
        mostrar_ultimos_atualizados(auditoria, dados)
    with st.expander("CRLV", expanded=False):
        dados = filtrados[filtrados["documento"] == "CRLV"]
        st.subheader("CRLV")
        if dados.empty:
            st.info("Nenhum CRLV foi encontrado para os filtros atuais.")
        else:
            st.dataframe(
                estilizar_tabela(resumir_crlv(dados)),
                use_container_width=True,
                hide_index=True,
                height=360,
            )
        mostrar_ultimos_atualizados(auditoria, dados)

    with st.expander("Composições com documentos no filtro", expanded=False):
        st.dataframe(
            estilizar_tabela(resumir_composicoes(filtrados)),
            use_container_width=True,
            hide_index=True,
            height=390,
        )
        mostrar_ultimos_atualizados(auditoria, filtrados)

    with st.expander("Detalhes, histórico e backup", expanded=False):
        (
            tab_detalhe, tab_historico, tab_backup, tab_importacoes,
            tab_backup_base, tab_historico_base,
        ) = st.tabs(
            [
                "Detalhes", "Registros substituídos", "Backup documentos",
                "Importações", "Backup da base", "Histórico da base",
            ]
        )
        historico = carregar_historico()
        importacoes = carregar_importacoes()
        backup, backup_id = carregar_ultimo_backup()
        backup_base, metadata_backup_base = carregar_ultimo_backup_base()
        historico_bases = carregar_historico_bases()
        with tab_detalhe:
            st.dataframe(
                estilizar_tabela(preparar_detalhe(filtrados)),
                use_container_width=True,
                hide_index=True,
                height=360,
            )
        with tab_historico:
            st.caption("Versões que foram substituídas por um vencimento mais recente.")
            st.dataframe(
                estilizar_tabela(historico), use_container_width=True,
                hide_index=True, height=360
            )
        with tab_backup:
            if backup_id is None:
                st.info("Ainda não há snapshot anterior disponível.")
            else:
                st.caption(f"Estado do banco antes da importação {backup_id}.")
                st.dataframe(
                    estilizar_tabela(backup), use_container_width=True,
                    hide_index=True, height=360
                )
        with tab_importacoes:
            st.dataframe(
                estilizar_tabela(importacoes), use_container_width=True,
                hide_index=True, height=360
            )
        with tab_backup_base:
            if metadata_backup_base is None:
                st.info("Ainda não há uma base de composições anterior no backup.")
            else:
                st.caption(
                    f"Base anterior: {metadata_backup_base['nome_arquivo']} · "
                    f"{metadata_backup_base['total_linhas']} linhas · salva originalmente "
                    f"em {formatar_data_hora(metadata_backup_base['atualizado_em'])} por "
                    f"{metadata_backup_base['atualizado_por']} · backup criado em "
                    f"{formatar_data_hora(metadata_backup_base['backup_em'])}."
                )
                st.dataframe(
                    estilizar_tabela(backup_base), use_container_width=True,
                    hide_index=True, height=360
                )
        with tab_historico_base:
            st.dataframe(
                estilizar_tabela(historico_bases), use_container_width=True,
                hide_index=True, height=360
            )

        excel = gerar_excel(filtrados, historico, importacoes, auditoria)
        st.download_button(
            "Baixar relatório e histórico em Excel",
            data=excel,
            file_name=f"painel_vencimentos_{date.today():%Y%m%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    st.info(
        "Regra de atualização: cada registro ativo é identificado por placa + tipo de "
        "documento. O maior vencimento permanece ativo; duplicatas e datas mais antigas "
        "são ignoradas. A última base de composições salva é reutilizada automaticamente "
        "quando uma nova base não é enviada."
    )

    with st.expander("Histórico de Atualização", expanded=False):
        st.caption(
            "Auditoria das inserções e alterações registradas no banco de dados."
        )
        st.dataframe(
            estilizar_tabela(preparar_historico_atualizacoes(auditoria)),
            use_container_width=True,
            hide_index=True,
            height=430,
        )
        mostrar_ultimos_atualizados(auditoria, documentos_status)


if __name__ == "__main__":
    main()

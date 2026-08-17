import base64
import html
import json
import re
import shutil
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


# =========================================================
# CONFIGURAÇÕES
# =========================================================
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "painel_vencimentos.db"
ASSETS_DIR = APP_DIR / "assets"
WEB_DIR = APP_DIR / "web"
WEB_ASSETS_DIR = WEB_DIR / "assets"
WEB_DATA_DIR = WEB_DIR / "data"
WEB_JSON_PATH = WEB_DATA_DIR / "vencimentos_proximos.json"
WEB_LOGO_PATH = WEB_ASSETS_DIR / "logo.png"

COR_CABECALHO = "#020D3F"
COR_TEXTO = "#B5911B"
CONTROLE_TV_COUPA_URL = "https://controle-integrado.streamlit.app/?tv=documentos-coupa&painel=coupa"
MANUTENCAO_COLUNAS = ["Placa", "Manutenção Programada", "Previsão Saída"]

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
DOCUMENTOS_VENCIMENTOS_PROXIMOS = {
    "CIV",
    "CIPP",
    "CRLV",
    "AETs",
    "CRONOTACÓGRAFO",
    "AFERIÇÃO",
    "AGENDAMENTO AFERIÇÃO",
}


# =========================================================
# UTILIDADES
# =========================================================
def agora_local() -> datetime:
    try:
        return datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        return datetime.now().astimezone()


def query_param(nome: str, padrao: str = "") -> str:
    try:
        valor = st.query_params.get(nome, padrao)
    except Exception:
        return padrao
    if isinstance(valor, list):
        return str(valor[0] if valor else padrao)
    return str(valor if valor not in [None, ""] else padrao)


def query_ativo(nome: str) -> bool:
    return query_param(nome).strip().lower() in {"1", "sim", "s", "true", "yes"}


def normalizar_texto(valor) -> str:
    if valor is None or pd.isna(valor):
        return ""
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.upper()).strip()


def limpar_placa(valor) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalizar_texto(valor))


def chave_composicao(valor) -> str:
    texto = normalizar_texto(valor)
    placas = re.findall(r"(?<![A-Z0-9])([A-Z0-9]{7})(?![A-Z0-9])", texto)
    if not placas:
        texto_limpo = limpar_placa(valor)
        if texto_limpo and len(texto_limpo) % 7 == 0:
            placas = [
                texto_limpo[indice: indice + 7]
                for indice in range(0, len(texto_limpo), 7)
            ]
    if placas:
        return " + ".join(sorted(dict.fromkeys(placas)))
    return limpar_placa(valor)


def chave_documento_consolidacao(valor) -> str:
    documento = classificar_documento(valor) or normalizar_texto(valor)
    if documento in {"IBAMA", "CR IBAMA"}:
        return "IBAMA"
    return documento


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


def timestamp_prioridade(valor) -> str:
    if not valor:
        return ""
    try:
        convertido = datetime.fromisoformat(str(valor))
    except (TypeError, ValueError):
        convertido = pd.to_datetime(valor, errors="coerce")
        if pd.isna(convertido):
            return ""
        convertido = convertido.to_pydatetime()
    return convertido.isoformat(timespec="seconds")


def inteiro_prioridade(valor) -> int:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return 0
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0


def prioridade_documento(
    vencimento: str,
    atualizado_em: str,
    origem: str = "",
    importacao_id: int | None = None,
) -> tuple:
    return (
        inteiro_prioridade(importacao_id),
        timestamp_prioridade(atualizado_em),
        1 if str(origem or "").strip().upper() == "PLANILHA DE DOCUMENTOS" else 0,
        str(vencimento or ""),
    )


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


def texto_dias_restantes(dias: int) -> str:
    if dias < 0:
        dias_vencidos = abs(dias)
        if dias_vencidos == 1:
            return "1 dia vencido"
        return f"{dias_vencidos} dias vencidos"
    if dias == 0:
        return "vence hoje"
    if dias == 1:
        return "1 dia"
    return f"{dias} dias"


def localizar_logo() -> Path | None:
    for nome_arquivo in ["logo.png", "logo.jpg", "logo.jpeg", "logo.webp"]:
        caminho = ASSETS_DIR / nome_arquivo
        if caminho.exists():
            return caminho
    return None


def logo_data_uri() -> str:
    caminho = localizar_logo()
    if not caminho:
        return ""
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(caminho.suffix.lower(), "image/png")
    conteudo = base64.b64encode(caminho.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{conteudo}"


def milissegundos_ate_proxima_atualizacao() -> int:
    agora = agora_local()
    proxima = datetime.combine(
        agora.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=agora.tzinfo,
    ) + timedelta(minutes=5)
    return max(int((proxima - agora).total_seconds() * 1000), 60_000)


def configurar_recarga_diaria() -> None:
    intervalo_ms = milissegundos_ate_proxima_atualizacao()
    components.html(
        f"""
        <script>
        const atraso = {intervalo_ms};
        window.setTimeout(() => {{
            window.parent.location.reload();
        }}, atraso);
        </script>
        """,
        height=0,
        width=0,
    )


def formatar_data_saida_manutencao(valor) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    if isinstance(valor, pd.Timestamp):
        return valor.strftime("%d/%m/%Y") if not pd.isna(valor) else ""
    if isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    convertido = pd.to_datetime(valor, dayfirst=True, errors="coerce")
    if not pd.isna(convertido):
        return convertido.strftime("%d/%m/%Y")
    return str(valor).strip()


def carregar_manutencoes_programadas() -> pd.DataFrame:
    colunas_sql = "placa, manutencao_programada, data_saida"
    with conectar() as conn:
        linhas = conn.execute(
            f"""
            SELECT {colunas_sql}
            FROM manutencoes_programadas
            ORDER BY ordem, id
            """
        ).fetchall()
    if not linhas:
        return pd.DataFrame(columns=MANUTENCAO_COLUNAS)
    return pd.DataFrame(
        [
            {
                "Placa": row["placa"],
                "Manutenção Programada": row["manutencao_programada"],
                "Previsão Saída": row["data_saida"],
            }
            for row in linhas
        ],
        columns=MANUTENCAO_COLUNAS,
    )


def salvar_manutencoes_programadas(df: pd.DataFrame) -> int:
    registros = []
    for _, row in df.iterrows():
        placa = limpar_placa(row.get("Placa", ""))
        manutencao = str(row.get("Manutenção Programada", "") or "").strip()
        data_saida = formatar_data_saida_manutencao(row.get("Previsão Saída"))
        if not placa and not manutencao and not data_saida:
            continue
        registros.append((len(registros) + 1, placa, manutencao, data_saida))

    atualizado_em = agora_local().isoformat(timespec="seconds")
    with conectar() as conn:
        conn.execute("DELETE FROM manutencoes_programadas")
        conn.executemany(
            """
            INSERT INTO manutencoes_programadas
                (ordem, placa, manutencao_programada, data_saida, atualizado_em)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(*registro, atualizado_em) for registro in registros],
        )
    return len(registros)


def ultima_atualizacao_manutencoes() -> str:
    with conectar() as conn:
        row = conn.execute(
            "SELECT MAX(atualizado_em) AS atualizado_em FROM manutencoes_programadas"
        ).fetchone()
    return formatar_data_hora(row["atualizado_em"]) if row and row["atualizado_em"] else "Sem registros salvos"


def preparar_editor_manutencoes(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            [{"Placa": "", "Manutenção Programada": "", "Previsão Saída": None}],
            columns=MANUTENCAO_COLUNAS,
        )
    editor = df.copy()
    editor["Previsão Saída"] = pd.to_datetime(
        editor["Previsão Saída"], dayfirst=True, errors="coerce"
    ).dt.date
    return editor[MANUTENCAO_COLUNAS]


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

            CREATE TABLE IF NOT EXISTS manutencoes_programadas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ordem INTEGER NOT NULL,
                placa TEXT NOT NULL,
                manutencao_programada TEXT NOT NULL,
                data_saida TEXT,
                atualizado_em TEXT NOT NULL
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
            CREATE INDEX IF NOT EXISTS idx_manutencoes_ordem
                ON manutencoes_programadas(ordem, id);
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
        prioridade_registro = prioridade_documento(
            registro["vencimento"],
            registro.get("alterado_em_origem", ""),
            registro.get("origem", ""),
        )
        prioridade_anterior = prioridade_documento(
            anterior["vencimento"],
            anterior.get("alterado_em_origem", ""),
            anterior.get("origem", ""),
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

            prioridade_registro = prioridade_documento(
                registro["vencimento"],
                alterado_em_registro,
                registro.get("origem", ""),
                importacao_id,
            )
            prioridade_existente = prioridade_documento(
                existente["vencimento"],
                existente["importado_em"],
                existente["origem"],
                existente["importacao_id"],
            )
            if prioridade_registro <= prioridade_existente:
                estatisticas["ignorados"] += 1
                continue

            if registro["vencimento"] == existente["vencimento"]:
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


def consolidar_documentos_mais_atualizados(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    resultado = df.copy()
    if "importacao_id" not in resultado.columns:
        resultado["importacao_id"] = 0
    if "origem" not in resultado.columns:
        resultado["origem"] = ""
    resultado["_prioridade_atualizacao"] = resultado.apply(
        lambda row: prioridade_documento(
            row.get("vencimento", ""),
            row.get("importado_em", ""),
            row.get("origem", ""),
            row.get("importacao_id", 0),
        ),
        axis=1,
    )
    resultado["_chave_placa_consolidacao"] = resultado["placa"].apply(limpar_placa)
    resultado["_chave_documento_consolidacao"] = resultado["documento"].apply(
        chave_documento_consolidacao
    )
    resultado = (
        resultado.sort_values("_prioridade_atualizacao", ascending=False)
        .drop_duplicates(
            subset=["_chave_placa_consolidacao", "_chave_documento_consolidacao"],
            keep="first",
        )
        .reset_index(drop=True)
    )
    if "composicao" in resultado.columns:
        resultado["_chave_composicao_consolidacao"] = resultado["composicao"].apply(
            chave_composicao
        )
        composicao_valida = resultado["_chave_composicao_consolidacao"].str.strip().ne("")
        com_composicao = resultado[composicao_valida]
        sem_composicao = resultado[~composicao_valida]
        com_composicao = (
            com_composicao.sort_values("_prioridade_atualizacao", ascending=False)
            .drop_duplicates(
                subset=[
                    "_chave_composicao_consolidacao",
                    "_chave_documento_consolidacao",
                ],
                keep="first",
            )
        )
        resultado = pd.concat([com_composicao, sem_composicao], ignore_index=True)
    resultado = resultado.drop(
        columns=[
            "_prioridade_atualizacao",
            "_chave_placa_consolidacao",
            "_chave_documento_consolidacao",
            "_chave_composicao_consolidacao",
        ],
        errors="ignore",
    )
    return resultado


def carregar_documentos() -> pd.DataFrame:
    df = consultar_sql(
        """
        SELECT placa, documento, vencimento, composicao, placa_cavalo,
               placa_carreta_1, placa_carreta_2, equipamento, origem,
               importado_em, importado_por, importacao_id
        FROM documentos
        UNION ALL
        SELECT placa, documento, vencimento, composicao, placa_cavalo,
               placa_carreta_1, placa_carreta_2, equipamento, origem,
               importado_em, importado_por, importacao_original_id AS importacao_id
        FROM historico_documentos
        """
    )
    if not df.empty:
        df = consolidar_documentos_mais_atualizados(df)
        df["vencimento"] = pd.to_datetime(df["vencimento"], errors="coerce")
    return df


def ultima_atualizacao_banco_documentos() -> str:
    df = consultar_sql(
        """
        SELECT MAX(valor) AS data_hora
        FROM (
            SELECT MAX(data_hora) AS valor FROM importacoes
            UNION ALL
            SELECT MAX(importado_em) AS valor FROM documentos
            UNION ALL
            SELECT MAX(data_hora) AS valor FROM historico_atualizacoes
        )
        """
    )
    if df.empty:
        return "Sem atualizacao"
    data_hora = formatar_data_hora(df.iloc[0].get("data_hora"))
    return data_hora or "Sem atualizacao"


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


def buscar_vencimentos_proximos(
    df: pd.DataFrame,
    data_atual: date | None = None,
    dias_janela: int = 30,
) -> pd.DataFrame:
    colunas = [
        "tipo_documento",
        "placa_ou_composicao",
        "data_vencimento",
        "dias_restantes",
        "texto_dias_restantes",
    ]
    if df.empty:
        return pd.DataFrame(columns=colunas)

    referencia = pd.Timestamp(data_atual or date.today()).normalize()
    dados = df[df["documento"].isin(DOCUMENTOS_VENCIMENTOS_PROXIMOS)].copy()
    if dados.empty:
        return pd.DataFrame(columns=colunas)

    dados["data_vencimento"] = pd.to_datetime(dados["vencimento"], errors="coerce")
    dados = dados[dados["data_vencimento"].notna()]
    if dados.empty:
        return pd.DataFrame(columns=colunas)

    dados["dias_restantes"] = (dados["data_vencimento"] - referencia).dt.days
    dados = dados[dados["dias_restantes"].le(dias_janela)]
    if dados.empty:
        return pd.DataFrame(columns=colunas)

    dados["placa_ou_composicao"] = dados["composicao"].where(
        dados["composicao"].astype(str).str.strip().ne(""),
        dados["placa"],
    )
    dados["tipo_documento"] = dados["documento"]
    dados["texto_dias_restantes"] = dados["dias_restantes"].astype(int).apply(
        texto_dias_restantes
    )

    saida = (
        dados[
            [
                "tipo_documento",
                "placa_ou_composicao",
                "data_vencimento",
                "dias_restantes",
                "texto_dias_restantes",
            ]
        ]
        .drop_duplicates()
        .sort_values(
            [
                "dias_restantes",
                "data_vencimento",
                "tipo_documento",
                "placa_ou_composicao",
            ]
        )
        .reset_index(drop=True)
    )
    return saida[colunas]


def preparar_vencimentos_proximos_tv(df: pd.DataFrame) -> pd.DataFrame:
    colunas = [
        "Tipo do documento",
        "Placas da composiÃ§Ã£o",
        "Vencimento",
        "Prazo",
    ]
    if df.empty:
        return pd.DataFrame(columns=colunas)
    saida = df.copy()
    saida["Vencimento"] = saida["data_vencimento"].dt.strftime("%d/%m/%Y")
    saida = saida.rename(
        columns={
            "tipo_documento": "Tipo do documento",
            "placa_ou_composicao": "Placas da composiÃ§Ã£o",
            "texto_dias_restantes": "Prazo",
        }
    )
    return saida[colunas]


def sincronizar_logo_web() -> None:
    WEB_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    logo = localizar_logo()
    if logo and logo.exists():
        if not WEB_LOGO_PATH.exists() or logo.read_bytes() != WEB_LOGO_PATH.read_bytes():
            shutil.copy2(logo, WEB_LOGO_PATH)


def exportar_vencimentos_proximos_json(
    data_atual: date | None = None,
) -> dict:
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    sincronizar_logo_web()
    documentos = carregar_documentos()
    proximos = buscar_vencimentos_proximos(documentos, data_atual or date.today(), 30)
    registros = []
    for item in proximos.itertuples(index=False):
        registros.append(
            {
                "tipo_documento": item.tipo_documento,
                "placas_composicao": item.placa_ou_composicao,
                "data_vencimento": item.data_vencimento.strftime("%d/%m/%Y"),
                "dias_restantes": int(item.dias_restantes),
                "texto_dias_restantes": item.texto_dias_restantes,
            }
        )

    payload = {
        "atualizado_em": agora_local().isoformat(timespec="seconds"),
        "janela_dias": 30,
        "inclui_vencidos": True,
        "registros": registros,
    }
    WEB_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def garantir_exportacao_web_diaria() -> None:
    precisa_exportar = not WEB_JSON_PATH.exists()
    if not precisa_exportar:
        try:
            payload = json.loads(WEB_JSON_PATH.read_text(encoding="utf-8"))
            atualizado_em = datetime.fromisoformat(str(payload.get("atualizado_em", "")))
            precisa_exportar = (
                atualizado_em.date() < agora_local().date()
                or payload.get("inclui_vencidos") is not True
            )
        except Exception:
            precisa_exportar = True
    if precisa_exportar:
        exportar_vencimentos_proximos_json()


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
        resultado = resultado[resultado["vencimento"].ge(pd.Timestamp(data_inicio))]
    if data_fim:
        resultado = resultado[resultado["vencimento"].le(pd.Timestamp(data_fim))]
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


def estilizar_tabela_vencimentos_proximos(df: pd.DataFrame):
    return (
        df.style
        .set_table_styles(
            [
                {
                    "selector": "th",
                    "props": [
                        ("background-color", COR_CABECALHO),
                        ("color", COR_TEXTO),
                        ("font-weight", "800"),
                        ("font-size", "19px"),
                        ("text-align", "center"),
                    ],
                },
                {
                    "selector": "td",
                    "props": [
                        ("color", COR_TEXTO),
                        ("font-size", "20px"),
                        ("padding", "12px 10px"),
                    ],
                },
            ]
        )
        .set_properties(
            subset=["Dias para vencer"],
            **{
                "font-weight": "900",
                "text-align": "center",
            },
        )
        .set_properties(
            subset=["Data de vencimento"],
            **{"font-weight": "800", "text-align": "center"},
        )
    )


def montar_html_painel_vencimentos_proximos(
    tabela: pd.DataFrame,
    atualizado_banco: str = "",
    compacto: bool = False,
    titulo: str = "VENCIMENTOS PR&Oacute;XIMOS",
    subtitulo: str = "Documentos vencidos em vermelho e vencimentos nos pr&oacute;ximos 30 dias",
    mensagem_vazia: str = "Nenhum documento vencido ou com vencimento nos pr&oacute;ximos 30 dias.",
    painel_id: str = "painel-vencimentos-proximos",
    destacar_vencidos: bool = True,
    rotulo_atualizacao: str = "Atualizacao do banco Documentos",
) -> str:
    logo_uri = logo_data_uri()
    logo_html = (
        f'<img class="logo-tv" src="{logo_uri}" alt="Logo DocumentosRW">'
        if logo_uri and not compacto else ""
    )
    atualizado_banco = atualizado_banco or "Sem atualizacao"
    compacto_class = " painel-tv-compacto" if compacto else ""
    if tabela.empty:
        corpo_tabela = (
            '<div class="mensagem-vazia">'
            f"{mensagem_vazia}"
            "</div>"
        )
    else:
        cabecalho = "".join(
            f"<th>{html.escape(str(coluna))}</th>" for coluna in tabela.columns
        )
        linhas = []
        for _, row in tabela.iterrows():
            prazo = str(row.get("Prazo", ""))
            classe_linha = (
                ' class="linha-vencida"'
                if destacar_vencidos and "vencido" in prazo.lower()
                else ""
            )
            celulas = "".join(
                f"<td>{html.escape(str(row[coluna]))}</td>"
                for coluna in tabela.columns
            )
            linhas.append(f"<tr{classe_linha}>{celulas}</tr>")
        corpo_tabela = (
            '<div class="tabela-tv-wrap">'
            '<table class="tabela-tv">'
            f"<thead><tr>{cabecalho}</tr></thead>"
            f"<tbody>{''.join(linhas)}</tbody>"
            "</table>"
            "</div>"
        )

    return f"""
    <!doctype html>
    <html lang="pt-BR">
    <head>
        <meta charset="utf-8">
        <style>
            :root {{
                --cabecalho: {COR_CABECALHO};
                --texto: {COR_TEXTO};
            }}
            * {{ box-sizing: border-box; }}
            html, body {{
                width: 100%;
                height: 100%;
                margin: 0;
                padding: 0;
                background: #030914;
                color: var(--texto);
                font-family: "Source Sans Pro", Arial, sans-serif;
                overflow: hidden;
            }}
            .painel-tv-fullscreen {{
                position: relative;
                width: 100%;
                height: 100%;
                min-height: 0;
                display: flex;
                flex-direction: column;
                background: #030914;
                color: var(--texto);
                border: 1px solid var(--cabecalho);
                border-radius: 8px;
                padding: .45rem .55rem .45rem;
                overflow: hidden;
            }}
            .botao-fullscreen {{
                position: absolute;
                top: .85rem;
                right: .9rem;
                z-index: 5;
                border: 1px solid var(--cabecalho);
                border-radius: 999px;
                background: transparent;
                color: var(--texto);
                cursor: pointer;
                font-weight: 800;
                font-size: .95rem;
                padding: .45rem .85rem;
            }}
            .cabecalho-tv {{
                flex: 0 0 auto;
                text-align: center;
                padding-top: .2rem;
            }}
            .logo-tv {{
                display: block;
                max-width: 210px;
                width: min(22vw, 210px);
                height: auto;
                margin: 0 auto .45rem;
            }}
            .titulo-tv {{
                color: var(--texto);
                font-size: 1.55rem;
                line-height: 1.05;
                font-weight: 950;
                letter-spacing: .08em;
                margin: .05rem 0 0;
                padding-top: 0;
            }}
            .subtitulo-tv {{
                color: var(--texto);
                font-size: .82rem;
                font-weight: 700;
                margin-top: .18rem;
            }}
            .atualizacao-banco-tv {{
                color: var(--texto);
                font-size: .78rem;
                font-weight: 850;
                margin-top: .12rem;
            }}
            .tabela-tv-wrap {{
                flex: 1 1 auto;
                min-height: 0;
                width: 100%;
                max-height: none;
                margin-top: .35rem;
                overflow-y: auto;
                overflow-x: hidden;
                border: 1px solid #D8C98D;
                border-radius: 8px;
                background: #071526;
            }}
            .tabela-tv {{
                width: 100%;
                border-collapse: collapse;
                table-layout: fixed;
            }}
            .tabela-tv th {{
                position: sticky;
                top: 0;
                z-index: 2;
                background-color: var(--cabecalho);
                color: var(--texto);
                font-size: 14px;
                font-weight: 800;
                text-align: center;
                padding: 6px 8px;
            }}
            .tabela-tv td {{
                background: #071526;
                color: var(--texto);
                font-size: 15px;
                padding: 6px 8px;
                border-bottom: 1px solid #D8C98D;
                overflow-wrap: anywhere;
                vertical-align: middle;
            }}
            .tabela-tv tr:nth-child(even) td {{
                background: #0f2438;
            }}
            .tabela-tv td:nth-child(3),
            .tabela-tv td:nth-child(4) {{
                text-align: center;
                font-weight: 800;
            }}
            .tabela-tv tr.linha-vencida td {{
                color: #ff4d6d;
                font-weight: 900;
            }}
            .mensagem-vazia {{
                margin-top: 1.2rem;
                border: 1px solid #D8C98D;
                border-radius: 12px;
                padding: 1.1rem;
                text-align: center;
                font-size: 1.25rem;
                font-weight: 800;
            }}
            .painel-tv-fullscreen:fullscreen {{
                width: 100vw;
                height: 100vh;
                border-radius: 0;
                padding: clamp(.4rem, 1vh, .8rem) clamp(.45rem, 1vw, 1rem);
                display: flex;
                flex-direction: column;
            }}
            .painel-tv-fullscreen:fullscreen .cabecalho-tv {{
                flex: 0 0 auto;
            }}
            .painel-tv-fullscreen:fullscreen .logo-tv {{
                width: min(18vw, 220px);
                max-width: 220px;
                margin-bottom: .25rem;
            }}
            .painel-tv-fullscreen:fullscreen .titulo-tv {{
                font-size: clamp(1.35rem, 2.2vw, 2.2rem);
            }}
            .painel-tv-fullscreen:fullscreen .subtitulo-tv {{
                font-size: clamp(.75rem, 1.1vw, 1rem);
            }}
            .painel-tv-fullscreen:fullscreen .tabela-tv-wrap {{
                flex: 1 1 auto;
                min-height: 0;
                max-height: none;
                margin-top: clamp(.25rem, .8vh, .55rem);
            }}
            .painel-tv-fullscreen:fullscreen .tabela-tv th {{
                font-size: clamp(.85rem, 1.1vw, 1.2rem);
                padding: clamp(.28rem, .7vh, .48rem);
            }}
            .painel-tv-fullscreen:fullscreen .tabela-tv td {{
                font-size: clamp(.82rem, 1.05vw, 1.15rem);
                padding: clamp(.28rem, .7vh, .48rem);
            }}
            .painel-tv-fullscreen:fullscreen .botao-fullscreen {{
                top: 1.2rem;
                right: 1.4rem;
                font-size: clamp(1rem, 1.4vw, 1.45rem);
            }}
            .painel-tv-compacto {{
                border: 0;
                padding: .15rem .25rem .25rem;
            }}
            .painel-tv-compacto .botao-fullscreen,
            .painel-tv-compacto .logo-tv,
            .painel-tv-compacto .titulo-tv,
            .painel-tv-compacto .subtitulo-tv {{
                display: none;
            }}
            .painel-tv-compacto .cabecalho-tv {{
                text-align: left;
                min-height: 0;
                padding: 0;
            }}
            .painel-tv-compacto .atualizacao-banco-tv {{
                font-size: 11px;
                line-height: 1.1;
                margin: 0 0 .15rem;
                opacity: .82;
            }}
            .painel-tv-compacto .tabela-tv-wrap {{
                margin-top: 0;
                border-radius: 6px;
            }}
            .painel-tv-compacto .tabela-tv th {{
                font-size: 12px;
                padding: 4px 6px;
            }}
            .painel-tv-compacto .tabela-tv td {{
                font-size: 13px;
                padding: 4px 6px;
            }}
        </style>
    </head>
    <body>
        <section id="{html.escape(painel_id)}" class="painel-tv-fullscreen{compacto_class}">
            <button id="botao-fullscreen" class="botao-fullscreen" type="button">
                Tela cheia
            </button>
            <header class="cabecalho-tv">
                {logo_html}
                <div class="titulo-tv">{titulo}</div>
                <div class="subtitulo-tv">
                    {subtitulo}
                </div>
                <div class="atualizacao-banco-tv">
                    {html.escape(rotulo_atualizacao)}: {html.escape(atualizado_banco)}
                </div>
            </header>
            {corpo_tabela}
        </section>
        <script>
            const painel = document.getElementById("{html.escape(painel_id)}");
            const botao = document.getElementById("botao-fullscreen");
            if (window.frameElement) {{
                window.frameElement.setAttribute("allowfullscreen", "true");
                window.frameElement.setAttribute("allow", "fullscreen");
            }}

            function fullscreenAtivo() {{
                return document.fullscreenElement === painel;
            }}

            function atualizarBotao() {{
                botao.textContent = fullscreenAtivo()
                    ? "Sair da tela cheia"
                    : "Tela cheia";
            }}

            botao.addEventListener("click", async () => {{
                try {{
                    if (fullscreenAtivo()) {{
                        await document.exitFullscreen();
                    }} else {{
                        await painel.requestFullscreen();
                    }}
                }} catch (erro) {{
                    console.error("Nao foi possivel alternar tela cheia", erro);
                }}
            }});

            document.addEventListener("fullscreenchange", atualizarBotao);
            atualizarBotao();
        </script>
    </body>
    </html>
    """


def mostrar_painel_vencimentos_proximos(
    documentos_banco: pd.DataFrame,
    atualizado_banco: str = "",
    compacto: bool = False,
) -> None:
    configurar_recarga_diaria()
    proximos = buscar_vencimentos_proximos(documentos_banco, date.today(), 30)
    tabela = preparar_vencimentos_proximos_tv(proximos)
    altura = 1020
    components.html(
        montar_html_painel_vencimentos_proximos(
            tabela,
            atualizado_banco or ultima_atualizacao_banco_documentos(),
            compacto=compacto,
        ),
        height=altura,
        scrolling=False,
    )


def mostrar_painel_manutencao_programada(compacto: bool = False) -> None:
    configurar_recarga_diaria()
    tabela = carregar_manutencoes_programadas()
    components.html(
        montar_html_painel_vencimentos_proximos(
            tabela,
            ultima_atualizacao_manutencoes(),
            compacto=compacto,
            titulo="MANUTEN&Ccedil;&Atilde;O PROGRAMADA",
            subtitulo="Placas com manuten&ccedil;&atilde;o programada e previs&atilde;o de sa&iacute;da",
            mensagem_vazia="Nenhuma manuten&ccedil;&atilde;o programada cadastrada.",
            painel_id="painel-manutencao-programada",
            destacar_vencidos=False,
            rotulo_atualizacao="Atualizacao do painel",
        ),
        height=1020,
        scrolling=False,
    )


def render_editor_manutencao_programada() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.2rem !important;
            max-width: 1250px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="titulo">Manutenção programada</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="subtitulo">Preencha as placas que devem aparecer na LogisTV.</div>',
        unsafe_allow_html=True,
    )
    dados = carregar_manutencoes_programadas()
    editor = st.data_editor(
        preparar_editor_manutencoes(dados),
        column_config={
            "Placa": st.column_config.TextColumn("Placa", width="medium"),
            "Manutenção Programada": st.column_config.TextColumn(
                "Manutenção Programada", width="large"
            ),
            "Previsão Saída": st.column_config.DateColumn(
                "Previsão Saída", format="DD/MM/YYYY", width="medium"
            ),
        },
        hide_index=True,
        num_rows="dynamic",
        use_container_width=True,
        key="editor_manutencao_programada",
    )
    col_salvar, col_tv = st.columns([0.35, 0.65])
    with col_salvar:
        if st.button("Salvar painel", type="primary", use_container_width=True):
            total = salvar_manutencoes_programadas(editor)
            st.success(f"Painel salvo com {total} registro(s).")
            st.rerun()
    with col_tv:
        st.link_button(
            "Abrir visualização TV",
            "?painel=manutencao&embed_tv=1",
            use_container_width=True,
        )

    st.markdown('<div class="faixa">Prévia da TV</div>', unsafe_allow_html=True)
    mostrar_painel_manutencao_programada(compacto=True)


def render_painel_manutencao_programada() -> None:
    inicializar_banco()
    css_tv_documentos_coupa()
    if query_ativo("embed_tv"):
        mostrar_painel_manutencao_programada(compacto=True)
    else:
        render_editor_manutencao_programada()


def recarregar_tv(intervalo_segundos: int) -> None:
    components.html(
        f"""
        <script>
        setTimeout(function() {{
            try {{
                window.parent.location.reload();
            }} catch (erro) {{
                window.location.reload();
            }}
        }}, {int(intervalo_segundos) * 1000});
        </script>
        """,
        height=0,
    )


def css_tv_documentos_coupa() -> None:
    st.markdown(
        """
        <style>
        html, body, .stApp {
            background: #030914 !important;
            color: #f8fafc !important;
        }
        [data-testid="stSidebar"],
        [data-testid="stToolbar"],
        [data-testid="stHeader"],
        [data-testid="stDecoration"],
        .stDeployButton,
        #MainMenu,
        footer {
            display: none !important;
        }
        .block-container {
            max-width: 100% !important;
            padding: 0.45rem 0.75rem 0.75rem !important;
            min-height: 100vh;
            background: #030914;
        }
        .tv-topo {
            background: #071526;
            border: 1px solid rgba(181,145,27,0.45);
            border-radius: 10px;
            padding: 12px 16px;
            margin-bottom: 8px;
        }
        .tv-titulo {
            color: #f8fafc;
            font-size: 30px;
            font-weight: 900;
            line-height: 1.05;
        }
        .tv-subtitulo {
            color: rgba(248,250,252,0.76);
            font-size: 14px;
            font-weight: 700;
            margin-top: 4px;
        }
        .tv-admin {
            color: rgba(248,250,252,0.74);
            font-size: 13px;
            margin-top: 6px;
        }
        iframe {
            background: #030914 !important;
            border-radius: 10px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_tv_documentos_coupa() -> None:
    inicializar_banco()
    garantir_exportacao_web_diaria()
    css_tv_documentos_coupa()
    painel_param = query_param("painel", "auto").strip().lower()
    if painel_param in {
        "manutencao",
        "manutenção",
        "manutencao-programada",
        "manutencao_programada",
    }:
        render_painel_manutencao_programada()
        return
    embed_tv = query_ativo("embed_tv")
    intervalo = pd.to_numeric(pd.Series([query_param("tempo", "60")]), errors="coerce").fillna(60).iloc[0]
    intervalo = max(15, min(600, int(intervalo)))
    if painel_param in {"documentos", "docs"}:
        painel_ativo = "Documentos"
    elif painel_param in {"coupa", "resumo"}:
        painel_ativo = "Coupa"
    else:
        painel_ativo = "Documentos" if int(time.time() // intervalo) % 2 == 0 else "Coupa"
        recarregar_tv(intervalo)
    atualizacao_documentos = ultima_atualizacao_banco_documentos()
    atualizacao_banco = (
        f"Banco Documentos: {atualizacao_documentos}"
        if painel_ativo == "Documentos"
        else "Banco Coupa: horario exibido no painel Coupa"
    )
    if not embed_tv:
        st.markdown(
            f"""
            <div class="tv-topo">
                <div class="tv-titulo">TV Operacional - {html.escape(painel_ativo)}</div>
                <div class="tv-subtitulo">Documentos RW x Resumo Coupa | {agora_local().strftime('%d/%m/%Y %H:%M')}</div>
                <div class="tv-subtitulo">{html.escape(atualizacao_banco)}</div>
                <div class="tv-admin">Administracao do Documentos: abra este app com ?admin=1</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    if painel_ativo == "Coupa":
        components.iframe(
            f"{CONTROLE_TV_COUPA_URL}&tempo={intervalo}",
            height=930,
            scrolling=True,
        )
        return
    documentos_banco = carregar_documentos()
    if documentos_banco.empty:
        st.warning("Nenhum documento importado. Abra com ?admin=1 para importar a base.")
        return
    mostrar_painel_vencimentos_proximos(documentos_banco, atualizacao_documentos, compacto=embed_tv)


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
    .block-container {{ padding-top: 2.6rem; max-width: 1550px; }}
    h1, h2, h3, label, p {{ color: var(--texto); }}
    .titulo {{ font-size: 2.15rem; font-weight: 850; color: var(--texto); }}
    .subtitulo {{ color: #4B536F; margin: 0.1rem 0 1rem; }}
    .faixa {{ background: var(--cabecalho); color: var(--texto); padding: .75rem 1rem;
              border-radius: 12px; font-weight: 800; margin: .7rem 0; }}
    .painel-tv {{
        text-align: center; margin: 1.4rem 0 1.35rem; padding: 2.15rem 1.25rem 1.8rem;
        border-radius: 18px; border: 2px solid var(--cabecalho);
        overflow: visible;
    }}
    .logo-tv {{
        display: block; max-width: 360px; width: min(36vw, 360px);
        height: auto; margin: 0 auto 1.3rem;
    }}
    .titulo-tv {{
        color: var(--texto); font-size: 3rem; line-height: 1.05;
        font-weight: 950; letter-spacing: .08em; margin-top: .35rem;
        padding-top: .15rem;
    }}
    .subtitulo-tv {{
        color: var(--texto); font-size: 1.2rem; font-weight: 700;
        margin-top: .45rem;
    }}
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
    if not query_ativo("admin"):
        render_tv_documentos_coupa()
        return
    inicializar_banco()
    garantir_exportacao_web_diaria()
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
                        exportacao_web = exportar_vencimentos_proximos_json()

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
                    mensagens.append(
                        f"JSON Web atualizado ({len(exportacao_web['registros'])} registro(s))"
                    )
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
                            exportar_vencimentos_proximos_json()
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
                            exportar_vencimentos_proximos_json()
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
                        exportar_vencimentos_proximos_json()
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

    mostrar_painel_vencimentos_proximos(documentos_banco)

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
    documentos_status = consolidar_documentos_mais_atualizados(documentos_status)
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
        "Regra de atualização: cada registro ativo é identificado por placa/composição + tipo de "
        "documento. Em caso de duplicidade, permanece o registro da importação ou "
        "atualização mais recente. A última base de composições salva é reutilizada "
        "automaticamente quando uma nova base não é enviada."
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

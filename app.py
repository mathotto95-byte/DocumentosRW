import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from io import BytesIO
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

COR_CABECALHO = "#B5911B"
COR_TEXTO = "#020D3F"

TIPOS_DOCUMENTO = [
    "CIV",
    "CIPP",
    "AFERIÇÃO",
    "CRONOTACÓGRAFO",
    "IBAMA",
    "CR IBAMA",
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


def classificar_documento(valor) -> str | None:
    texto = normalizar_texto(valor)
    if not texto:
        return None
    if "CR IBAMA" in texto or "CERTIFICADO DE REGULARIDADE" in texto:
        return "CR IBAMA"
    if "CRLV" in texto:
        return "CRLV"
    if "IBAMA" in texto:
        return "IBAMA"
    if "CIPP" in texto:
        return "CIPP"
    if re.search(r"(^|\W)CIV($|\W)", texto):
        return "CIV"
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
            CREATE INDEX IF NOT EXISTS idx_historico_placa_documento
                ON historico_documentos(placa, documento);
            CREATE INDEX IF NOT EXISTS idx_backup_importacao
                ON backup_documentos(importacao_id);
            """
        )


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
        if anterior is None or registro["vencimento"] > anterior["vencimento"]:
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
                        registro["equipamento"], registro["origem"], momento,
                        usuario, importacao_id,
                    ),
                )
                estatisticas["inseridos"] += 1
                continue

            if registro["vencimento"] <= existente["vencimento"]:
                estatisticas["ignorados"] += 1
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
                    registro["origem"], momento, usuario, importacao_id,
                    existente["id"],
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


def ler_planilha_documentos(arquivo, aba: str) -> pd.DataFrame:
    arquivo.seek(0)
    bruto = pd.read_excel(arquivo, sheet_name=aba, header=None, dtype=object)
    linha_cabecalho = localizar_linha_cabecalho_documentos(bruto)
    df = bruto.iloc[linha_cabecalho + 1 :].copy()
    df.columns = criar_nomes_unicos(bruto.iloc[linha_cabecalho].tolist())
    return df.dropna(how="all")


def ler_base(arquivo, aba: str) -> pd.DataFrame:
    arquivo.seek(0)
    return pd.read_excel(arquivo, sheet_name=aba, dtype=object).dropna(how="all")


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
    if col_placa is None or col_tipo is None or col_vencimento is None:
        raise ValueError("A planilha de documentos não possui as colunas obrigatórias.")

    registros = []
    for _, row in df.iterrows():
        placa = limpar_placa(row.get(col_placa))
        documento = classificar_documento(row.get(col_tipo))
        vencimento = data_iso(row.get(col_vencimento))
        if placa and documento and vencimento:
            registros.append(
                {
                    "placa": placa,
                    "documento": documento,
                    "vencimento": vencimento,
                    "origem": "PLANILHA DE DOCUMENTOS",
                }
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

        # Colunas nomeadas permitem trazer IBAMA, CR IBAMA e CRLV da base,
        # quando o nome da coluna também identifica o equipamento.
        for coluna in df_base.columns:
            documento = nome_coluna_para_documento(coluna)
            if documento not in {"IBAMA", "CR IBAMA", "CRLV"}:
                continue
            nome = normalizar_texto(coluna)
            destinos = []
            if "CAVALO" in nome:
                destinos = [(cavalo, "Cavalo")]
            elif "CARRETA 1" in nome:
                destinos = [(carreta_1, "Carreta 1")]
            elif "CARRETA 2" in nome:
                destinos = [(carreta_2, "Carreta 2")]
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
    ]
    if df.empty:
        return pd.DataFrame(columns=colunas)

    def juntar_unicos(serie) -> str:
        return "\n".join(dict.fromkeys(str(x) for x in serie if str(x).strip()))

    temporario = df.copy()
    temporario["vencimento_formatado"] = temporario["vencimento"].dt.strftime("%d/%m/%Y")
    resumo = (
        temporario.groupby("composicao", sort=False)
        .agg(
            **{
                "Placa do documento": ("placa", juntar_unicos),
                "Documento/Laudo": ("documento", juntar_unicos),
                "Data de vencimento": ("vencimento_formatado", juntar_unicos),
            }
        )
        .reset_index()
        .rename(columns={"composicao": "Placas da composição"})
    )
    return resumo[colunas]


def preparar_detalhe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "Status", "Composição", "Equipamento", "Placa", "Documento/Laudo",
                "Vencimento", "Dias", "Origem", "Atualizado por", "Atualizado em",
            ]
        )
    saida = df.copy()
    saida["vencimento"] = saida["vencimento"].dt.strftime("%d/%m/%Y")
    saida["importado_em"] = pd.to_datetime(
        saida["importado_em"], errors="coerce"
    ).dt.strftime("%d/%m/%Y %H:%M:%S")
    saida = saida.rename(
        columns={
            "composicao": "Composição",
            "equipamento": "Equipamento",
            "placa": "Placa",
            "documento": "Documento/Laudo",
            "vencimento": "Vencimento",
            "origem": "Origem",
            "importado_por": "Atualizado por",
            "importado_em": "Atualizado em",
        }
    )
    colunas = [
        "Status", "Composição", "Equipamento", "Placa", "Documento/Laudo",
        "Vencimento", "Dias", "Origem", "Atualizado por", "Atualizado em",
    ]
    return saida[colunas]


def painel_status(
    titulo: str, df: pd.DataFrame, status: list[str], mensagem_vazia: str
) -> None:
    st.subheader(titulo)
    dados = df[df["Status"].isin(status)] if not df.empty else df
    if dados.empty:
        st.success(mensagem_vazia)
        return
    st.dataframe(
        estilizar_tabela(resumir_composicoes(dados)),
        use_container_width=True,
        hide_index=True,
        height=min(460, 75 + len(resumir_composicoes(dados)) * 36),
    )


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
) -> bytes:
    output = BytesIO()
    detalhe = preparar_detalhe(documentos_filtrados)
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        detalhe.to_excel(writer, sheet_name="DOCUMENTOS", index=False)
        resumir_composicoes(documentos_filtrados).to_excel(
            writer, sheet_name="COMPOSICOES", index=False
        )
        historico.to_excel(writer, sheet_name="HISTORICO", index=False)
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

    with st.expander("Importar e atualizar o banco de dados", expanded=False):
        col_usuario, col_base, col_documentos = st.columns([0.7, 1.15, 1.15])
        with col_usuario:
            usuario = st.text_input(
                "Usuário responsável *", placeholder="Nome do usuário"
            )
        with col_base:
            arquivo_base = st.file_uploader(
                "Base de composições", type=["xlsx", "xls"], key="base"
            )
        with col_documentos:
            arquivo_documentos = st.file_uploader(
                "Laudos/documentos", type=["xlsx", "xls"], key="documentos"
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
        if abas_base and abas_documentos:
            c1, c2 = st.columns(2)
            with c1:
                aba_base = st.selectbox("Aba da base", abas_base)
            with c2:
                aba_documentos = st.selectbox("Aba dos documentos", abas_documentos)

        if st.button("Importar e atualizar banco", key="executar_importacao"):
            if not usuario.strip():
                st.error("Informe o usuário responsável pela importação.")
            elif not arquivo_base or not arquivo_documentos:
                st.error("Envie a base de composições e a planilha de documentos.")
            else:
                try:
                    with st.spinner("Validando, criando backup e atualizando o banco..."):
                        df_base = ler_base(arquivo_base, aba_base)
                        df_documentos = ler_planilha_documentos(
                            arquivo_documentos, aba_documentos
                        )
                        registros = preparar_registros_importacao(df_base, df_documentos)
                        if not registros:
                            raise ValueError("Nenhum documento válido foi encontrado.")
                        resultado = salvar_importacao(
                            registros,
                            usuario,
                            arquivo_base.name,
                            arquivo_documentos.name,
                        )
                    st.success(
                        f"Importação {resultado['importacao_id']} concluída: "
                        f"{resultado['inseridos']} inseridos, "
                        f"{resultado['atualizados']} atualizados e "
                        f"{resultado['ignorados']} duplicados/mais antigos ignorados."
                    )
                except Exception as erro:
                    st.error(f"Não foi possível importar: {erro}")

    documentos_banco = carregar_documentos()
    if documentos_banco.empty:
        st.info("O banco ainda está vazio. Faça a primeira importação acima.")
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
    tab_vencidos, tab_semana, tab_mes = st.tabs(
        ["Vencidos", "Vencimentos na semana", "Vencimentos no mês"]
    )
    with tab_vencidos:
        painel_status(
            "Documentos vencidos", filtrados, ["VENCIDO"], "Nenhum documento vencido."
        )
    with tab_semana:
        painel_status(
            "Vencimentos desta semana",
            filtrados,
            ["VENCE HOJE", "VENCE NA SEMANA"],
            "Nenhum documento vence nesta semana.",
        )
    with tab_mes:
        painel_status(
            "Vencimentos após esta semana, ainda neste mês",
            filtrados,
            ["VENCE NO MÊS"],
            "Nenhum documento vence no restante do mês.",
        )

    st.markdown('<div class="faixa">Painéis exclusivos</div>', unsafe_allow_html=True)
    tab_afericao, tab_ambiental = st.tabs(
        ["AFERIÇÃO", "IBAMA · CR IBAMA · CRLV"]
    )
    with tab_afericao:
        dados = filtrados[filtrados["documento"] == "AFERIÇÃO"]
        a1, a2, a3 = st.tabs(["Vencidos", "Semana", "Mês"])
        with a1:
            painel_status("Aferições vencidas", dados, ["VENCIDO"], "Nenhuma aferição vencida.")
        with a2:
            painel_status(
                "Aferições da semana", dados, ["VENCE HOJE", "VENCE NA SEMANA"],
                "Nenhuma aferição vence nesta semana."
            )
        with a3:
            painel_status(
                "Aferições do mês", dados, ["VENCE NO MÊS"],
                "Nenhuma aferição vence no restante do mês."
            )
    with tab_ambiental:
        dados = filtrados[
            filtrados["documento"].isin(["IBAMA", "CR IBAMA", "CRLV"])
        ]
        i1, i2, i3 = st.tabs(["Vencidos", "Semana", "Mês"])
        with i1:
            painel_status(
                "IBAMA, CR IBAMA e CRLV vencidos", dados, ["VENCIDO"],
                "Nenhum documento deste grupo está vencido."
            )
        with i2:
            painel_status(
                "IBAMA, CR IBAMA e CRLV da semana", dados,
                ["VENCE HOJE", "VENCE NA SEMANA"],
                "Nenhum documento deste grupo vence nesta semana."
            )
        with i3:
            painel_status(
                "IBAMA, CR IBAMA e CRLV do mês", dados, ["VENCE NO MÊS"],
                "Nenhum documento deste grupo vence no restante do mês."
            )

    st.markdown(
        '<div class="faixa">Composições com documentos no filtro</div>',
        unsafe_allow_html=True,
    )
    st.dataframe(
        estilizar_tabela(resumir_composicoes(filtrados)),
        use_container_width=True,
        hide_index=True,
        height=390,
    )

    with st.expander("Detalhes, histórico e backup", expanded=False):
        tab_detalhe, tab_historico, tab_backup, tab_importacoes = st.tabs(
            ["Detalhes", "Registros substituídos", "Último backup", "Importações"]
        )
        historico = carregar_historico()
        importacoes = carregar_importacoes()
        backup, backup_id = carregar_ultimo_backup()
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

        excel = gerar_excel(filtrados, historico, importacoes)
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
        "são ignoradas. Antes de cada importação, o estado completo do banco é salvo."
    )


if __name__ == "__main__":
    main()
